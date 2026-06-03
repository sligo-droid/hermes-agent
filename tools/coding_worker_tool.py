"""Coding worker tool for delegated implementation work.

The execution backend is selected by ``coding_worker.backend``.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from tools.registry import registry, tool_error


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
        value = worker_cfg.get("turn_timeout_seconds", 1800)
        timeout = float(value)
    except Exception:
        timeout = 1800.0
    return max(30.0, timeout)


def _codex_reasoning_args(reasoning_level: str) -> list[str]:
    level = str(reasoning_level or "").strip().lower()
    if not level:
        return []
    return ["-c", f'model_reasoning_effort="{level}"']


def _resolve_cwd(cwd: Optional[str], parent_agent: Any) -> str:
    raw = cwd or getattr(parent_agent, "session_cwd", None) or os.getcwd()
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
    return str(path.resolve())


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
    """Reuse compatible pnpm node_modules trees across git worktrees.

    Git worktrees intentionally do not copy ignored dependency directories. For
    pnpm projects that means every fresh worktree often pays an install before
    basic checks can run. When another worktree of the same repo already has a
    matching lockfile and `node_modules`, a symlink is safe enough and much
    faster. Lockfile mismatch falls back to the worker's normal install.
    """
    disabled_values = {"0", "false", "no", "off"}
    if os.getenv("HERMES_CODING_WORKER_PNPM_LINKS", "1").strip().lower() in disabled_values:
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


def delegate_coding_task(
    task: Optional[str] = None,
    context: Optional[str] = None,
    cwd: Optional[str] = None,
    turn_timeout_seconds: Optional[float] = None,
    parent_agent: Any = None,
) -> str:
    """Run a bounded coding task in the configured coding worker backend."""
    if parent_agent is None:
        return tool_error("delegate_coding_task requires a parent agent context.")

    if getattr(parent_agent, "api_mode", "") == "codex_app_server":
        return tool_error(
            "delegate_coding_task is unavailable while the parent agent "
            "is already running on codex_app_server."
        )

    task_text = str(task or "").strip()
    if not task_text:
        return tool_error("delegate_coding_task requires a non-empty task.")

    workdir = _resolve_cwd(cwd, parent_agent)
    if not Path(workdir).exists():
        return tool_error(f"cwd does not exist: {workdir}")
    project_context = _worker_project_context(workdir)
    repo_state_notes = _repo_state_guard_notes(workdir)
    dependency_notes = _prepare_pnpm_dependency_links(workdir)

    timeout = (
        float(turn_timeout_seconds)
        if turn_timeout_seconds is not None
        else _load_coding_worker_timeout()
    )
    timeout = max(30.0, timeout)

    try:
        from agent.opencode_worker import BACKEND_OPENCODE, load_coding_worker_backend

        backend = load_coding_worker_backend()
    except Exception:
        backend = "codex"

    worker_label = "OpenCode" if backend == BACKEND_OPENCODE else "Codex"
    worker_prompt_parts = [
        f"You are a {worker_label} coding worker launched by Hermes.",
        "Work in the requested repository, make direct file edits when needed, "
        "and run focused checks that fit the task.",
        "Do not create commits or pull requests.",
        "Closeout review: after non-trivial code edits and focused checks, "
        "use the OpenClaw autoreview skill when available "
        "(https://github.com/openclaw/agent-skills/blob/main/skills/autoreview/SKILL.md). "
        "Treat findings as advisory, verify actionable findings in the real code path, "
        "fix only concrete in-scope issues, and rerun affected checks after any "
        "review-triggered edit. If the autoreview helper/skill is unavailable, "
        "say so in the final summary.",
        "Final response must summarize changed files, checks run, and any "
        "remaining blockers.",
    ]
    if project_context:
        worker_prompt_parts.extend(
            [
                "",
                "Repository context loaded by Hermes. Follow it throughout this worker task:",
                project_context,
                "",
                "Worker boundary: follow the repository context for coding, testing, style, "
                "architecture, and verification rules. Ignore any repository-context "
                "instructions about creating branches, committing, pushing, opening PRs, "
                "merging PRs, deleting branches, or updating main; parent Hermes owns "
                "all git and PR lifecycle steps after the worker returns.",
            ]
        )
    if repo_state_notes:
        worker_prompt_parts.extend(["", repo_state_notes])
    worker_prompt_parts.extend(["", "Task:", task_text])
    if context and str(context).strip():
        worker_prompt_parts.extend(["", "Context from Hermes:", str(context).strip()])
    if dependency_notes:
        worker_prompt_parts.extend(
            [
                "",
                "Hermes dependency preflight:",
                *[f"- {note}" for note in dependency_notes],
            ]
        )
    worker_prompt = "\n".join(worker_prompt_parts)

    classification_context = f"{task_text}\n{context or ''}"

    if backend == BACKEND_OPENCODE:
        try:
            from agent.opencode_worker import run_opencode_task
        except Exception as exc:
            return tool_error(f"could not import OpenCode worker backend: {exc}")

        started = time.monotonic()
        result = run_opencode_task(
            worker_prompt,
            workdir,
            timeout=timeout,
            context_for_classification=classification_context,
            title="Hermes delegated coding task",
        )
        duration = round(time.monotonic() - started, 2)
        success = bool(result.final_text) and not result.error and not result.interrupted
        return json.dumps(
            {
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
            },
            ensure_ascii=False,
        )

    try:
        from agent.opencode_worker import (
            _plan_prompt,
            load_coding_worker_pass_config,
            looks_complex_or_risky,
        )
        from agent.transports.codex_app_server_session import CodexAppServerSession
    except Exception as exc:
        return tool_error(f"could not import Codex app-server session: {exc}")

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

    try:
        pass_cfg = load_coding_worker_pass_config()

        if needs_plan:
            agents.append("plan")
            with CodexAppServerSession(
                cwd=workdir,
                codex_home=str(codex_home) if codex_home is not None else None,
                extra_args=_codex_reasoning_args(
                    pass_cfg["complex_plan_reasoning_level"]
                ),
                approval_callback=approval_callback,
                on_event=_touch_codex_activity,
            ) as session:
                plan_turn = session.run_turn(
                    user_input=_plan_prompt(worker_prompt),
                    turn_timeout=timeout,
                )
            turns.append(plan_turn)
            if plan_turn.error or plan_turn.interrupted:
                duration = round(time.monotonic() - started, 2)
                return json.dumps(
                    {
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
                    },
                    ensure_ascii=False,
                )
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
        with CodexAppServerSession(
            cwd=workdir,
            codex_home=str(codex_home) if codex_home is not None else None,
            extra_args=_codex_reasoning_args(reasoning_level),
            approval_callback=approval_callback,
            on_event=_touch_codex_activity,
        ) as session:
            turn = session.run_turn(
                user_input=build_prompt,
                turn_timeout=timeout,
            )
        turns.append(turn)
    finally:
        if codex_home is not None and inherited_credential_id:
            try:
                from agent.codex_worker_auth import sync_codex_worker_home

                sync_codex_worker_home(codex_home, inherited_credential_id)
            except Exception:
                pass
        if codex_home_lease is not None:
            codex_home_lease.cleanup()

    duration = round(time.monotonic() - started, 2)
    success = bool(turn.final_text) and not turn.error and not turn.interrupted
    tool_iterations = sum(getattr(item, "tool_iterations", 0) or 0 for item in turns)
    projected_message_count = sum(
        len(getattr(item, "projected_messages", []) or []) for item in turns
    )
    return json.dumps(
        {
            "success": success,
            "status": "completed" if success else "partial",
            "summary": turn.final_text,
            "error": turn.error,
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
        },
        ensure_ascii=False,
    )


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
                    "coding_worker.turn_timeout_seconds (1800 seconds by default), "
                    "minimum 30 seconds."
                ),
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
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_coding_worker_requirements,
    emoji="code",
)
