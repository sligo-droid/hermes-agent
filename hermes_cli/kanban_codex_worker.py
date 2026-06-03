"""Run one Kanban planner/dev/reviewer task through a coding worker."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from agent.transports.codex_app_server_session import CodexAppServerSession
from hermes_cli import kanban_db
from hermes_cli.discord_worker_boards import (
    DEV_TICKET_BODY_GUIDANCE,
    ROLE_DEV,
    ROLE_PLANNER,
    ROLE_REVIEWER,
    active_dev_round_for_board,
    format_role_round_title,
    is_cancelled,
    mark_dispatch_dirty,
    record_codex_worker_event,
    record_codex_worker_result,
)

_OPENCODE_ROLES = {ROLE_PLANNER, ROLE_DEV}
_CODEX_AUTH_RETRY_LIMIT = 2


def main() -> int:
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    board = os.environ.get("HERMES_KANBAN_BOARD", "").strip() or None
    role = os.environ.get("HERMES_CODEX_WORKER_ROLE", "").strip().lower()
    workspace = os.environ.get("HERMES_KANBAN_WORKSPACE", "").strip() or os.getcwd()
    if not task_id or role not in {ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER}:
        return 2

    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            return 2
        prompt = _build_prompt(conn, task_id, role)
        result = _run_role_backend(
            prompt,
            workspace,
            role,
            task=task,
            task_id=task_id,
            board=board,
        )
        if result.error:
            raise RuntimeError(result.error)
        payload = _parse_json(result.final_text)
        if board and is_cancelled(board):
            return 0
        _apply_role_output(
            conn,
            task_id,
            role,
            payload,
            board=board,
            workspace=workspace,
            expected_run_id=task.current_run_id,
        )
        return 0
    except Exception as exc:
        try:
            kanban_db.block_task(conn, task_id, reason=f"{_backend_label(role)} worker failed: {exc}")
        except Exception:
            pass
        return 1
    finally:
        conn.close()
        if board:
            try:
                mark_dispatch_dirty(board=board, reason=f"{role}-worker-finished")
            except Exception:
                pass


def _build_prompt(conn: Any, task_id: str, role: str) -> str:
    context = kanban_db.build_worker_context(conn, task_id)
    outcome = _role_outcome_frame(role)
    schema = _schema_instructions(role)
    git = _git_summary(os.environ.get("HERMES_KANBAN_WORKSPACE", "") or os.getcwd())
    discord_access = (
        "Discord and board control access:\n"
        "- You may inspect Discord message context with "
        "`python -m hermes_cli.discord_worker_read fetch-message --channel-id <id> --message-id <id>`.\n"
        "- You may inspect recent thread/channel history with "
        "`python -m hermes_cli.discord_worker_read fetch-messages --channel-id <id> --limit 25`.\n"
        "- You may call Discord REST actions with "
        "`python -m hermes_cli.discord_worker_read discord-request --method PATCH --path /channels/<channel_id>/messages/<message_id> --body-json '{\"content\":\"...\"}'`.\n"
        "- You may update any Hermes/Discord worker board metadata with "
        "`python -m hermes_cli.discord_worker_read update-board --board <slug> --goal-status done --phase complete --sync-summary --sync-reaction`.\n"
        "- You may move tickets on any accessible board with "
        "`python -m hermes_cli.discord_worker_read task-status --board <slug> --task-id <id> --status ready`.\n"
        "- You may patch the Discord feature summary from board state with "
        "`python -m hermes_cli.discord_worker_read sync-summary --board <slug>`.\n\n"
    )
    return (
        f"You are the Discord Kanban {role} worker.\n"
        "Use the repository, shell, files, and worker helper commands available in this worker environment to complete the task; mutate Hermes/Discord state when that is the correct outcome.\n"
        "Return exactly one raw JSON object matching the schema below, with no Markdown fence or surrounding prose.\n\n"
        f"{outcome}\n\n"
        f"{discord_access}"
        f"{schema}\n\n"
        f"Git context:\n{git}\n\n"
        f"Kanban context:\n{context}"
    )


def _role_outcome_frame(role: str) -> str:
    if role == ROLE_PLANNER:
        return (
            "Outcome frame:\n"
            "Goal: Convert the Kanban context into the smallest coherent implementation plan for dev workers.\n"
            "Success means:\n"
            "- The JSON status is planned or blocked.\n"
            "- The board-level acceptance_criteria list is deduplicated and canonical.\n"
            "- Each dev ticket body opens with Goal, Success means, and Stop when, then gives the worker scope, files, dependencies, verification, and out-of-scope boundaries.\n"
            "Stop when: Return the JSON plan or a concise blocker."
        )
    if role == ROLE_REVIEWER:
        return (
            "Outcome frame:\n"
            "Goal: Decide whether the board work satisfies the user goal and acceptance criteria.\n"
            "Success means:\n"
            "- The JSON status is approved, changes_requested, or blocked.\n"
            "- Findings name concrete issues, or findings is empty when the work is approved.\n"
            "- New dev tasks are outcome-first follow-up briefs when changes are required.\n"
            "- criteria_assessment maps each criterion to evidence or a gap.\n"
            "Stop when: Return the JSON review verdict."
        )
    return (
        "Outcome frame:\n"
        "Goal: Complete the assigned Kanban ticket or produce a checkpoint/blocker with evidence.\n"
        "Success means:\n"
        "- The smallest correct change within ticket scope is implemented.\n"
        "- Focused verification is run when available and recorded in tests.\n"
        "- changed_files, tests, pr_ready, and blocker reflect the actual repository state.\n"
        "Stop when: Return the JSON completion, checkpoint, or blocker object."
    )


def _schema_instructions(role: str) -> str:
    if role == ROLE_PLANNER:
        return (
            'Schema: {"status":"planned|blocked","summary":"...","acceptance_criteria":["..."],'
            '"requirements":[{"id":"REQ-1","text":"...","source_message_ids":[],"owner_task_indices":[0],"required":true}],'
            '"tasks":[{"title":"...","body":"...","priority":0,"parents":[],"requirement_ids":["REQ-1"]}],"blocker":null} '
            'In each task, "parents" is a list of earlier task indices this task depends on. '
            "Use requirements for distinct obligations from the request or Discord context; give each a stable ID and set task requirement_ids for the dev ticket that owns it. "
            "Break the job into the fewest coherent dev tickets that can be implemented and verified independently. "
            "Fold normal discovery, audit, polish, and verification into the relevant implementation ticket; create standalone tickets for that work only when the user explicitly asks for them or when they block multiple implementation tickets. "
            f"{DEV_TICKET_BODY_GUIDANCE} "
            "Write Success means as ticket-specific acceptance criteria for the slice owned by that dev ticket; include board-level criteria only when that ticket owns the whole outcome. "
            "Set Stop when to the concrete handoff point for that ticket, usually code changed and verification recorded or a blocker stated. "
            "Include enough surrounding context from the overall request for a fresh dev worker to execute the ticket without guessing, but keep the scope tight to the ticket. "
            "Do not paste the full Discord thread context into dev tickets; use requirement IDs, context_pack paths, and concise relevant notes instead. "
            "The top-level acceptance_criteria must be one deduplicated canonical board-level list; if criteria already exist in the Kanban context, reuse them instead of paraphrasing or adding near-duplicates. "
            "Treat slash-looking text in the request as user prose unless the Kanban context explicitly says otherwise."
        )
    if role == ROLE_REVIEWER:
        return (
            'Schema: {"status":"approved|changes_requested|blocked","summary":"...","findings":["..."],'
            '"new_tasks":[{"title":"...","body":"...","priority":0}],"criteria_assessment":{}, "blocker":null} '
            "Assess any requirements included in the Kanban context for coverage gaps. "
            "When requesting changes, each new_tasks body must be a self-contained follow-up brief that opens with Goal, Success means, and Stop when."
        )
    return (
        'Schema: {"status":"completed|blocked|checkpoint","summary":"...","changed_files":["..."],'
        '"tests":[{"command":"...","result":"passed|failed|not_run","output":"..."}],"blocker":null,"pr_ready":false}'
    )


def _configured_backend() -> str:
    try:
        from agent.opencode_worker import load_coding_worker_backend

        return load_coding_worker_backend()
    except Exception:
        return "codex"


def _backend_label(role: str) -> str:
    if _role_uses_opencode(role):
        return "OpenCode"
    return "Codex"


def _role_uses_opencode(role: str) -> bool:
    return role in _OPENCODE_ROLES and _configured_backend() == "opencode"


def _run_role_backend(
    prompt: str,
    workspace: str,
    role: str,
    *,
    task: Any,
    task_id: str,
    board: Optional[str],
):
    if _role_uses_opencode(role):
        return _run_opencode(prompt, workspace, role, task=task, task_id=task_id, board=board)
    return _run_codex(prompt, workspace, role, task_id=task_id, board=board)


def _run_codex(
    prompt: str,
    workspace: str,
    role: str,
    *,
    task_id: str,
    board: Optional[str],
):
    extra_args = _role_extra_args(role)

    def on_event(note: dict) -> None:
        try:
            record_codex_worker_event(task_id, board=board, event=note)
        except Exception:
            pass

    try:
        attempt = 0
        while True:
            session = CodexAppServerSession(
                cwd=workspace,
                codex_home=os.environ.get("CODEX_HOME"),
                extra_args=extra_args,
                env={
                    "HERMES_DISABLE_MCP": "1",
                    "HERMES_CODEX_WORKER_NETWORK_ACCESS": "1",
                },
                on_event=on_event,
            )
            try:
                result = session.run_turn(prompt, turn_timeout=_role_timeout(role))
                _attach_scheduled_runtime(result, role)
                try:
                    record_codex_worker_result(task_id, board=board, result=result)
                except Exception:
                    pass
            finally:
                session.close()
                try:
                    from agent.codex_worker_auth import sync_codex_worker_home

                    sync_codex_worker_home(
                        os.environ.get("CODEX_HOME"),
                        os.environ.get("HERMES_CODEX_WORKER_CREDENTIAL_ID"),
                    )
                except Exception:
                    pass

            if not _codex_result_auth_failed(result) or attempt >= _CODEX_AUTH_RETRY_LIMIT:
                return result
            if not _rotate_codex_worker_credential_after_auth_failure(result):
                return result
            attempt += 1
    finally:
        if os.environ.get("HERMES_CODEX_WORKER_CLEANUP_HOME") == "1":
            try:
                from agent.codex_worker_auth import cleanup_codex_worker_home

                cleanup_codex_worker_home(os.environ.get("CODEX_HOME"))
            except Exception:
                pass


def _codex_result_auth_failed(result: Any) -> bool:
    if bool(getattr(result, "auth_failed", False)):
        return True
    error = str(getattr(result, "error", "") or "").lower()
    return "codex authentication failed" in error


def _rotate_codex_worker_credential_after_auth_failure(result: Any) -> bool:
    failed_credential_id = os.environ.get("HERMES_CODEX_WORKER_CREDENTIAL_ID", "").strip()
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    if not failed_credential_id or not codex_home:
        return False

    try:
        from agent.codex_worker_auth import (
            mark_codex_worker_credential_auth_failed,
            prepare_codex_worker_home,
        )
    except Exception:
        return False

    if not mark_codex_worker_credential_auth_failed(
        failed_credential_id,
        message=str(getattr(result, "error", "") or "Codex worker auth failure."),
    ):
        return False

    source_env = os.environ.copy()
    source_env.pop("CODEX_HOME", None)
    source_env.pop("HERMES_CODEX_WORKER_CREDENTIAL_ID", None)
    next_codex_home = codex_home
    use_shared_home_symlink = True
    if os.environ.get("HERMES_CODEX_WORKER_CONTAINER_CODEX_HOME") == "1":
        next_codex_home = str(Path(codex_home) / ".rotated-credential-home")
        use_shared_home_symlink = False
    next_credential_id = prepare_codex_worker_home(
        next_codex_home,
        source_env=source_env,
        allow_fallback=False,
        use_shared_home_symlink=use_shared_home_symlink,
    )
    if not next_credential_id or next_credential_id == failed_credential_id:
        return False
    os.environ["CODEX_HOME"] = next_codex_home
    os.environ["HERMES_CODEX_WORKER_CREDENTIAL_ID"] = next_credential_id
    return True


def _run_opencode(
    prompt: str,
    workspace: str,
    role: str,
    *,
    task: Any,
    task_id: str,
    board: Optional[str],
):
    def on_event(note: dict) -> None:
        try:
            record_codex_worker_event(
                task_id,
                board=board,
                event={
                    "method": f"opencode/{note.get('type') or 'event'}",
                    "params": {"item": note},
                },
            )
        except Exception:
            pass

    from agent.opencode_worker import (
        load_opencode_config,
        run_opencode_single_pass,
        run_opencode_task,
    )

    context = "\n".join(
        str(part or "")
        for part in (
            getattr(task, "title", ""),
            getattr(task, "body", ""),
            prompt,
        )
    )
    if role == ROLE_PLANNER:
        cfg = load_opencode_config()
        reasoning_level = _scheduled_opencode_reasoning(
            cfg["complex_plan_reasoning_level"]
        )
        result = run_opencode_single_pass(
            prompt,
            workspace,
            timeout=_role_timeout(role),
            agent=cfg["plan_agent"],
            reasoning_level=reasoning_level,
            title=f"kanban {task_id}",
            on_event=on_event,
        )
    else:
        result = run_opencode_task(
            prompt,
            workspace,
            timeout=_role_timeout(role),
            context_for_classification=context,
            force_plan=False,
            title=f"kanban {task_id}",
            worker_config=_scheduled_opencode_worker_config(),
            on_event=on_event,
        )
    _attach_scheduled_runtime(result, role)
    try:
        record_codex_worker_result(task_id, board=board, result=result)
    except Exception:
        pass
    return result


def _scheduled_opencode_reasoning(default: str) -> str:
    effort = str(os.environ.get("HERMES_CODEX_WORKER_REASONING") or "").strip().lower()
    if effort in {"minimal", "low", "medium", "high", "xhigh", "max"}:
        return effort
    return default


def _scheduled_opencode_worker_config() -> Optional[dict[str, str]]:
    if os.environ.get("HERMES_CODEX_WORKER_REASONING_SOURCE") != "adaptive":
        return None
    if _raw_opencode_pass_configured():
        return None
    effort = _scheduled_opencode_reasoning("")
    if not effort:
        return None
    return {
        "simple_build_reasoning_level": effort,
        "complex_build_reasoning_level": effort,
    }


def _raw_opencode_pass_configured() -> bool:
    try:
        from hermes_cli.config import read_raw_config

        raw = read_raw_config() or {}
    except Exception:
        return False
    coding = raw.get("coding_worker") if isinstance(raw, dict) else None
    if not isinstance(coding, dict):
        return False
    return any(
        key in coding
        for key in (
            "simple_build_reasoning_level",
            "complex_plan_reasoning_level",
            "complex_build_reasoning_level",
        )
    )


def _role_extra_args(role: str) -> list[str]:
    effort = _worker_reasoning_effort(role)
    service_tier = _worker_service_tier()
    # Codex accepts arbitrary config overrides through -c. Unknown keys are
    # ignored by older versions, so this remains forward-compatible.
    return [
        "-c", f'model_reasoning_effort="{effort}"',
        "-c", f'service_tier="{service_tier}"',
    ]


def _worker_reasoning_effort(role: str) -> str:
    effort = str(os.environ.get("HERMES_CODEX_WORKER_REASONING") or "").strip().lower()
    if effort in {"minimal", "low", "medium", "high", "xhigh"}:
        return effort
    if role in {ROLE_PLANNER, ROLE_REVIEWER}:
        return "xhigh"
    return "medium"


def _worker_service_tier() -> str:
    service_tier = str(os.environ.get("HERMES_CODEX_WORKER_SERVICE_TIER") or "normal").strip().lower()
    return service_tier if service_tier in {"fast", "normal"} else "normal"


def _attach_scheduled_runtime(result: Any, role: str) -> None:
    service_tier = _worker_service_tier()
    try:
        setattr(result, "service_tier", service_tier)
        setattr(result, "fast_mode", service_tier == "fast")
        if not getattr(result, "run_profile", None):
            name = "build"
            if role == ROLE_PLANNER:
                name = "plan"
            elif role == ROLE_REVIEWER:
                name = "review"
            setattr(
                result,
                "run_profile",
                {
                    "kind": f"one_pass_{name}",
                    "label": f"1-pass {name}",
                    "pass_count": 1,
                    "plan_used": False,
                    "passes": [
                        {
                            "name": name,
                            "agent": role,
                            "reasoning": _worker_reasoning_effort(role),
                        }
                    ],
                },
            )
    except Exception:
        pass


def _role_timeout(role: str) -> float:
    if role == ROLE_DEV:
        return float(os.environ.get("HERMES_CODEX_DEV_TIMEOUT", "3600"))
    return float(os.environ.get("HERMES_CODEX_PLANNER_REVIEWER_TIMEOUT", "1800"))


def _parse_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise ValueError("worker did not return JSON")
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("worker JSON must be an object")
    return data


def _apply_role_output(
    conn: Any,
    task_id: str,
    role: str,
    payload: dict[str, Any],
    *,
    board: Optional[str],
    workspace: str,
    expected_run_id: Optional[int],
) -> None:
    status = str(payload.get("status") or "").strip().lower()
    summary = str(payload.get("summary") or "").strip()
    if board and is_cancelled(board):
        return
    if role == ROLE_PLANNER:
        if status == "blocked":
            kanban_db.block_task(
                conn,
                task_id,
                reason=payload.get("blocker") or summary,
                expected_run_id=expected_run_id,
            )
            return
        criteria = _string_list(payload.get("acceptance_criteria"))
        tasks = payload.get("tasks") if isinstance(payload.get("tasks"), list) else []
        dev_round = active_dev_round_for_board(board)
        requirements = _normalize_requirements(payload.get("requirements"))
        specs = _planner_dev_task_specs(tasks, dev_round=dev_round, workspace=workspace, board=board)
        _add_context_headers(specs, board=board)
        created: list[str] = []
        try:
            created = _create_planned_dev_tasks(conn, specs, created_by=ROLE_PLANNER)
            _merge_criteria(board, criteria)
            completed = kanban_db.complete_task(
                conn,
                task_id,
                summary=summary or f"Planned {len(created)} task(s).",
                metadata={
                    "created_tasks": created,
                    "acceptance_criteria": criteria,
                    "requirements": requirements,
                    "raw": payload,
                },
                created_cards=created,
                expected_run_id=expected_run_id,
            )
            if not completed:
                _cleanup_created_tasks(conn, created)
            else:
                _persist_requirements(board, requirements, specs, created)
        except Exception:
            _cleanup_created_tasks(conn, created)
            raise
        return

    if role == ROLE_REVIEWER:
        if status == "approved":
            metadata = {"raw": payload}
            criteria_assessment = payload.get("criteria_assessment")
            if isinstance(criteria_assessment, dict):
                metadata["criteria_assessment"] = criteria_assessment
            completed = kanban_db.complete_task(
                conn,
                task_id,
                summary=summary or "Reviewer approved.",
                metadata=metadata,
                expected_run_id=expected_run_id,
            )
            if not completed:
                return
            if _ensure_pr(board, workspace):
                _update_phase(board, "complete", goal_status="done")
            else:
                _update_phase(board, "blocked", goal_status="blocked")
            return
        if status == "blocked":
            blocked = kanban_db.block_task(
                conn,
                task_id,
                reason=payload.get("blocker") or summary,
                expected_run_id=expected_run_id,
            )
            if not blocked:
                return
            _update_phase(board, "blocked", goal_status="blocked")
            return
        new_tasks = payload.get("new_tasks") if isinstance(payload.get("new_tasks"), list) else []
        dev_round = active_dev_round_for_board(board)
        specs = _reviewer_dev_task_specs(new_tasks, dev_round=dev_round, workspace=workspace, board=board)
        _add_context_headers(specs, board=board)
        created: list[str] = []
        try:
            created = _create_planned_dev_tasks(conn, specs, created_by=ROLE_REVIEWER)
            completed = kanban_db.complete_task(
                conn,
                task_id,
                summary=summary or "Reviewer requested changes.",
                metadata={"created_tasks": created, "raw": payload},
                created_cards=created,
                expected_run_id=expected_run_id,
            )
            if not completed:
                _cleanup_created_tasks(conn, created)
                return
        except Exception:
            _cleanup_created_tasks(conn, created)
            raise
        _update_phase(board, "dev", goal_status="active")
        return

    if status == "blocked":
        blocked = kanban_db.block_task(
            conn,
            task_id,
            reason=payload.get("blocker") or summary,
            expected_run_id=expected_run_id,
        )
        if not blocked:
            return
        _update_phase(board, "blocked", goal_status="blocked")
        return
    _checkpoint_commit(workspace, task_id, summary)
    kanban_db.complete_task(
        conn,
        task_id,
        summary=summary or "Dev task completed.",
        metadata={
            "changed_files": _string_list(payload.get("changed_files")),
            "tests": payload.get("tests") if isinstance(payload.get("tests"), list) else [],
            "pr_ready": bool(payload.get("pr_ready")),
            "raw": payload,
        },
        expected_run_id=expected_run_id,
    )


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _priority(value: Any, default: int) -> int:
    return int(value if value not in (None, "") else default)


def _planner_dev_task_specs(
    tasks: list[Any],
    *,
    dev_round: int,
    workspace: str,
    board: Optional[str],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for raw_idx, spec in enumerate(tasks):
        if not isinstance(spec, dict):
            continue
        title = str(spec.get("title") or "").strip()
        if not title:
            continue
        specs.append(
            {
                "raw_index": raw_idx,
                "title": format_role_round_title(title, dev_round),
                "body": str(spec.get("body") or ""),
                "workspace": workspace,
                "tenant": board,
                "priority": _priority(spec.get("priority"), 50 - (raw_idx + 1)),
                "parent_indices": _parent_indices(spec, len(tasks), raw_idx),
                "requirement_ids": _string_list(spec.get("requirement_ids")),
            }
        )
    return specs


def _reviewer_dev_task_specs(
    tasks: list[Any],
    *,
    dev_round: int,
    workspace: str,
    board: Optional[str],
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for raw_idx, spec in enumerate(tasks):
        if not isinstance(spec, dict):
            continue
        title = str(spec.get("title") or "").strip()
        if not title:
            continue
        specs.append(
            {
                "raw_index": raw_idx,
                "title": format_role_round_title(title, dev_round),
                "body": str(spec.get("body") or ""),
                "workspace": workspace,
                "tenant": board,
                "priority": _priority(spec.get("priority"), 90 - (raw_idx + 1)),
                "parent_indices": [],
                "requirement_ids": _string_list(spec.get("requirement_ids")),
            }
        )
    return specs


def _add_context_headers(specs: list[dict[str, Any]], *, board: Optional[str]) -> None:
    if not board:
        return
    metadata = kanban_db.read_board_metadata(board)
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY

    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    context_path = str(worker.get("context_pack_markdown_path") or worker.get("context_pack_path") or "").strip()
    if not context_path:
        return
    for spec in specs:
        requirement_ids = _string_list(spec.get("requirement_ids"))
        lines = ["Context pack:", f"- {context_path}"]
        if worker.get("context_pack_path") and str(worker.get("context_pack_path")) != context_path:
            lines.append(f"- JSON: {worker.get('context_pack_path')}")
        if requirement_ids:
            lines.append("Requirement IDs: " + ", ".join(requirement_ids))
        header = "\n".join(lines).strip()
        body = str(spec.get("body") or "").lstrip()
        spec["body"] = f"{header}\n\n{body}" if body else header


def _normalize_requirements(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    requirements: list[dict[str, Any]] = []
    seen: set[str] = set()
    for idx, raw in enumerate(value, start=1):
        if not isinstance(raw, dict):
            continue
        req_id = re.sub(r"[^0-9A-Za-z_.:-]+", "-", str(raw.get("id") or f"REQ-{idx}").strip())[:80]
        text = str(raw.get("text") or "").strip()
        if not req_id or not text or req_id in seen:
            continue
        seen.add(req_id)
        owner_indices: list[int] = []
        for item in raw.get("owner_task_indices") if isinstance(raw.get("owner_task_indices"), list) else []:
            try:
                owner_indices.append(int(item))
            except (TypeError, ValueError):
                continue
        requirements.append(
            {
                "id": req_id,
                "text": text,
                "source_message_ids": _string_list(raw.get("source_message_ids")),
                "owner_task_indices": owner_indices,
                "owner_task_ids": [],
                "required": bool(raw.get("required", True)),
            }
        )
    return requirements


def _persist_requirements(
    board: Optional[str],
    requirements: list[dict[str, Any]],
    specs: list[dict[str, Any]],
    created: list[str],
) -> None:
    if not board:
        return
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY
    from utils import atomic_json_write

    raw_to_task = {
        int(spec["raw_index"]): created[idx]
        for idx, spec in enumerate(specs)
        if idx < len(created)
    }
    for req in requirements:
        owner_ids = [raw_to_task[idx] for idx in req.get("owner_task_indices") or [] if idx in raw_to_task]
        if not owner_ids:
            owner_ids = [
                raw_to_task[int(spec["raw_index"])]
                for spec in specs
                if req["id"] in _string_list(spec.get("requirement_ids"))
                and int(spec["raw_index"]) in raw_to_task
            ]
        req["owner_task_ids"] = owner_ids
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    # Planner output is authoritative for the current planning pass. Clear stale
    # requirements when an older/auxiliary planner omits the optional field.
    worker["requirements"] = requirements
    metadata[DISCORD_WORKER_META_KEY] = worker
    metadata.pop("db_path", None)
    atomic_json_write(kanban_db.board_metadata_path(board), metadata, indent=2)


def _create_planned_dev_tasks(
    conn: Any,
    specs: list[dict[str, Any]],
    *,
    created_by: str,
) -> list[str]:
    created: list[str] = []
    created_by_raw_index: dict[int, str] = {}
    for spec in specs:
        parent_ids = [
            created_by_raw_index[parent_idx]
            for parent_idx in spec["parent_indices"]
            if parent_idx in created_by_raw_index
        ]
        task_id = kanban_db.create_task(
            conn,
            title=spec["title"],
            body=spec["body"],
            assignee=ROLE_DEV,
            parents=parent_ids,
            created_by=created_by,
            workspace_kind="dir",
            workspace_path=spec["workspace"],
            tenant=spec["tenant"],
            priority=spec["priority"],
            max_runtime_seconds=3600,
        )
        created.append(task_id)
        created_by_raw_index[spec["raw_index"]] = task_id
    return created


def _cleanup_created_tasks(conn: Any, task_ids: list[str]) -> None:
    for task_id in reversed(task_ids):
        try:
            kanban_db.delete_task(conn, task_id)
        except Exception:
            pass


def _parent_indices(spec: dict[str, Any], task_count: int, current_idx: int) -> list[int]:
    raw = spec.get("parents")
    if raw is None and "depends_on" in spec:
        raw = spec.get("depends_on")
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    seen: set[int] = set()
    for item in raw:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= task_count or idx >= current_idx or idx in seen:
            continue
        seen.add(idx)
        out.append(idx)
    return out


def _git_summary(workspace: str) -> str:
    root = Path(workspace)
    try:
        status = subprocess.run(["git", "status", "--short"], cwd=root, capture_output=True, text=True, timeout=10)
        branch = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        current = (branch.stdout or "").strip() or "unknown"
        short_status = (status.stdout or "").strip() or "(clean)"
        return f"branch: {current}\nstatus:\n{short_status}"
    except Exception as exc:
        return f"git summary unavailable: {exc}"


def _checkpoint_commit(workspace: str, task_id: str, summary: str) -> None:
    try:
        root = Path(workspace)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=10)
        if not (status.stdout or "").strip():
            return
        subprocess.run(["git", "add", "-A"], cwd=root, timeout=60)
        message = f"checkpoint {task_id}: {(summary or 'worker progress')[:80]}"
        subprocess.run(["git", "commit", "-m", message], cwd=root, timeout=120)
    except Exception:
        return


def _github_repo_from_url(url: str) -> Optional[str]:
    raw = str(url or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^git\+", "", raw)
    patterns = (
        r"^https?://github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?(?:[#?].*)?$",
        r"^ssh://git@github\.com/([^/]+)/([^/#?]+?)(?:\.git)?/?(?:[#?].*)?$",
        r"^git@github\.com:([^/]+)/([^/#?]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, raw)
        if match:
            owner, repo = match.groups()
            return f"{owner}/{repo}"
    return None


def _resolve_github_repo(worker: dict[str, Any], root: Path) -> Optional[str]:
    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        remote = None
    if remote is not None and remote.returncode == 0:
        repo = _github_repo_from_url(remote.stdout)
        if repo:
            return repo

    context = worker.get("project_context") if isinstance(worker.get("project_context"), dict) else {}
    for source in (context, worker):
        for key in ("github_url", "project_github_url", "repo_url", "repository_url"):
            repo = _github_repo_from_url(str(source.get(key) or ""))
            if repo:
                return repo
    return None


def _pr_number_from_url(url: str) -> str:
    match = re.search(r"/pull/(\d+)(?:\D.*)?$", str(url or "").strip())
    return match.group(1) if match else ""


def _reset_pr_status_fields(worker: dict[str, Any]) -> None:
    for key in (
        "pr_error",
        "pr_status_error",
        "pr_state",
        "pr_merge_state",
        "pr_mergeable",
        "pr_is_draft",
        "pr_review_decision",
        "pr_merged_at",
        "pr_merge_commit",
        "pr_checks_status",
        "pr_checks_total",
        "pr_checks_failed",
        "pr_blocker",
        "pr_skipped_no_changes",
    ):
        worker.pop(key, None)


def _is_foreman_generated_worker(worker: dict[str, Any]) -> bool:
    request = str(
        worker.get("root_goal") or worker.get("initial_request") or ""
    ).lstrip()
    return request.startswith("Foreman escalation:") or request.startswith(
        "/goal Foreman escalation:"
    )


def _branch_has_commits(root: Path, *, base: str, branch: str) -> Optional[bool]:
    if not base or not branch:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{base}..{branch}"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    try:
        return int((result.stdout or "").strip() or "0") > 0
    except ValueError:
        return None


def _check_rollup_summary(items: Any) -> tuple[str, int, list[str]]:
    if not isinstance(items, list) or not items:
        return "not checked", 0, []
    failed: list[str] = []
    pending = 0
    total = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        total += 1
        name = str(
            item.get("name")
            or item.get("context")
            or item.get("workflowName")
            or item.get("__typename")
            or "check"
        )
        status = str(item.get("status") or item.get("state") or "").strip().upper()
        conclusion = str(item.get("conclusion") or item.get("conclusionState") or "").strip().upper()
        if conclusion in {"FAILURE", "FAILED", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"} or status in {"FAILURE", "FAILED"}:
            failed.append(name)
        elif conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"} or status in {"COMPLETED", "SUCCESS"}:
            continue
        else:
            pending += 1
    if failed:
        return "failed", total, failed[:8]
    if pending:
        return "pending", total, []
    return "passed", total, []


def _pr_merge_wait_seconds() -> float:
    raw = os.environ.get("HERMES_KANBAN_PR_MERGE_WAIT_SECONDS", "300")
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 300.0


def _pr_merge_poll_seconds() -> float:
    raw = os.environ.get("HERMES_KANBAN_PR_MERGE_POLL_SECONDS", "10")
    try:
        return max(0.5, float(raw))
    except (TypeError, ValueError):
        return 10.0


def _pr_ref(worker: dict[str, Any]) -> str:
    return str(worker.get("pr_url") or worker.get("pr_number") or "").strip()


def _pr_is_merged(worker: dict[str, Any]) -> bool:
    state = str(worker.get("pr_state") or "").strip().upper()
    return state == "MERGED" or bool(worker.get("pr_merged_at"))


def _pr_blocker(worker: dict[str, Any]) -> str:
    if worker.get("pr_error"):
        return str(worker.get("pr_error") or "")
    if worker.get("pr_status_error"):
        return str(worker.get("pr_status_error") or "")
    if not worker.get("pr_url"):
        return "PR not opened"
    state = str(worker.get("pr_state") or "").strip().upper()
    if state == "MERGED":
        return ""
    if state and state not in {"OPEN", "UNKNOWN"}:
        return f"PR state: {state}"
    if worker.get("pr_is_draft") is True:
        return "PR is draft"
    review_decision = str(worker.get("pr_review_decision") or "").strip().upper()
    if review_decision == "CHANGES_REQUESTED":
        return "review changes requested"
    checks_status = str(worker.get("pr_checks_status") or "not checked")
    if checks_status == "failed":
        failed = ", ".join(str(item) for item in (worker.get("pr_checks_failed") or []) if item)
        return f"checks failed: {failed}" if failed else "checks failed"
    if checks_status == "pending":
        return "checks pending"
    if checks_status == "not checked":
        return "checks not checked"
    merge_state = str(worker.get("pr_merge_state") or "unknown").strip().upper()
    if merge_state and merge_state not in {"CLEAN", "HAS_HOOKS", "UNKNOWN"}:
        return f"merge state: {merge_state}"
    return ""


def _refresh_pr_status(worker: dict[str, Any], *, root: Path, repo: str) -> None:
    pr_ref = _pr_ref(worker)
    if not pr_ref:
        worker.setdefault("pr_checks_status", "not checked")
        worker.setdefault("pr_merge_state", "unknown")
        worker["pr_blocker"] = _pr_blocker(worker)
        return
    if not worker.get("pr_number") and worker.get("pr_url"):
        number = _pr_number_from_url(str(worker.get("pr_url") or ""))
        if number:
            worker["pr_number"] = number
    viewed = subprocess.run(
        [
            "gh",
            "pr",
            "view",
            pr_ref,
            "--repo",
            repo,
            "--json",
            "number,url,state,mergedAt,mergeCommit,mergeStateStatus,mergeable,isDraft,reviewDecision,statusCheckRollup",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if viewed.returncode != 0:
        worker["pr_status_error"] = (viewed.stderr or viewed.stdout or "gh pr view failed").strip()
        worker.setdefault("pr_checks_status", "not checked")
        worker.setdefault("pr_merge_state", "unknown")
        worker["pr_blocker"] = _pr_blocker(worker)
        return
    try:
        data = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError as exc:
        worker["pr_status_error"] = f"gh pr view returned invalid JSON: {exc}"
        worker.setdefault("pr_checks_status", "not checked")
        worker.setdefault("pr_merge_state", "unknown")
        worker["pr_blocker"] = _pr_blocker(worker)
        return
    if not isinstance(data, dict):
        worker["pr_status_error"] = "gh pr view returned non-object JSON"
        worker.setdefault("pr_checks_status", "not checked")
        worker.setdefault("pr_merge_state", "unknown")
        worker["pr_blocker"] = _pr_blocker(worker)
        return
    if data.get("url"):
        worker["pr_url"] = str(data.get("url") or "")
    if data.get("number") is not None:
        worker["pr_number"] = str(data.get("number"))
    worker["pr_state"] = str(data.get("state") or "unknown")
    worker["pr_merged_at"] = str(data.get("mergedAt") or "")
    merge_commit = data.get("mergeCommit") if isinstance(data.get("mergeCommit"), dict) else {}
    worker["pr_merge_commit"] = str(merge_commit.get("oid") or "")
    worker["pr_merge_state"] = str(data.get("mergeStateStatus") or "unknown")
    worker["pr_mergeable"] = data.get("mergeable") if data.get("mergeable") is not None else "unknown"
    worker["pr_is_draft"] = bool(data.get("isDraft"))
    worker["pr_review_decision"] = str(data.get("reviewDecision") or "").strip() or "unknown"
    checks_status, checks_total, failed = _check_rollup_summary(data.get("statusCheckRollup"))
    worker["pr_checks_status"] = checks_status
    worker["pr_checks_total"] = checks_total
    worker["pr_checks_failed"] = failed
    worker["pr_blocker"] = _pr_blocker(worker)


def _ensure_pr_merged(worker: dict[str, Any], *, root: Path, repo: str) -> bool:
    pr_ref = _pr_ref(worker)
    if not pr_ref:
        worker.setdefault("pr_checks_status", "not checked")
        worker.setdefault("pr_merge_state", "unknown")
        worker["pr_blocker"] = _pr_blocker(worker)
        return False

    deadline = time.monotonic() + _pr_merge_wait_seconds()
    poll_seconds = _pr_merge_poll_seconds()
    while True:
        _refresh_pr_status(worker, root=root, repo=repo)
        if _pr_is_merged(worker):
            worker["pr_blocker"] = ""
            return True

        blocker = _pr_blocker(worker)
        if not blocker:
            merged = subprocess.run(
                ["gh", "pr", "merge", pr_ref, "--repo", repo, "--merge", "--delete-branch"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=300,
            )
            if merged.returncode != 0:
                worker["pr_status_error"] = (
                    merged.stderr or merged.stdout or "gh pr merge failed"
                ).strip()
                worker["pr_blocker"] = _pr_blocker(worker)
                return False
            _refresh_pr_status(worker, root=root, repo=repo)
            if _pr_is_merged(worker):
                worker["pr_blocker"] = ""
                return True
            worker["pr_blocker"] = _pr_blocker(worker) or "PR did not report merged after merge"
            return False

        if blocker not in {"checks pending", "checks not checked"}:
            worker["pr_blocker"] = blocker
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            worker["pr_blocker"] = blocker
            return False
        time.sleep(min(poll_seconds, remaining))


def _ensure_pr(board: Optional[str], workspace: str) -> bool:
    if not board:
        return True
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY
    from utils import atomic_json_write

    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    root = Path(workspace)
    branch = str(worker.get("worker_branch") or "").strip()
    base = str(worker.get("base_branch") or "main").strip() or "main"
    repo = _resolve_github_repo(worker, root)
    _reset_pr_status_fields(worker)
    try:
        missing = []
        if not repo:
            missing.append("GitHub repository")
        if not branch:
            missing.append("worker branch")
        if missing:
            worker["pr_error"] = f"Cannot create PR: missing {', '.join(missing)}"
            worker["pr_checks_status"] = "not checked"
            worker["pr_merge_state"] = "unknown"
            worker["pr_blocker"] = worker["pr_error"]
            raise RuntimeError(worker["pr_error"])
        has_commits = _branch_has_commits(root, base=base, branch=branch)
        if _is_foreman_generated_worker(worker) and has_commits is False:
            worker["pr_skipped_no_changes"] = True
            worker["pr_state"] = "not_needed"
            worker["pr_checks_status"] = "passed"
            worker["pr_checks_total"] = 0
            worker["pr_checks_failed"] = []
            worker["pr_merge_state"] = "clean"
            worker["pr_mergeable"] = True
            worker["pr_blocker"] = ""
        else:
            existing_url = ""
            if worker.get("pr_url"):
                existing_url = str(worker.get("pr_url") or "").strip()
            if not existing_url:
                existing = subprocess.run(
                    [
                        "gh",
                        "pr",
                        "list",
                        "--repo",
                        repo,
                        "--head",
                        branch,
                        "--base",
                        base,
                        "--state",
                        "open",
                        "--json",
                        "url",
                        "--jq",
                        ".[0].url",
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                existing_url = (existing.stdout or "").strip()
            if existing_url and existing_url != "null":
                worker["pr_url"] = existing_url
            else:
                pushed = subprocess.run(
                    ["git", "push", "-u", "origin", branch],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if pushed.returncode != 0:
                    worker["pr_error"] = (
                        pushed.stderr or pushed.stdout or "git push failed"
                    ).strip()
                    worker["pr_checks_status"] = "not checked"
                    worker["pr_merge_state"] = "unknown"
                    worker["pr_blocker"] = worker["pr_error"]
                    raise RuntimeError(worker["pr_error"])
                title_source = str(
                    worker.get("root_goal")
                    or worker.get("initial_request")
                    or "implementation"
                )
                title = f"Discord worker: {title_source[:80]}"
                body = (
                    f"Board: {worker.get('public_url') or board}\n\n"
                    f"Goal:\n{worker.get('root_goal') or worker.get('initial_request') or ''}"
                )
                created = subprocess.run(
                    [
                        "gh",
                        "pr",
                        "create",
                        "--repo",
                        repo,
                        "--base",
                        base,
                        "--head",
                        branch,
                        "--title",
                        title,
                        "--body",
                        body,
                    ],
                    cwd=root,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if created.returncode == 0 and (created.stdout or "").strip():
                    worker["pr_url"] = created.stdout.strip()
                else:
                    worker["pr_error"] = (
                        created.stderr or created.stdout or "gh pr create failed"
                    ).strip()
        if worker.get("pr_url"):
            _ensure_pr_merged(worker, root=root, repo=repo)
        elif not worker.get("pr_skipped_no_changes"):
            worker.setdefault("pr_checks_status", "not checked")
            worker.setdefault("pr_merge_state", "unknown")
            worker["pr_blocker"] = _pr_blocker(worker)
    except Exception as exc:
        worker.setdefault("pr_error", str(exc))
        worker.setdefault("pr_checks_status", "not checked")
        worker.setdefault("pr_merge_state", "unknown")
        worker["pr_blocker"] = _pr_blocker(worker)
    metadata[DISCORD_WORKER_META_KEY] = worker
    metadata.pop("db_path", None)
    atomic_json_write(kanban_db.board_metadata_path(board), metadata, indent=2)
    has_pr_or_skip = bool(worker.get("pr_url")) or bool(
        worker.get("pr_skipped_no_changes")
    )
    if worker.get("pr_skipped_no_changes"):
        return not bool(worker.get("pr_error"))
    return has_pr_or_skip and _pr_is_merged(worker) and not bool(worker.get("pr_error"))


def _merge_criteria(board: Optional[str], criteria: list[str]) -> None:
    if not board or not criteria:
        return
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY
    from utils import atomic_json_write

    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    existing = list(worker.get("criteria") or [])
    seen = {
        str(item.get("text") if isinstance(item, dict) else item).strip().lower()
        for item in existing
    }
    for text in criteria:
        if text.lower() in seen:
            continue
        existing.append({"text": text, "active": True})
    worker["criteria"] = existing
    metadata[DISCORD_WORKER_META_KEY] = worker
    path = kanban_db.board_metadata_path(board)
    metadata.pop("db_path", None)
    atomic_json_write(path, metadata, indent=2)


def _update_phase(board: Optional[str], phase: str, *, goal_status: str) -> None:
    if not board:
        return
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY
    from utils import atomic_json_write

    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    if worker.get("cancelled") or worker.get("goal_status") == "cancelled":
        return
    worker["phase"] = phase
    worker["goal_status"] = goal_status
    worker["updated_at"] = int(time.time())
    if goal_status in {"done", "blocked"}:
        worker["terminal_reaction_sync_pending"] = True
        worker["terminal_summary_sync_pending"] = True
    if goal_status == "done":
        worker["terminal_completion_message_pending"] = True
    metadata[DISCORD_WORKER_META_KEY] = worker
    metadata.pop("db_path", None)
    atomic_json_write(kanban_db.board_metadata_path(board), metadata, indent=2)
    if goal_status in {"done", "blocked"}:
        try:
            from hermes_cli.discord_worker_boards import persist_board_run_summary

            persist_board_run_summary(board)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
