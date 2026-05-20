"""Coding worker tool for delegated implementation work.

The execution backend is selected by ``coding_worker.backend``.
"""

from __future__ import annotations

import json
import os
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
        "Final response must summarize changed files, checks run, and any "
        "remaining blockers.",
        "",
        "Task:",
        task_text,
    ]
    if context and str(context).strip():
        worker_prompt_parts.extend(["", "Context from Hermes:", str(context).strip()])
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
    inherited_credential_id = None
    try:
        from agent.codex_worker_auth import prepare_codex_worker_home
        from hermes_constants import get_hermes_home

        codex_home = (
            get_hermes_home()
            / "codex-worker-homes"
            / f"delegate-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        inherited_credential_id = prepare_codex_worker_home(
            codex_home,
            parent_agent=parent_agent,
        )
    except Exception:
        codex_home = None
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
    needs_plan = looks_complex_or_risky(worker_prompt, classification_context)
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
                    "coding_worker.turn_timeout_seconds, minimum 30 seconds."
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
