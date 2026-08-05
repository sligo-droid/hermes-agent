"""Bounded terminal outcome, exact-lock, and closeout receipt policy."""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Iterable


TERMINAL_FAILURE_KINDS = frozenset(
    {"source_parse", "dependency_missing", "command_context", "test_failure", "unknown"}
)
_SUCCESS_CLOSEOUT_STATUSES = frozenset(
    {
        "deployed",
        "verified",
        "complete",
        "completed",
        "passed",
        "success",
        "succeeded",
    }
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_SCAN_CHARS = 24_000
_MAX_RELATIVE_PATH_CHARS = 240


def _bounded_scan_text(*values: Any) -> str:
    text = "\n".join(str(value or "") for value in values)
    if len(text) <= _MAX_SCAN_CHARS:
        return text.lower()
    half = _MAX_SCAN_CHARS // 2
    return (text[:half] + "\n" + text[-half:]).lower()


def classify_terminal_outcome(
    *,
    command: Any,
    output: Any,
    exit_code: Any,
    error: Any = None,
) -> dict[str, Any]:
    """Return a pure, bounded, sanitized classification for one terminal result."""

    try:
        code = int(exit_code)
    except (TypeError, ValueError):
        code = -1 if error else 0
    text = _bounded_scan_text(output, error)

    vite_markdown_parse = bool(
        ("vite:import-analysis" in text or "plugin:vite:import-analysis" in text)
        and "failed to parse source for import analysis" in text
        and (".md" in text or "markdown" in text)
    )
    if vite_markdown_parse:
        return {
            "kind": "source_parse",
            "semantic_failure": True,
            "dependency_installation_indicated": False,
            "summary": (
                "Vite/Vitest import analysis could not parse Markdown as source; "
                "dependency installation is not indicated."
            ),
        }

    source_parse = any(
        marker in text
        for marker in (
            "failed to parse source for import analysis",
            "syntaxerror:",
            "syntax error:",
            "parse error:",
            "unexpected token",
            "unterminated string",
        )
    )
    dependency_missing = any(
        marker in text
        for marker in (
            "modulenotfounderror:",
            "module not found:",
            "cannot find module ",
            "cannot find package ",
            "no module named ",
            "err_module_not_found",
            "could not resolve dependency",
        )
    )
    command_context = any(
        marker in text
        for marker in (
            "err_pnpm_no_pkg_manifest",
            "err_pnpm_aborted_remove_modules_dir_no_tty",
            "command not found",
            "no such file or directory",
            "not recognized as an internal or external command",
            "permission denied",
            "cannot chdir",
            "failed to change directory",
        )
    )
    test_failure = any(
        marker in text
        for marker in (
            "tests failed",
            "test failed",
            "failed tests",
            "failed suites",
            "assertionerror",
            "pytest failures",
            "vitest failed",
        )
    ) or bool(re.search(r"(?:^|\n)\s*fail(?:ed)?\s+[^\n]+", text))

    if source_parse:
        kind = "source_parse"
        summary = "Command output indicates a source parsing failure."
    elif dependency_missing:
        kind = "dependency_missing"
        summary = "Command output indicates a missing dependency."
    elif command_context:
        kind = "command_context"
        summary = "Command output indicates an invalid command or execution context."
    elif test_failure:
        kind = "test_failure"
        summary = "Command output indicates a test failure."
    else:
        kind = "unknown"
        summary = (
            "Command completed without a recognized semantic failure."
            if code == 0 and not error
            else "Command failed for an unclassified reason."
        )

    semantic_failure = bool(error) or code != 0 or kind != "unknown"
    return {
        "kind": kind,
        "semantic_failure": semantic_failure,
        "dependency_installation_indicated": kind == "dependency_missing",
        "summary": summary,
    }


def sanitize_terminal_classification(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    kind = str(raw.get("kind") or "unknown").strip().lower()
    if kind not in TERMINAL_FAILURE_KINDS:
        kind = "unknown"
    summaries = {
        "source_parse": "Command output indicates a source parsing failure.",
        "dependency_missing": "Command output indicates a missing dependency.",
        "command_context": "Command output indicates an invalid command or execution context.",
        "test_failure": "Command output indicates a test failure.",
        "unknown": "Command failed for an unclassified reason.",
    }
    summary = str(raw.get("summary") or summaries[kind]).strip()[:240]
    return {
        "kind": kind,
        "semantic_failure": raw.get("semantic_failure") is True,
        "dependency_installation_indicated": (
            raw.get("dependency_installation_indicated") is True
            and kind == "dependency_missing"
        ),
        "summary": summary,
    }


def _iter_top_level_shell_segments(command: str) -> Iterable[str]:
    start = 0
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
        elif char == "\\" and quote != "'":
            escaped = True
        elif quote:
            if char == quote:
                quote = None
        elif char in {"'", '"'}:
            quote = char
        elif char in ";&|\n":
            if start < index:
                yield command[start:index]
            if char in "&|" and index + 1 < len(command) and command[index + 1] == char:
                index += 1
            start = index + 1
        index += 1
    if start < len(command):
        yield command[start:]


def _segment_tokens(segment: str) -> list[str] | None:
    try:
        return shlex.split(segment, posix=True)
    except ValueError:
        return None


def _pnpm_install_target(tokens: list[str], cwd: Path) -> Path | None:
    if not tokens:
        return None
    index = 0
    while index < len(tokens) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[index]):
        index += 1
    if index >= len(tokens) or Path(tokens[index]).name != "pnpm":
        return None
    args = tokens[index + 1 :]
    target = cwd
    command_name = ""
    arg_index = 0
    while arg_index < len(args):
        token = args[arg_index]
        if token in {"--dir", "-C"}:
            if arg_index + 1 >= len(args):
                return None
            target = Path(args[arg_index + 1]).expanduser()
            if not target.is_absolute():
                target = cwd / target
            arg_index += 2
            continue
        if token.startswith("--dir="):
            target = Path(token.split("=", 1)[1]).expanduser()
            if not target.is_absolute():
                target = cwd / target
            arg_index += 1
            continue
        if token.startswith("-C="):
            target = Path(token.split("=", 1)[1]).expanduser()
            if not target.is_absolute():
                target = cwd / target
            arg_index += 1
            continue
        if token.startswith("-"):
            arg_index += 1
            continue
        if not command_name:
            command_name = token
        arg_index += 1
    if command_name not in {"install", "i"}:
        return None
    return target.resolve(strict=False)


def _nearest_pnpm_package_root(path: Path, repo_root: Path) -> Path | None:
    candidate = path if path.is_dir() else path.parent
    while True:
        if (candidate / "package.json").is_file() and (candidate / "pnpm-lock.yaml").is_file():
            return candidate
        if candidate == repo_root or candidate.parent == candidate:
            return None
        try:
            candidate.relative_to(repo_root)
        except ValueError:
            return None
        candidate = candidate.parent


def exact_lock_pnpm_install_block(command: Any, cwd: Any) -> dict[str, Any] | None:
    """Return structured remediation when pnpm install would mutate a shared tree."""

    if not isinstance(command, str) or not command.strip():
        return None
    command_cwd = Path(str(cwd or ".")).expanduser().resolve(strict=False)
    for segment in _iter_top_level_shell_segments(command):
        tokens = _segment_tokens(segment)
        if not tokens:
            continue
        target = _pnpm_install_target(tokens, command_cwd)
        if target is None:
            continue
        try:
            from hermes_cli.worktree_runtime import (
                git_worktree_records,
                repo_root_for_path,
            )

            repo_root = repo_root_for_path(target)
            if repo_root is None:
                continue
            package_root = _nearest_pnpm_package_root(target, repo_root)
            records = git_worktree_records(repo_root)
            if package_root is None or not records:
                continue
            primary_root = Path(records[0].path).resolve(strict=False)
            if primary_root == repo_root:
                continue
            relative = package_root.relative_to(repo_root)
            primary_package = primary_root / relative
            modules = package_root / "node_modules"
            primary_modules = primary_package / "node_modules"
            if not modules.is_symlink():
                continue
            if modules.resolve(strict=False) != primary_modules.resolve(strict=False):
                continue
            if not primary_modules.is_dir():
                continue
        except Exception:
            continue
        return {
            "code": "exact_lock_install_blocked",
            "package_root": str(package_root),
            "node_modules": str(modules),
            "shared_target": str(primary_modules),
            "remediation": (
                "Do not install through an exact-lock node_modules symlink. "
                "Change dependencies in the primary worktree, or unlink node_modules "
                "in this worktree before intentionally installing and then rerun "
                "`hermes worktrees prepare .` when the locks match."
            ),
        }
    return None


def sanitize_closeout_receipt(value: Any) -> dict[str, Any] | None:
    raw = value if isinstance(value, dict) else {}
    status = str(raw.get("status") or "").strip().lower()
    head_sha = str(
        raw.get("active_sha")
        or raw.get("head_sha")
        or raw.get("commit_sha")
        or raw.get("sha")
        or ""
    ).strip().lower()
    script = str(raw.get("script") or "").strip().replace("\\", "/")
    if status not in _SUCCESS_CLOSEOUT_STATUSES or not _SHA_RE.fullmatch(head_sha):
        return None
    if not script or len(script) > _MAX_RELATIVE_PATH_CHARS or script.startswith("/") or ".." in Path(script).parts:
        return None
    return {
        "schema_version": 1,
        "status": status,
        "head_sha": head_sha,
        "script": script,
    }


def _clean_repo_state(cwd: Path) -> tuple[Path, str] | None:
    try:
        root_proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if root_proc.returncode != 0:
            return None
        repo_root = Path(root_proc.stdout.strip()).resolve(strict=False)
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if status.returncode != 0 or status.stdout.strip():
            return None
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        observed_head = head.stdout.strip().lower()
        if head.returncode != 0 or not _SHA_RE.fullmatch(observed_head):
            return None
        return repo_root, observed_head
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def closeout_receipt_matches_repo_state(value: Any, cwd: Any) -> bool:
    """Return whether a prior receipt still matches one clean repository state."""

    receipt = sanitize_closeout_receipt(value)
    if receipt is None:
        return False
    command_cwd = Path(str(cwd or ".")).expanduser().resolve(strict=False)
    state = _clean_repo_state(command_cwd)
    if state is None:
        return False
    repo_root, observed_head = state
    if observed_head != receipt["head_sha"]:
        return False
    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", receipt["script"]],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return tracked.returncode == 0
    except (OSError, subprocess.SubprocessError, ValueError):
        return False


def inspect_repo_closeout_receipt(
    *,
    command: Any,
    cwd: Any,
    exit_code: Any,
    classification: Any,
    output: Any,
) -> dict[str, Any] | None:
    """Recognize a successful final direct invocation of a tracked closeout script."""

    if not isinstance(command, str) or not command.strip():
        return None
    try:
        if int(exit_code) != 0:
            return None
    except (TypeError, ValueError):
        return None
    safe_classification = sanitize_terminal_classification(classification)
    if safe_classification["semantic_failure"]:
        return None
    segments = [segment.strip() for segment in _iter_top_level_shell_segments(command) if segment.strip()]
    if not segments:
        return None
    final_segment = segments[-1]
    if any(char in final_segment for char in (">", "<", "`")) or "$(" in final_segment:
        return None
    tokens = _segment_tokens(final_segment)
    if not tokens or any(token == "--dry-run" or token.startswith("--dry-run=") for token in tokens):
        return None
    executable = Path(tokens[0])
    if executable.name in {"sh", "bash"}:
        if len(tokens) < 2:
            return None
        script_token = tokens[1]
    else:
        script_token = tokens[0]
    script_executable = Path(script_token)
    if script_executable.name not in {"closeout", "closeout.sh"}:
        return None
    if not script_executable.is_absolute() and "/" not in script_token:
        return None
    command_cwd = Path(str(cwd or ".")).expanduser().resolve(strict=False)
    script_path = script_executable.expanduser()
    if not script_path.is_absolute():
        script_path = command_cwd / script_path
    script_path = script_path.resolve(strict=False)

    state = _clean_repo_state(command_cwd)
    if state is None:
        return None
    repo_root, observed_head = state
    try:
        relative = script_path.relative_to(repo_root)
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative.as_posix()],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if tracked.returncode != 0:
            return None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None

    last_line = next(
        (line.strip() for line in reversed(str(output or "").splitlines()) if line.strip()),
        "",
    )
    if len(last_line) > 4096:
        return None
    try:
        payload = json.loads(last_line)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload = {**payload, "script": relative.as_posix()}
    receipt = sanitize_closeout_receipt(payload)
    if receipt is None or receipt["head_sha"] != observed_head:
        return None
    return receipt
