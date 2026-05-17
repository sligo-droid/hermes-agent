"""Codex app-server worker tool for coding delegation."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from tools.registry import registry, tool_error


def check_codex_worker_requirements() -> bool:
    try:
        from agent.transports.codex_app_server import check_codex_binary

        ok, _ = check_codex_binary()
        return bool(ok)
    except Exception:
        return False


def _load_codex_worker_timeout() -> float:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        worker_cfg = cfg.get("codex_worker") or {}
        value = worker_cfg.get("turn_timeout_seconds", 1800)
        timeout = float(value)
    except Exception:
        timeout = 1800.0
    return max(30.0, timeout)


def _resolve_cwd(cwd: Optional[str], parent_agent: Any) -> str:
    raw = cwd or getattr(parent_agent, "session_cwd", None) or os.getcwd()
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = Path(os.getcwd()) / path
    return str(path.resolve())


def delegate_codex_coding_task(
    task: Optional[str] = None,
    context: Optional[str] = None,
    cwd: Optional[str] = None,
    turn_timeout_seconds: Optional[float] = None,
    parent_agent: Any = None,
) -> str:
    """Run a bounded coding task in a Codex app-server worker."""
    if parent_agent is None:
        return tool_error("delegate_codex_coding_task requires a parent agent context.")

    if getattr(parent_agent, "api_mode", "") == "codex_app_server":
        return tool_error(
            "delegate_codex_coding_task is unavailable while the parent agent "
            "is already running on codex_app_server."
        )

    task_text = str(task or "").strip()
    if not task_text:
        return tool_error("delegate_codex_coding_task requires a non-empty task.")

    workdir = _resolve_cwd(cwd, parent_agent)
    if not Path(workdir).exists():
        return tool_error(f"cwd does not exist: {workdir}")

    timeout = (
        float(turn_timeout_seconds)
        if turn_timeout_seconds is not None
        else _load_codex_worker_timeout()
    )
    timeout = max(30.0, timeout)

    worker_prompt_parts = [
        "You are a Codex coding worker launched by Hermes.",
        "Work in the requested repository, make direct file edits when needed, "
        "and run focused checks that fit the task.",
        "Do not create commits or pull requests.",
        "Final response must summarize changed files, checks run, and any "
        "remaining blockers.",
        "",
        "Task:",
        task_text,
    ]
    if context and str(context).strip():
        worker_prompt_parts.extend(["", "Context from Hermes:", str(context).strip()])
    worker_prompt = "\n".join(worker_prompt_parts)

    try:
        from agent.transports.codex_app_server_session import CodexAppServerSession
    except Exception as exc:
        return tool_error(f"could not import Codex app-server session: {exc}")

    try:
        from tools.terminal_tool import _get_approval_callback

        approval_callback = _get_approval_callback()
    except Exception:
        approval_callback = None

    def _touch_codex_activity(note: dict) -> None:
        try:
            method = note.get("method", "")
            item = ((note.get("params") or {}).get("item") or {})
            item_type = item.get("type") or ""
            suffix = f": {item_type}" if item_type else ""
            parent_agent._touch_activity(f"Codex worker event: {method}{suffix}")
        except Exception:
            pass

    started = time.monotonic()
    with CodexAppServerSession(
        cwd=workdir,
        approval_callback=approval_callback,
        on_event=_touch_codex_activity,
    ) as session:
        turn = session.run_turn(
            user_input=worker_prompt,
            turn_timeout=timeout,
        )

    duration = round(time.monotonic() - started, 2)
    success = bool(turn.final_text) and not turn.error and not turn.interrupted
    return json.dumps(
        {
            "success": success,
            "status": "completed" if success else "partial",
            "summary": turn.final_text,
            "error": turn.error,
            "interrupted": turn.interrupted,
            "duration_seconds": duration,
            "cwd": workdir,
            "thread_id": turn.thread_id,
            "turn_id": turn.turn_id,
            "tool_iterations": turn.tool_iterations,
            "projected_message_count": len(turn.projected_messages),
        },
        ensure_ascii=False,
    )


CODEX_WORKER_SCHEMA = {
    "name": "delegate_codex_coding_task",
    "description": (
        "Delegate a bounded implementation, debugging, test-fixing, refactor, "
        "or code-review task to a Codex app-server coding worker. Use from "
        "Hermes' normal runtime when Codex should do the coding-heavy step; "
        "Hermes remains responsible for reviewing the worker result and "
        "reporting final status to the user."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "Concrete coding task for the Codex worker.",
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
                    "codex_worker.turn_timeout_seconds, minimum 30 seconds."
                ),
            },
        },
        "required": ["task"],
    },
}


registry.register(
    name="delegate_codex_coding_task",
    toolset="delegation",
    schema=CODEX_WORKER_SCHEMA,
    handler=lambda args, **kw: delegate_codex_coding_task(
        task=args.get("task"),
        context=args.get("context"),
        cwd=args.get("cwd"),
        turn_timeout_seconds=args.get("turn_timeout_seconds"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_codex_worker_requirements,
    emoji="codex",
)
