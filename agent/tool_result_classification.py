"""Shared helpers for classifying tool result payloads."""

from __future__ import annotations

import json
from typing import Any


FILE_MUTATING_TOOL_NAMES = frozenset({"write_file", "patch"})


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


def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """Return True when a file mutation result proves the write landed."""
    if tool_name == "delegate_coding_task":
        return bool(coding_worker_mutation_paths(result))
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
