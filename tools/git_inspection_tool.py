"""Bounded first-class Git inspection for read-only runtimes."""

from __future__ import annotations

import json
import os
import re
import resource
import signal
import subprocess
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from agent.runtime_capabilities import RuntimeMode, ToolEffect, normalize_runtime_mode
from gateway.session_context import get_session_env
from tools.registry import registry, tool_error


_OUTPUT_LIMIT = 100_000
_CAPTURE_LIMIT = 1_000_000
_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@{}~^:+-]{0,199}$")


def _bounded(value: bytes) -> str:
    text = value.decode("utf-8", errors="replace")
    if len(text) <= _OUTPUT_LIMIT:
        return text
    half = (_OUTPUT_LIMIT - 64) // 2
    return text[:half] + "\n...[git inspection truncated]...\n" + text[-half:]


def _safe_relative_path(value: Any) -> str | None:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or os.path.isabs(raw):
        return None
    path = PurePosixPath(raw)
    if ".." in path.parts or "\x00" in raw:
        return None
    return raw


def _git_root(workdir: str) -> tuple[Path | None, str | None]:
    cwd = Path(workdir).expanduser().resolve(strict=False)
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel"],
            cwd=str(cwd),
            capture_output=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return None, f"Git root lookup failed: {type(exc).__name__}"
    if result.returncode != 0:
        return None, "git_inspect requires a Git working tree"
    return Path(result.stdout.decode(errors="replace").strip()).resolve(), None


def _git_resource_limits() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (_CAPTURE_LIMIT, _CAPTURE_LIMIT))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    resource.setrlimit(resource.RLIMIT_CPU, (25, 25))


def _run_git_bounded(command: list[str], root: Path) -> tuple[int, bytes, bytes, bool]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    with tempfile.TemporaryFile() as stdout_file, tempfile.TemporaryFile() as stderr_file:
        process = subprocess.Popen(
            command,
            cwd=str(root),
            stdout=stdout_file,
            stderr=stderr_file,
            env=env,
            start_new_session=True,
            preexec_fn=_git_resource_limits,
        )
        try:
            return_code = process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
            raise
        stdout_file.seek(0)
        stderr_file.seek(0)
        stdout = stdout_file.read(_CAPTURE_LIMIT + 1)
        stderr = stderr_file.read(_CAPTURE_LIMIT + 1)
        truncated = len(stdout) > _OUTPUT_LIMIT or len(stderr) > _OUTPUT_LIMIT
        return return_code, stdout, stderr, truncated


def git_inspect(
    *,
    operation: str,
    revision: str = "",
    paths: Any = None,
    limit: int = 20,
    staged: bool = False,
    workdir: str = "",
    runtime_mode: Any = None,
) -> str:
    if normalize_runtime_mode(runtime_mode) is not RuntimeMode.READ_ONLY:
        return tool_error("git_inspect is available only in a read-only runtime")
    root, error = _git_root(
        str(workdir or get_session_env("HERMES_SESSION_CWD", "") or os.getcwd())
    )
    if root is None:
        return tool_error(error or "Git working tree unavailable")

    operation = str(operation or "").strip().lower().replace("-", "_")
    raw_paths = paths if isinstance(paths, list) else ([] if paths in {None, ""} else [paths])
    safe_paths: list[str] = []
    for value in raw_paths:
        safe = _safe_relative_path(value)
        if safe is None:
            return tool_error("Git inspection paths must be relative and remain inside the repository")
        safe_paths.append(safe)
    if len(safe_paths) > 32:
        return tool_error("Git inspection is limited to 32 paths")

    try:
        limit_value = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit_value = 20

    command = [
        "git",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.pager=cat",
        "-c",
        "interactive.diffFilter=",
        "-c",
        "diff.external=",
        "--literal-pathspecs",
        "--no-optional-locks",
    ]
    if operation == "status":
        command += ["status", "--short", "--branch", "--untracked-files=normal"]
    elif operation == "diff":
        command += ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "--stat"] if revision == "stat" else [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
        ]
        if staged:
            command.append("--cached")
        if revision and revision != "stat":
            if not _REF_RE.fullmatch(revision):
                return tool_error("Git revision contains unsupported characters")
            command.append(revision)
    elif operation == "log":
        command += [
            "log",
            "--no-textconv",
            f"--max-count={limit_value}",
            "--date=iso-strict",
            "--pretty=format:%h%x09%ad%x09%an%x09%s",
        ]
        if revision:
            if not _REF_RE.fullmatch(revision):
                return tool_error("Git revision contains unsupported characters")
            command.append(revision)
    elif operation == "show":
        target = revision or "HEAD"
        if not _REF_RE.fullmatch(target):
            return tool_error("Git revision contains unsupported characters")
        command += [
            "show",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--format=fuller",
            target,
        ]
    elif operation == "branches":
        command += [
            "for-each-ref",
            f"--count={limit_value}",
            "--sort=-committerdate",
            "--format=%(refname:short)%09%(objectname:short)%09%(committerdate:iso-strict)",
            "refs/heads/",
            "refs/remotes/",
        ]
    else:
        return tool_error("operation must be one of: status, diff, log, show, branches")

    if safe_paths and operation in {"status", "diff", "log", "show"}:
        command += ["--", *safe_paths]
    try:
        return_code, stdout, stderr, output_truncated = _run_git_bounded(command, root)
    except subprocess.TimeoutExpired:
        return tool_error("Git inspection exceeded its 20s timeout")
    return json.dumps(
        {
            "success": return_code == 0,
            "operation": operation,
            "exit_code": return_code,
            "output": _bounded(stdout),
            "error": _bounded(stderr) or None,
            "output_truncated": output_truncated,
        },
        ensure_ascii=False,
    )


GIT_INSPECT_SCHEMA = {
    "name": "git_inspect",
    "description": (
        "Inspect repository status, bounded diffs, recent log entries, one revision, or "
        "branch refs without exposing arbitrary Git execution. Available only in read-only runtime."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["status", "diff", "log", "show", "branches"],
            },
            "revision": {
                "type": "string",
                "description": "Optional bounded revision/range; use 'stat' with diff for summary-only output.",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 32,
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            "staged": {"type": "boolean", "default": False},
        },
        "required": ["operation"],
    },
}


registry.register(
    name="git_inspect",
    toolset="terminal",
    schema=GIT_INSPECT_SCHEMA,
    handler=lambda args, **kw: git_inspect(
        operation=args.get("operation", ""),
        revision=args.get("revision", ""),
        paths=args.get("paths"),
        limit=args.get("limit", 20),
        staged=bool(args.get("staged", False)),
        runtime_mode=kw.get("runtime_mode"),
    ),
    effect=ToolEffect.READ_ONLY,
    emoji="🔎",
    max_result_size_chars=_OUTPUT_LIMIT,
)
