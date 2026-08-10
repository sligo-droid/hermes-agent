"""Run one Kanban planner/dev/reviewer task through a coding worker."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from agent.transports.codex_app_server_session import CodexAppServerSession
from hermes_cli import kanban_db
from hermes_cli.discord_worker_boards import (
    DEV_TICKET_BODY_GUIDANCE,
    DISCORD_WORKER_META_KEY,
    MERGE_POLICY_NEVER,
    PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL,
    PR_OPEN_POLICY_NEVER,
    ROLE_DEV,
    ROLE_FOREMAN,
    ROLE_PLANNER,
    ROLE_REVIEWER,
    _format_plan_artifact_markdown,
    _normalize_discord_plan_artifacts,
    active_dev_round_for_board,
    format_role_round_title,
    is_cancelled,
    mark_dispatch_dirty,
    project_inspection_prompt_for_context,
    record_codex_worker_event,
    record_codex_worker_result,
)
from hermes_cli.github_remote import (
    github_cli_env,
    github_origin_repo,
    github_remote_preflight_error,
    github_repo_from_url,
    github_repo_from_value,
)
from hermes_cli.pr_body_format import check_project_state_requirement
from hermes_cli.ui_work_routing import (
    UIWorkRouteDecision,
    resolve_ui_work_route,
    ui_specialist_skill_prompt,
)
from hermes_cli.worker_autoreview import materialize_autoreview_helper

_OPENCODE_ROLES = {ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER}
_CODEX_AUTH_RETRY_LIMIT = 2
_FULL_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_PR_GUARDED_ROLES = {ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER}
_GH_PR_MUTATING_SUBCOMMANDS = {
    "close",
    "create",
    "edit",
    "lock",
    "merge",
    "ready",
    "reopen",
    "review",
    "unlock",
}
_PR_LIFECYCLE_RE = re.compile(
    r"\b("
    r"git\s+push|push\s+(?:the\s+)?(?:branch|head)|remote\s+branch|"
    r"gh\s+pr\s+(?:checks|create|edit|merge|status|view|watch)|"
    r"(?:open|create|update|sync)\s+(?:the\s+)?(?:pull\s+request|pr)|"
    r"(?:pull\s+request|pr)\s+(?:checks|status|metadata|description|title)|"
    r"wait\s+for\s+(?:ci|checks)|final\s+branch\s+state"
    r")\b",
    re.IGNORECASE,
)
_CODE_CHANGE_SIGNAL_RE = re.compile(
    r"\b("
    r"fix|implement|refactor|debug|repair|change\s+code|modify\s+code|"
    r"update\s+(?:code|tests?|docs?|files?)|failing\s+(?:test|check|ci)|"
    r"test\s+failure|lint\s+failure|type\s+error|regression|bug"
    r")\b",
    re.IGNORECASE,
)
_PLANNER_ROUTE_DECISION_RE = re.compile(
    r"(?:recorded\s+planner\s+route\s+decision(?:\s+for\s+this\s+ticket)?|route_decision)\s*[:=]\s*(\{[^\n]*\})",
    re.IGNORECASE,
)
_KANBAN_ACTIVITY_HEARTBEAT_INTERVAL_SECONDS = 10
_PR_TITLE_MAX_CHARS = 80
KANBAN_CONTROL_ENV_VARS = frozenset(
    {
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_BOARD",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_WORKSPACE",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BRANCH",
        "HERMES_KANBAN_TASK",
        "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK",
        "HERMES_KANBAN_ROOT",
    }
)
_last_activity_heartbeat_at: dict[tuple[str, str], float] = {}
_WORKER_PROJECT_STATE_JUSTIFICATION = (
    "Project-state: not needed - Discord worker board implementation; "
    "no current project-state ledger change required."
)
_DASHBOARD_QA_USERNAME = "hermes_qa"
_PID_QA_USERNAME = "hermes_qa"
class PRFinalizationOutcome(str, Enum):
    """The dispatcher-facing result of one PR publication pass."""

    PUBLISHED = "published"
    PENDING = "pending"
    FAILED = "failed"


def _single_line_pr_title(value: Any) -> str:
    title = re.sub(r"\s+", " ", str(value or "implementation")).strip()
    if not title:
        title = "implementation"
    if len(title) <= _PR_TITLE_MAX_CHARS:
        return title
    return title[: _PR_TITLE_MAX_CHARS - 3].rstrip(" .,-:;") + "..."


def _pr_title_source(worker: dict[str, Any]) -> str:
    for key in ("summary_title", "initial_request", "root_goal"):
        value = str(worker.get(key) or "").strip()
        if not value:
            continue
        for line in value.splitlines():
            candidate = line.strip(" -")
            if not candidate:
                continue
            candidate = re.sub(r"^(goal|title|request):\s*", "", candidate, flags=re.IGNORECASE).strip()
            if candidate:
                return candidate
    return "implementation"


def _pr_summary_bullets(worker: dict[str, Any]) -> list[str]:
    bullets: list[str] = []
    for key in ("summary", "review_summary"):
        value = str(worker.get(key) or "").strip()
        if value:
            bullets.append(re.sub(r"\s+", " ", value))
    for item in worker.get("changed_files") or []:
        text = str(item or "").strip()
        if text:
            bullets.append(f"Changed `{text}`")
    if not bullets:
        bullets.append("Implemented the approved Kanban worker-board changes.")
    deduped: list[str] = []
    seen: set[str] = set()
    for bullet in bullets:
        clean = bullet.strip(" -")
        if not clean or clean in seen:
            continue
        seen.add(clean)
        deduped.append(clean[:200].rstrip())
        if len(deduped) == 4:
            break
    return deduped


def _pr_verification_bullets(worker: dict[str, Any]) -> list[str]:
    bullets: list[str] = []
    raw_tests = worker.get("tests")
    if isinstance(raw_tests, list):
        for item in raw_tests:
            if isinstance(item, dict):
                command = str(item.get("command") or "").strip()
                result = str(item.get("result") or "").strip()
                output = str(item.get("output") or "").strip()
                text = command
                if result:
                    text = f"{text} {result}".strip()
                if output and len(text) < 160:
                    text = f"{text}: {output}".strip(": ")
            else:
                text = str(item or "").strip()
            if text:
                bullets.append(re.sub(r"\s+", " ", text)[:220].rstrip())
            if len(bullets) == 4:
                break
    if not bullets:
        bullets.append("See Kanban handoff metadata for verification details.")
    return bullets


def _build_worker_pr_copy(worker: dict[str, Any], *, board: str) -> tuple[str, str]:
    title = _single_line_pr_title(f"Discord worker: {_pr_title_source(worker)}")
    board_ref = str(worker.get("public_url") or board).strip() or board
    summary = "\n".join(f"- {bullet}" for bullet in _pr_summary_bullets(worker))
    verification = "\n".join(f"- {bullet}" for bullet in _pr_verification_bullets(worker))
    body = f"Board: {board_ref}\n\n## Summary\n{summary}\n\n## Verification\n{verification}"
    return title, body


def _github_cli_env() -> dict[str, str]:
    """Return an env that lets deterministic PR finalization see gh auth.

    Kanban worker processes can run with a profile-isolated ``HOME`` such as
    ``$HERMES_HOME/home``. That is good for tool-state isolation, but Sligo's
    host-level GitHub CLI auth often lives under the real user's
    ``~/.config/gh``. The terminal tool already bridges this via
    ``GH_CONFIG_DIR``; PR finalization uses direct subprocess calls and must do
    the same or approved worker boards block forever on ``gh`` 401s after all
    role tasks are done.
    """
    return github_cli_env()


def _run_gh(args: list[str], *, root: Path, timeout: int | float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["gh", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=_github_cli_env(),
    )


def main() -> int:
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    board = os.environ.get("HERMES_KANBAN_BOARD", "").strip() or None
    role = os.environ.get("HERMES_CODEX_WORKER_ROLE", "").strip().lower()
    workspace = os.environ.get("HERMES_KANBAN_WORKSPACE", "").strip() or os.getcwd()
    if not task_id or role not in {ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER, ROLE_FOREMAN}:
        return 2

    task = None
    result: Any = None
    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task_with_transient_retry(
            conn,
            task_id,
            board=board,
            operation_name="coding_worker.initial_get_task",
        )
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
        if _recover_completed_role_output(
            conn,
            task_id,
            role,
            result,
            board=board,
            workspace=workspace,
        ):
            return 0
        _rollback_and_close_connection(conn)
        conn = None
        if _recover_completed_role_output_fresh(
            task_id,
            role,
            result,
            board=board,
            workspace=workspace,
        ):
            return 0
        if _recover_recorded_role_output_fresh(
            task_id,
            role,
            board=board,
            workspace=workspace,
        ):
            return 0
        reason = f"{_backend_label(role, task)} worker failed: {exc}"
        try:
            fresh_conn = kanban_db.connect(board=board)
            try:
                blocked = kanban_db.block_task(fresh_conn, task_id, reason=reason)
                if blocked:
                    return 0
            finally:
                fresh_conn.close()
        except Exception:
            pass
        return 1
    finally:
        if conn is not None:
            _rollback_and_close_connection(conn)
        if board:
            try:
                mark_dispatch_dirty(board=board, reason=f"{role}-worker-finished")
            except Exception:
                pass


def _rollback_and_close_connection(conn: Any) -> None:
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def _recover_completed_role_output(
    conn: Any,
    task_id: str,
    role: str,
    result: Any,
    *,
    board: Optional[str],
    workspace: str,
    allow_blocked: bool = False,
) -> bool:
    """Apply an already-produced role JSON after cleanup/transient failures.

    A Codex role worker can finish the model turn and record a valid JSON
    result, then fail while tearing down the app-server or while the first DB
    write attempt hits a transient lock. In that case the worker process must
    not exit nonzero and let the dispatcher overwrite the useful verdict with
    ``pid not alive``. Try one narrow recovery pass; if it cannot record a
    terminal Kanban transition, the caller falls back to a normal blocked task.
    """
    if result is None or getattr(result, "error", None):
        return False
    final_text = str(getattr(result, "final_text", "") or "").strip()
    if not final_text:
        return False
    try:
        task = kanban_db.get_task_with_transient_retry(
            conn,
            task_id,
            board=board,
            operation_name="coding_worker.recovery_get_task",
        )
    except Exception:
        return False
    if task is None:
        return False
    if task.status in {"done", "scheduled", "archived"}:
        return True
    if task.status == "blocked" and not allow_blocked:
        return True
    if task.status not in {"running", "ready", "blocked"}:
        return False
    try:
        payload = _parse_json(final_text)
        if board and is_cancelled(board):
            return True
        _apply_role_output(
            conn,
            task_id,
            role,
            payload,
            board=board,
            workspace=workspace,
            expected_run_id=task.current_run_id,
        )
    except Exception:
        return False
    try:
        task = kanban_db.get_task_with_transient_retry(
            conn,
            task_id,
            board=board,
            operation_name="coding_worker.recovery_confirm_get_task",
            run_id=task.current_run_id,
        )
    except Exception:
        return False
    return bool(task and task.status in {"done", "blocked", "scheduled", "archived"})


def _recover_completed_role_output_fresh(
    task_id: str,
    role: str,
    result: Any,
    *,
    board: Optional[str],
    workspace: str,
    allow_blocked: bool = False,
) -> bool:
    """Retry terminal-result recording through a new SQLite connection.

    The role worker keeps its first connection open while OpenCode/Codex runs.
    If a later DB write fails because that connection is stale, locked, or
    otherwise poisoned, the process must still preserve a valid model result
    instead of exiting nonzero and letting the dispatcher report only
    ``pid not alive``.
    """
    try:
        fresh_conn = kanban_db.connect(board=board)
    except Exception:
        return False
    try:
        return _recover_completed_role_output(
            fresh_conn,
            task_id,
            role,
            result,
            board=board,
            workspace=workspace,
            allow_blocked=allow_blocked,
        )
    finally:
        try:
            fresh_conn.close()
        except Exception:
            pass


def _recover_recorded_role_output_fresh(
    task_id: str,
    role: str,
    *,
    board: Optional[str],
    workspace: str,
    allow_blocked: bool = False,
) -> bool:
    """Recover a role result that was persisted before an exception escaped.

    OpenCode/Codex result sidecars are written before the Kanban terminal
    transition. If an exception occurs between those two points, ``main()`` may
    enter its handler before the local ``result`` variable is assigned. The
    sidecar is then the only durable source of the valid worker JSON.
    """
    try:
        from hermes_cli.discord_worker_state import read_codex_worker_state

        state = read_codex_worker_state(task_id, board=board)
    except Exception:
        return False
    result_data = state.get("result") if isinstance(state, dict) else None
    if not isinstance(result_data, dict):
        return False
    if result_data.get("error"):
        return False
    final_text = str(result_data.get("final_text") or "").strip()
    if not final_text:
        return False
    if not _recorded_result_is_fresh_for_current_run(task_id, board=board, state=state):
        return False
    return _recover_completed_role_output_fresh(
        task_id,
        role,
        SimpleNamespace(final_text=final_text, error=None),
        board=board,
        workspace=workspace,
        allow_blocked=allow_blocked,
    )


_DEAD_PID_FAILURE_RE = re.compile(r"\bpid\s+\d+\s+not\s+alive\b", re.IGNORECASE)


def recover_recorded_role_outputs_for_running_tasks(conn: Any, *, board: Optional[str]) -> list[str]:
    """Apply durable role results before dispatcher dead-PID crash accounting.

    Also revisits tasks that already tripped the dead-PID circuit breaker. Older
    dispatchers could mark a role task blocked before noticing its recorded
    result sidecar; if the sidecar belongs to the current run, recover it on the
    next tick instead of leaving the board permanently stuck.
    """
    rows = conn.execute(
        "SELECT t.id, t.assignee, t.workspace_path, t.status, "
        "       t.current_run_id, t.last_failure_error, "
        "       r.error AS run_error "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE (t.status = 'running' AND t.current_run_id IS NOT NULL) "
        "   OR (t.status = 'blocked' AND t.current_run_id IS NOT NULL)"
    ).fetchall()
    recovered: list[str] = []
    for row in rows:
        role = str(row["assignee"] or "").strip().lower()
        if role not in {ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER, ROLE_FOREMAN}:
            continue
        if row["status"] == "blocked":
            failure_text = "\n".join(
                str(value or "")
                for value in (row["last_failure_error"], row["run_error"])
            )
            if not _DEAD_PID_FAILURE_RE.search(failure_text):
                continue
        workspace = str(row["workspace_path"] or os.getcwd())
        if _recover_recorded_role_output_fresh(
            row["id"],
            role,
            board=board,
            workspace=workspace,
            allow_blocked=row["status"] == "blocked",
        ):
            recovered.append(row["id"])
    return recovered


def _recorded_result_is_fresh_for_current_run(
    task_id: str,
    *,
    board: Optional[str],
    state: dict[str, Any],
) -> bool:
    try:
        updated_at = int(state.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated_at = 0
    try:
        conn = kanban_db.connect(board=board)
    except Exception:
        return False
    try:
        task = kanban_db.get_task_with_transient_retry(
            conn,
            task_id,
            board=board,
            operation_name="coding_worker.recorded_result_get_task",
        )
        if task is None:
            return False
        if task.status in {"done", "scheduled", "archived"}:
            return True
        if task.status not in {"running", "ready", "blocked"}:
            return False
        if not task.current_run_id:
            return True
        row = conn.execute(
            "SELECT started_at FROM task_runs WHERE id = ? AND task_id = ?",
            (int(task.current_run_id), task_id),
        ).fetchone()
        if row is None:
            return False
        try:
            started_at = int(row["started_at"] or 0)
        except (TypeError, ValueError):
            started_at = 0
        return bool(updated_at and started_at and updated_at >= started_at)
    finally:
        conn.close()


def _build_prompt(conn: Any, task_id: str, role: str) -> str:
    context = (
        _build_reviewer_context(conn, task_id)
        if role == ROLE_REVIEWER
        else kanban_db.build_worker_context(conn, task_id)
    )
    outcome = _role_outcome_frame(role)
    schema = _schema_instructions(role)
    git = _git_summary(os.environ.get("HERMES_KANBAN_WORKSPACE", "") or os.getcwd())
    discord_access = _discord_access_prompt(role)
    frontend_smoke = (
        "Frontend preview smoke contract:\n"
        "- If you start a frontend preview server, every smoke probe must use the exact host:port you started.\n"
        "- Do not fall back to framework defaults, stale browser tabs, or another worker's port.\n"
        "- Prefer `python -m hermes_cli.worker_frontend_smoke --url <exact-url> --cmd '<preview command with that host:port>' --route /` when practical.\n\n"
    )
    browser_preflight = (
        "Browser-task preflight contract:\n"
        "- Before launching ad-hoc Playwright/Chromium browser scripts or browser dogfood from this worker, run `python -m hermes_cli.browser_preflight chromium`.\n"
        "- Treat a nonzero preflight as an environment blocker for that browser-dependent check only; do not install browsers or fail unrelated non-browser work.\n\n"
    )
    dashboard_qa_auth = _dashboard_qa_auth_prompt()
    pr_policy = _pr_policy_prompt_note(role)
    autoreview = _dev_autoreview_prompt(role)
    visual_qa_handoff = _visual_qa_handoff_prompt(role)
    project_contracts = _board_project_context_prompt(conn, task_id)
    forced_skills = _forced_worker_skill_prompt(conn, task_id, role)
    return (
        f"You are the Discord Kanban {role} worker.\n"
        "Use the repository, shell, files, and worker helper commands available in this worker environment to complete the task.\n"
        f"{pr_policy}"
        "Return exactly one raw JSON object matching the schema below, with no Markdown fence or surrounding prose.\n\n"
        f"{outcome}\n\n"
        f"{discord_access}"
        f"{frontend_smoke}"
        f"{browser_preflight}"
        f"{dashboard_qa_auth}"
        f"{autoreview}"
        f"{visual_qa_handoff}"
        f"{project_contracts}"
        f"{forced_skills}"
        f"{schema}\n\n"
        f"Git context:\n{git}\n\n"
        f"Kanban context:\n{context}"
    )


def _dashboard_qa_auth_prompt() -> str:
    return (
        "Protected dashboard/browser QA auth contract:\n"
        "- For PID customer-surface browser QA, use the repository's native `pnpm --dir dashboard qa:auth` flow. It uses the read-only PID QA account "
        f"`{_PID_QA_USERNAME}` through `PID_QA_USERNAME`/`PID_QA_PASSWORD`; when those are absent, let PID's native launcher load its configured QA env file. Do not parse or source that file in an ad-hoc script.\n"
        "- Treat that PID account as role `admin_viewer`: it can inspect privileged read surfaces, including admin-gated pages and non-mutating admin APIs, but every mutation must remain denied.\n"
        "- Do not seek, request, or substitute mutation-capable admin credentials for ordinary QA. Report mutation testing as blocked unless separate explicit admin credentials are already supplied; do not claim read-only visibility proves mutation authority.\n"
        "- PID QA credentials are separate from Hermes dashboard Basic Auth; do not use `HERMES_DASHBOARD_*` credentials for PID.\n"
        f"- For protected Hermes dashboard or frontend smoke checks, use dashboard Basic Auth username `{_DASHBOARD_QA_USERNAME}`.\n"
        "- Read the password from `HERMES_DASHBOARD_PASSWORD` in the worker environment; do not ask for it if that env var is absent.\n"
        "- Never print, log, copy into prompts, or include any PID or Hermes password value in final output, test output, screenshots, URLs, or handoff metadata.\n\n"
    )


def _board_project_context(conn: Any, task_id: str) -> dict[str, Any]:
    try:
        task = kanban_db.get_task(conn, task_id)
    except Exception:
        return {}
    board = str(getattr(task, "tenant", "") or "").strip() if task else ""
    if not board:
        return {}
    try:
        metadata = kanban_db.read_board_metadata(board)
    except Exception:
        return {}
    worker = metadata.get(DISCORD_WORKER_META_KEY)
    if not isinstance(worker, dict):
        return {}
    context = worker.get("project_context")
    return dict(context) if isinstance(context, dict) else {}


def _normalized_visual_requirement(value: Any) -> dict[str, Any]:
    try:
        from agent.visual_qa import normalize_visual_requirement

        return normalize_visual_requirement(value)
    except Exception:
        return {"level": "none", "target": "", "assertions": []}


def _board_project_context_prompt(conn: Any, task_id: str) -> str:
    context = _board_project_context(conn, task_id)
    blocks: list[str] = []
    inspection = project_inspection_prompt_for_context(context)
    if inspection:
        blocks.append(inspection)
    if "visual_qa_requirement" in context:
        requirement = _normalized_visual_requirement(
            context.get("visual_qa_requirement")
        )
        if requirement["level"] == "none":
            blocks.append(
                "Structured board visual-QA requirement: not required. Keep the "
                "ticket handoff explicit with a Visual QA: N/A reason."
            )
        else:
            blocks.append(
                "Structured board visual-QA requirement: required. Preserve this "
                "opaque bounded contract in the dev/reviewer handoff:\n"
                + json.dumps(
                    requirement,
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
    return ("\n\n".join(blocks).rstrip() + "\n\n") if blocks else ""


def _forced_worker_skill_prompt(conn: Any, task_id: str, role: str) -> str:
    """Render task/board force-loaded skills into role-worker context.

    Discord worker-board role workers run through Codex/OpenCode, not the
    legacy Hermes ``hermes chat --skills ...`` worker path. The normal Kanban
    ``Task.skills`` field is still useful metadata, but it is not sufficient by
    itself here: the coding-worker prompt must contain the resolved skill text
    for the role worker to actually receive it. Keep this scoped to dev workers
    so planner/reviewer prompts do not get implementation style manuals.
    """

    if role != ROLE_DEV:
        return ""
    try:
        task = kanban_db.get_task(conn, task_id)
    except Exception:
        return ""
    if not task:
        return ""
    names = _merge_skill_names(
        getattr(task, "skills", None),
        _board_worker_skill_hints(getattr(task, "tenant", None)),
    )
    if not names:
        return ""

    try:
        from agent.skill_commands import build_automatic_skills_message

        message, _loaded, missing = build_automatic_skills_message(
            names,
            task_id=task_id,
            source_label="Kanban dev worker task/board worker_skill_hints",
        )
    except Exception:
        message, missing = None, names

    rendered: list[str] = ["Force-loaded implementation skills for this dev worker:"]
    if message:
        rendered.append(message)
    for name in missing:
        rendered.append(
            f"[Skill load warning: requested skill `{name}` did not resolve in this worker environment. "
            "Continue only with the task context and record the missing skill in handoff.notes.]"
        )
    return "\n\n".join(rendered).rstrip() + "\n\n"


def _merge_skill_names(*groups: Any) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        if isinstance(group, str):
            values = [group]
        elif isinstance(group, (list, tuple, set)):
            values = list(group)
        else:
            values = []
        for item in values:
            name = str(item or "").strip()
            if not name or name in seen:
                continue
            seen.add(name)
            merged.append(name)
    return merged


def _board_worker_skill_hints(board: Optional[str]) -> list[str]:
    if not board:
        return []
    try:
        metadata = kanban_db.read_board_metadata(board)
    except Exception:
        return []
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY

    worker = metadata.get(DISCORD_WORKER_META_KEY)
    if not isinstance(worker, dict):
        return []
    context = worker.get("project_context")
    if not isinstance(context, dict):
        return []
    return _merge_skill_names(context.get("worker_skill_hints"))


def _worker_plan_artifact_lines(worker: dict[str, Any]) -> list[str]:
    artifacts = _normalize_discord_plan_artifacts(worker.get("discord_plan_artifacts"))
    if not artifacts:
        pack_path = str(worker.get("context_pack_path") or "").strip()
        if pack_path:
            try:
                with Path(pack_path).open("r", encoding="utf-8") as fh:
                    pack = json.load(fh)
                if isinstance(pack, dict):
                    artifacts = _normalize_discord_plan_artifacts(pack.get("plan_artifacts"))
            except (OSError, json.JSONDecodeError):
                artifacts = []
    return [_format_plan_artifact_markdown(item) for item in artifacts[:8]]


def _build_reviewer_context(conn: Any, task_id: str) -> str:
    task = kanban_db.get_task(conn, task_id)
    if not task:
        raise ValueError(f"unknown task {task_id}")
    lines = [
        f"# Kanban reviewer task {task.id}: {task.title}",
        "",
        f"Assignee: {task.assignee or '(unassigned)'}",
        f"Status:   {task.status}",
    ]
    if task.tenant:
        lines.append(f"Tenant:   {task.tenant}")
    lines.append(f"Workspace: {task.workspace_kind} @ {task.workspace_path or '(unresolved)'}")
    lines.append("")

    worker: dict[str, Any] = {}
    if task.tenant:
        metadata = kanban_db.read_board_metadata(task.tenant)
        worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    lines.append("## Board review inputs")
    goal = str(worker.get("root_goal") or worker.get("initial_request") or "").strip()
    if goal:
        lines.append(f"Goal: {goal[:1000]}")
    criteria = worker.get("criteria") if isinstance(worker.get("criteria"), list) else []
    if criteria:
        lines.append("Acceptance criteria:")
        lines.extend(f"- {str(item)[:500]}" for item in criteria[:20])
    requirements = worker.get("requirements") if isinstance(worker.get("requirements"), list) else []
    if requirements:
        lines.append("Requirements:")
        for item in requirements[:30]:
            if isinstance(item, dict):
                lines.append(f"- {item.get('id') or 'REQ'}: {str(item.get('text') or '')[:500]}")
            else:
                lines.append(f"- {str(item)[:500]}")
    context_paths = [
        str(worker.get("context_pack_markdown_path") or "").strip(),
        str(worker.get("context_pack_path") or "").strip(),
    ]
    context_paths = [path for path in context_paths if path]
    if context_paths:
        lines.append("Context pack paths:")
        lines.extend(f"- {path}" for path in context_paths)
    artifact_paths = _worker_plan_artifact_lines(worker)
    if artifact_paths:
        lines.append("Durable Discord plan artifact paths:")
        lines.extend(artifact_paths)
    lines.append("")

    parent_rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        (task_id,),
    ).fetchall()
    if parent_rows:
        lines.append("## Parent task handoff manifests")
        for row in parent_rows:
            parent_id = row["parent_id"]
            parent = kanban_db.get_task(conn, parent_id)
            if not parent or parent.status != "done":
                continue
            runs = [run for run in kanban_db.list_runs(conn, parent_id) if run.outcome == "completed"]
            runs.sort(key=lambda run: run.started_at, reverse=True)
            run = runs[0] if runs else None
            metadata = run.metadata if run and isinstance(run.metadata, dict) else {}
            lines.append(f"### {parent_id}: {parent.title}")
            if run and run.summary:
                lines.append(str(run.summary).strip()[:1000])
            handoff = metadata.get("handoff") if isinstance(metadata, dict) else None
            if isinstance(handoff, dict):
                lines.append("handoff:")
                lines.append(json.dumps(handoff, ensure_ascii=False, sort_keys=True)[:4000])
            elif metadata:
                subset = {
                    key: metadata.get(key)
                    for key in ("changed_files", "tests", "verification", "preview", "known_warnings")
                    if key in metadata
                }
                if subset:
                    lines.append(json.dumps(subset, ensure_ascii=False, sort_keys=True)[:3000])
            lines.append("")

    if task.tenant:
        rows = conn.execute(
            "SELECT t.id, t.title, r.summary, r.ended_at "
            "FROM task_runs r JOIN tasks t ON r.task_id = t.id "
            "WHERE r.profile = ? AND r.task_id != ? AND r.outcome = 'completed' AND t.tenant = ? "
            "ORDER BY r.ended_at DESC LIMIT 5",
            (ROLE_REVIEWER, task_id, task.tenant),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT t.id, t.title, r.summary, r.ended_at "
            "FROM task_runs r JOIN tasks t ON r.task_id = t.id "
            "WHERE r.profile = ? AND r.task_id != ? AND r.outcome = 'completed' "
            "ORDER BY r.ended_at DESC LIMIT 5",
            (ROLE_REVIEWER, task_id),
        ).fetchall()
    comments = kanban_db.list_comments(conn, task_id)[-5:]
    if rows or comments:
        lines.append("## Recent reviewer history and comments")
        for row in rows:
            first = str(row["summary"] or "").strip().splitlines()
            lines.append(f"- {row['id']} — {row['title']}: {(first[0] if first else '(no summary)')[:300]}")
        for comment in comments:
            lines.append(f"- comment from worker `{str(comment.author).replace('`', '')}`: {comment.body[:500]}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _dev_autoreview_prompt(role: str) -> str:
    if role != ROLE_DEV:
        return ""
    return (
        "Autoreview closeout contract for dev workers:\n"
        "- After non-trivial code edits and focused checks, run `.agents/skills/autoreview/scripts/autoreview --mode local` as the final closeout review before returning JSON.\n"
        "- Hermes materializes this repo-local helper in worker workspaces before launch; report it unavailable only if materialization failed or the file is missing.\n"
        "- The local helper is deterministic and advisory; do not report it as a model review.\n"
        "- Treat review findings as advisory: verify each actionable finding in the real code path, fix only concrete in-scope issues, rerun affected checks, and rerun autoreview after review-triggered edits until no accepted/actionable findings remain.\n"
        "- If the autoreview helper is unavailable in this worker environment, record that explicitly in `handoff.notes` and continue with the normal focused verification you can run.\n"
        "- Record the autoreview command/result, or the unavailable reason, in `tests` or `handoff.notes` so the reviewer can audit closeout.\n\n"
    )


def _visual_qa_handoff_prompt(role: str) -> str:
    """Return the bounded visual-QA handoff contract for dev workers only."""
    if role != ROLE_DEV:
        return ""
    return (
        "Visual-QA handoff contract:\n"
        "- Match the ticket's `Visual QA: required` or `Visual QA: N/A — <reason>` acceptance criterion in `handoff.visual_qa`. For explicit visual UI or rendered-artifact work, run one assertion-driven rendered check and record `{level,target,assertions,check,status,evidence_ref}` with the actual passed, failed, or blocked status.\n"
        "- Navigation, a generic screenshot, or console success alone is not evidence. For nonvisual work record `{status: \"not_applicable\", reason: \"...\"}` instead and do not launch visual tooling.\n\n"
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
            "- Each dev ticket's Success means explicitly states `Visual QA: required` with one assertion-driven receipt for explicit visual work, or `Visual QA: N/A — <reason>` for nonvisual work.\n"
            "- Simple or single-surface Hermes/Discord jobs default to exactly one dev ticket unless the request explicitly needs separate deliverables.\n"
            "- Dev tickets stop at local verified branch state; they do not open PRs, push remotes, or merge.\n"
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
            "- If requirements are satisfied and only optional improvements remain, approve with empty new_tasks and mention optional follow-up in the summary.\n"
            "- PR lifecycle chores are excluded from new dev tasks; the deterministic finalizer owns push/open after approval.\n"
            "- Live pickup, deployment, active-path, and provenance gaps are treated as real implementation/closeout gaps, not PR lifecycle chores.\n"
            "- criteria_assessment checks each ticket's Visual QA required-or-N/A acceptance criterion against its handoff.\n"
            "- criteria_assessment maps each criterion to evidence or a gap.\n"
            "Stop when: Return the JSON review verdict."
        )
    if role == ROLE_FOREMAN:
        return (
            "Outcome frame:\n"
            "Goal: Repair a blocked Command Center worker-board ticket without creating code-change PR work.\n"
            "Success means:\n"
            "- The JSON status is completed, blocked, or checkpoint.\n"
            "- actions, verification, and changed_tasks describe every safe Kanban board/task mutation performed.\n"
            "- Stuck worker-board tickets are retried, unblocked, closed, reassigned, or left blocked only when safe and explained.\n"
            "- Dispatch is marked dirty when repair actions should wake the dispatcher.\n"
            "- Secrets and credentials stay redacted from summaries and metadata.\n"
            "Stop when: Return the JSON repair result."
        )
    return (
        "Outcome frame:\n"
        "Goal: Complete the assigned Kanban ticket or produce a checkpoint/blocker with evidence.\n"
        "Success means:\n"
        "- The smallest correct change within ticket scope is implemented.\n"
        "- Focused verification is run when available and recorded in tests.\n"
        "- Autoreview closeout is applied after non-trivial code edits when available, with command/result or unavailable reason recorded for reviewers.\n"
        "- changed_files, tests, handoff (including required visual_qa receipt or N/A), pr_ready, and blocker reflect the actual repository state.\n"
        "- Remote push and PR lifecycle work are not attempted by this role.\n"
        "Stop when: Return the JSON completion, checkpoint, or blocker object."
    )


def _pr_policy_prompt_note(role: str) -> str:
    if role == ROLE_PLANNER:
        return (
            "PR lifecycle policy: Dev workers must not open pull requests, push to remote branches, wait on remote checks, or merge. "
            "Plan tickets only for local implementation/verification; Hermes pushes the feature branch and opens the PR after reviewer approval.\n"
        )
    if role == ROLE_REVIEWER:
        return (
            "PR lifecycle policy: Do not create new dev tickets for pure PR chores such as git push, gh pr create/view/checks, updating a PR, or waiting on checks. "
            "If the implementation satisfies the goal and only PR lifecycle work remains, approve it; Hermes finalizes the PR deterministically.\n"
        )
    if role == ROLE_FOREMAN:
        return (
            "PR lifecycle policy: Do not create code-change PRs, push branches, or merge. "
            "This repair role may safely mutate Kanban board/task state to recover stuck worker-board tickets.\n"
        )
    return (
        "PR lifecycle policy: Do not run git push, gh pr create, gh pr merge, or other remote/PR mutation commands. "
        "Complete local implementation and verification only; Hermes performs the final push and opens the PR after reviewer approval.\n"
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
            "For simple or single-surface Hermes/Discord jobs, default to exactly one dev ticket. "
            "Fold docs, runbook notes, migration verification, closeout evidence, active-path evidence, normal tests, polish, and routine audit into the owning implementation ticket. "
            "Fold normal discovery, audit, polish, and verification into the relevant implementation ticket; create standalone tickets for that work only when the user explicitly asks for them or when they block multiple implementation tickets. "
            "Create standalone dev tickets only for truly independent implementation slices, user-requested separate deliverables, or shared blockers/prerequisites affecting multiple tickets. "
            "Do not create extra assertion, telemetry, debug, hardening, or PR-check tickets unless they are tied to a concrete unmet acceptance criterion. "
            f"{DEV_TICKET_BODY_GUIDANCE} "
            "Write Success means as ticket-specific acceptance criteria for the slice owned by that dev ticket; include board-level criteria only when that ticket owns the whole outcome. "
            "Each dev ticket's Success means must state `Visual QA: required` for explicit visual UI or rendered-artifact work and require one assertion-driven rendered-check receipt, or `Visual QA: N/A — <reason>` for nonvisual work. "
            "Set Stop when to the concrete handoff point for that ticket, usually code changed and verification recorded or a blocker stated. "
            "Do not create dev tickets whose goal is to push a branch, open/update a PR, watch PR checks, or merge; those are finalizer chores after review approval. "
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
            "Use parent task handoff manifests as primary review inputs, along with focused code/tests inspection. "
            "Assess each ticket's `Visual QA: required` or `Visual QA: N/A — <reason>` acceptance criterion against the parent handoff. "
            "For an explicit visual UI or rendered-artifact change, treat a missing `handoff.visual_qa` receipt or explicit `not_applicable` record as a concrete evidence gap; do not require visual tooling for nonvisual work. "
            "Use any pre_review_readiness advisory only as evidence to inspect, not as approval. "
            "Request a new round only for concrete acceptance gaps, evidenced regressions, real defects, or requested behavior that is unmet. "
            "Do not emit new_tasks for optional hardening, extra tests, PR lifecycle, docs polish, telemetry, routine active-path/code-island checks, or nice-to-have cleanup when requirements are satisfied. "
            "If requirements are satisfied and only optional improvements remain, set status to approved, keep new_tasks empty, and list optional follow-up only in the summary. "
            "When requesting changes, each new_tasks body must be a self-contained follow-up brief that opens with Goal, Success means, and Stop when. "
            "If changes are required for live pickup, deployment, active runtime paths, source-of-truth, or provenance because those are part of the requested behavior or acceptance criteria, the follow-up dev task must explicitly ask dev to verify and record the active path and source of truth. "
            "Do not emit new_tasks for pure PR lifecycle chores: git push, gh pr create/view/checks, updating an existing PR, waiting on checks, or merging."
        )
    if role == ROLE_FOREMAN:
        return (
            'Schema: {"status":"completed|blocked|checkpoint","summary":"...",'
            '"actions":["..."],"verification":["..."],"changed_tasks":[{"id":"...","action":"...","status":"..."}],'
            '"follow_up_proposals":[{"id":"...","title":"...","url":"...","reason":"..."}],'
            '"blocker":null} '
            "Record every Kanban repair action in actions and changed_tasks. "
            "If you create a Command Center self-improvement proposal/job for a durable repo fix discovered during repair, report it in optional follow_up_proposals; otherwise use an empty list or omit it. "
            "Use blocked only when repair cannot safely proceed and blocker explains the next human/operator action. "
            "Never include secrets, raw credentials, or unredacted tokens in the JSON."
        )
    return (
        'Schema: {"status":"completed|blocked|checkpoint","summary":"...","changed_files":["..."],'
        '"tests":[{"command":"...","result":"passed|failed|not_run","output":"..."}],'
        '"handoff":{"changed_files":["..."],"tests":[{"command":"...","result":"passed|failed|not_run","output":"..."}],'
        '"verification":["..."],"preview":{"url":"...","command":"...","status":"passed|failed|not_run"},'
        '"smoke_routes":["..."],"visual_qa":{},"known_warnings":["..."],"notes":"..."},"blocker":null,"pr_ready":false} '
        "Always include handoff so reviewers can audit the exact changed files, checks, autoreview closeout command/result or unavailable reason, preview URL/command, smoke routes, required visual_qa receipt or N/A record, warnings, and notes. "
        "Never push to a remote branch and never create, update, or merge a PR; stop after local code and verification."
    )


def _discord_access_prompt(role: str) -> str:
    if role == ROLE_FOREMAN:
        return (
            "Discord and board control access:\n"
            "- Foreman repair workers are not subject to the planner/dev/reviewer read-only Discord or PR-mutation restrictions.\n"
            "- You may inspect and safely mutate Kanban board/task state to recover blocked worker-board tickets: mark dispatch dirty, retry, unblock, close, reassign, or leave blocked with a clear explanation when safe.\n"
            "- Use Discord worker read/control broker access only when necessary to inspect context or coordinate recovery; keep Discord writes minimal and operator-safe.\n"
            "- Keep secrets, credentials, tokens, and private environment values redacted in all summaries, actions, and metadata.\n\n"
        )
    return (
        "Discord and board control access:\n"
        "- Planner/dev/reviewer workers are read-only for normal Discord access. "
        "The finalizer/operator owns board and Discord mutation.\n"
        "- You may inspect Discord message context with "
        "`python -m hermes_cli.discord_worker_read fetch-message --channel-id <id> --message-id <id>`.\n"
        "- You may inspect recent thread/channel history with "
        "`python -m hermes_cli.discord_worker_read fetch-messages --channel-id <id> --limit 25`.\n"
        "- Do not call mutation helpers such as Discord REST writes, board updates, task status changes, or summary syncs from this role.\n\n"
    )


def _configured_backend() -> str:
    try:
        from agent.opencode_worker import load_coding_worker_backend

        return load_coding_worker_backend()
    except Exception:
        return "opencode"


def _backend_label(role: str, task: Any = None) -> str:
    if _role_uses_opencode(role, task):
        return "OpenCode"
    return "Codex"


def _task_forces_opencode(task: Any = None) -> bool:
    """Return whether a role task must bypass the configured coding backend.

    No current task type forces OpenCode. Command Center repair work follows the
    same configured coding-worker backend as every other role lane.
    """
    return False


def _role_uses_opencode(role: str, task: Any = None) -> bool:
    if _task_forces_opencode(task):
        return True
    return _configured_backend() == "opencode"


def _role_pr_mutation_guard_env(role: str) -> tuple[dict[str, str], Optional[Path]]:
    """Prepend git/gh wrappers that block worker-owned PR mutations.

    The deterministic finalizer is the only code path allowed to push, open,
    or merge PRs. Role workers may still inspect local git state and run safe
    read-only gh commands through the real binaries.
    """
    if role not in _PR_GUARDED_ROLES:
        return {}, None
    guard_dir = Path(tempfile.mkdtemp(prefix="hermes-kanban-pr-guard-"))
    real_git = shutil.which("git")
    real_gh = shutil.which("gh")
    if real_git:
        _write_pr_guard_wrapper(
            guard_dir / "git",
            real_binary=real_git,
            kind="git",
        )
    if real_gh:
        _write_pr_guard_wrapper(
            guard_dir / "gh",
            real_binary=real_gh,
            kind="gh",
        )
    if not any(guard_dir.iterdir()):
        _cleanup_pr_mutation_guard(guard_dir)
        return {}, None
    path = f"{guard_dir}{os.pathsep}{os.environ.get('PATH', '')}"
    return {"PATH": path, "HERMES_KANBAN_PR_GUARD": "1"}, guard_dir


def _write_pr_guard_wrapper(path: Path, *, real_binary: str, kind: str) -> None:
    mutating = sorted(_GH_PR_MUTATING_SUBCOMMANDS)
    script = f"""#!/usr/bin/env python3
import os
import sys

REAL_BINARY = {json.dumps(real_binary)}
KIND = {json.dumps(kind)}
GH_PR_MUTATING_SUBCOMMANDS = {json.dumps(mutating)}

args = sys.argv[1:]
blocked = False
if KIND == "git":
    blocked = "push" in args
elif KIND == "gh":
    for index, arg in enumerate(args[:-1]):
        if arg == "pr" and args[index + 1] in GH_PR_MUTATING_SUBCOMMANDS:
            blocked = True
            break

if blocked:
    print(
        "Hermes Kanban role workers may not push branches or mutate PRs; "
        "the deterministic finalizer handles PR sync/open/merge after reviewer approval.",
        file=sys.stderr,
    )
    sys.exit(126)

os.execv(REAL_BINARY, [REAL_BINARY, *args])
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def _cleanup_pr_mutation_guard(path: Optional[Path]) -> None:
    if not path:
        return
    try:
        for child in path.iterdir():
            child.unlink()
        path.rmdir()
    except Exception:
        pass


def _role_read_only_discord_env(role: str) -> dict[str, str]:
    if role not in _PR_GUARDED_ROLES:
        return {}
    return {
        "HERMES_DISCORD_WORKER_READ_ONLY": "1",
        "HERMES_DISCORD_WORKER_CONTROL_URL": "",
        "HERMES_DISCORD_WORKER_CONTROL_TOKEN": "",
    }


def _backend_child_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in KANBAN_CONTROL_ENV_VARS
    }
    if extra:
        env.update(extra)

    explicit_keys = set((extra or {}).keys())
    try:
        from tools.environments.local import _bootstrap_profile_subprocess_env

        _bootstrap_profile_subprocess_env(env, explicit_keys)
    except Exception:
        pass

    return env


def _restore_environ(old_values: dict[str, Optional[str]]) -> None:
    for key, old in old_values.items():
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old


def _extract_task_route_decision(task: Any) -> Any:
    explicit = getattr(task, "route_decision", None)
    if explicit:
        return explicit
    body = str(getattr(task, "body", "") or "")
    try:
        parsed_body = json.loads(body)
    except Exception:
        parsed_body = None
    if isinstance(parsed_body, dict):
        parsed_decision = parsed_body.get("route_decision")
        if isinstance(parsed_decision, dict) and parsed_decision.get("route"):
            return parsed_decision
    text = "\n".join(
        str(part or "")
        for part in (
            getattr(task, "title", ""),
            body,
        )
    )
    for match in _PLANNER_ROUTE_DECISION_RE.finditer(text):
        raw = match.group(1).strip()
        try:
            parsed = json.loads(raw)
        except Exception:
            continue
        if isinstance(parsed, dict) and parsed.get("route"):
            parsed.setdefault("source", "planner")
            return parsed
    return None


def _resolve_task_ui_work_route(
    task: Any,
    role: str,
    *,
    workspace: str,
    backend: str,
    model_tier: str = "",
) -> UIWorkRouteDecision | None:
    if role != ROLE_DEV:
        return None
    route_decision = _extract_task_route_decision(task)
    if not route_decision:
        return None
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    decision = resolve_ui_work_route(
        cfg,
        title=str(getattr(task, "title", "") or ""),
        task=str(getattr(task, "body", "") or ""),
        context=str(getattr(task, "result", "") or ""),
        cwd=workspace,
        backend=backend,
        route_decision=route_decision,
        # Skill breadth follows trusted scheduler setup, never planner-authored
        # task prose or route-decision metadata.
        model_tier=model_tier,
    )
    if not decision.matched and decision.selected_route == "default_coding_worker":
        return None
    return decision


def _ui_work_route_env(decision: UIWorkRouteDecision | None) -> dict[str, str]:
    if decision is None:
        return {}
    metadata = decision.metadata()
    values = {
        "HERMES_UI_WORK_ROUTE": metadata.get("selected_route"),
        "HERMES_UI_WORK_ROUTE_DECISION": metadata.get("route_decision"),
        "HERMES_UI_WORK_ROUTE_DECISION_SOURCE": metadata.get("route_decision_source"),
        "HERMES_UI_WORK_ROUTE_DECISION_RATIONALE": metadata.get("route_decision_rationale"),
        "HERMES_UI_WORK_FALLBACK_USED": str(bool(metadata.get("fallback_used"))).lower(),
    }
    return {key: str(value) for key, value in values.items() if value not in (None, "")}


def _ui_work_route_prompt(decision: UIWorkRouteDecision | None) -> str:
    if decision is None:
        return ""
    metadata = decision.metadata()
    return (
        "UI specialist route metadata for this worker launch:\n"
        f"- selected_route: {metadata.get('selected_route') or ''}\n"
        f"- recommended_skills: {', '.join(metadata.get('recommended_skills') or [])}\n"
        f"- route_decision_source: {metadata.get('route_decision_source') or ''}\n"
        f"- route_decision_rationale: {metadata.get('route_decision_rationale') or ''}\n"
        f"- advisory_reason: {metadata.get('advisory_reason') or ''}\n"
        "This is structured launch evidence; include it in route-smoke verification instead of re-inferring the route from prose.\n"
        f"{ui_specialist_skill_prompt(decision)}\n"
    )


def _record_ui_work_route(task_id: str, *, board: Optional[str], decision: UIWorkRouteDecision | None) -> None:
    if decision is None:
        return
    try:
        record_codex_worker_event(
            task_id,
            board=board,
            event={
                "method": "ui_work_route/decision",
                "params": {"route": decision.metadata()},
            },
        )
    except Exception:
        pass


def _attach_ui_work_route(
    result: Any,
    decision: UIWorkRouteDecision | None,
    *,
    backend: str,
) -> None:
    if decision is None:
        return
    metadata = decision.metadata()
    if decision.selected_route == "ui_visual_specialist":
        passes = (getattr(result, "run_profile", None) or {}).get("passes") or []
        actual_pass = passes[-1] if passes and isinstance(passes[-1], dict) else {}
        metadata.update(
            {
                "actual_backend": backend,
                "actual_model": actual_pass.get("model") or "",
                "actual_reasoning_effort": actual_pass.get("reasoning") or "",
            }
        )
    try:
        setattr(result, "ui_work_route", metadata)
    except Exception:
        pass


def _trusted_scheduled_model_tier() -> str:
    """Return the scheduler-resolved model tier, if one was supplied."""

    source = str(
        os.environ.get("HERMES_CODEX_WORKER_MODEL_TIER_SOURCE") or "none"
    ).strip().lower()
    if source != "role":
        return ""
    return str(
        os.environ.get("HERMES_CODEX_WORKER_MODEL_TIER") or ""
    ).strip().lower()


def _run_role_backend(
    prompt: str,
    workspace: str,
    role: str,
    *,
    task: Any,
    task_id: str,
    board: Optional[str],
):
    materialization_note = _materialize_role_autoreview(workspace, role)
    if materialization_note:
        prompt = f"{prompt.rstrip()}\n\n{materialization_note}\n"
    uses_opencode = _role_uses_opencode(role, task)
    ui_work_route = _resolve_task_ui_work_route(
        task,
        role,
        workspace=workspace,
        backend="opencode" if uses_opencode else "codex",
        model_tier=_trusted_scheduled_model_tier(),
    )
    route_prompt = _ui_work_route_prompt(ui_work_route)
    if route_prompt:
        prompt = f"{prompt.rstrip()}\n\n{route_prompt}"
    _record_ui_work_route(task_id, board=board, decision=ui_work_route)
    if uses_opencode:
        return _run_opencode(
            prompt,
            workspace,
            role,
            task=task,
            task_id=task_id,
            board=board,
            ui_work_route=ui_work_route,
        )
    return _run_codex(
        prompt,
        workspace,
        role,
        task_id=task_id,
        board=board,
        ui_work_route=ui_work_route,
    )


def _materialize_role_autoreview(workspace: str, role: str) -> str:
    try:
        materialize_autoreview_helper(workspace)
    except Exception as exc:
        return f"Autoreview helper materialization failed before {role} worker start: {exc}"
    return ""


def _run_codex(
    prompt: str,
    workspace: str,
    role: str,
    *,
    task_id: str,
    board: Optional[str],
    ui_work_route: UIWorkRouteDecision | None = None,
):
    extra_args = _role_extra_args(role)

    def on_event(note: dict) -> None:
        try:
            record_codex_worker_event(task_id, board=board, event=note)
        except Exception:
            pass
        _heartbeat_worker_activity(task_id, board=board)

    guard_env, guard_dir = _role_pr_mutation_guard_env(role)
    runtime_env = {
        **guard_env,
        **_role_read_only_discord_env(role),
        **_ui_work_route_env(ui_work_route),
    }
    try:
        attempt = 0
        while True:
            worker_env = _backend_child_env(
                {
                    "HERMES_DISABLE_MCP": "1",
                    "HERMES_CODEX_WORKER_NETWORK_ACCESS": "1",
                    "HERMES_KANBAN_WORKSPACE": workspace,
                    **runtime_env,
                }
            )
            session = CodexAppServerSession(
                cwd=workspace,
                codex_home=os.environ.get("CODEX_HOME"),
                extra_args=extra_args,
                env=worker_env,
                replace_env=True,
                on_event=on_event,
            )
            try:
                result = session.run_turn(prompt, turn_timeout=_role_timeout(role))
                _attach_scheduled_runtime(result, role)
                _attach_ui_work_route(result, ui_work_route, backend="codex")
                try:
                    record_codex_worker_result(task_id, board=board, result=result)
                except Exception:
                    pass
                _heartbeat_worker_activity(task_id, board=board, force=True)
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
        _cleanup_pr_mutation_guard(guard_dir)
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
        prefer_pool=True,
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
    ui_work_route: UIWorkRouteDecision | None = None,
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
        _heartbeat_worker_activity(task_id, board=board)

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
    guard_env, guard_dir = _role_pr_mutation_guard_env(role)
    runtime_env = {
        **guard_env,
        **_role_read_only_discord_env(role),
        **_ui_work_route_env(ui_work_route),
    }
    old_env = {key: os.environ.get(key) for key in runtime_env}
    os.environ.update(runtime_env)
    try:
        if role == ROLE_PLANNER:
            scheduled_worker_config = _scheduled_opencode_worker_config()
            cfg = (
                load_opencode_config(worker_config=scheduled_worker_config)
                if scheduled_worker_config is not None
                else load_opencode_config()
            )
            result = run_opencode_single_pass(
                prompt,
                workspace,
                timeout=_role_timeout(role),
                agent=cfg["plan_agent"],
                reasoning_level=cfg["complex_plan_reasoning_level"],
                fast_mode=cfg.get("complex_plan_fast_mode", False),
                title=f"kanban {task_id}",
                env=_backend_child_env(runtime_env),
                on_event=on_event,
                scope_session_key=str(board or task_id or ""),
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
                env=_backend_child_env(runtime_env),
                on_event=on_event,
                scope_session_key=str(board or task_id or ""),
            )
    finally:
        _restore_environ(old_env)
        _cleanup_pr_mutation_guard(guard_dir)
    _attach_scheduled_runtime(result, role)
    _attach_ui_work_route(result, ui_work_route, backend="opencode")
    try:
        record_codex_worker_result(task_id, board=board, result=result)
    except Exception:
        pass
    _heartbeat_worker_activity(task_id, board=board, force=True)
    return result


def _heartbeat_worker_activity(task_id: str, *, board: Optional[str], force: bool = False) -> None:
    """Best-effort Kanban liveness for coding-worker stream/result activity."""
    now = time.monotonic()
    key = (str(board or ""), str(task_id or ""))
    if not force:
        last = _last_activity_heartbeat_at.get(key)
        if last is not None and now - last < _KANBAN_ACTIVITY_HEARTBEAT_INTERVAL_SECONDS:
            return
    _last_activity_heartbeat_at[key] = now
    expected_run_id = _env_run_id(task_id)
    try:
        conn = kanban_db.connect(board=board)
        try:
            kanban_db.heartbeat_claim(
                conn,
                task_id,
                claimer=os.environ.get("HERMES_KANBAN_CLAIM_LOCK"),
            )
            kanban_db.heartbeat_worker(
                conn,
                task_id,
                note="coding worker activity",
                expected_run_id=expected_run_id,
            )
        finally:
            conn.close()
    except Exception:
        pass


def _env_run_id(task_id: str) -> Optional[int]:
    env_task = os.environ.get("HERMES_KANBAN_TASK")
    if env_task and env_task != task_id:
        return None
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _scheduled_opencode_reasoning(default: str) -> str:
    from hermes_constants import normalize_reasoning_effort

    effort = normalize_reasoning_effort(os.environ.get("HERMES_CODEX_WORKER_REASONING") or "")
    if effort == "ultra":
        return "max"
    if effort in {"minimal", "low", "medium", "high", "xhigh", "max"}:
        return effort
    return default


def _scheduled_opencode_worker_config() -> Optional[dict[str, Any]]:
    model_tier = str(os.environ.get("HERMES_CODEX_WORKER_MODEL_TIER") or "").strip()
    opencode_model = str(os.environ.get("HERMES_OPENCODE_WORKER_MODEL") or "").strip()
    service_tier = str(
        os.environ.get("HERMES_CODEX_WORKER_SERVICE_TIER") or ""
    ).strip().lower()
    if model_tier:
        effort = _scheduled_opencode_reasoning("")
        worker_config: dict[str, Any] = {"model_tier": model_tier}
        if service_tier in {"fast", "normal"}:
            # The dispatcher has already resolved explicit role/config values
            # against the named tier. Preserve that result across the
            # subprocess boundary instead of letting OpenCode re-derive it.
            worker_config["service_tier"] = service_tier
        if opencode_model:
            worker_config["opencode"] = {"model": opencode_model}
        if effort:
            worker_config.update(
                {
                    "simple_build_reasoning_level": effort,
                    "complex_plan_reasoning_level": effort,
                    "complex_build_reasoning_level": effort,
                }
            )
        return worker_config or None

    worker_config: dict[str, Any] = {}
    if (
        os.environ.get("HERMES_CODEX_WORKER_REASONING_SOURCE") == "adaptive"
        and not _raw_opencode_pass_configured()
    ):
        effort = _scheduled_opencode_reasoning("")
        if effort:
            worker_config.update(
                {
                    "simple_build_reasoning_level": effort,
                    "complex_build_reasoning_level": effort,
                }
            )
    if service_tier in {"fast", "normal"}:
        worker_config["service_tier"] = service_tier
    return worker_config or None


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
    args = []
    model = str(os.environ.get("HERMES_CODEX_WORKER_MODEL") or "").strip()
    if model:
        args.extend(["-c", f'model={json.dumps(model)}'])
    args.extend([
        "-c", f'model_reasoning_effort="{effort}"',
        "-c", f'service_tier="{service_tier}"',
    ])
    return args


def _worker_reasoning_effort(role: str) -> str:
    from hermes_constants import normalize_reasoning_effort

    effort = normalize_reasoning_effort(os.environ.get("HERMES_CODEX_WORKER_REASONING") or "")
    if effort in {"max", "ultra"}:
        effort = "xhigh"
    if effort in {"minimal", "low", "medium", "high", "xhigh"}:
        if role != ROLE_REVIEWER and effort == "xhigh":
            return "high"
        return effort
    if role == ROLE_REVIEWER:
        return "xhigh"
    if role in {ROLE_PLANNER, ROLE_FOREMAN}:
        return "high"
    return "medium"


def _worker_service_tier() -> str:
    service_tier = str(os.environ.get("HERMES_CODEX_WORKER_SERVICE_TIER") or "normal").strip().lower()
    return service_tier if service_tier in {"fast", "normal"} else "normal"


def _attach_scheduled_runtime(result: Any, role: str) -> None:
    service_tier = _worker_service_tier()
    model = str(os.environ.get("HERMES_CODEX_WORKER_MODEL") or "").strip()
    model_tier = str(os.environ.get("HERMES_CODEX_WORKER_MODEL_TIER") or "").strip()
    model_tier_source = str(
        os.environ.get("HERMES_CODEX_WORKER_MODEL_TIER_SOURCE") or "none"
    ).strip()
    reasoning_source = str(
        os.environ.get("HERMES_CODEX_WORKER_REASONING_SOURCE") or "default"
    ).strip()
    service_tier_source = str(
        os.environ.get("HERMES_CODEX_WORKER_SERVICE_TIER_SOURCE") or "default"
    ).strip()
    try:
        setattr(result, "service_tier", service_tier)
        setattr(result, "fast_mode", service_tier == "fast")
        if not getattr(result, "run_profile", None):
            name = "build"
            if role == ROLE_PLANNER:
                name = "plan"
            elif role == ROLE_REVIEWER:
                name = "review"
            elif role == ROLE_FOREMAN:
                name = "repair"
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
                            "reasoning_source": reasoning_source,
                            "model": model,
                            "model_tier": model_tier,
                            "model_tier_source": model_tier_source,
                            "service_tier": service_tier,
                            "service_tier_source": service_tier_source,
                        }
                    ],
                },
            )
    except Exception:
        pass


def _role_timeout(role: str) -> float:
    if role == ROLE_DEV:
        return float(os.environ.get("HERMES_CODEX_DEV_TIMEOUT", "3600"))
    if role == ROLE_FOREMAN:
        return float(os.environ.get("HERMES_CODEX_FOREMAN_TIMEOUT", "1800"))
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


def _request_dispatch_reconciliation(board: Optional[str]) -> None:
    if not board:
        return
    try:
        mark_dispatch_dirty(board=board, reason="reviewer-approval-persisted")
    except Exception:
        pass


def _record_reviewer_approval_head(board: Optional[str], workspace: str) -> None:
    """Bind reviewer approval only to the exact clean checkpoint inspected."""

    if not board:
        return
    root = Path(workspace).expanduser().resolve(strict=False)
    try:
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20,
        )
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return
    head_sha = str(head.stdout or "").strip().lower() if head.returncode == 0 else ""
    if status.returncode != 0 or bool(str(status.stdout or "").strip()):
        return
    if not _FULL_SHA_RE.fullmatch(head_sha):
        return
    try:
        from hermes_cli.discord_worker_boards import _update_worker_meta

        _update_worker_meta(board, {"review_approved_head": head_sha})
    except Exception:
        return


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
            completed = _complete_role_task(
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
            completed = _complete_role_task(
                conn,
                task_id,
                summary=summary or "Reviewer approved.",
                metadata=metadata,
                expected_run_id=expected_run_id,
            )
            if not completed:
                return
            _record_reviewer_approval_head(board, workspace)
            _request_dispatch_reconciliation(board)
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
        raw_new_tasks = payload.get("new_tasks")
        new_tasks: list[Any] = raw_new_tasks if isinstance(raw_new_tasks, list) else []
        filtered_new_tasks, pr_lifecycle_tasks = _filter_pr_lifecycle_tasks(new_tasks)
        if pr_lifecycle_tasks and not filtered_new_tasks:
            metadata = {
                "raw": payload,
                "filtered_pr_lifecycle_tasks": pr_lifecycle_tasks,
            }
            completed = _complete_role_task(
                conn,
                task_id,
                summary=summary or "Reviewer found only PR lifecycle follow-up; handing off finalization.",
                metadata=metadata,
                expected_run_id=expected_run_id,
            )
            if not completed:
                return
            _record_reviewer_approval_head(board, workspace)
            _request_dispatch_reconciliation(board)
            return
        dev_round = active_dev_round_for_board(board)
        specs = _reviewer_dev_task_specs(filtered_new_tasks, dev_round=dev_round, workspace=workspace, board=board)
        _add_context_headers(specs, board=board)
        created: list[str] = []
        try:
            created = _create_planned_dev_tasks(conn, specs, created_by=ROLE_REVIEWER)
            metadata = {"created_tasks": created, "raw": payload}
            if pr_lifecycle_tasks:
                metadata["filtered_pr_lifecycle_tasks"] = pr_lifecycle_tasks
            completed = _complete_role_task(
                conn,
                task_id,
                summary=summary or "Reviewer requested changes.",
                metadata=metadata,
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

    if role == ROLE_FOREMAN:
        metadata = _foreman_metadata(payload)
        if status == "blocked":
            kanban_db.block_task(
                conn,
                task_id,
                reason=payload.get("blocker") or summary,
                expected_run_id=expected_run_id,
            )
            return
        if status == "checkpoint":
            kanban_db.schedule_task(
                conn,
                task_id,
                reason=summary or "Foreman repair checkpoint.",
                expected_run_id=expected_run_id,
            )
            return
        completed = _complete_role_task(
            conn,
            task_id,
            summary=summary or "Foreman repair completed.",
            metadata=metadata,
            expected_run_id=expected_run_id,
        )
        if not completed:
            raise RuntimeError("foreman completed but Kanban task transition was rejected")
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
    checkpoint_commit_error = _checkpoint_commit(workspace, task_id, summary)
    metadata = {
        "changed_files": _string_list(payload.get("changed_files")),
        "tests": payload.get("tests") if isinstance(payload.get("tests"), list) else [],
        "handoff": payload.get("handoff") if isinstance(payload.get("handoff"), dict) else {},
        "pr_ready": bool(payload.get("pr_ready")),
        "raw": payload,
    }
    if checkpoint_commit_error:
        metadata["checkpoint_commit_error"] = checkpoint_commit_error
    completed = _complete_role_task(
        conn,
        task_id,
        summary=summary or "Dev task completed.",
        metadata=metadata,
        expected_run_id=expected_run_id,
    )
    if not completed:
        raise RuntimeError("worker completed but Kanban task transition was rejected")


def _foreman_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {"raw": payload}
    actions = payload.get("actions")
    if isinstance(actions, list):
        metadata["actions"] = actions
    verification = payload.get("verification")
    if isinstance(verification, list):
        metadata["verification"] = verification
    changed_tasks = payload.get("changed_tasks")
    if isinstance(changed_tasks, list):
        metadata["changed_tasks"] = changed_tasks
    return metadata


def _complete_role_task(
    conn: Any,
    task_id: str,
    *,
    summary: str,
    metadata: dict[str, Any],
    expected_run_id: Optional[int],
    created_cards: Optional[list[str]] = None,
) -> bool:
    completed = kanban_db.complete_task(
        conn,
        task_id,
        summary=summary,
        metadata=metadata,
        created_cards=created_cards,
        expected_run_id=expected_run_id,
    )
    if completed or expected_run_id is None:
        return completed
    if not _still_owns_claim(conn, task_id):
        return False
    return kanban_db.complete_task(
        conn,
        task_id,
        summary=summary,
        metadata=metadata,
        created_cards=created_cards,
        expected_run_id=None,
    )


def _still_owns_claim(conn: Any, task_id: str) -> bool:
    lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK", "").strip()
    if not lock:
        return False
    row = conn.execute(
        "SELECT status, claim_lock FROM tasks WHERE id = ?",
        (task_id,),
    ).fetchone()
    return bool(row and row["status"] == "running" and row["claim_lock"] == lock)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def _filter_pr_lifecycle_tasks(tasks: list[Any]) -> tuple[list[Any], list[dict[str, str]]]:
    kept: list[Any] = []
    filtered: list[dict[str, str]] = []
    for item in tasks:
        if _is_pure_pr_lifecycle_task(item):
            filtered.append(
                {
                    "title": str(item.get("title") or "").strip() if isinstance(item, dict) else str(item),
                    "body": str(item.get("body") or "").strip() if isinstance(item, dict) else "",
                }
            )
            continue
        kept.append(item)
    return kept, filtered


def _is_pure_pr_lifecycle_task(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    title = str(item.get("title") or "")
    body = str(item.get("body") or "")
    text = re.sub(r"\s+", " ", f"{title} {body}").strip()
    if not text or not _PR_LIFECYCLE_RE.search(text):
        return False
    if _CODE_CHANGE_SIGNAL_RE.search(text):
        return False
    return True


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
    artifact_paths = _worker_plan_artifact_lines(worker)
    if not context_path and not artifact_paths:
        return
    for spec in specs:
        requirement_ids = _string_list(spec.get("requirement_ids"))
        lines = []
        if context_path:
            lines.extend(["Context pack:", f"- {context_path}"])
            if worker.get("context_pack_path") and str(worker.get("context_pack_path")) != context_path:
                lines.append(f"- JSON: {worker.get('context_pack_path')}")
        if artifact_paths:
            lines.append("Durable Discord plan artifact paths:")
            lines.extend(artifact_paths)
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
    from hermes_cli.discord_worker_boards import _update_worker_meta

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
    # Planner output is authoritative for the current planning pass. Clear stale
    # requirements when an older/auxiliary planner omits the optional field.
    _update_worker_meta(board, {"requirements": requirements})


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


def _checkpoint_commit(workspace: str, task_id: str, summary: str) -> Optional[str]:
    import logging

    logger = logging.getLogger(__name__)
    try:
        root = Path(workspace)
        status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, timeout=10)
        if not (status.stdout or "").strip():
            return None
        added = subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, text=True, timeout=60)
        if added.returncode != 0:
            err = (added.stderr or added.stdout or "git add failed").strip()
            logger.warning("checkpoint commit failed for task %s: %s", task_id, err)
            return err
        message = f"checkpoint {task_id}: {(summary or 'worker progress')[:80]}"
        committed = subprocess.run(["git", "commit", "-m", message], cwd=root, capture_output=True, text=True, timeout=120)
        if committed.returncode != 0:
            err = (committed.stderr or committed.stdout or "git commit failed").strip()
            logger.warning("checkpoint commit failed for task %s: %s", task_id, err)
            return err
        return None
    except Exception as exc:
        logger.warning("checkpoint commit failed for task %s: %s", task_id, exc)
        return str(exc)


def _resolve_github_repo(worker: dict[str, Any], root: Path) -> Optional[str]:
    context = worker.get("project_context") if isinstance(worker.get("project_context"), dict) else {}
    for source in (context, worker):
        for key in ("github_pr_target_repo", "pr_target_repo"):
            repo = github_repo_from_value(source.get(key))
            if repo:
                return repo

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
        repo = github_repo_from_url(remote.stdout)
        if repo:
            return repo

    for source in (context, worker):
        for key in ("github_url", "project_github_url", "repo_url", "repository_url"):
            repo = github_repo_from_url(str(source.get(key) or ""))
            if repo:
                return repo
    return None


def _pr_number_from_url(url: str) -> str:
    match = re.search(r"/pull/(\d+)(?:\D.*)?$", str(url or "").strip())
    return match.group(1) if match else ""


def _reset_pr_status_fields(worker: dict[str, Any]) -> None:
    """Clear stale publication and legacy merge facts before a PR refresh."""
    keys = (
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
        "canonical_sync_state",
        "canonical_sync_error",
        "canonical_sync_path",
        "canonical_sync_branch",
        "canonical_sync_head",
        "canonical_sync_merge_commit",
        "canonical_synced_at",
    )
    for key in keys:
        # Keep previously persisted keys present so the final metadata merge
        # overwrites stale blockers. Popping only clears the local dict; it does
        # not delete/clear existing fields in board metadata. Do not introduce
        # absent fields just to clear them.
        if key in worker:
            worker[key] = None


def _clear_pr_ci_wait(worker: dict[str, Any]) -> None:
    for key in (
        "pr_ci_wait_state",
        "pr_ci_wait_started_at",
        "pr_ci_next_poll_at",
        "pr_ci_wait_seconds",
    ):
        if key in worker:
            worker[key] = None


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


def _changed_files_for_pr_body(root: Path, *, base: str) -> list[str]:
    changed: list[str] = []
    seen: set[str] = set()
    specs = (
        ["git", "diff", "--name-only", f"origin/{base}...HEAD"],
        ["git", "diff", "--name-only", "--cached"],
        ["git", "diff", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    for cmd in specs:
        try:
            result = subprocess.run(cmd, cwd=root, capture_output=True, text=True, timeout=20)
        except Exception:
            continue
        if result.returncode != 0:
            continue
        for line in (result.stdout or "").splitlines():
            path = line.strip()
            if " -> " in path:
                path = path.rsplit(" -> ", 1)[1].strip()
            if path and path not in seen:
                seen.add(path)
                changed.append(path)
    return changed


def _worker_pr_body(worker: dict[str, Any], *, board: Optional[str], changed_files: list[str]) -> str:
    _title, body = _build_worker_pr_copy(worker, board=str(board or ""))
    ok, _message = check_project_state_requirement(body, changed_files)
    if ok:
        return body
    return f"{body}\n\n{_WORKER_PROJECT_STATE_JUSTIFICATION}"


def _ensure_worker_pr_body_hygiene(
    worker: dict[str, Any],
    *,
    root: Path,
    repo: str,
    board: Optional[str],
    changed_files: list[str],
) -> None:
    pr_ref = _pr_ref(worker)
    if not pr_ref:
        return
    current = _run_gh(
        ["pr", "view", pr_ref, "--repo", repo, "--json", "body", "--jq", ".body"],
        root=root,
        timeout=20,
    )
    if current.returncode != 0:
        worker["pr_body_update_error"] = (current.stderr or current.stdout or "gh pr view failed").strip()
        return
    existing_body = current.stdout or ""
    ok, _message = check_project_state_requirement(existing_body, changed_files)
    if ok:
        return
    if _WORKER_PROJECT_STATE_JUSTIFICATION in existing_body:
        return
    updated_body = f"{existing_body.rstrip()}\n\n{_WORKER_PROJECT_STATE_JUSTIFICATION}"
    if not existing_body.strip():
        updated_body = _worker_pr_body(worker, board=board, changed_files=changed_files)
    edited = _run_gh(
        ["pr", "edit", pr_ref, "--repo", repo, "--body", updated_body],
        root=root,
        timeout=60,
    )
    if edited.returncode != 0:
        worker["pr_body_update_error"] = (edited.stderr or edited.stdout or "gh pr edit failed").strip()


def _pr_ref(worker: dict[str, Any]) -> str:
    return str(worker.get("pr_url") or worker.get("pr_number") or "").strip()


def _pr_is_published(worker: dict[str, Any]) -> bool:
    state = str(worker.get("pr_state") or "").strip().upper()
    return state == "OPEN"


def _pr_open_blocker(worker: dict[str, Any]) -> str:
    if worker.get("pr_error"):
        return str(worker.get("pr_error") or "")
    if worker.get("pr_status_error"):
        return str(worker.get("pr_status_error") or "")
    if not worker.get("pr_url"):
        return "PR not opened"
    state = str(worker.get("pr_state") or "").strip().upper()
    if state == "OPEN":
        return ""
    if state and state != "UNKNOWN":
        return f"PR state: {state}"
    return ""


def _merge_policy(worker: dict[str, Any]) -> str:
    return MERGE_POLICY_NEVER


def _pr_open_policy(worker: dict[str, Any]) -> str:
    policy = str(worker.get("pr_open_policy") or PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL).strip().lower()
    return policy if policy in {PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL, PR_OPEN_POLICY_NEVER} else PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL


def _github_pr_amend_context(worker: dict[str, Any]) -> dict[str, Any]:
    context = worker.get("project_context") if isinstance(worker.get("project_context"), dict) else {}
    amend = context.get("github_pr_amend") if isinstance(context.get("github_pr_amend"), dict) else {}
    return dict(amend)


def _pr_amend_requires_head_sha_advance(worker: dict[str, Any]) -> bool:
    amend = _github_pr_amend_context(worker)
    if amend.get("requires_head_sha_advance") is True:
        return True
    source_kind = str(amend.get("source_kind") or "").strip()
    review_state = str(amend.get("review_state") or amend.get("source_state") or "").strip().upper()
    if source_kind == "review_comment":
        return True
    return source_kind == "review" and review_state == "CHANGES_REQUESTED"


def _validate_pr_amend_target(worker: dict[str, Any], *, repo: str, base: str) -> str:
    amend = _github_pr_amend_context(worker)
    if not amend:
        return ""
    head_repo = str(amend.get("head_repo") or "").strip()
    head_ref = str(amend.get("head_ref") or "").strip()
    if head_repo and repo != head_repo:
        return f"PR-amend target mismatch: finalizer repo {repo} is not PR head repo {head_repo}"
    if head_ref and base != head_ref:
        return f"PR-amend target mismatch: finalizer base {base} is not PR head ref {head_ref}"
    upstream_repo = str(amend.get("upstream_repo") or "").strip()
    if upstream_repo and upstream_repo != head_repo and repo == upstream_repo:
        return f"PR-amend target mismatch: refusing to use upstream repo {upstream_repo} for amendment PR lifecycle"
    return ""


def _validate_pr_push_remote(worker: dict[str, Any], *, root: Path, repo: str) -> str:
    origin_repo = github_origin_repo(root)
    if not origin_repo or origin_repo == repo:
        return ""
    amend = _github_pr_amend_context(worker)
    if amend:
        upstream_repo = str(amend.get("upstream_repo") or "").strip()
        head_repo = str(amend.get("head_repo") or "").strip()
        review_context = (
            f" Upstream/base repo {upstream_repo} is source/review context only;"
            if upstream_repo
            else " Upstream/base repo is source/review context only;"
        )
        head_context = f" target repo must be PR head repo {head_repo}." if head_repo else " target repo must be the PR head repo."
        return (
            f"PR-amend checkout origin mismatch: origin repo {origin_repo} is not finalizer target repo {repo}."
            f"{review_context}{head_context} Fix checkout origin before finalization."
        )
    return (
        f"Checkout origin mismatch: origin repo {origin_repo} is not finalizer target repo {repo}. "
        "Fix checkout origin before PR finalization."
    )


def _verify_pr_amend_head_advanced(worker: dict[str, Any], *, root: Path) -> bool:
    if not _pr_amend_requires_head_sha_advance(worker):
        return True
    amend = _github_pr_amend_context(worker)
    upstream_repo = str(amend.get("upstream_repo") or "").strip()
    upstream_pr_number = str(amend.get("upstream_pr_number") or "").strip()
    trigger_sha = str(amend.get("head_sha") or "").strip()
    if not upstream_repo or not upstream_pr_number or not _FULL_SHA_RE.fullmatch(trigger_sha):
        worker["pr_amend_head_advanced"] = False
        worker["pr_blocker"] = "PR-amend completion blocked: missing exact upstream PR head SHA verification context."
        return False

    viewed = _run_gh(
        [
            "pr",
            "view",
            upstream_pr_number,
            "--repo",
            upstream_repo,
            "--json",
            "headRefOid",
            "--jq",
            ".headRefOid",
        ],
        root=root,
        timeout=30,
    )
    if viewed.returncode != 0:
        worker["pr_amend_head_advanced"] = False
        worker["pr_status_error"] = (viewed.stderr or viewed.stdout or "gh pr view failed").strip()
        worker["pr_blocker"] = worker["pr_status_error"]
        return False
    current_sha = (viewed.stdout or "").strip().strip('"')
    worker["pr_amend_upstream_head_sha"] = current_sha
    worker["pr_amend_trigger_head_sha"] = trigger_sha
    if not _FULL_SHA_RE.fullmatch(current_sha):
        worker["pr_amend_head_advanced"] = False
        worker["pr_blocker"] = "PR-amend completion blocked: upstream PR returned an invalid head SHA."
        return False
    advanced = current_sha != trigger_sha
    worker["pr_amend_head_advanced"] = advanced
    if not advanced:
        worker["pr_blocker"] = "PR-amend completion blocked: upstream PR head SHA did not advance from triggering review commit."
    return advanced


def _refresh_pr_status(worker: dict[str, Any], *, root: Path, repo: str) -> None:
    """Refresh only the exact PR publication identity.

    CI, review, mergeability, merge, and canonical-sync state do not gate PR
    publication. Shared closeout observes its own exact-head gates separately.
    """

    pr_ref = _pr_ref(worker)
    if not pr_ref:
        worker["pr_status_error"] = "PR not opened"
        worker["pr_blocker"] = _pr_open_blocker(worker)
        return
    if not worker.get("pr_number") and worker.get("pr_url"):
        number = _pr_number_from_url(str(worker.get("pr_url") or ""))
        if number:
            worker["pr_number"] = number
    viewed = _run_gh(
        [
            "pr",
            "view",
            pr_ref,
            "--repo",
            repo,
            "--json",
            "number,url,state,headRefOid,isDraft",
        ],
        root=root,
        timeout=30,
    )
    if viewed.returncode != 0:
        worker["pr_status_error"] = (
            viewed.stderr or viewed.stdout or "gh pr view failed"
        ).strip()
        worker["pr_blocker"] = _pr_open_blocker(worker)
        return
    try:
        data = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError as exc:
        worker["pr_status_error"] = f"gh pr view returned invalid JSON: {exc}"
        worker["pr_blocker"] = _pr_open_blocker(worker)
        return
    if not isinstance(data, dict):
        worker["pr_status_error"] = "gh pr view returned non-object JSON"
        worker["pr_blocker"] = _pr_open_blocker(worker)
        return

    if data.get("url"):
        worker["pr_url"] = str(data.get("url") or "")
    if data.get("number") is not None:
        worker["pr_number"] = str(data.get("number"))
    worker["pr_ci_head_sha"] = str(data.get("headRefOid") or "").strip()
    worker["pr_state"] = str(data.get("state") or "unknown")
    worker["pr_is_draft"] = bool(data.get("isDraft"))
    worker["pr_checks_status"] = "not required"
    worker["pr_checks_total"] = 0
    worker["pr_checks_failed"] = []
    worker["pr_review_decision"] = "not required"
    worker["pr_status_error"] = ""
    worker["pr_blocker"] = _pr_open_blocker(worker)
    worker["_pr_status_just_refreshed"] = True

def _ensure_pr_open(
    worker: dict[str, Any],
    *,
    root: Path,
    repo: str,
    branch: str,
    base: str,
    board: Optional[str],
    draft: bool = False,
    allow_draft: bool = False,
) -> bool:
    remote_error = github_remote_preflight_error(root, operation="create/finalize PR")
    if remote_error:
        worker["pr_error"] = remote_error
        worker["pr_checks_status"] = "not checked"
        worker["pr_blocker"] = remote_error
        return False
    origin_error = _validate_pr_push_remote(worker, root=root, repo=repo)
    if origin_error:
        worker["pr_error"] = origin_error
        worker["pr_checks_status"] = "not checked"
        worker["pr_blocker"] = origin_error
        return False

    pushed = subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=300,
        env=_github_cli_env(),
    )
    if pushed.returncode != 0:
        worker["pr_error"] = (pushed.stderr or pushed.stdout or "git push failed").strip()
        worker["pr_checks_status"] = "not checked"
        worker["pr_blocker"] = worker["pr_error"]
        return False

    changed_files = _changed_files_for_pr_body(root, base=base)
    existing_url = str(worker.get("pr_url") or "").strip()
    if not existing_url:
        existing = _run_gh(
            [
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
            root=root,
            timeout=20,
        )
        existing_url = (existing.stdout or "").strip()
    if existing_url and existing_url != "null":
        worker["pr_url"] = existing_url
        _ensure_worker_pr_body_hygiene(
            worker,
            root=root,
            repo=repo,
            board=board,
            changed_files=changed_files,
        )
    else:
        title, _body_without_project_state_hygiene = _build_worker_pr_copy(worker, board=str(board or ""))
        body = _worker_pr_body(worker, board=board, changed_files=changed_files)
        create_args = [
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
        ]
        if draft:
            create_args.append("--draft")
        created = _run_gh(
            create_args,
            root=root,
            timeout=60,
        )
        if created.returncode == 0 and (created.stdout or "").strip():
            worker["pr_url"] = created.stdout.strip()
        else:
            worker["pr_error"] = (created.stderr or created.stdout or "gh pr create failed").strip()
            worker.setdefault("pr_checks_status", "not checked")
            worker["pr_blocker"] = _pr_open_blocker(worker)
            return False

    _refresh_pr_status(worker, root=root, repo=repo)
    blocker = _pr_open_blocker(worker)
    if allow_draft and blocker == "PR is draft":
        blocker = ""
    worker["pr_blocker"] = blocker
    return not bool(blocker) and _pr_is_published(worker)


def _kanban_closeout_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        config = load_config()
    except Exception:
        return {}
    closeout = config.get("closeout") if isinstance(config, dict) else None
    return dict(closeout) if isinstance(closeout, dict) else {}


def _kanban_shared_closeout_enabled(config: dict[str, Any]) -> bool:
    surfaces = config.get("surfaces") if isinstance(config.get("surfaces"), dict) else {}
    return surfaces.get("kanban") is True and str(config.get("mode") or "off").lower() != "off"


def _worker_visual_requirement(worker: dict[str, Any]) -> dict[str, Any]:
    """Read only the normalized structured board requirement, never task prose."""
    context = worker.get("project_context")
    sources = [context, worker] if isinstance(context, dict) else [worker]
    for source in sources:
        if isinstance(source, dict) and "visual_qa_requirement" in source:
            return _normalized_visual_requirement(source.get("visual_qa_requirement"))
    return _normalized_visual_requirement(None)


def _trusted_visual_receipts(worker: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect only host-shaped receipts carried in structured metadata."""
    receipts: list[dict[str, Any]] = []
    context = worker.get("project_context")
    for source in (context, worker):
        if not isinstance(source, dict):
            continue
        values = source.get("visual_qa_receipts")
        if isinstance(values, list):
            receipts.extend(item for item in values if isinstance(item, dict))
        runtime = source.get("runtime_breakdown")
        if isinstance(runtime, dict) and isinstance(runtime.get("visual_qa_receipts"), list):
            receipts.extend(
                item
                for item in runtime["visual_qa_receipts"]
                if isinstance(item, dict)
            )
    return receipts


def _trusted_visual_receipt_status(
    worker: dict[str, Any],
    requirement: dict[str, Any],
    head_sha: str,
) -> str:
    try:
        from agent.visual_qa import visual_receipt_completion

        min_order = 0
        context = worker.get("project_context")
        for source in (context, worker):
            if not isinstance(source, dict):
                continue
            try:
                min_order = max(
                    min_order,
                    int(source.get("visual_qa_min_receipt_order") or 0),
                )
            except (TypeError, ValueError):
                continue
        completion = visual_receipt_completion(
            requirement,
            _trusted_visual_receipts(worker),
            min_order=min_order,
        )
    except Exception:
        return ""
    if completion.get("receipt") is None:
        return ""
    status = str(completion.get("status") or "").strip().lower()
    if status not in {"passed", "failed", "blocked", "uncertain"}:
        return ""
    receipt = completion["receipt"]
    receipt_key = ":".join(
        (
            str(receipt.get("requirement_id") or ""),
            str(receipt.get("contract_id") or ""),
            str(receipt.get("order") or 0),
        )
    )
    binding = worker.get("trusted_visual_qa_receipt_binding")
    binding = binding if isinstance(binding, dict) else {}
    if str(binding.get("receipt_key") or "") == receipt_key:
        return (
            status
            if str(binding.get("head_sha") or "").strip().lower() == head_sha
            else ""
        )
    worker["trusted_visual_qa_receipt_binding"] = {
        "receipt_key": receipt_key,
        "head_sha": head_sha,
    }
    return status


def _legacy_worker_closeout_state(
    worker: dict[str, Any],
    *,
    board: str,
    workspace: str,
    repo: str,
    branch: str,
    base: str,
    config: dict[str, Any],
) -> dict[str, Any]:
    """Normalize flattened board metadata into the shared closeout schema."""

    from hermes_cli.trusted_closeout import normalize_closeout_state

    existing = worker.get("closeout") if isinstance(worker.get("closeout"), dict) else {}
    state = normalize_closeout_state(existing)
    state.update(
        {
            "id": str(existing.get("id") or f"kanban:{board}"),
            "source": "kanban",
            "mode": str(config.get("mode") or "shadow").lower(),
        }
    )
    state["workspace"].update(
        {
            "path": workspace,
            "canonical_path": str(worker.get("project_path") or ""),
            "repository": repo,
            "branch": branch,
            "base_branch": base,
        }
    )
    visual_requirement = _worker_visual_requirement(worker)
    require_visual_qa = visual_requirement["level"] != "none"
    preview_config = config.get("preview") if isinstance(config.get("preview"), dict) else {}
    require_preview = (
        str(config.get("mode") or "off").strip().lower() == "enforce"
        and preview_config.get("required") is not False
    )
    state["policy"].update(
        {
            "merge": _merge_policy(worker),
            "pr_open": _pr_open_policy(worker),
            "early_draft_pr": True,
            "require_preview": require_preview,
            "require_local_verification": True,
            "require_review": True,
            "require_visual_qa": require_visual_qa,
            "post_merge_requirements": {},
        }
    )
    pr = state["pr"]
    pr.update(
        {
            "url": str(worker.get("pr_url") or pr.get("url") or ""),
            "number": str(worker.get("pr_number") or pr.get("number") or ""),
            "title": str(worker.get("pr_title") or pr.get("title") or _pr_title_source(worker)),
            "body": str(worker.get("pr_body") or pr.get("body") or _worker_pr_body(worker, board=board, changed_files=[])),
            "state": str(worker.get("pr_state") or pr.get("state") or ""),
            "is_draft": worker.get("pr_is_draft") is True,
            "head_sha": str(worker.get("pr_ci_head_sha") or pr.get("head_sha") or ""),
            # Historical board state can still be inspected, but current
            # publication passes never author or act on merge facts.
            "merge_sha": str(worker.get("pr_merge_commit") or pr.get("merge_sha") or ""),
            "merge_state": str(worker.get("pr_merge_state") or pr.get("merge_state") or "unknown"),
            "mergeable": worker.get("pr_mergeable", pr.get("mergeable", "unknown")),
            "review_decision": str(worker.get("pr_review_decision") or pr.get("review_decision") or "unknown"),
        }
    )
    head_sha = str(pr.get("head_sha") or "")
    if not require_visual_qa:
        state["visual_qa"] = {"status": "not_required"}
    elif head_sha:
        trusted_visual_status = _trusted_visual_receipt_status(
            worker,
            visual_requirement,
            head_sha,
        )
        existing_visual = state.get("visual_qa")
        existing_visual = existing_visual if isinstance(existing_visual, dict) else {}
        existing_visual_head = str(existing_visual.get("head_sha") or "").strip().lower()
        if trusted_visual_status:
            state["visual_qa"] = {
                "status": trusted_visual_status,
                "head_sha": head_sha,
            }
        elif existing_visual_head == head_sha and str(
            existing_visual.get("status") or ""
        ).strip().lower() in {"pending", "passed", "failed", "blocked", "uncertain"}:
            state["visual_qa"] = dict(existing_visual)
        else:
            state["visual_qa"] = {"status": "pending", "head_sha": head_sha}
    else:
        state["visual_qa"] = {"status": "pending"}
    if not existing:
        state["telemetry"].update(
            {
                "green_unmerged_since": worker.get("green_unmerged_since"),
                "green_unmerged_overdue": worker.get("green_unmerged_overdue") is True,
            }
        )
    trusted_local_head = str(worker.get("trusted_local_verification_head") or "").strip().lower()
    review_approved_head = str(worker.get("review_approved_head") or "").strip().lower()
    if head_sha and trusted_local_head == head_sha:
        state["local_verification"] = {"status": "passed", "head_sha": head_sha}
    if head_sha and review_approved_head == head_sha:
        state["review"] = {"status": "approved", "head_sha": head_sha}
    state["ci"].update(
        {
            "head_sha": head_sha,
            "status": str(worker.get("pr_checks_status") or state["ci"].get("status") or "not_checked").replace(" ", "_"),
            "total": int(worker.get("pr_checks_total") or 0),
            "failed": list(worker.get("pr_checks_failed") or []),
            "wait_state": str(worker.get("pr_ci_wait_state") or state["ci"].get("wait_state") or "queued"),
        }
    )
    return normalize_closeout_state(state)


def _dual_write_closeout_to_worker(worker: dict[str, Any], state: dict[str, Any]) -> None:
    """Preserve flattened Kanban compatibility fields beside nested state."""

    pr = state["pr"]
    ci = state["ci"]
    preview = state["preview"]
    worker.update(
        {
            "closeout": state,
            "pr_url": pr.get("url") or "",
            "pr_number": pr.get("number") or "",
            "pr_state": pr.get("state") or "unknown",
            "pr_is_draft": pr.get("is_draft") is True,
            "pr_ci_head_sha": pr.get("head_sha") or ci.get("head_sha") or "",
            "pr_review_decision": pr.get("review_decision") or "unknown",
            "pr_checks_status": ci.get("status") or "not checked",
            "pr_checks_total": int(ci.get("total") or 0),
            "pr_checks_failed": list(ci.get("failed") or []),
            "pr_ci_wait_state": ci.get("wait_state") or "",
            "pr_ci_next_poll_at": state.get("next_due_at") or 0,
            "preview_url": preview.get("url") or "",
            "preview_status": preview.get("status") or "not checked",
            "preview_head_sha": preview.get("observed_sha") or "",
        }
    )
    errors = state.get("errors") or []
    if state.get("status") in {"blocked", "repair_required"} and errors:
        worker["pr_error"] = str(errors[-1].get("message") or "trusted closeout blocked")
        worker["pr_blocker"] = worker["pr_error"]
    elif state.get("status") in {"pr_published", "pr_open", "completed"}:
        worker["pr_error"] = None
        worker["pr_blocker"] = ""


def _reconcile_kanban_closeout(
    worker: dict[str, Any],
    *,
    board: str,
    workspace: str,
    repo: str,
    branch: str,
    base: str,
    config: dict[str, Any],
) -> PRFinalizationOutcome:
    from hermes_cli.trusted_closeout import reconcile_trusted_closeout

    state = _legacy_worker_closeout_state(
        worker,
        board=board,
        workspace=workspace,
        repo=repo,
        branch=branch,
        base=base,
        config=config,
    )

    def run(args: list[str], *, cwd: Path, timeout: int | float = 60, github: bool = False):
        if args and args[0] == "gh":
            return _run_gh(args[1:], root=cwd, timeout=int(timeout))
        return subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=_github_cli_env() if github else None,
        )

    transition = reconcile_trusted_closeout(
        state,
        poll_seconds=float(config.get("poll_seconds") or 30),
        run=run,
    )
    _dual_write_closeout_to_worker(worker, transition.state)
    if transition.state.get("mode") != "enforce":
        # Shadow contributes diagnostics only; the legacy Kanban finalizer keeps
        # ownership of success/failure and all repository mutations.
        return PRFinalizationOutcome.PENDING
    if transition.outcome in {"pr_published", "pr_open", "completed", "not_required"}:
        return PRFinalizationOutcome.PUBLISHED
    if transition.outcome in {"blocked", "repair_required"}:
        return PRFinalizationOutcome.FAILED
    return PRFinalizationOutcome.PENDING


def _ensure_early_draft_pr(board: Optional[str], workspace: str) -> dict[str, Any]:
    """Push one settled Kanban checkpoint and open/refresh its draft PR.

    This dispatcher-only path runs before reviewer creation. It never waits for
    CI or marks review complete; later dev checkpoints push a new exact head and
    leave the prior review evidence stale until the next reviewer approves.
    """

    result: dict[str, Any] = {"status": "disabled", "head_sha": ""}
    if not board:
        return result
    config = _kanban_closeout_config()
    if (
        not _kanban_shared_closeout_enabled(config)
        or str(config.get("mode") or "off").strip().lower() != "enforce"
        or config.get("early_draft_pr") is not True
    ):
        return result
    if str(os.environ.get("HERMES_KANBAN_TASK") or "").strip():
        return {"status": "blocked", "head_sha": "", "diagnostic_code": "role_worker_context"}

    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY
    from hermes_cli.discord_worker_boards import effective_pr_policy_for_worker
    from hermes_cli.discord_worker_boards import _update_worker_meta

    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    worker.update(effective_pr_policy_for_worker(worker))
    if _pr_open_policy(worker) == PR_OPEN_POLICY_NEVER:
        worker["early_draft_status"] = "not_required"
        _update_worker_meta(board, worker)
        return {"status": "not_required", "head_sha": ""}

    root = Path(workspace).expanduser().resolve(strict=False)
    branch = str(worker.get("worker_branch") or "").strip()
    base = str(worker.get("base_branch") or "main").strip() or "main"
    repo = _resolve_github_repo(worker, root) if root.is_dir() else None
    diagnostic = ""
    head_sha = ""
    if not root.is_dir():
        diagnostic = "workspace_unavailable"
    elif not branch:
        diagnostic = "branch_missing"
    elif not repo:
        diagnostic = "repository_missing"
    else:
        try:
            diff_check = subprocess.run(
                ["git", "diff", "--check"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            status = subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            head = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:
            diagnostic = "checkpoint_probe_failed"
        else:
            head_sha = str(head.stdout or "").strip().lower() if head.returncode == 0 else ""
            if diff_check.returncode != 0:
                diagnostic = "checkpoint_diff_invalid"
            elif status.returncode != 0 or bool(str(status.stdout or "").strip()):
                diagnostic = "checkpoint_not_clean"
            elif not _FULL_SHA_RE.fullmatch(head_sha):
                diagnostic = "checkpoint_head_invalid"

    if diagnostic:
        worker.update(
            {
                "early_draft_status": "blocked",
                "early_draft_diagnostic_code": diagnostic,
            }
        )
        _update_worker_meta(board, worker)
        return {"status": "blocked", "head_sha": "", "diagnostic_code": diagnostic}

    assert repo is not None
    already_pushed = str(worker.get("early_draft_pushed_head_sha") or "").strip().lower()
    if already_pushed == head_sha and _pr_ref(worker):
        _refresh_pr_status(worker, root=root, repo=repo)
        observed = str(worker.get("pr_ci_head_sha") or "").strip().lower()
        opened = observed == head_sha and _pr_is_published(worker)
    else:
        opened = _ensure_pr_open(
            worker,
            root=root,
            repo=repo,
            branch=branch,
            base=base,
            board=board,
            draft=True,
            allow_draft=True,
        )
        observed = str(worker.get("pr_ci_head_sha") or "").strip().lower()

    if not opened or observed != head_sha or not _FULL_SHA_RE.fullmatch(observed):
        diagnostic = "early_draft_head_mismatch" if opened else "early_draft_open_failed"
        worker.update(
            {
                "early_draft_status": "blocked",
                "early_draft_diagnostic_code": diagnostic,
            }
        )
        _update_worker_meta(board, worker)
        return {"status": "blocked", "head_sha": head_sha, "diagnostic_code": diagnostic}

    worker.update(
        {
            "early_draft_status": "existing" if already_pushed == head_sha else "opened",
            "early_draft_diagnostic_code": "",
            "early_draft_pushed_head_sha": head_sha,
        }
    )
    state = _legacy_worker_closeout_state(
        worker,
        board=board,
        workspace=str(root),
        repo=repo,
        branch=branch,
        base=base,
        config=config,
    )
    if str(worker.get("trusted_local_verification_head") or "").strip().lower() != head_sha:
        state["local_verification"] = {"status": "pending"}
    if str(worker.get("review_approved_head") or "").strip().lower() == head_sha:
        state["review"] = {"status": "approved", "head_sha": head_sha}
    else:
        state["review"] = {"status": "pending", "head_sha": head_sha}
    _dual_write_closeout_to_worker(worker, state)
    _update_worker_meta(board, worker)
    return {"status": worker["early_draft_status"], "head_sha": head_sha}


def _ensure_pr(board: Optional[str], workspace: str) -> PRFinalizationOutcome:
    """Advance one board PR finalization pass from trusted dispatcher context."""
    role_worker_task = str(os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not board:
        return PRFinalizationOutcome.FAILED if role_worker_task else PRFinalizationOutcome.PUBLISHED
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY
    from hermes_cli.discord_worker_boards import effective_pr_policy_for_worker
    from hermes_cli.discord_worker_boards import _update_worker_meta

    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    if role_worker_task:
        error = (
            "Refusing PR finalization from a Kanban role-worker process; "
            "dispatcher reconciliation must finalize approved reviewer work"
        )
        worker.update(
            {
                "pr_error": error,
                "pr_blocker": error,
                "pr_checks_status": "not checked",
                "pr_finalizer_guard": "role_worker_context",
            }
        )
        _update_worker_meta(board, worker)
        return PRFinalizationOutcome.FAILED
    worker["pr_finalizer_guard"] = ""
    root = Path(workspace)
    branch = str(worker.get("worker_branch") or "").strip()
    base = str(worker.get("base_branch") or "main").strip() or "main"
    repo = _resolve_github_repo(worker, root)
    effective_policy = effective_pr_policy_for_worker(worker)
    worker.update(effective_policy)
    policy = _merge_policy(worker)
    open_policy = _pr_open_policy(worker)
    worker["pr_open_policy"] = open_policy
    worker["merge_policy"] = policy

    # PR-amend target and advancement are publication preconditions. No push,
    # PR creation, or shared-closeout handoff may run until the exact upstream
    # PR head is proven to have advanced.
    amend_preflight_blocked = False
    if repo:
        target_error = _validate_pr_amend_target(worker, repo=repo, base=base)
        if target_error:
            worker["pr_error"] = target_error
            worker["pr_checks_status"] = "not checked"
            worker["pr_blocker"] = target_error
            amend_preflight_blocked = True
    if not amend_preflight_blocked and _pr_amend_requires_head_sha_advance(worker):
        if not _verify_pr_amend_head_advanced(worker, root=root):
            worker.setdefault("pr_checks_status", "not checked")
            amend_preflight_blocked = True

    closeout_config = _kanban_closeout_config()
    if not amend_preflight_blocked and _kanban_shared_closeout_enabled(closeout_config):
        outcome = _reconcile_kanban_closeout(
            worker,
            board=board,
            workspace=str(root),
            repo=str(repo or ""),
            branch=branch,
            base=base,
            config=closeout_config,
        )
        _update_worker_meta(board, worker)
        if str(closeout_config.get("mode") or "shadow").lower() == "enforce":
            return outcome
    outcome = PRFinalizationOutcome.FAILED
    try:
        skip_pr_lifecycle = amend_preflight_blocked
        if open_policy == PR_OPEN_POLICY_NEVER:
            _reset_pr_status_fields(worker)
            worker["pr_skipped_no_changes"] = True
            worker["pr_state"] = "not_needed"
            worker["pr_checks_status"] = "passed"
            worker["pr_checks_total"] = 0
            worker["pr_checks_failed"] = []
            worker["pr_merge_skipped"] = True
            worker["pr_merge_skipped_reason"] = "pr_open_policy_never"
            worker["pr_blocker"] = ""
            skip_pr_lifecycle = True
            outcome = PRFinalizationOutcome.PUBLISHED
        if not skip_pr_lifecycle:
            missing = []
            if not repo:
                missing.append("GitHub repository")
            if not branch:
                missing.append("worker branch")
            if missing:
                worker["pr_error"] = f"Cannot create PR: missing {', '.join(missing)}"
                worker["pr_checks_status"] = "not checked"
                worker["pr_blocker"] = worker["pr_error"]
                outcome = PRFinalizationOutcome.FAILED
                skip_pr_lifecycle = True
            if not skip_pr_lifecycle:
                assert repo is not None
            if not skip_pr_lifecycle and _pr_ref(worker):
                _reset_pr_status_fields(worker)
                _refresh_pr_status(worker, root=root, repo=repo)
                worker["pr_merge_skipped"] = True
                worker["pr_merge_skipped_reason"] = MERGE_POLICY_NEVER
                worker["pr_blocker"] = _pr_open_blocker(worker)
                outcome = (
                    PRFinalizationOutcome.PUBLISHED
                    if not worker.get("pr_blocker") and _pr_is_published(worker)
                    else PRFinalizationOutcome.FAILED
                )
                skip_pr_lifecycle = True
            if not skip_pr_lifecycle:
                _reset_pr_status_fields(worker)
                has_commits = _branch_has_commits(root, base=base, branch=branch)
                if _is_foreman_generated_worker(worker) and has_commits is False:
                    worker["pr_skipped_no_changes"] = True
                    worker["pr_state"] = "not_needed"
                    worker["pr_checks_status"] = "passed"
                    worker["pr_checks_total"] = 0
                    worker["pr_checks_failed"] = []
                    worker["pr_blocker"] = ""
                    outcome = PRFinalizationOutcome.PUBLISHED
                else:
                    opened = _ensure_pr_open(
                        worker,
                        root=root,
                        repo=repo,
                        branch=branch,
                        base=base,
                        board=board,
                        draft=True,
                        allow_draft=True,
                    )
                    if opened:
                        worker["pr_merge_skipped"] = True
                        worker["pr_merge_skipped_reason"] = MERGE_POLICY_NEVER
                        worker["pr_blocker"] = _pr_open_blocker(worker)
                        outcome = (
                            PRFinalizationOutcome.PUBLISHED
                            if not worker.get("pr_blocker") and _pr_is_published(worker)
                            else PRFinalizationOutcome.FAILED
                        )
                    elif not worker.get("pr_skipped_no_changes"):
                        worker.setdefault("pr_checks_status", "not checked")
                        worker["pr_blocker"] = _pr_open_blocker(worker)
                        outcome = PRFinalizationOutcome.FAILED
    except Exception as exc:
        worker.setdefault("pr_error", str(exc))
        worker.setdefault("pr_checks_status", "not checked")
        worker["pr_blocker"] = _pr_open_blocker(worker)
        outcome = PRFinalizationOutcome.FAILED
    _clear_pr_ci_wait(worker)
    _update_worker_meta(
        board,
        {
            key: worker.get(key)
            for key in (
                "pr_open_policy",
                "merge_policy",
                "pr_skipped_no_changes",
                "pr_state",
                "pr_checks_status",
                "pr_checks_total",
                "pr_checks_failed",
                "pr_ci_wait_state",
                "pr_ci_wait_started_at",
                "pr_ci_next_poll_at",
                "pr_ci_wait_seconds",
                "pr_ci_head_sha",
                "pr_merge_state",
                "pr_mergeable",
                "pr_merge_skipped",
                "pr_merge_skipped_reason",
                "pr_blocker",
                "pr_error",
                "pr_url",
                "pr_number",
                "pr_status_error",
                "pr_merged_at",
                "pr_merge_commit",
                "pr_is_draft",
                "pr_review_decision",
                "canonical_sync_state",
                "canonical_sync_error",
                "canonical_sync_path",
                "canonical_sync_branch",
                "canonical_sync_head",
                "canonical_sync_merge_commit",
                "canonical_synced_at",
                "pr_amend_head_advanced",
                "pr_amend_upstream_head_sha",
                "pr_amend_trigger_head_sha",
                "pr_finalizer_guard",
            )
            if key in worker
        },
    )
    return outcome


def _merge_criteria(board: Optional[str], criteria: list[str]) -> None:
    if not board or not criteria:
        return
    from hermes_cli.discord_worker_boards import _update_worker_meta

    canonical: list[dict[str, Any]] = []
    seen: set[str] = set()
    for text in criteria:
        normalized = str(text or "").strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        canonical.append({"text": normalized, "active": True})
        seen.add(key)
    if not canonical:
        return
    _update_worker_meta(board, {"criteria": canonical, "criteria_source": "planner"})


def _update_phase(board: Optional[str], phase: str, *, goal_status: str) -> None:
    if not board:
        return
    from hermes_cli.discord_worker_boards import _read_worker_meta, _update_worker_meta

    worker = _read_worker_meta(board)
    if worker.get("cancelled") or worker.get("goal_status") == "cancelled":
        return
    updates: dict[str, Any] = {"phase": phase, "goal_status": goal_status, "updated_at": int(time.time())}
    if goal_status in {"done", "blocked"}:
        updates["terminal_reaction_sync_pending"] = True
        updates["terminal_summary_sync_pending"] = True
    _update_worker_meta(board, updates)
    if goal_status in {"done", "blocked"}:
        try:
            from hermes_cli.discord_worker_boards import persist_board_run_summary

            persist_board_run_summary(board)
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
