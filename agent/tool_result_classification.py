"""Shared helpers for classifying tool result payloads."""

from __future__ import annotations

import ast
import json
import re
from typing import Any


FILE_MUTATING_TOOL_NAMES = frozenset({"write_file", "patch"})

_SHELL_FILE_MUTATION_RE = re.compile(
    r"(?:^|[;&|]\s*)(?:cp|install|mv|rm)\s+|"
    r"\bsed\s+-i(?:\s|[\"'])|(?:^|[^<])(?:>>|>)\s*[^=]",
    re.IGNORECASE | re.MULTILINE,
)
_MUTATING_METHOD_NAMES = frozenset(
    {
        "mkdir",
        "rmdir",
        "touch",
        "unlink",
        "write_bytes",
        "write_text",
    }
)
_MUTATING_MODULE_METHODS = frozenset(
    {
        ("os", "makedirs"),
        ("os", "mkdir"),
        ("os", "remove"),
        ("os", "removedirs"),
        ("os", "rename"),
        ("os", "renames"),
        ("os", "replace"),
        ("os", "rmdir"),
        ("os", "unlink"),
        ("shutil", "copy"),
        ("shutil", "copy2"),
        ("shutil", "copyfile"),
        ("shutil", "copytree"),
        ("shutil", "move"),
        ("shutil", "rmtree"),
    }
)


def _literal_string(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _execute_code_may_mutate_files(code: Any) -> bool:
    """Conservatively identify successful scripts that can change files."""

    text = str(code or "")
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return bool(_SHELL_FILE_MUTATION_RE.search(text))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _SHELL_FILE_MUTATION_RE.search(node.value):
                return True
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name):
            if function.id in {"patch", "write_file"}:
                return True
            if function.id == "open":
                mode = _literal_string(node.args[1] if len(node.args) > 1 else None)
                for keyword in node.keywords:
                    if keyword.arg == "mode":
                        mode = _literal_string(keyword.value)
                if any(flag in mode for flag in "wax+"):
                    return True
        if not isinstance(function, ast.Attribute):
            continue
        if function.attr in _MUTATING_METHOD_NAMES:
            return True
        owner = function.value
        if (
            isinstance(owner, ast.Name)
            and (owner.id, function.attr) in _MUTATING_MODULE_METHODS
        ):
            return True
    return False


def coding_worker_mutation_paths(result: Any) -> list[str]:
    """Return host-inspected paths changed by a delegated coding worker."""
    if not isinstance(result, str):
        return []
    try:
        data = json.loads(result.strip())
    except Exception:
        return []
    scope_check = data.get("scope_check") if isinstance(data, dict) else None
    raw_paths = scope_check.get("changed_files") if isinstance(scope_check, dict) else None
    if not isinstance(raw_paths, list):
        return []
    paths: list[str] = []
    for value in raw_paths:
        path = str(value or "").strip()
        if path and path not in paths:
            paths.append(path)
    return paths


# Tools whose interrupted/dangling execution is safe to discard because they
# cannot mutate either external state or Hermes session state. Unknown/plugin/
# MCP tools stay effect-capable by default.
NO_EFFECT_TOOL_NAMES = frozenset({
    "read_file", "search_files", "session_search", "skill_view", "skills_list",
    "web_extract", "web_search", "vision_analyze", "browser_snapshot",
    "browser_get_images", "browser_console", "read_terminal",
})


def tool_may_have_side_effect(tool_name: str) -> bool:
    return tool_name not in NO_EFFECT_TOOL_NAMES


def file_mutation_result_landed(
    tool_name: str,
    result: Any,
    args: Any = None,
) -> bool:
    """Return True when a file mutation result proves the write landed."""
    if tool_name == "delegate_coding_task":
        return bool(coding_worker_mutation_paths(result))
    if tool_name == "execute_code":
        if not isinstance(result, str) or not isinstance(args, dict):
            return False
        try:
            data = json.loads(result.strip())
        except Exception:
            return False
        if (
            not isinstance(data, dict)
            or data.get("status") != "success"
            or data.get("error")
            or data.get("exit_code") not in (None, 0, "0")
        ):
            return False
        return _execute_code_may_mutate_files(args.get("code"))
    if tool_name not in FILE_MUTATING_TOOL_NAMES or not isinstance(result, str):
        return False
    try:
        data = json.loads(result.strip())
    except Exception:
        return False
    if not isinstance(data, dict) or data.get("error"):
        return False
    if tool_name == "write_file":
        return "bytes_written" in data
    if tool_name == "patch":
        return data.get("success") is True
    return False
