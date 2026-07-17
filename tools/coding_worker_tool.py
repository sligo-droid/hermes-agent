"""Coding worker tool for delegated implementation work.

The execution backend is selected by ``coding_worker.backend``.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from hermes_cli.model_tiers import DEFAULT_WORKER_TIERS, resolve_worker_tier
from tools.parallel_worker_worktrees import (
    ParallelWorkerContext as _ParallelWorkerContext,
    merge_parallel_worker_result_unlocked as _merge_parallel_worker_result_locked,
    provision_parallel_worker as _provision_parallel_worker,
)
from tools.registry import registry, tool_error


DEFAULT_CODING_WORKER_GIT_SSH_COMMAND = "ssh -F /dev/null"
_PARALLEL_MERGE_LOCKS: dict[str, threading.Lock] = {}
_PARALLEL_MERGE_LOCKS_GUARD = threading.Lock()
_TURN_WORKER_RUNS_LOCK = threading.Lock()
_CODING_WORKER_FALLBACK_ENV_KEYS = frozenset({
    "ALL_PROXY",
    "GIT_CONFIG_GLOBAL",
    "GIT_SSH_COMMAND",
    "GH_CONFIG_DIR",
    "HOME",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "PATH",
    "SSH_AUTH_SOCK",
})


def check_coding_worker_requirements() -> bool:
    try:
        from agent.opencode_worker import BACKEND_OPENCODE, check_opencode_binary, load_coding_worker_backend

        if load_coding_worker_backend() == BACKEND_OPENCODE:
            ok, _ = check_opencode_binary()
            return bool(ok)
    except Exception:
        return False

    try:
        from agent.transports.codex_app_server import check_codex_binary

        ok, _ = check_codex_binary()
        return bool(ok)
    except Exception:
        return False


def _load_coding_worker_timeout() -> float:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        worker_cfg = cfg.get("coding_worker") or {}
        value = worker_cfg.get("turn_timeout_seconds", 3600)
        timeout = float(value)
    except Exception:
        timeout = 3600.0
    return max(30.0, timeout)


def _coding_worker_config(config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config() or {}
        except Exception:
            config = {}
    worker_cfg = config.get("coding_worker") if isinstance(config, dict) else None
    return worker_cfg if isinstance(worker_cfg, dict) else {}


def is_coding_worker_parallel_enabled(config: Optional[dict[str, Any]] = None) -> bool:
    """Return whether trusted parallel coding-worker provisioning is enabled."""
    parallel_cfg = _coding_worker_config(config).get("parallel") or {}
    if not isinstance(parallel_cfg, dict):
        return True
    return bool(parallel_cfg.get("enabled", True))


def get_coding_worker_parallel_max_workers(config: Optional[dict[str, Any]] = None) -> int:
    """Return the configured coding-worker parallelism with a floor of one."""
    parallel_cfg = _coding_worker_config(config).get("parallel") or {}
    value = parallel_cfg.get("max_workers", 3) if isinstance(parallel_cfg, dict) else 3
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 3


def _codex_reasoning_args(reasoning_level: str) -> list[str]:
    level = str(reasoning_level or "").strip().lower()
    if not level:
        return []
    return ["-c", f'model_reasoning_effort="{level}"']


def _codex_model_args(model: str) -> list[str]:
    selected_model = str(model or "").strip()
    return ["-c", f"model={json.dumps(selected_model)}"] if selected_model else []


def _worker_tier_config(tier: Any) -> dict[str, Any]:
    """Build a pass-wide OpenCode override for an orchestrator worker tier."""
    return {
        "model_tier": "disabled",
        "simple_build_reasoning_level": tier.reasoning_effort,
        "complex_plan_reasoning_level": tier.reasoning_effort,
        "complex_build_reasoning_level": tier.reasoning_effort,
        "opencode": {"model": tier.opencode_model},
    }


def _start_worker_run(
    parent_agent: Any,
    *,
    backend: str,
    model: str,
    reasoning: str,
    tier: Optional[str],
) -> Optional[dict[str, Any]]:
    """Append a best-effort per-turn worker record before execution starts."""
    record: dict[str, Any] = {
        "backend": str(backend or "").strip(),
        "model": str(model or "").strip(),
        "reasoning": str(reasoning or "").strip(),
        "tier": str(tier).strip() if tier else None,
        "failed": True,
    }
    try:
        with _TURN_WORKER_RUNS_LOCK:
            runs = getattr(parent_agent, "turn_worker_runs", None)
            if not isinstance(runs, list):
                runs = []
                parent_agent.turn_worker_runs = runs
            runs.append(record)
    except Exception:
        return None
    return record


def _update_worker_run(
    record: Optional[dict[str, Any]],
    *,
    model: Any,
    reasoning: Any,
) -> None:
    """Update a run to the last pass that actually started."""
    if record is None:
        return
    record["model"] = str(model or "").strip()
    record["reasoning"] = str(reasoning or "").strip()


def _finish_worker_run(record: Optional[dict[str, Any]], *, failed: bool) -> None:
    """Finalize the failure marker without affecting worker execution."""
    if record is None:
        return
    if failed:
        record["failed"] = True
    else:
        record.pop("failed", None)


def _merge_worker_config(
    base: Optional[dict[str, Any]],
    override: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    if not base and not override:
        return None
    merged = dict(base or {})
    override = override or {}
    for key, value in override.items():
        if key == "opencode" and isinstance(value, dict):
            opencode = dict(merged.get("opencode") or {})
            opencode.update(value)
            merged[key] = opencode
        else:
            merged[key] = value
    return merged


def _context_pack_lines(
    relevant_files: Any,
    approach: Any,
    constraints: Any,
    verification: Any,
) -> list[str]:
    file_lines: list[str] = []
    if isinstance(relevant_files, list):
        for item in relevant_files:
            if not isinstance(item, dict):
                continue
            path = str(item.get("path") or "").strip()
            note = str(item.get("note") or "").strip()
            if not path:
                continue
            file_lines.append(f"- `{path}` — {note}" if note else f"- `{path}`")

    sections = [
        ("Approach", str(approach or "").strip()),
        ("Constraints", str(constraints or "").strip()),
        ("Verification", str(verification or "").strip()),
    ]
    if not file_lines and not any(value for _label, value in sections):
        return []

    lines = ["", "## Context from orchestrator"]
    if file_lines:
        lines.extend(["", "Relevant files:", *file_lines])
    for label, value in sections:
        if value:
            lines.extend(["", f"{label}:", value])
    lines.extend(["", "## End context from orchestrator"])
    return lines


def _normalize_scope_paths(scope_paths: Any) -> tuple[Optional[list[str]], Optional[str]]:
    if scope_paths is None:
        return None, None
    if not isinstance(scope_paths, list):
        return None, "scope_paths must be a list of relative path prefixes."

    normalized: list[str] = []
    for raw_path in scope_paths:
        value = str(raw_path or "").strip().replace("\\", "/")
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts:
            return None, (
                "scope_paths must contain non-empty relative path prefixes "
                "without parent-directory ('..') segments."
            )
        rendered = str(path)
        if rendered not in normalized:
            normalized.append(rendered)
    return normalized, None


def _scope_prompt_lines(scope_paths: Optional[list[str]]) -> list[str]:
    if scope_paths is None:
        return []
    if not scope_paths:
        return [
            "",
            "Scope guardrail: scope_paths is empty, so do not modify any files.",
        ]
    return [
        "",
        "Scope guardrail: You may only modify files under these workdir-relative prefixes:",
        *[f"- `{path}`" for path in scope_paths],
        "Do not modify files outside those prefixes; Hermes will check the worktree after you return.",
    ]


def _git_changed_paths(workdir: str) -> list[str]:
    root_proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
    )
    if root_proc.returncode != 0:
        detail = str(root_proc.stderr or root_proc.stdout or "not a git worktree").strip()
        raise RuntimeError(detail)
    repo_root = Path(root_proc.stdout.strip()).resolve(strict=False)
    workdir_path = Path(workdir).resolve(strict=False)
    status_proc = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
        check=False,
    )
    if status_proc.returncode != 0:
        detail = bytes(status_proc.stderr or status_proc.stdout or b"git status failed").decode(
            "utf-8", errors="replace"
        ).strip()
        raise RuntimeError(detail)

    records = bytes(status_proc.stdout or b"").split(b"\0")
    changed: set[str] = set()
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        status = record[:2]
        paths = [record[3:]]
        if (b"R" in status or b"C" in status) and index < len(records):
            paths.append(records[index])
            index += 1
        for raw_path in paths:
            if not raw_path:
                continue
            repo_relative = raw_path.decode("utf-8", errors="surrogateescape")
            absolute = repo_root / repo_relative
            relative = os.path.relpath(absolute, workdir_path).replace(os.sep, "/")
            changed.add(relative)
    return sorted(changed)


def _scope_check(workdir: str, scope_paths: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "scope_paths": list(scope_paths),
        "out_of_scope_files": [],
        "clean": False,
    }
    try:
        changed = _git_changed_paths(workdir)
    except Exception as exc:
        result["inspection_error"] = str(exc)
        return result

    def in_scope(path: str) -> bool:
        return any(
            prefix == "." or path == prefix or path.startswith(f"{prefix}/")
            for prefix in scope_paths
        )

    out_of_scope = [path for path in changed if not in_scope(path)]
    result["out_of_scope_files"] = out_of_scope
    result["clean"] = not out_of_scope
    return result


def _parallel_merge_lock_for(group_id: str) -> threading.Lock:
    with _PARALLEL_MERGE_LOCKS_GUARD:
        lock = _PARALLEL_MERGE_LOCKS.get(group_id)
        if lock is None:
            lock = threading.Lock()
            _PARALLEL_MERGE_LOCKS[group_id] = lock
        return lock


def merge_parallel_worker_result(
    base_cwd: str,
    worker_cwd: str,
    group_id: str,
) -> dict[str, Any]:
    """Apply one isolated worker diff to the turn workspace under a group lock."""
    with _parallel_merge_lock_for(str(group_id)):
        return _merge_parallel_worker_result_locked(
            str(Path(base_cwd).expanduser().resolve()),
            str(Path(worker_cwd).expanduser().resolve()),
            str(group_id),
        )


_UI_ROUTE_DEFAULT_FALLBACK_HINTS = (
    "model provider",
    "not found",
    "not configured",
    "missing api key",
    "api key required",
    "invalid api key",
    "authentication failed",
    "authorization failed",
    "payment required",
    "insufficient credit",
    "insufficient credits",
    "insufficient funds",
    "no usable credits",
    "balance_depleted",
    "billing",
    "credit balance",
    "top up",
    "quota exceeded",
    "402",
    "401",
    "403",
)


def _should_fallback_ui_route_error(error: Any) -> bool:
    """Return True for specialist provider failures safe to retry on default model."""
    text = str(error or "").lower()
    if not text:
        return False
    if "openrouter" in text and any(hint in text for hint in _UI_ROUTE_DEFAULT_FALLBACK_HINTS):
        return True
    if "model provider" in text and any(
        hint in text for hint in ("not found", "not configured", "missing")
    ):
        return True
    return False


def _mark_ui_route_fallback(metadata: dict[str, Any], reason: str) -> dict[str, Any]:
    updated = dict(metadata)
    updated["fallback_used"] = True
    updated["fallback_reason"] = str(reason or "specialist route unavailable")
    updated["selected_route"] = "default_coding_worker"
    updated["selected_provider"] = ""
    updated["selected_model"] = ""
    return updated


def _resolve_cwd(cwd: Optional[str], parent_agent: Any) -> str:
    raw = cwd or getattr(parent_agent, "session_cwd", None) or os.getcwd()
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
    return str(path.resolve())


_TASK_CONTEXT_MARKERS = (
    "task:",
    "task details:",
    "what i need now:",
    "requested change:",
)
_TASK_CONTEXT_STOP_MARKERS = {
    "constraints",
    "context",
    "context from hermes",
    "non-goals",
    "return",
    "verification",
}


def _squash_task_text(text: str, *, limit: int = 800) -> str:
    squashed = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(squashed) <= limit:
        return squashed
    return squashed[: max(0, limit - 1)].rstrip() + "…"


def _infer_task_from_context(context: Any) -> str:
    """Best-effort recovery when a provider omits required ``task``.

    Some tool-call backends do not enforce JSON Schema ``required`` fields.
    Treating a rich ``context``-only call as a hard validation error wastes a
    model round and creates noisy CLI false starts, so recover a concise task
    from common worker-brief sections instead.
    """

    text = str(context or "").strip()
    if not text:
        return ""

    lines = [line.strip() for line in text.splitlines()]
    for idx, line in enumerate(lines):
        lower = line.lower()
        marker = next((m for m in _TASK_CONTEXT_MARKERS if lower.startswith(m)), "")
        if not marker:
            continue
        inline = line[len(marker) :].strip()
        collected: list[str] = [inline] if inline else []
        for next_line in lines[idx + 1 :]:
            stripped = next_line.strip()
            if not stripped:
                if collected:
                    break
                continue
            heading = stripped.lower().rstrip(":")
            if heading in _TASK_CONTEXT_STOP_MARKERS:
                break
            collected.append(stripped)
            if len(" ".join(collected)) >= 800:
                break
        candidate = _squash_task_text(" ".join(part for part in collected if part))
        if candidate:
            return candidate

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower().startswith("user request:"):
            return _squash_task_text(stripped.split(":", 1)[1].strip() or stripped)

    return _squash_task_text(text)


def _normalize_task_and_context(task: Any, context: Any) -> tuple[str, str, bool]:
    task_text = str(task or "").strip()
    context_text = str(context or "").strip()
    if task_text:
        return task_text, context_text, False
    inferred = _infer_task_from_context(context_text)
    if not inferred:
        return "", context_text, False
    repair_note = (
        "Hermes tool-call repair: delegate_coding_task was invoked without "
        "the required `task` field. Hermes inferred the Task below from the "
        "supplied context instead of spending another model round on a "
        "validation error."
    )
    repaired_context = f"{repair_note}\n\n{context_text}" if context_text else repair_note
    return inferred, repaired_context, True


@dataclass(frozen=True)
class DelegateCodingTaskPreflight:
    args: dict[str, Any]
    suppressed_result: str | None = None


def _workspaces_path() -> Path:
    return Path("/home/droid/workspaces").resolve(strict=False)


def _path_inside(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except Exception:
        return False


def _mutable_worktree_for_canonical_cwd(workdir: str) -> str | None:
    """Return an existing mutable worktree for a protected canonical checkout."""

    try:
        from tools.canonical_repo_guard import _repo_info_for_path

        info = _repo_info_for_path(workdir)
    except Exception:
        info = None
    if info is None:
        return None

    workspace_root = _workspaces_path()
    repo_name = info.repo_root.name
    preferred = workspace_root / repo_name
    candidates: list[Path] = []
    try:
        proc = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(info.repo_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        proc = None
    if proc is not None and proc.returncode == 0:
        for line in (proc.stdout or "").splitlines():
            if not line.startswith("worktree "):
                continue
            raw = line.split(" ", 1)[1].strip()
            if not raw:
                continue
            candidate = Path(raw).expanduser()
            try:
                candidate = candidate.resolve(strict=False)
            except Exception:
                pass
            if candidate == info.repo_root or not _path_inside(candidate, workspace_root):
                continue
            if candidate.exists() and candidate.is_dir():
                candidates.append(candidate)

    if preferred in candidates:
        return str(preferred)
    for candidate in candidates:
        if candidate.name == repo_name:
            return str(candidate)
    for candidate in candidates:
        if candidate.name.startswith(f"{repo_name}-"):
            return str(candidate)
    return str(candidates[0]) if candidates else None


def _is_fable_implementation_parent(parent_agent: Any) -> bool:
    """Whether this delegated turn is a Discord Fable implementation parent."""
    return bool(getattr(parent_agent, "_fable_implementation_turn", False))


def _fable_mutable_worktree_error(workdir: str) -> str | None:
    """Return a clear error unless *workdir* is an editable git worktree."""
    try:
        path = Path(str(workdir)).expanduser().resolve(strict=False)
    except Exception:
        path = Path(str(workdir))
    if not path.is_dir():
        return (
            "Fable implementation requires a mutable git worktree, but the configured "
            f"working directory does not exist: {workdir}."
        )
    if not os.access(path, os.W_OK):
        return (
            "Fable implementation requires a mutable git worktree, but the configured "
            f"working directory is not writable: {path}."
        )
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree", "--is-bare-repository"],
            cwd=str(path),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception as exc:
        return (
            "Fable implementation requires a mutable git worktree, but Git could not "
            f"inspect {path}: {exc}."
        )
    values = [line.strip().lower() for line in str(proc.stdout or "").splitlines()]
    if proc.returncode != 0 or not values or values[0] != "true":
        return (
            "Fable implementation requires a mutable git worktree; "
            f"{path} is not inside a Git worktree."
        )
    if len(values) > 1 and values[1] == "true":
        return (
            "Fable implementation requires a mutable git worktree; "
            f"{path} is a bare Git repository."
        )
    return None


def preflight_delegate_coding_task(function_args: dict[str, Any] | None, parent_agent: Any) -> DelegateCodingTaskPreflight:
    """Normalize/suppress malformed worker starts before visible execution."""

    args = dict(function_args or {})
    task_text, context_text, _task_inferred = _normalize_task_and_context(
        args.get("task"),
        args.get("context"),
    )
    args["task"] = task_text
    args["context"] = context_text
    if not task_text:
        return DelegateCodingTaskPreflight(
            args=args,
            suppressed_result=tool_error("delegate_coding_task requires a non-empty task."),
        )

    try:
        workdir = _resolve_cwd(args.get("cwd"), parent_agent)
    except Exception:
        return DelegateCodingTaskPreflight(args=args)

    try:
        from tools.canonical_repo_guard import canonical_main_worker_violation

        canonical_error = canonical_main_worker_violation(workdir)
    except Exception:
        canonical_error = None
    worker_required = bool(
        getattr(parent_agent, "_coding_worker_required_this_turn", False)
        or _is_fable_implementation_parent(parent_agent)
    )
    if not canonical_error:
        if _is_fable_implementation_parent(parent_agent):
            worktree_error = _fable_mutable_worktree_error(workdir)
            if worktree_error:
                return DelegateCodingTaskPreflight(
                    args=args,
                    suppressed_result=tool_error(worktree_error),
                )
        return DelegateCodingTaskPreflight(args=args)
    if not worker_required:
        return DelegateCodingTaskPreflight(args=args)

    repaired_cwd = _mutable_worktree_for_canonical_cwd(workdir)
    if repaired_cwd:
        args["cwd"] = repaired_cwd
        args["context"] = _prepend_context_note(
            context_text,
            (
                "Hermes worker routing repair: delegate_coding_task was invoked "
                f"with protected canonical cwd {workdir}. Hermes redirected the "
                f"coding worker to mutable worktree {repaired_cwd} before launch."
            ),
        )
        if _is_fable_implementation_parent(parent_agent):
            worktree_error = _fable_mutable_worktree_error(repaired_cwd)
            if worktree_error:
                return DelegateCodingTaskPreflight(
                    args=args,
                    suppressed_result=tool_error(worktree_error),
                )
        return DelegateCodingTaskPreflight(args=args)

    return DelegateCodingTaskPreflight(
        args=args,
        suppressed_result=tool_error(
            "delegate_coding_task routing preflight could not find a mutable "
            f"/home/droid/workspaces/ worktree for protected canonical cwd {workdir}. "
            "Create or select a Hermes worktree and retry with cwd set to that absolute path."
        ),
    )


def _workspace_fallback_for_missing_cwd(workdir: str) -> tuple[str, dict[str, str]] | tuple[None, None]:
    """Return a safe existing start directory for a missing requested cwd.

    This is intentionally narrow: only use an existing ancestor that is clearly
    a workspace-style directory. That lets workers locate/clone a missing repo
    without silently running from arbitrary parents like ``/tmp`` or ``/home``.
    """

    requested = Path(str(workdir or "")).expanduser()
    if not requested.is_absolute():
        return None, None
    parent = requested.parent
    try:
        parent_resolved = parent.resolve()
    except Exception:
        parent_resolved = parent
    try:
        exists = parent_resolved.exists() and parent_resolved.is_dir()
    except Exception:
        exists = False
    if not exists:
        return None, None
    parts = {part.lower() for part in parent_resolved.parts}
    if parent_resolved.name.lower() not in {"workspace", "workspaces"} and not (
        {"workspace", "workspaces"} & parts
    ):
        return None, None
    fallback = str(parent_resolved)
    return fallback, {"requested_cwd": str(requested), "fallback_cwd": fallback, "reason": "requested cwd did not exist"}


def _prepend_context_note(context: str, note: str) -> str:
    context_text = str(context or "").strip()
    note_text = str(note or "").strip()
    if not note_text:
        return context_text
    return f"{note_text}\n\n{context_text}" if context_text else note_text


def _call_opencode_task(run_opencode_task: Any, *args: Any, scope_session_key: str = "", **kwargs: Any) -> Any:
    """Call the OpenCode backend with parent session scoping when supported."""
    scoped_kwargs = dict(kwargs)
    try:
        parameters = inspect.signature(run_opencode_task).parameters
    except (TypeError, ValueError):
        parameters = {}
    if "scope_session_key" in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    ):
        scoped_kwargs["scope_session_key"] = scope_session_key
    try:
        return run_opencode_task(*args, **scoped_kwargs)
    except TypeError as exc:
        if (
            "scope_session_key" not in scoped_kwargs
            or "scope_session_key" not in str(exc)
            or "unexpected" not in str(exc).lower()
        ):
            raise
        return run_opencode_task(*args, **kwargs)


def _worker_project_context(workdir: str) -> str:
    """Load the project context block Hermes would use for this repository."""
    try:
        from agent.prompt_builder import build_context_files_prompt

        return build_context_files_prompt(cwd=workdir, skip_soul=True).strip()
    except Exception:
        return ""


def _repo_state_guard_notes(workdir: str) -> str:
    """Return a compact git-state warning block for worker prompts."""
    try:
        from agent.repo_state_guard import format_repo_state_preflight, repo_state_preflight

        return format_repo_state_preflight(repo_state_preflight(workdir)).strip()
    except Exception:
        return ""


def _coding_worker_git_lifecycle_env(workdir: str, parent_agent: Any) -> dict[str, str]:
    """Build a secret-scrubbed env for explicitly authorized git/PR workers."""
    session_key = str(getattr(parent_agent, "session_key", "") or "")
    extra = {
        "HERMES_SESSION_KEY": session_key,
        "HERMES_CODEX_WORKER_NETWORK_ACCESS": "1",
        "HERMES_CODEX_WORKER_WORKSPACE": workdir,
    }
    common_dir = _git_common_dir_outside_workspace(workdir)
    if common_dir:
        extra["HERMES_CODEX_WORKER_GIT_COMMON_DIR"] = common_dir
    try:
        from tools.environments.local import _sanitize_subprocess_env

        env = _sanitize_subprocess_env(os.environ, extra)
    except Exception:
        env = _coding_worker_fallback_env(extra)
    env["HERMES_SESSION_KEY"] = session_key
    for secret_key in ("GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(secret_key, None)
    if _repo_has_ssh_remote(workdir):
        env.setdefault("GIT_SSH_COMMAND", DEFAULT_CODING_WORKER_GIT_SSH_COMMAND)
    return env


def _repo_has_ssh_remote(workdir: str) -> bool:
    """Return True when git operations in this repo would use ssh remotes."""
    try:
        result = subprocess.run(
            ["git", "remote", "-v"],
            cwd=workdir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        return False
    remotes = result.stdout or ""
    return bool(re.search(r"(?m)\b(?:git@|ssh://)", remotes))


def _git_common_dir_outside_workspace(workdir: str) -> str | None:
    """Return linked-worktree Git metadata that Codex must be able to update."""
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=workdir,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    raw = str(proc.stdout or "").strip()
    if proc.returncode != 0 or not raw:
        return None
    try:
        workspace = Path(workdir).expanduser().resolve(strict=False)
        common_dir = Path(raw).expanduser()
        if not common_dir.is_absolute():
            common_dir = workspace / common_dir
        common_dir = common_dir.resolve(strict=False)
    except Exception:
        return None
    return None if _path_inside(common_dir, workspace) else str(common_dir)


def _fable_git_lifecycle_mode(parent_agent: Any) -> str:
    """Read the gateway-owned Fable capability without trusting model input."""
    raw = str(getattr(parent_agent, "_fable_git_lifecycle", "") or "").strip().lower()
    return raw if raw in {"pr", "merge"} else "none"


def _coding_worker_fallback_env(extra: dict[str, str]) -> dict[str, str]:
    """Minimal fallback if the shared sanitizer is unavailable."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _CODING_WORKER_FALLBACK_ENV_KEYS
    }
    env.update(extra)
    return env


def _coding_worker_basic_env(parent_agent: Any) -> dict[str, str]:
    """Build a local-only, secret-scrubbed worker env for repo inspection."""
    extra = {"HERMES_SESSION_KEY": str(getattr(parent_agent, "session_key", "") or "")}
    try:
        from tools.environments.local import _sanitize_subprocess_env

        env = _sanitize_subprocess_env(os.environ, extra)
    except Exception:
        env = _coding_worker_fallback_env(extra)
    env["HERMES_SESSION_KEY"] = extra["HERMES_SESSION_KEY"]
    for secret_key in ("GH_TOKEN", "GITHUB_TOKEN"):
        env.pop(secret_key, None)
    return env


def _allow_ui_route_provider_env(env: dict[str, str], ui_route: Any) -> None:
    """Allow only the provider credential required by an explicit UI route."""
    if (
        ui_route is None
        or not getattr(ui_route, "matched", False)
        or not getattr(ui_route, "enabled", False)
        or str(getattr(ui_route, "provider", "") or "").strip().lower() != "openrouter"
    ):
        return
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return
    env["_HERMES_FORCE_OPENROUTER_API_KEY"] = key


def _run_ui_specialist(
    *,
    prompt: str,
    workdir: str,
    timeout: float,
    parent_agent: Any,
    route_metadata: dict[str, Any],
) -> str:
    """Run UI implementation through its independent Claude Code backend."""
    started = time.monotonic()
    try:
        from hermes_cli.fable_planner import fable_claude_code_env
        from hermes_cli.ui_work_routing import resolve_ui_specialist_runtime

        runtime = resolve_ui_specialist_runtime()
        if runtime["backend"] != "claude_code":
            raise RuntimeError(f"Unsupported UI specialist backend: {runtime['backend']}")
        binary = shutil.which(runtime["binary"])
        if not binary:
            raise RuntimeError(f"Claude Code CLI not found in PATH: {runtime['binary']}")
        proc = subprocess.run(
            [
                binary,
                "--print",
                "--output-format",
                "text",
                "--model",
                runtime["model"],
                "--effort",
                runtime["reasoning_effort"],
                "--permission-mode",
                "acceptEdits",
                "--no-session-persistence",
                prompt,
            ],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=fable_claude_code_env(),
        )
        final_text = proc.stdout.strip()
        error = proc.stderr.strip() if proc.returncode else ""
        success = proc.returncode == 0 and bool(final_text)
    except Exception as exc:
        final_text = ""
        error = str(exc)
        success = False

    actual_route = dict(route_metadata)
    actual_route["actual_provider"] = "anthropic"
    actual_route["actual_model"] = "claude-fable-5"
    actual_route["actual_reasoning_effort"] = "medium"
    return json.dumps(
        {
            "success": success,
            "status": "completed" if success else "error",
            "summary": final_text,
            "error": error,
            "duration_seconds": round(time.monotonic() - started, 2),
            "cwd": workdir,
            "backend": "claude_code",
            "agents": ["ui_visual_specialist"],
            "plan_used": False,
            "ui_work_route": actual_route,
        },
        ensure_ascii=False,
    )


def _trusted_git_pr_lifecycle_enabled(
    parent_agent: Any,
    requested: bool,
    trusted_allow_git_pr_lifecycle: bool,
) -> bool:
    """Gate remote git/PR authority behind trusted orchestrator state only."""
    if not requested:
        return False
    if trusted_allow_git_pr_lifecycle:
        return True
    return bool(getattr(parent_agent, "_allow_git_pr_lifecycle", False))


_SKILL_ACTIVATION_RE = re.compile(r"(?m)^\[IMPORTANT:.*?\bskill\b.*?\]")
_POST_SKILL_CONTEXT_RE = re.compile(
    r"(?m)^\[System note:|^# Project Context\b|^Conversation started:\b"
)
_SKILL_NAME_RE = re.compile(r'"([^"]+)"\s+skill')
_SKILL_DIR_RE = re.compile(r"(?m)^\[Skill directory:\s*(.*?)\]")
_SKILL_DESCRIPTION_RE = re.compile(r"(?m)^description:\s*(.+?)\s*$")
_WORKER_RELEVANT_SKILL_RE = re.compile(
    r"(?i)\b(?:worker skill|worker-relevant skill|load worker skill|pass full skill)\s*:\s*([A-Za-z0-9_.:/-]+)"
    r"|\bload worker skill\s+([A-Za-z0-9_.:/-]+)"
    r"|\bpass full skill\s+([A-Za-z0-9_.:/-]+)"
)

# Budget for skill context inherited from the parent session.  Full active skill
# bodies can be large; coding workers get compact references unless explicitly
# marked worker-relevant, and this cap prevents runaway prompt growth.
_INHERITED_SKILL_CONTEXT_BUDGET_CHARS = 12000
_ALWAYS_FULL_WORKER_SKILL = "general-coding"


@dataclass(frozen=True)
class _SkillBlock:
    name: str
    body: str
    summary: str = ""
    directory: str = ""


def _extract_active_skill_blocks(text: str) -> list[str]:
    """Return loaded/preloaded skill payloads from parent-visible text."""
    if not text or "[IMPORTANT:" not in text or "skill" not in text.lower():
        return []
    matches = list(_SKILL_ACTIVATION_RE.finditer(text))
    blocks: list[str] = []
    for idx, match in enumerate(matches):
        activation = match.group(0).lower()
        if not any(
            phrase in activation
            for phrase in (
                "skill is auto-loaded",
                "skill preloaded",
                "invoked the",
                "skill, indicating",
            )
        ):
            continue
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        post_skill = _POST_SKILL_CONTEXT_RE.search(text, match.end(), end)
        if post_skill:
            end = post_skill.start()
        block = text[match.start() : end].strip()
        if block:
            blocks.append(block)
    return blocks


def _extract_skill_name(block: str) -> str:
    match = _SKILL_NAME_RE.search(block)
    if match:
        return match.group(1).strip()
    return "unknown-skill"


def _extract_skill_summary(block: str) -> str:
    match = _SKILL_DESCRIPTION_RE.search(block)
    if match:
        return match.group(1).strip().strip('"\'')
    for line in block.splitlines()[1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("[") or stripped.startswith("---"):
            continue
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        return stripped[:240]
    return "No summary available."


def _extract_skill_directory(block: str) -> str:
    match = _SKILL_DIR_RE.search(block)
    return match.group(1).strip() if match else ""


def _parse_skill_block(block: str) -> _SkillBlock:
    return _SkillBlock(
        name=_extract_skill_name(block),
        body=block,
        summary=_extract_skill_summary(block),
        directory=_extract_skill_directory(block),
    )


def _worker_relevant_skill_names(task: str = "", context: str = "") -> set[str]:
    names: set[str] = set()
    for match in _WORKER_RELEVANT_SKILL_RE.finditer(f"{task}\n{context}"):
        raw = next((group for group in match.groups() if group), "")
        name = raw.strip().strip("`'\".,;)")
        if name:
            names.add(name.lower())
    return names


def _load_general_coding_skill() -> _SkillBlock | None:
    try:
        from tools.skills_tool import skill_view

        loaded = json.loads(
            skill_view(
                _ALWAYS_FULL_WORKER_SKILL,
                preprocess=False,
                full_content=True,
            )
        )
    except Exception:
        return None
    if not loaded.get("success"):
        return None
    content = str(loaded.get("content") or "").strip()
    if not content:
        return None
    skill_dir = str(loaded.get("skill_dir") or "")
    if skill_dir and "[Skill directory:" not in content:
        content = f"{content}\n\n[Skill directory: {skill_dir}]"
    return _SkillBlock(
        name=str(loaded.get("name") or _ALWAYS_FULL_WORKER_SKILL),
        body=content,
        summary=str(loaded.get("description") or _extract_skill_summary(content)),
        directory=skill_dir,
    )


def _parent_skill_blocks(parent_agent: Any, parent_messages: Optional[list[dict]] = None) -> list[_SkillBlock]:
    """Collect active skill instructions already visible to the parent agent."""
    candidates: list[str] = []
    for attr in ("ephemeral_system_prompt", "_cached_system_prompt"):
        value = getattr(parent_agent, attr, None)
        if isinstance(value, str) and value.strip():
            candidates.append(value)
    for message in parent_messages or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            candidates.append(content)

    blocks: list[_SkillBlock] = []
    seen: set[str] = set()
    for candidate in candidates:
        for block in _extract_active_skill_blocks(candidate):
            if block in seen:
                continue
            seen.add(block)
            blocks.append(_parse_skill_block(block))
    return blocks


def _format_skill_reference(block: _SkillBlock) -> str:
    parts = [f"- {block.name}: {block.summary}"]
    if block.directory:
        parts.append(f"  Skill directory: {block.directory}")
    return "\n".join(parts)


def _append_with_budget(
    sections: list[str],
    section: str,
    used: int,
    omitted: list[str],
) -> int:
    needed = len(section) + (2 if sections else 0)
    if used + needed <= _INHERITED_SKILL_CONTEXT_BUDGET_CHARS:
        sections.append(section)
        return used + needed
    names = ", ".join(_SKILL_NAME_RE.findall(section))
    title = names or section.splitlines()[0].strip("# ").strip() or "skill context"
    omitted.append(title)
    return used


def _parent_skill_context(
    parent_agent: Any,
    parent_messages: Optional[list[dict]] = None,
    *,
    task: str = "",
    context: str = "",
) -> str:
    """Build bounded worker skill context from active parent skills."""
    inherited = _parent_skill_blocks(parent_agent, parent_messages)
    relevant = _worker_relevant_skill_names(task, context)
    general_full: list[_SkillBlock] = []
    full: list[_SkillBlock] = []
    references: list[_SkillBlock] = []
    seen_names: set[str] = set()

    general = _load_general_coding_skill()
    if general is not None:
        general_full.append(general)
        seen_names.add(general.name.lower())

    for block in inherited:
        normalized = block.name.lower()
        if normalized == _ALWAYS_FULL_WORKER_SKILL:
            if normalized not in seen_names:
                full.append(block)
                seen_names.add(normalized)
            continue
        if normalized in relevant:
            full.append(block)
        else:
            references.append(block)

    sections: list[str] = []
    omitted: list[str] = []
    used = 0
    if general_full:
        general_section = "\n\n".join(block.body for block in general_full)
        sections.append("Full worker skill instructions:\n" + general_section)
        # `general-coding` is intentionally always loaded in full.  Do not let
        # that required baseline consume the inherited parent-skill budget, or
        # compact references for omitted parent skills would disappear whenever
        # general-coding itself is large.
        used = 0
    if full:
        full_section = "\n\n".join(block.body for block in full)
        used = _append_with_budget(
            sections,
            "Full explicitly worker-relevant inherited skill instructions:\n" + full_section,
            used,
            omitted,
        )
    if references:
        reference_section = (
            "Omitted active parent skills passed as compact references. "
            "If one becomes relevant, inspect the listed skill directory before relying on it:\n"
            + "\n".join(_format_skill_reference(block) for block in references)
        )
        used = _append_with_budget(sections, reference_section, used, omitted)
    if omitted:
        sections.append(
            "Inherited skill context budget note: omitted or truncated sections because "
            f"the {_INHERITED_SKILL_CONTEXT_BUDGET_CHARS}-character budget was exceeded: "
            + ", ".join(omitted)
            + "."
        )
    return "\n\n".join(sections)


_PNPM_SCAN_SKIP_DIRS = {
    ".git",
    ".hermes",
    ".next",
    ".svelte-kit",
    ".turbo",
    ".venv",
    "build",
    "dist",
    "node_modules",
    "venv",
}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repo_root_for_path(path: Path) -> Optional[Path]:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return Path(raw).resolve()
    except Exception:
        return Path(raw)


def _git_worktree_paths(repo_root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    paths: list[Path] = []
    for line in result.stdout.splitlines():
        if not line.startswith("worktree "):
            continue
        raw = line[len("worktree ") :].strip()
        if not raw:
            continue
        try:
            paths.append(Path(raw).resolve())
        except Exception:
            paths.append(Path(raw))
    return paths


def _pnpm_package_roots(workdir: Path, *, max_depth: int = 4) -> list[Path]:
    roots: list[Path] = []

    def consider(path: Path) -> None:
        if (path / "package.json").is_file() and (path / "pnpm-lock.yaml").is_file():
            try:
                resolved = path.resolve()
            except Exception:
                resolved = path
            if resolved not in roots:
                roots.append(resolved)

    consider(workdir)
    for current, dirs, files in os.walk(workdir):
        current_path = Path(current)
        try:
            rel_parts = current_path.relative_to(workdir).parts
        except Exception:
            rel_parts = ()
        dirs[:] = [d for d in dirs if d not in _PNPM_SCAN_SKIP_DIRS]
        if len(rel_parts) >= max_depth:
            dirs[:] = []
        if "package.json" in files and "pnpm-lock.yaml" in files:
            consider(current_path)
    return roots


def _prepare_pnpm_dependency_links(workdir: str) -> list[str]:
    """Optionally reuse compatible pnpm node_modules trees across worktrees.

    Git worktrees intentionally do not copy ignored dependency directories. For
    pnpm projects that means every fresh worktree often pays an install before
    basic checks can run. When another worktree of the same repo already has a
    matching lockfile and `node_modules`, an explicitly enabled symlink can
    avoid that install. This is disabled by default because packaging tools may
    otherwise resolve dependencies outside the active checkout. Set
    ``HERMES_CODING_WORKER_PNPM_LINKS=1`` to opt in. Lockfile mismatch falls
    back to the worker's normal install.
    """
    enabled_values = {"1", "true", "yes", "on"}
    if os.getenv("HERMES_CODING_WORKER_PNPM_LINKS", "").strip().lower() not in enabled_values:
        return []
    try:
        root = Path(workdir).resolve()
    except Exception:
        root = Path(workdir)
    repo_root = _repo_root_for_path(root)
    if repo_root is None:
        return []
    worktrees = _git_worktree_paths(repo_root)
    if not worktrees:
        return []
    notes: list[str] = []
    for package_root in _pnpm_package_roots(root):
        node_modules = package_root / "node_modules"
        if node_modules.exists() or node_modules.is_symlink():
            continue
        lockfile = package_root / "pnpm-lock.yaml"
        try:
            rel_package = package_root.relative_to(repo_root)
            lock_hash = _hash_file(lockfile)
        except Exception:
            continue
        for worktree_root in worktrees:
            try:
                candidate_root = (worktree_root / rel_package).resolve()
            except Exception:
                candidate_root = worktree_root / rel_package
            if candidate_root == package_root:
                continue
            candidate_modules = candidate_root / "node_modules"
            candidate_lock = candidate_root / "pnpm-lock.yaml"
            if not candidate_modules.is_dir() or not candidate_lock.is_file():
                continue
            try:
                if _hash_file(candidate_lock) != lock_hash:
                    continue
            except Exception:
                continue
            try:
                node_modules.symlink_to(candidate_modules, target_is_directory=True)
            except Exception:
                continue
            note = f"linked {node_modules} -> {candidate_modules}"
            notes.append(note)
            break
    return notes


def _delegate_coding_task_impl(
    task: Optional[str] = None,
    context: Optional[str] = None,
    cwd: Optional[str] = None,
    turn_timeout_seconds: Optional[float] = None,
    worker_tier: Optional[str] = None,
    relevant_files: Optional[list[dict[str, str]]] = None,
    approach: Optional[str] = None,
    constraints: Optional[str] = None,
    verification: Optional[str] = None,
    scope_paths: Optional[list[str]] = None,
    allow_git_pr_lifecycle: bool = False,
    trusted_allow_git_pr_lifecycle: bool = False,
    route_decision: Any = None,
    parent_agent: Any = None,
    parent_messages: Optional[list[dict]] = None,
    _parallel_request: Optional[dict[str, Any]] = None,
) -> str:
    """Run a bounded coding task in the configured coding worker backend."""
    if parent_agent is None:
        return tool_error("delegate_coding_task requires a parent agent context.")

    if getattr(parent_agent, "api_mode", "") == "codex_app_server":
        return tool_error(
            "delegate_coding_task is unavailable while the parent agent "
            "is already running on codex_app_server."
        )

    task_text, context_text, task_inferred_from_context = _normalize_task_and_context(task, context)
    if not task_text:
        return tool_error("delegate_coding_task requires a non-empty task.")
    normalized_scope_paths, scope_error = _normalize_scope_paths(scope_paths)
    if scope_error:
        return tool_error(scope_error)

    try:
        from hermes_cli.config import load_config

        loaded_config = load_config() or {}
    except Exception:
        loaded_config = {}

    selected_worker_tier = None
    if worker_tier is not None:
        selected_worker_tier = resolve_worker_tier(loaded_config, worker_tier)
        if selected_worker_tier is None:
            valid_tiers = ", ".join(DEFAULT_WORKER_TIERS)
            return tool_error(
                f"Unknown worker_tier {worker_tier!r}. Valid tiers: {valid_tiers}."
            )
    worker_tier_config = (
        _worker_tier_config(selected_worker_tier)
        if selected_worker_tier is not None
        else None
    )

    fable_implementation = _is_fable_implementation_parent(parent_agent)
    fable_git_lifecycle = _fable_git_lifecycle_mode(parent_agent)
    allow_git_pr_lifecycle = _trusted_git_pr_lifecycle_enabled(
        parent_agent,
        bool(allow_git_pr_lifecycle),
        trusted_allow_git_pr_lifecycle,
    )
    # Fable follows the same ownership split as the Kanban coding lanes:
    # Codex owns implementation and focused verification, while trusted Hermes
    # code owns commit/push/PR/CI/merge. Never give the Fable child GitHub
    # authority or linked-worktree Git metadata merely because the parent
    # lifecycle policy is ``pr`` or ``merge``.
    if fable_implementation:
        allow_git_pr_lifecycle = False
    allow_git_pr_merge = False
    workdir = _resolve_cwd(cwd, parent_agent)
    _parallel_context: Optional[_ParallelWorkerContext] = None
    fable_git_preparation = None
    cwd_fallback_metadata: dict[str, str] | None = None
    if not Path(workdir).exists():
        fallback_workdir, cwd_fallback_metadata = _workspace_fallback_for_missing_cwd(workdir)
        if not fallback_workdir:
            return tool_error(f"cwd does not exist: {workdir}")
        context_text = _prepend_context_note(
            context_text,
            (
                "Hermes cwd repair: the requested worker cwd did not exist: "
                f"{workdir}. The worker was started from the existing workspace "
                f"directory {fallback_workdir}. First locate an existing checkout "
                "or clone/create the intended repository path before editing; "
                "preserve unrelated local work."
            ),
        )
        workdir = fallback_workdir
    try:
        from tools.canonical_repo_guard import canonical_main_worker_violation

        canonical_error = canonical_main_worker_violation(workdir)
    except Exception:
        canonical_error = None
    if canonical_error:
        if fable_implementation:
            repaired_cwd = _mutable_worktree_for_canonical_cwd(workdir)
            if repaired_cwd:
                workdir = repaired_cwd
            else:
                return tool_error(
                    "Fable implementation requires a mutable /home/droid/workspaces/ "
                    f"worktree for protected canonical cwd {workdir}; refusing direct Fable edits."
                )
        else:
            return tool_error(canonical_error)
    if fable_implementation:
        worktree_error = _fable_mutable_worktree_error(workdir)
        if worktree_error:
            return tool_error(worktree_error)
        if fable_git_lifecycle in {"pr", "merge"}:
            try:
                from hermes_cli.fable_git_finalizer import prepare_fable_git_lifecycle

                fable_git_preparation = prepare_fable_git_lifecycle(
                    workdir,
                    fable_git_lifecycle,
                )
            except Exception as exc:
                return tool_error(
                    "Fable Git lifecycle preflight failed before the Codex worker "
                    f"started: {exc}"
                )
            if not fable_git_preparation.success:
                return tool_error(fable_git_preparation.error)
            if getattr(fable_git_preparation, "resume_existing_pr", False):
                recovery_kind = str(
                    getattr(fable_git_preparation, "recovery_kind", "") or ""
                )
                conflict_files = list(
                    getattr(fable_git_preparation, "conflict_files", []) or []
                )
                if recovery_kind == "merge_conflict":
                    recovery_note = (
                        "Trusted Hermes lifecycle recovery has started a local merge of "
                        f"origin/{fable_git_preparation.base_branch} into the existing PR "
                        f"branch {fable_git_preparation.branch}. Resolve the file contents "
                        "for these conflicts, then run focused verification: "
                        + ", ".join(conflict_files)
                        + ". Do not run git add, commit, push, PR, CI, or merge commands; "
                        "trusted Hermes will validate, stage, commit, push, wait for checks, "
                        "and merge after you return."
                    )
                elif recovery_kind == "base_refresh":
                    recovery_note = (
                        "Trusted Hermes lifecycle recovery has prepared a non-conflicting "
                        f"local merge of origin/{fable_git_preparation.base_branch} into the "
                        f"existing PR branch {fable_git_preparation.branch}. Review the merged "
                        "result and run focused verification, but do not stage, commit, push, "
                        "or touch the PR; trusted Hermes owns finalization."
                    )
                elif recovery_kind == "merged_pr_observation":
                    recovery_note = (
                        f"Trusted Hermes found that PR {fable_git_preparation.pr_url} "
                        "is already merged and aligned this owned worktree to the remote "
                        "base. Perform only the requested read-only/focused verification; "
                        "do not attribute the earlier commit, push, or merge to this Codex "
                        "worker or to the current Hermes finalization attempt."
                    )
                else:
                    recovery_note = (
                        f"Trusted Hermes is resuming existing PR {fable_git_preparation.pr_url} "
                        "for this owned branch. Inspect the current files and run the requested "
                        "focused verification; do not stage, commit, push, or touch the PR."
                    )
                context_text = _prepend_context_note(context_text, recovery_note)

    if _parallel_request is not None:
        try:
            _parallel_context = _provision_parallel_worker(
                str(_parallel_request["base_cwd"]),
                str(_parallel_request["group_id"]),
                requested_cwd=workdir,
            )
        except Exception as exc:
            _parallel_request["provision_error"] = str(exc)
            return tool_error(f"parallel worker provisioning failed: {exc}")
        _parallel_request["context"] = _parallel_context
        workdir = _parallel_context.worker_cwd

    try:
        from agent.opencode_worker import (
            BACKEND_CODEX,
            BACKEND_OPENCODE,
            load_coding_worker_backend,
        )

        try:
            backend = load_coding_worker_backend(config=loaded_config)
        except TypeError:
            backend = load_coding_worker_backend()
    except Exception as exc:
        if fable_implementation:
            return tool_error(
                "Fable implementation requires the Codex coding worker, but Hermes could not "
                f"load that backend: {exc}. Refusing to fall back to OpenCode."
            )
        BACKEND_CODEX = "codex"
        BACKEND_OPENCODE = "opencode"
        backend = "codex"

    if fable_implementation:
        if backend != BACKEND_CODEX:
            return tool_error(
                "Fable implementation requires coding_worker.backend=codex; "
                "refusing to fall back to OpenCode or direct Fable edits."
            )
        try:
            from agent.transports.codex_app_server import check_codex_binary

            codex_ok, codex_detail = check_codex_binary()
        except Exception as exc:
            codex_ok, codex_detail = False, str(exc)
        if not codex_ok:
            return tool_error(
                "Fable implementation requires an available Codex coding worker; "
                f"{codex_detail}. Refusing to fall back to OpenCode or direct Fable edits."
            )

    if fable_implementation:
        # A Fable parent may review the Codex worker result, but must never
        # route a mutation through the Claude Code visual-specialist path.
        ui_route = None
    else:
        try:
            from hermes_cli.ui_work_routing import resolve_ui_work_route

            ui_route = resolve_ui_work_route(
                loaded_config,
                task=task_text,
                context=context_text,
                cwd=workdir,
                backend=backend,
                route_decision=route_decision,
            )
        except Exception:
            ui_route = None
    ui_route_metadata = (
        ui_route.metadata()
        if ui_route is not None
        else {
            "matched": False,
            "enabled": False,
            "reason": "ui routing unavailable",
            "provider": "",
            "model": "",
            "backend": backend,
            "fallback_allowed": False,
            "error": "",
            "route_decision": "default_coding_worker",
            "route_decision_source": "routing_unavailable",
            "route_decision_confidence": None,
            "route_decision_rationale": "",
            "selected_route": "default_coding_worker",
            "selected_provider": "",
            "selected_model": "",
            "fallback_used": False,
            "fallback_reason": "",
            "advisory_matched": False,
            "advisory_reason": "",
        }
    )
    if ui_route is not None and ui_route.error:
        return json.dumps(
            {
                "success": False,
                "status": "error",
                "summary": "",
                "error": ui_route.error,
                "cwd": workdir,
                "backend": backend,
                "ui_work_route": ui_route_metadata,
            },
            ensure_ascii=False,
        )
    if ui_route is not None and not ui_route.launch_worker:
        return json.dumps(
            {
                "success": False,
                "status": "skipped",
                "summary": "",
                "error": ui_route.reason,
                "cwd": workdir,
                "backend": backend,
                "ui_work_route": ui_route_metadata,
            },
            ensure_ascii=False,
        )
    if ui_route is not None and ui_route.matched and ui_route.enabled and ui_route.backend == "codex":
        backend = "codex"

    repo_specific_preflight = cwd_fallback_metadata is None
    project_context = _worker_project_context(workdir) if repo_specific_preflight else ""
    skill_context = _parent_skill_context(
        parent_agent,
        parent_messages,
        task=task_text,
        context=context_text,
    )
    repo_state_notes = _repo_state_guard_notes(workdir) if repo_specific_preflight else ""
    dependency_notes = _prepare_pnpm_dependency_links(workdir) if repo_specific_preflight else []
    try:
        from hermes_cli.worker_autoreview import autoreview_prompt_note, materialize_autoreview_helper

        helper_path = (
            materialize_autoreview_helper(workdir)
            if cwd_fallback_metadata is None
            else None
        )
        autoreview_note = autoreview_prompt_note(helper_path)
    except Exception as exc:
        autoreview_note = f"Autoreview helper materialization failed before worker start: {exc}"
    if cwd_fallback_metadata is not None:
        autoreview_note = (
            "Autoreview helper materialization was deferred because the requested "
            "repository cwd did not exist at worker launch. After locating or "
            "creating the actual repo checkout, run any repo-local closeout helper "
            "if available; otherwise report that it was unavailable because the "
            "worker began from a workspace parent."
        )

    timeout = (
        float(turn_timeout_seconds)
        if turn_timeout_seconds is not None
        else _load_coding_worker_timeout()
    )
    timeout = max(30.0, timeout)

    worker_label = "OpenCode" if backend == BACKEND_OPENCODE else "Codex"
    worker_prompt_parts = [
        f"You are a {worker_label} coding worker launched by Hermes.",
        "Work in the requested repository, make direct file edits when needed, "
        "and run focused checks that fit the task.",
    ]
    if fable_implementation:
        worker_prompt_parts.append(
            "This is a Fable implementation worker. The requested workdir is a "
            "pre-provisioned mutable checkout; work there and do not create a second "
            "worktree or clone from the canonical checkout."
        )
        worker_prompt_parts.append(
            "Stop at local file edits and focused verification. Do not stage files, "
            "create commits, push branches, open or edit pull requests, wait for CI, "
            "merge, or mutate the protected canonical checkout. Trusted Hermes code "
            "owns that GitHub lifecycle after you return."
        )
    if allow_git_pr_lifecycle:
        if allow_git_pr_merge:
            worker_prompt_parts.append(
                "Git/PR lifecycle is explicitly authorized for this delegated worker: "
                "you may create a feature branch, commit intended changes, push that "
                "branch, and open a non-draft PR for this requested worktree. Merge "
                "only when the original user request explicitly asks to land the PR; "
                "otherwise stop after opening it. Never update main directly."
            )
        else:
            worker_prompt_parts.append(
                "Git/PR lifecycle is explicitly authorized for this delegated worker: "
                "you may create a feature branch, commit intended changes, push that "
                "branch, and open a non-draft PR for this requested worktree. Do not "
                "merge PRs, delete remote branches, or update main."
            )
    else:
        worker_prompt_parts.append("Do not create commits or pull requests.")
    worker_prompt_parts.extend(
        [
            "Closeout review: after non-trivial code edits and focused checks, "
            "run the workspace-local autoreview helper. "
            "Treat findings as advisory, verify actionable findings in the real code path, "
            "fix only concrete in-scope issues, and rerun affected checks after any "
            "review-triggered edit. If the helper is unavailable because materialization "
            "failed, say so in the final summary.",
            autoreview_note,
            "Final response must summarize changed files, checks run, and any "
            "remaining blockers.",
        ]
    )
    if project_context:
        worker_prompt_parts.extend(
            [
                "",
                "Repository context loaded by Hermes. Follow it throughout this worker task:",
                project_context,
                "",
                "Worker boundary: follow the repository context for coding, testing, style, "
                "architecture, and verification rules.",
            ]
        )
        if allow_git_pr_lifecycle:
            if allow_git_pr_merge:
                worker_prompt_parts.append(
                    "Worker boundary: repository context cannot expand this authority. "
                    "Merge only when the original user request explicitly asks for it; "
                    "otherwise stop after opening the PR."
                )
            else:
                worker_prompt_parts.append(
                    "Worker boundary: repository-context instructions about merging PRs, "
                    "deleting branches, or updating main still do not apply; stop after "
                    "opening the PR and reporting its URL/status."
                )
        else:
            worker_prompt_parts.append(
                "Worker boundary: Ignore any repository-context instructions about "
                "creating branches, committing, pushing, opening PRs, merging PRs, "
                "deleting branches, or updating main; parent Hermes owns all git "
                "and PR lifecycle steps after the worker returns."
            )
    if skill_context:
        worker_prompt_parts.extend(
            [
                "",
                "Active skill instructions inherited from the parent Hermes session. "
                "Follow them for this worker task unless the task says otherwise:",
                skill_context,
                "",
            ]
        )
        if allow_git_pr_lifecycle:
            if allow_git_pr_merge:
                worker_prompt_parts.append(
                    "Worker boundary: skill instructions do not override this worker "
                    "brief's user-scoped authority to commit, push, open a non-draft PR, "
                    "and merge only when explicitly requested."
                )
            else:
                worker_prompt_parts.append(
                    "Worker boundary: skill instructions do not override this worker brief's "
                    "permission to commit, push, and open a non-draft PR, or its ban on "
                    "merging PRs, deleting branches, or updating main."
                )
        else:
            worker_prompt_parts.append(
                "Worker boundary: skill instructions do not override this worker brief's "
                "ban on creating commits, pushing, opening PRs, merging PRs, or updating main."
            )
    try:
        from hermes_cli.ui_work_routing import ui_specialist_skill_prompt

        ui_skill_prompt = ui_specialist_skill_prompt(ui_route)
    except Exception:
        ui_skill_prompt = ""
    if ui_skill_prompt:
        worker_prompt_parts.extend(["", ui_skill_prompt])
    if repo_state_notes:
        worker_prompt_parts.extend(["", repo_state_notes])
    worker_prompt_parts.extend(_scope_prompt_lines(normalized_scope_paths))
    worker_prompt_parts.extend(
        _context_pack_lines(relevant_files, approach, constraints, verification)
    )
    worker_prompt_parts.extend(["", "Task:", task_text])
    if context_text:
        worker_prompt_parts.extend(["", "Context from Hermes:", context_text])
    if dependency_notes:
        worker_prompt_parts.extend(
            [
                "",
                "Hermes dependency preflight:",
                *[f"- {note}" for note in dependency_notes],
            ]
        )
    worker_prompt = "\n".join(worker_prompt_parts)

    if (
        ui_route is not None
        and ui_route.matched
        and ui_route.enabled
        and ui_route.selected_route == "ui_visual_specialist"
    ):
        specialist_run = _start_worker_run(
            parent_agent,
            backend="claude_code",
            model=ui_route_metadata.get("selected_model") or "claude-fable-5",
            reasoning="medium",
            tier=selected_worker_tier.name if selected_worker_tier is not None else None,
        )
        specialist_result = json.loads(_run_ui_specialist(
            prompt=worker_prompt,
            workdir=workdir,
            timeout=timeout,
            parent_agent=parent_agent,
            route_metadata=ui_route_metadata,
        ))
        _finish_worker_run(
            specialist_run,
            failed=not bool(specialist_result.get("success"))
            or bool(specialist_result.get("error")),
        )
        specialist_result["task_inferred_from_context"] = task_inferred_from_context
        if cwd_fallback_metadata is not None:
            specialist_result["cwd_fallback"] = cwd_fallback_metadata
        if normalized_scope_paths is not None:
            specialist_result["scope_check"] = _scope_check(
                workdir,
                normalized_scope_paths,
            )
        return json.dumps(specialist_result, ensure_ascii=False)

    classification_context = f"{task_text}\n{context_text}"
    worker_env = (
        _coding_worker_git_lifecycle_env(workdir, parent_agent)
        if allow_git_pr_lifecycle
        else _coding_worker_basic_env(parent_agent)
    )
    _allow_ui_route_provider_env(worker_env, ui_route)

    if backend == BACKEND_OPENCODE:
        try:
            from agent.opencode_worker import (
                load_opencode_config,
                looks_complex_or_risky,
                run_opencode_task,
            )
        except Exception as exc:
            return tool_error(f"could not import OpenCode worker backend: {exc}")

        try:
            from hermes_cli.ui_work_routing import opencode_ui_work_worker_config
        except Exception:
            opencode_ui_work_worker_config = None

        def _touch_opencode_activity(event: dict) -> None:
            try:
                event_type = str(event.get("type") or event.get("method") or "event")
                agent = str(event.get("agent") or "")
                suffix = f": {agent}" if agent else ""
                touch_activity = getattr(parent_agent, "_touch_activity", None)
                if callable(touch_activity):
                    touch_activity(f"OpenCode coding worker event: {event_type}{suffix}")
            except Exception:
                pass

        started = time.monotonic()
        opencode_kwargs = {
            "timeout": timeout,
            "context_for_classification": classification_context,
            "title": "Hermes delegated coding task",
            "on_event": _touch_opencode_activity,
        }
        opencode_worker_config = None
        if opencode_ui_work_worker_config is not None and ui_route is not None:
            ui_opencode_config = opencode_ui_work_worker_config(ui_route)
            if ui_opencode_config:
                # The visual specialist is an explicit independent route, not
                # an ordinary feature-worker tier. Preserve its own pass
                # configuration while its model override is active.
                ui_opencode_config["model_tier"] = "disabled"
                opencode_worker_config = ui_opencode_config
        opencode_worker_config = _merge_worker_config(
            opencode_worker_config,
            worker_tier_config,
        )
        if opencode_worker_config is not None:
            opencode_kwargs["worker_config"] = opencode_worker_config
        if allow_git_pr_lifecycle:
            opencode_kwargs["env"] = worker_env
        elif ui_route is not None and ui_route.matched and ui_route.enabled:
            opencode_kwargs["env"] = worker_env
        opencode_runtime = load_opencode_config(worker_config=opencode_worker_config)
        opencode_needs_plan = looks_complex_or_risky(
            worker_prompt,
            classification_context,
        )
        opencode_pass = "complex_build" if opencode_needs_plan else "simple_build"
        opencode_run = _start_worker_run(
            parent_agent,
            backend="opencode",
            model=opencode_runtime.get(f"{opencode_pass}_model") or "",
            reasoning=opencode_runtime.get(f"{opencode_pass}_reasoning_level") or "",
            tier=selected_worker_tier.name if selected_worker_tier is not None else None,
        )
        result = _call_opencode_task(
            run_opencode_task,
            worker_prompt,
            workdir,
            scope_session_key=getattr(parent_agent, "session_key", ""),
            **opencode_kwargs,
        )
        duration = round(time.monotonic() - started, 2)
        success = bool(result.final_text) and not result.error and not result.interrupted
        run_profile = getattr(result, "run_profile", None)
        profile_passes = run_profile.get("passes") if isinstance(run_profile, dict) else None
        if isinstance(profile_passes, list) and profile_passes:
            executed_passes = len(getattr(result, "agents", []) or [])
            profile_index = min(max(executed_passes - 1, 0), len(profile_passes) - 1)
            actual_pass = profile_passes[profile_index]
            if isinstance(actual_pass, dict):
                _update_worker_run(
                    opencode_run,
                    model=actual_pass.get("model"),
                    reasoning=actual_pass.get("reasoning"),
                )
        _finish_worker_run(opencode_run, failed=not success)
        payload = {
            "success": success,
            "status": "completed" if success else "partial",
            "summary": result.final_text,
            "error": result.error,
            "interrupted": result.interrupted,
            "duration_seconds": duration,
            "cwd": workdir,
            "backend": "opencode",
            "agents": result.agents,
            "plan_used": bool(result.plan_text),
            "thread_id": result.thread_id,
            "turn_id": result.turn_id,
            "tool_iterations": result.tool_iterations,
            "ui_work_route": ui_route_metadata,
            "task_inferred_from_context": task_inferred_from_context,
        }
        if cwd_fallback_metadata is not None:
            payload["cwd_fallback"] = cwd_fallback_metadata
        no_final_metadata = getattr(result, "no_final_metadata", None)
        if no_final_metadata:
            payload["evidence_status"] = no_final_metadata.get("evidence_status") or "degraded"
            payload["failure_class"] = no_final_metadata.get("failure_class") or "no_final_text"
            payload["no_final_metadata"] = no_final_metadata
        if normalized_scope_paths is not None:
            payload["scope_check"] = _scope_check(workdir, normalized_scope_paths)
        return json.dumps(payload, ensure_ascii=False)

    try:
        from agent.opencode_worker import (
            _plan_prompt,
            load_coding_worker_pass_profiles,
            load_coding_worker_pass_config,
            looks_complex_or_risky,
        )
        from agent.transports.codex_app_server_session import CodexAppServerSession
    except Exception as exc:
        return tool_error(f"could not import Codex app-server session: {exc}")
    try:
        from hermes_cli.ui_work_routing import codex_ui_work_extra_args
    except Exception as exc:
        if (
            ui_route is not None
            and ui_route.matched
            and ui_route.enabled
            and ui_route.backend == "codex"
            and not ui_route.fallback_allowed
        ):
            return tool_error(
                "ui_work routing matched but Codex route args could not be built: "
                f"{exc}"
            )
        codex_ui_work_extra_args = None

    try:
        from tools.terminal_tool import _get_approval_callback

        approval_callback = _get_approval_callback()
    except Exception:
        approval_callback = None

    codex_home = None
    codex_home_lease = None
    inherited_credential_id = None
    try:
        from agent.codex_worker_auth import create_codex_worker_home

        codex_home_lease = create_codex_worker_home(
            parent_agent=parent_agent,
            prefix=f"delegate-{os.getpid()}-{uuid.uuid4().hex[:8]}-",
        )
        codex_home = codex_home_lease.path
        inherited_credential_id = codex_home_lease.credential_id
    except Exception:
        codex_home = None
        codex_home_lease = None
        inherited_credential_id = None

    def _touch_codex_activity(note: dict) -> None:
        try:
            method = note.get("method", "")
            item = ((note.get("params") or {}).get("item") or {})
            item_type = item.get("type") or ""
            suffix = f": {item_type}" if item_type else ""
            parent_agent._touch_activity(f"Coding worker event: {method}{suffix}")
        except Exception:
            pass

    started = time.monotonic()
    needs_plan = looks_complex_or_risky(task_text, classification_context)
    agents: list[str] = []
    plan_text = ""
    turns = []
    ui_codex_args = (
        codex_ui_work_extra_args(ui_route)
        if codex_ui_work_extra_args is not None and ui_route is not None
        else []
    )
    if selected_worker_tier is not None:
        default_profiles = {
            pass_name: {"codex_model": selected_worker_tier.model}
            for pass_name in ("simple_build", "complex_plan", "complex_build")
        }
    else:
        default_profiles = load_coding_worker_pass_profiles()
    turn = None
    codex_run = None
    if (
        ui_route is not None
        and ui_route.matched
        and ui_route.enabled
        and ui_route.backend == "codex"
        and not ui_route.fallback_allowed
        and not ui_codex_args
    ):
        return tool_error(
            "ui_work routing matched but Codex route args were empty; "
            "refusing to fall back to the default coding worker."
        )

    try:
        default_pass_cfg = (
            {
                "simple_build_reasoning_level": selected_worker_tier.reasoning_effort,
                "complex_plan_reasoning_level": selected_worker_tier.reasoning_effort,
                "complex_build_reasoning_level": selected_worker_tier.reasoning_effort,
            }
            if selected_worker_tier is not None
            else load_coding_worker_pass_config()
        )
        ui_worker_config = _merge_worker_config(
            {"model_tier": "disabled"}
            if ui_route is not None
            and ui_route.matched
            and ui_route.enabled
            and ui_route.backend == "codex"
            else None,
            worker_tier_config,
        )
        ui_pass_cfg = (
            load_coding_worker_pass_config(worker_config=ui_worker_config)
            if ui_worker_config is not None and selected_worker_tier is None
            else default_pass_cfg
        )
        route_attempts = [
            (
                ui_codex_args,
                ui_pass_cfg,
                default_profiles if selected_worker_tier is not None else None,
            )
            if ui_codex_args
            else ([], default_pass_cfg, default_profiles)
        ]
        if (
            ui_codex_args
            and ui_route is not None
            and ui_route.matched
            and ui_route.enabled
            and ui_route.backend == "codex"
            and ui_route.fallback_allowed
        ):
            route_attempts.append(([], default_pass_cfg, default_profiles))
        if (
            not ui_codex_args
            and ui_route is not None
            and ui_route.matched
            and ui_route.enabled
            and ui_route.backend == "codex"
            and ui_route.fallback_allowed
        ):
            ui_route_metadata = _mark_ui_route_fallback(
                ui_route_metadata,
                ui_route.reason,
            )

        for attempt_index, (active_ui_codex_args, pass_cfg, pass_profiles) in enumerate(route_attempts):
            agents = []
            turns = []
            plan_text = ""

            def _attempt_model(pass_name: str) -> str:
                if active_ui_codex_args and ui_route is not None:
                    return str(ui_route.model or "").strip()
                if pass_profiles:
                    return str(pass_profiles[pass_name]["codex_model"] or "").strip()
                return ""

            initial_pass = "complex_plan" if needs_plan else "simple_build"
            codex_run = _start_worker_run(
                parent_agent,
                backend="codex",
                model=_attempt_model(initial_pass),
                reasoning=pass_cfg[f"{initial_pass}_reasoning_level"],
                tier=selected_worker_tier.name if selected_worker_tier is not None else None,
            )

            if needs_plan:
                agents.append("plan")
                with CodexAppServerSession(
                    cwd=workdir,
                    codex_home=str(codex_home) if codex_home is not None else None,
                    extra_args=active_ui_codex_args + _codex_model_args(
                        pass_profiles["complex_plan"]["codex_model"] if pass_profiles else ""
                    ) + _codex_reasoning_args(
                        pass_cfg["complex_plan_reasoning_level"]
                    ),
                    approval_callback=approval_callback,
                    on_event=_touch_codex_activity,
                    env=worker_env,
                    replace_env=False,
                    scope_kind="coding-worker",
                    scope_purpose="Codex coding worker plan pass",
                ) as session:
                    plan_turn = session.run_turn(
                        user_input=_plan_prompt(worker_prompt),
                        turn_timeout=timeout,
                    )
                turns.append(plan_turn)
                if plan_turn.error or plan_turn.interrupted:
                    if (
                        attempt_index == 0
                        and len(route_attempts) > 1
                        and plan_turn.error
                        and _should_fallback_ui_route_error(plan_turn.error)
                    ):
                        ui_route_metadata = _mark_ui_route_fallback(
                            ui_route_metadata,
                            plan_turn.error,
                        )
                        _finish_worker_run(codex_run, failed=True)
                        continue
                    duration = round(time.monotonic() - started, 2)
                    payload = {
                        "success": False,
                        "status": "partial",
                        "summary": plan_turn.final_text,
                        "error": plan_turn.error,
                        "interrupted": plan_turn.interrupted,
                        "duration_seconds": duration,
                        "cwd": workdir,
                        "backend": "codex",
                        "agents": agents,
                        "plan_used": True,
                        "thread_id": plan_turn.thread_id,
                        "turn_id": plan_turn.turn_id,
                        "tool_iterations": plan_turn.tool_iterations,
                        "projected_message_count": len(plan_turn.projected_messages),
                        "ui_work_route": ui_route_metadata,
                    }
                    if normalized_scope_paths is not None:
                        payload["scope_check"] = _scope_check(
                            workdir,
                            normalized_scope_paths,
                        )
                    _finish_worker_run(codex_run, failed=True)
                    return json.dumps(payload, ensure_ascii=False)
                plan_text = plan_turn.final_text.strip()

            agents.append("build")
            build_prompt = worker_prompt
            if plan_text:
                build_prompt = (
                    f"{worker_prompt.rstrip()}\n\n"
                    "Codex plan to follow:\n"
                    f"{plan_text}\n"
                )
            reasoning_level = (
                pass_cfg["complex_build_reasoning_level"]
                if needs_plan
                else pass_cfg["simple_build_reasoning_level"]
            )
            build_pass = "complex_build" if needs_plan else "simple_build"
            _update_worker_run(
                codex_run,
                model=_attempt_model(build_pass),
                reasoning=reasoning_level,
            )
            with CodexAppServerSession(
                cwd=workdir,
                codex_home=str(codex_home) if codex_home is not None else None,
                extra_args=active_ui_codex_args + _codex_model_args(
                    pass_profiles["complex_build" if needs_plan else "simple_build"]["codex_model"]
                    if pass_profiles else ""
                ) + _codex_reasoning_args(reasoning_level),
                approval_callback=approval_callback,
                on_event=_touch_codex_activity,
                env=worker_env,
                replace_env=False,
                scope_kind="coding-worker",
                scope_purpose="Codex coding worker build pass",
            ) as session:
                turn = session.run_turn(
                    user_input=build_prompt,
                    turn_timeout=timeout,
                )
            turns.append(turn)
            if (
                attempt_index == 0
                and len(route_attempts) > 1
                and turn.error
                and _should_fallback_ui_route_error(turn.error)
            ):
                ui_route_metadata = _mark_ui_route_fallback(
                    ui_route_metadata,
                    turn.error,
                )
                _finish_worker_run(codex_run, failed=True)
                continue
            break
    finally:
        if codex_home is not None and inherited_credential_id:
            try:
                from agent.codex_worker_auth import sync_codex_worker_home

                sync_codex_worker_home(codex_home, inherited_credential_id)
            except Exception:
                pass
        if codex_home_lease is not None:
            codex_home_lease.cleanup()

    if turn is None:
        _finish_worker_run(codex_run, failed=True)
        return tool_error("coding worker did not produce a build turn")

    duration = round(time.monotonic() - started, 2)
    success = bool(turn.final_text) and not turn.error and not turn.interrupted
    _finish_worker_run(codex_run, failed=not success)
    scope_check = (
        _scope_check(workdir, normalized_scope_paths)
        if normalized_scope_paths is not None
        else None
    )
    fable_git_result = None
    lifecycle_error = ""
    if (
        success
        and fable_implementation
        and fable_git_lifecycle in {"pr", "merge"}
        and fable_git_preparation is not None
    ):
        try:
            from hermes_cli.fable_git_finalizer import finalize_fable_git_lifecycle

            finalized = finalize_fable_git_lifecycle(
                fable_git_preparation,
                mode=fable_git_lifecycle,
                task=task_text,
                worker_summary=turn.final_text,
            )
            fable_git_result = finalized.as_dict()
            if not finalized.success:
                success = False
                lifecycle_error = finalized.error or (
                    f"Fable Git lifecycle stopped at {finalized.status}."
                )
        except Exception as exc:
            success = False
            lifecycle_error = f"Fable Git lifecycle finalization failed: {exc}"
    tool_iterations = sum(getattr(item, "tool_iterations", 0) or 0 for item in turns)
    projected_message_count = sum(
        len(getattr(item, "projected_messages", []) or []) for item in turns
    )
    payload = {
        "success": success,
        "status": "completed" if success else "partial",
        "summary": turn.final_text,
        "error": turn.error or lifecycle_error or None,
        "interrupted": turn.interrupted,
        "duration_seconds": duration,
        "cwd": workdir,
        "backend": "codex",
        "agents": agents,
        "plan_used": bool(plan_text),
        "thread_id": turn.thread_id,
        "turn_id": turn.turn_id,
        "tool_iterations": tool_iterations,
        "projected_message_count": projected_message_count,
        "ui_work_route": ui_route_metadata,
        "task_inferred_from_context": task_inferred_from_context,
        "fable_git_lifecycle": fable_git_lifecycle if fable_implementation else "none",
    }
    if fable_git_result is not None:
        payload["fable_git_result"] = fable_git_result
    if scope_check is not None:
        payload["scope_check"] = scope_check
    if cwd_fallback_metadata is not None:
        payload["cwd_fallback"] = cwd_fallback_metadata
    return json.dumps(payload, ensure_ascii=False)


def delegate_coding_task(
    task: Optional[str] = None,
    context: Optional[str] = None,
    cwd: Optional[str] = None,
    turn_timeout_seconds: Optional[float] = None,
    worker_tier: Optional[str] = None,
    relevant_files: Optional[list[dict[str, str]]] = None,
    approach: Optional[str] = None,
    constraints: Optional[str] = None,
    verification: Optional[str] = None,
    scope_paths: Optional[list[str]] = None,
    allow_git_pr_lifecycle: bool = False,
    trusted_allow_git_pr_lifecycle: bool = False,
    route_decision: Any = None,
    parent_agent: Any = None,
    parent_messages: Optional[list[dict]] = None,
    _parallel_group: Optional[dict[str, Any]] = None,
) -> str:
    """Run a coding worker, isolating trusted parallel calls in linked worktrees."""
    call_kwargs = {
        "task": task,
        "context": context,
        "cwd": cwd,
        "turn_timeout_seconds": turn_timeout_seconds,
        "worker_tier": worker_tier,
        "relevant_files": relevant_files,
        "approach": approach,
        "constraints": constraints,
        "verification": verification,
        "scope_paths": scope_paths,
        "allow_git_pr_lifecycle": allow_git_pr_lifecycle,
        "trusted_allow_git_pr_lifecycle": trusted_allow_git_pr_lifecycle,
        "route_decision": route_decision,
        "parent_agent": parent_agent,
        "parent_messages": parent_messages,
    }
    if _parallel_group is None:
        return _delegate_coding_task_impl(**call_kwargs)

    if not is_coding_worker_parallel_enabled():
        payload = json.loads(_delegate_coding_task_impl(**call_kwargs))
        payload["parallel"] = {"disabled": True}
        return json.dumps(payload, ensure_ascii=False)

    group = _parallel_group if isinstance(_parallel_group, dict) else {}
    group_id = str(group.get("group_id") or "").strip()
    base_cwd = str(group.get("base_cwd") or "").strip()
    if not group_id or not base_cwd:
        payload = json.loads(
            tool_error("_parallel_group requires non-empty group_id and base_cwd values.")
        )
        payload["parallel"] = {
            "group_id": group_id,
            "worker_cwd": "",
            "merged": False,
            "merge_conflicts": [],
            "worktree_kept": False,
        }
        return json.dumps(payload, ensure_ascii=False)

    parallel_request: dict[str, Any] = {
        "group_id": group_id,
        "base_cwd": base_cwd,
    }
    call_kwargs["cwd"] = _resolve_cwd(cwd, parent_agent) if cwd else base_cwd
    call_kwargs["_parallel_request"] = parallel_request
    try:
        raw_result = _delegate_coding_task_impl(**call_kwargs)
        payload = json.loads(raw_result)
    except Exception as exc:
        payload = json.loads(tool_error(f"parallel coding worker failed: {exc}"))
    parallel_context = parallel_request.get("context")
    if "parallel" not in payload and isinstance(
        parallel_context, _ParallelWorkerContext
    ):
        payload["parallel"] = {
            "group_id": parallel_context.group_id,
            "worker_cwd": parallel_context.worker_cwd,
            "merged": False,
            "merge_pending": True,
            "merge_conflicts": [],
            "worktree_kept": True,
        }
    elif "parallel" not in payload:
        payload["parallel"] = {
            "group_id": group_id,
            "worker_cwd": "",
            "merged": False,
            "merge_conflicts": [],
            "worktree_kept": False,
        }
    return json.dumps(payload, ensure_ascii=False)


CODING_WORKER_SCHEMA = {
    "name": "delegate_coding_task",
    "description": (
        "Delegate a bounded implementation, debugging, test-fixing, refactor, "
        "or code-review task to the configured coding worker backend. Use from "
        "Hermes' normal runtime when a worker should do the coding-heavy step; "
        "Hermes remains responsible for reviewing the worker result and "
        "reporting final status to the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Concrete coding task for the coding worker.",
            },
            "context": {
                "type": "string",
                "description": (
                    "Relevant file paths, errors, constraints, repo state, "
                    "and success criteria the worker needs."
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory for the worker. Defaults to the "
                    "current Hermes session directory."
                ),
            },
            "turn_timeout_seconds": {
                "type": "number",
                "description": (
                    "Optional per-call timeout. Defaults to "
                    "coding_worker.turn_timeout_seconds (3600 seconds / 1 hour by default), "
                    "minimum 30 seconds."
                ),
            },
            "worker_tier": {
                "type": "string",
                "enum": list(DEFAULT_WORKER_TIERS),
                "description": (
                    "Orchestrator-selected worker model/reasoning tier. "
                    "quick = trivial mechanical changes (rename, copy edit, config tweak); "
                    "standard = ordinary small features and fixes; "
                    "thorough = multi-file features, refactors, tricky bugs; "
                    "deep = complex cross-cutting work; "
                    "max = RARE: reach for this only under exceptional circumstances "
                    "such as a complete re-design; expensive and slow."
                ),
            },
            "relevant_files": {
                "type": "array",
                "description": (
                    "Files the orchestrator already inspected, with concise notes explaining "
                    "why each is relevant so the worker does not rediscover them."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Repo-relative file path; line references are welcome.",
                        },
                        "note": {
                            "type": "string",
                            "description": "What the orchestrator learned from this file.",
                        },
                    },
                    "required": ["path", "note"],
                },
            },
            "approach": {
                "type": "string",
                "description": (
                    "Intended approach or decomposition from the orchestrator's investigation."
                ),
            },
            "constraints": {
                "type": "string",
                "description": (
                    "Repo conventions, exclusions, compatibility requirements, and gotchas "
                    "already identified by the orchestrator."
                ),
            },
            "verification": {
                "type": "string",
                "description": (
                    "Focused verification the orchestrator expects the worker to run."
                ),
            },
            "scope_paths": {
                "type": "array",
                "description": (
                    "Optional workdir-relative path prefixes the worker may modify. Hermes "
                    "deterministically reports any changed files outside these prefixes."
                ),
                "items": {"type": "string"},
            },
            "route_decision": {
                "type": "object",
                "description": (
                    "Optional orchestrator-controlled worker route decision. "
                    "Use route=ui_visual_specialist only when this specific "
                    "coding worker should run on the configured UI specialist "
                    "provider/model; route=default_coding_worker keeps the "
                    "default coding worker even if visual keywords are present. "
                    "route=review_only_no_worker or route=ask_human records "
                    "the decision and skips launching a coding worker."
                ),
                "properties": {
                    "route": {
                        "type": "string",
                        "enum": [
                            "default_coding_worker",
                            "ui_visual_specialist",
                            "review_only_no_worker",
                            "ask_human",
                        ],
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                        "description": "Optional orchestrator confidence from 0 to 1.",
                    },
                    "rationale": {
                        "type": "string",
                        "description": "Short reason for the route decision.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Decision source label; defaults to orchestrator.",
                    },
                },
                "required": ["route"],
            },
        },
        "required": ["task"],
    },
}


registry.register(
    name="delegate_coding_task",
    toolset="delegation",
    schema=CODING_WORKER_SCHEMA,
    handler=lambda args, **kw: delegate_coding_task(
        task=args.get("task"),
        context=args.get("context"),
        cwd=args.get("cwd"),
        turn_timeout_seconds=args.get("turn_timeout_seconds"),
        worker_tier=args.get("worker_tier"),
        relevant_files=args.get("relevant_files"),
        approach=args.get("approach"),
        constraints=args.get("constraints"),
        verification=args.get("verification"),
        scope_paths=args.get("scope_paths"),
        allow_git_pr_lifecycle=False,
        trusted_allow_git_pr_lifecycle=False,
        route_decision=args.get("route_decision"),
        parent_agent=kw.get("parent_agent"),
        parent_messages=args.get("_parent_messages") or kw.get("parent_messages"),
        _parallel_group=args.get("_parallel_group"),
    ),
    check_fn=check_coding_worker_requirements,
    emoji="code",
)
