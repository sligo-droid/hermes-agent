"""Run one Kanban planner/dev/reviewer task through a coding worker."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

from agent.transports.codex_app_server_session import CodexAppServerSession
from hermes_cli import kanban_db
from hermes_cli.discord_worker_boards import (
    DEV_TICKET_BODY_GUIDANCE,
    DISCORD_WORKER_META_KEY,
    MERGE_POLICY_AUTO,
    MERGE_POLICY_MANUAL,
    MERGE_POLICY_NEVER,
    PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL,
    PR_OPEN_POLICY_NEVER,
    ROLE_DEV,
    ROLE_FOREMAN,
    ROLE_PLANNER,
    ROLE_REVIEWER,
    VALID_MERGE_POLICIES,
    _format_plan_artifact_markdown,
    _normalize_discord_plan_artifacts,
    active_dev_round_for_board,
    format_role_round_title,
    is_cancelled,
    mark_dispatch_dirty,
    record_codex_worker_event,
    record_codex_worker_result,
)
from hermes_cli.github_remote import (
    github_cli_env,
    github_remote_preflight_error,
    github_repo_from_url,
    github_repo_from_value,
)
from hermes_cli.pr_body_format import check_project_state_requirement
from hermes_cli.ui_work_routing import (
    UIWorkRouteDecision,
    codex_ui_work_extra_args,
    resolve_ui_work_route,
)
from hermes_cli.worker_autoreview import materialize_autoreview_helper

_OPENCODE_ROLES = {ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER}
_CODEX_AUTH_RETRY_LIMIT = 2
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
            blocked = kanban_db.block_task(conn, task_id, reason=reason)
            if blocked:
                return 0
        except Exception:
            pass
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
        conn.close()
        if board:
            try:
                mark_dispatch_dirty(board=board, reason=f"{role}-worker-finished")
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
    pr_policy = _pr_policy_prompt_note(role)
    autoreview = _dev_autoreview_prompt(role)
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
        f"{autoreview}"
        f"{forced_skills}"
        f"{schema}\n\n"
        f"Git context:\n{git}\n\n"
        f"Kanban context:\n{context}"
    )


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

    rendered: list[str] = ["Force-loaded implementation skills for this dev worker:"]
    for name in names:
        message = _render_worker_skill(name, task_id=task_id)
        if message:
            rendered.append(message)
        else:
            rendered.append(
                f"[Skill load warning: requested skill `{name}` did not resolve in this worker environment. "
                "Continue only with the task context and record the missing skill in handoff.notes.]"
            )
    return "\n\n".join(rendered).rstrip() + "\n\n"


def _render_worker_skill(name: str, *, task_id: str) -> str:
    try:
        from agent.skill_commands import _build_skill_message, _load_skill_payload

        loaded = _load_skill_payload(name, task_id=task_id)
        if not loaded:
            return ""
        payload, skill_dir, display_name = loaded
        return _build_skill_message(
            payload,
            skill_dir,
            activation_note=(
                f"Skill `{display_name}` is loaded for this Kanban dev worker. "
                "Follow it for implementation style, pitfalls, and verification."
            ),
            runtime_note="Loaded by hermes_cli.kanban_codex_worker from task/board worker_skill_hints.",
            session_id=task_id,
        )
    except Exception:
        return ""


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


def _role_outcome_frame(role: str) -> str:
    if role == ROLE_PLANNER:
        return (
            "Outcome frame:\n"
            "Goal: Convert the Kanban context into the smallest coherent implementation plan for dev workers.\n"
            "Success means:\n"
            "- The JSON status is planned or blocked.\n"
            "- The board-level acceptance_criteria list is deduplicated and canonical.\n"
            "- Each dev ticket body opens with Goal, Success means, and Stop when, then gives the worker scope, files, dependencies, verification, and out-of-scope boundaries.\n"
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
            "- PR lifecycle chores are excluded from new dev tasks; the deterministic finalizer owns push/open/merge after approval.\n"
            "- Live pickup, deployment, active-path, and provenance gaps are treated as real implementation/closeout gaps, not PR lifecycle chores.\n"
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
        "- changed_files, tests, handoff, pr_ready, and blocker reflect the actual repository state.\n"
        "- Remote push and PR lifecycle work are not attempted by this role.\n"
        "Stop when: Return the JSON completion, checkpoint, or blocker object."
    )


def _pr_policy_prompt_note(role: str) -> str:
    if role == ROLE_PLANNER:
        return (
            "PR lifecycle policy: Dev workers must not open pull requests, push to remote branches, wait on remote checks, or merge. "
            "Plan tickets only for local implementation/verification; Hermes opens/syncs/merges the PR after reviewer approval according to board merge_policy.\n"
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
        "Complete local implementation and verification only; Hermes performs final push/open/merge after reviewer approval.\n"
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
        '"smoke_routes":["..."],"known_warnings":["..."],"notes":"..."},"blocker":null,"pr_ready":false} '
        "Always include handoff so reviewers can audit the exact changed files, checks, autoreview closeout command/result or unavailable reason, preview URL/command, smoke routes, warnings, and notes. "
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
        return "codex"


def _backend_label(role: str, task: Any = None) -> str:
    if _role_uses_opencode(role, task):
        return "OpenCode"
    return "Codex"


def _task_forces_opencode(task: Any = None) -> bool:
    """Return whether a role task must bypass the configured Codex backend.

    No current task type forces OpenCode. Command Center repair work follows the
    same configured coding-worker backend as every other role lane.
    """
    return False


def _role_uses_opencode(role: str, task: Any = None) -> bool:
    if _task_forces_opencode(task):
        return True
    return role in _OPENCODE_ROLES and _configured_backend() == "opencode"


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
    text = "\n".join(
        str(part or "")
        for part in (
            getattr(task, "title", ""),
            getattr(task, "body", ""),
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
        "HERMES_UI_WORK_SELECTED_PROVIDER": metadata.get("selected_provider"),
        "HERMES_UI_WORK_SELECTED_MODEL": metadata.get("selected_model"),
        "HERMES_UI_WORK_FALLBACK_USED": str(bool(metadata.get("fallback_used"))).lower(),
    }
    return {key: str(value) for key, value in values.items() if value not in (None, "")}


def _ui_work_route_provider_env(decision: UIWorkRouteDecision | None) -> dict[str, str]:
    """Return intentionally scoped provider credentials for a selected UI route."""
    if decision is None or not decision.matched or not decision.enabled:
        return {}
    provider = str(decision.selected_provider or decision.provider or "").strip().lower()
    if provider != "openrouter":
        return {}
    try:
        from hermes_cli.config import get_env_value
        from tools.environments.local import _HERMES_PROVIDER_ENV_FORCE_PREFIX
    except Exception:
        return {}
    value = str(get_env_value("OPENROUTER_API_KEY") or "").strip()
    if not value:
        return {}
    return {f"{_HERMES_PROVIDER_ENV_FORCE_PREFIX}OPENROUTER_API_KEY": value}


def _ui_work_route_prompt(decision: UIWorkRouteDecision | None) -> str:
    if decision is None:
        return ""
    metadata = decision.metadata()
    return (
        "UI specialist route metadata for this worker launch:\n"
        f"- selected_route: {metadata.get('selected_route') or ''}\n"
        f"- selected_provider: {metadata.get('selected_provider') or ''}\n"
        f"- selected_model: {metadata.get('selected_model') or ''}\n"
        f"- route_decision_source: {metadata.get('route_decision_source') or ''}\n"
        f"- route_decision_rationale: {metadata.get('route_decision_rationale') or ''}\n"
        f"- advisory_reason: {metadata.get('advisory_reason') or ''}\n"
        "This is structured launch evidence; include it in route-smoke verification instead of re-inferring the route from prose.\n"
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


def _attach_ui_work_route(result: Any, decision: UIWorkRouteDecision | None) -> None:
    if decision is None:
        return
    try:
        setattr(result, "ui_work_route", decision.metadata())
    except Exception:
        pass


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
    if ui_work_route is not None:
        extra_args.extend(codex_ui_work_extra_args(ui_work_route))

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
        **_ui_work_route_provider_env(ui_work_route),
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
                _attach_ui_work_route(result, ui_work_route)
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
        **_ui_work_route_provider_env(ui_work_route),
    }
    old_env = {key: os.environ.get(key) for key in runtime_env}
    os.environ.update(runtime_env)
    try:
        if role == ROLE_PLANNER:
            cfg = load_opencode_config()
            result = run_opencode_single_pass(
                prompt,
                workspace,
                timeout=_role_timeout(role),
                agent=cfg["plan_agent"],
                reasoning_level=cfg["complex_plan_reasoning_level"],
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
    _attach_ui_work_route(result, ui_work_route)
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
    if role in {ROLE_PLANNER, ROLE_REVIEWER, ROLE_FOREMAN}:
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
                summary=summary or "Reviewer found only PR lifecycle follow-up; finalizing PR.",
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
    _checkpoint_commit(workspace, task_id, summary)
    completed = _complete_role_task(
        conn,
        task_id,
        summary=summary or "Dev task completed.",
        metadata={
            "changed_files": _string_list(payload.get("changed_files")),
            "tests": payload.get("tests") if isinstance(payload.get("tests"), list) else [],
            "handoff": payload.get("handoff") if isinstance(payload.get("handoff"), dict) else {},
            "pr_ready": bool(payload.get("pr_ready")),
            "raw": payload,
        },
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
        "canonical_sync_state",
        "canonical_sync_error",
        "canonical_sync_path",
        "canonical_sync_branch",
        "canonical_sync_head",
        "canonical_sync_merge_commit",
        "canonical_synced_at",
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


def _check_rollup_item_name(item: dict[str, Any]) -> str:
    return str(
        item.get("name")
        or item.get("context")
        or item.get("workflowName")
        or item.get("__typename")
        or "check"
    )


def _check_rollup_item_identity(item: dict[str, Any]) -> tuple[str, str, str]:
    """Stable identity for one logical PR check across reruns.

    GitHub's statusCheckRollup can include stale attempts for the same
    workflow/context. Counting every historical CANCELLED/FAILURE entry keeps
    an otherwise green PR blocked after a rerun succeeds. Group by the visible
    check identity and summarize only the newest item in each group.
    """

    raw_app = item.get("app")
    app_name = str(raw_app.get("name") or "") if isinstance(raw_app, dict) else ""
    return (
        str(item.get("workflowName") or ""),
        _check_rollup_item_name(item),
        app_name,
    )


def _check_rollup_item_sort_key(item: dict[str, Any], index: int) -> tuple[str, str, str, int]:
    timestamp = str(
        item.get("completedAt")
        or item.get("startedAt")
        or item.get("updatedAt")
        or item.get("createdAt")
        or ""
    )
    numeric = item.get("databaseId") or item.get("runNumber") or item.get("id") or 0
    numeric_text = str(numeric)
    return (timestamp, numeric_text, str(item.get("url") or item.get("detailsUrl") or ""), index)


def _latest_check_rollup_items(items: list[Any]) -> list[dict[str, Any]]:
    latest: dict[tuple[str, str, str], tuple[tuple[str, str, str, int], dict[str, Any]]] = {}
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        identity = _check_rollup_item_identity(item)
        sort_key = _check_rollup_item_sort_key(item, index)
        if identity not in latest or sort_key > latest[identity][0]:
            latest[identity] = (sort_key, item)
    return [value[1] for value in latest.values()]


def _check_rollup_summary(items: Any) -> tuple[str, int, list[str]]:
    if not isinstance(items, list) or not items:
        return "not checked", 0, []
    failed: list[str] = []
    pending = 0
    total = 0
    for item in _latest_check_rollup_items(items):
        total += 1
        name = _check_rollup_item_name(item)
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


def _pr_is_open_or_merged(worker: dict[str, Any]) -> bool:
    state = str(worker.get("pr_state") or "").strip().upper()
    return state in {"OPEN", "MERGED"} or bool(worker.get("pr_merged_at"))


def _pr_open_blocker(worker: dict[str, Any]) -> str:
    if worker.get("pr_error"):
        return str(worker.get("pr_error") or "")
    if worker.get("pr_status_error"):
        return str(worker.get("pr_status_error") or "")
    if not worker.get("pr_url"):
        return "PR not opened"
    state = str(worker.get("pr_state") or "").strip().upper()
    if state in {"OPEN", "MERGED"}:
        if worker.get("pr_is_draft") is True:
            return "PR is draft"
        return ""
    if state and state != "UNKNOWN":
        return f"PR state: {state}"
    return ""


def _merge_policy(worker: dict[str, Any]) -> str:
    policy = str(worker.get("merge_policy") or MERGE_POLICY_AUTO).strip().lower()
    return policy if policy in VALID_MERGE_POLICIES else MERGE_POLICY_AUTO


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


def _verify_pr_amend_head_advanced(worker: dict[str, Any], *, root: Path) -> bool:
    if not _pr_amend_requires_head_sha_advance(worker):
        return True
    amend = _github_pr_amend_context(worker)
    upstream_repo = str(amend.get("upstream_repo") or "").strip()
    upstream_pr_number = str(amend.get("upstream_pr_number") or "").strip()
    trigger_sha = str(amend.get("head_sha") or "").strip()
    if not upstream_repo or not upstream_pr_number or not trigger_sha:
        worker["pr_amend_head_advanced"] = False
        worker["pr_blocker"] = "PR-amend completion blocked: missing upstream PR head SHA verification context."
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
    advanced = bool(current_sha and current_sha != trigger_sha)
    worker["pr_amend_head_advanced"] = advanced
    if not advanced:
        worker["pr_blocker"] = "PR-amend completion blocked: upstream PR head SHA did not advance from triggering review commit."
    return advanced


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
    viewed = _run_gh(
        [
            "pr",
            "view",
            pr_ref,
            "--repo",
            repo,
            "--json",
            "number,url,state,mergedAt,mergeCommit,mergeStateStatus,mergeable,isDraft,reviewDecision,statusCheckRollup",
        ],
        root=root,
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


def _ensure_pr_open(
    worker: dict[str, Any],
    *,
    root: Path,
    repo: str,
    branch: str,
    base: str,
    board: Optional[str],
) -> bool:
    remote_error = github_remote_preflight_error(root, operation="create/finalize PR")
    if remote_error:
        worker["pr_error"] = remote_error
        worker["pr_checks_status"] = "not checked"
        worker["pr_merge_state"] = "unknown"
        worker["pr_blocker"] = remote_error
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
        worker["pr_merge_state"] = "unknown"
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
        created = _run_gh(
            [
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
            root=root,
            timeout=60,
        )
        if created.returncode == 0 and (created.stdout or "").strip():
            worker["pr_url"] = created.stdout.strip()
        else:
            worker["pr_error"] = (created.stderr or created.stdout or "gh pr create failed").strip()
            worker.setdefault("pr_checks_status", "not checked")
            worker.setdefault("pr_merge_state", "unknown")
            worker["pr_blocker"] = _pr_open_blocker(worker)
            return False

    _refresh_pr_status(worker, root=root, repo=repo)
    worker["pr_blocker"] = _pr_open_blocker(worker)
    return not bool(worker.get("pr_blocker")) and _pr_is_open_or_merged(worker)


def _ensure_pr_merged(worker: dict[str, Any], *, root: Path, repo: str) -> bool:
    pr_ref = _pr_ref(worker)
    if not pr_ref:
        worker.setdefault("pr_checks_status", "not checked")
        worker.setdefault("pr_merge_state", "unknown")
        worker["pr_blocker"] = _pr_blocker(worker)
        return False

    deadline = time.monotonic() + _pr_merge_wait_seconds()
    poll_seconds = _pr_merge_poll_seconds()
    first = True
    while True:
        if not first or not worker.get("pr_state"):
            _refresh_pr_status(worker, root=root, repo=repo)
        first = False
        if _pr_is_merged(worker):
            worker["pr_blocker"] = ""
            return True

        blocker = _pr_blocker(worker)
        if not blocker:
            merged = _run_gh(
                ["pr", "merge", pr_ref, "--repo", repo, "--merge", "--delete-branch"],
                root=root,
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

        if blocker == "merge state: UNSTABLE" and str(worker.get("pr_checks_status") or "").strip().lower() == "passed":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                worker["pr_blocker"] = blocker
                return False
            time.sleep(min(poll_seconds, remaining))
            continue
        if blocker not in {"checks pending", "checks not checked"}:
            worker["pr_blocker"] = blocker
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            worker["pr_blocker"] = blocker
            return False
        time.sleep(min(poll_seconds, remaining))


def _sync_canonical_checkout_after_merge(worker: dict[str, Any], *, branch: str) -> bool:
    raw_project_path = str(worker.get("project_path") or "").strip()
    project_path = Path(raw_project_path)
    merge_commit = str(worker.get("pr_merge_commit") or "").strip()
    if not raw_project_path or not project_path.is_dir():
        worker["canonical_sync_state"] = "blocked"
        worker["canonical_sync_error"] = f"Canonical checkout missing or invalid: {raw_project_path or '(missing)'}"
        worker["pr_error"] = worker["canonical_sync_error"]
        worker["pr_blocker"] = worker["pr_error"]
        return False

    def run_git(
        args: list[str],
        *,
        timeout: int = 60,
        cwd: Optional[Path] = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=cwd or project_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_github_cli_env(),
        )

    def fail(
        message: str,
        result: Optional[subprocess.CompletedProcess[str]] = None,
        *,
        sync_path: Optional[Path] = None,
    ) -> bool:
        detail = ""
        if result is not None:
            detail = (result.stderr or result.stdout or "").strip()
        effective_path = sync_path or project_path
        worker["canonical_sync_state"] = "blocked"
        worker["canonical_sync_path"] = str(effective_path)
        worker["canonical_sync_branch"] = branch
        worker["canonical_sync_merge_commit"] = merge_commit
        worker["pr_error"] = f"{message}: {detail}" if detail else message
        worker["canonical_sync_error"] = worker["pr_error"]
        worker["pr_blocker"] = worker["pr_error"]
        return False

    def find_existing_branch_worktree() -> Optional[Path]:
        try:
            listed = run_git(["worktree", "list", "--porcelain"], timeout=20)
        except Exception:
            return None
        if listed.returncode != 0:
            return None
        current_path: Optional[Path] = None
        wanted_ref = f"refs/heads/{branch}"
        for raw_line in (listed.stdout or "").splitlines():
            line = raw_line.strip()
            if line.startswith("worktree "):
                current_path = Path(line.split(" ", 1)[1])
                continue
            if line.startswith("branch ") and line.split(" ", 1)[1] == wanted_ref:
                if current_path and current_path.is_dir():
                    return current_path
        return None

    def verify_synced_checkout(sync_path: Path, *, state: str) -> bool:
        try:
            head = run_git(["rev-parse", "HEAD"], timeout=20, cwd=sync_path)
        except Exception as exc:
            return fail(f"Canonical checkout HEAD lookup failed: {exc}", sync_path=sync_path)
        if head.returncode != 0:
            return fail("Canonical checkout HEAD lookup failed", head, sync_path=sync_path)
        canonical_head = (head.stdout or "").strip()

        if merge_commit:
            try:
                ancestor = run_git(
                    ["merge-base", "--is-ancestor", merge_commit, "HEAD"],
                    timeout=20,
                    cwd=sync_path,
                )
            except Exception as exc:
                return fail(
                    f"Canonical checkout merge commit verification failed: {exc}",
                    sync_path=sync_path,
                )
            if ancestor.returncode != 0:
                return fail(
                    "Canonical checkout does not contain PR merge commit",
                    ancestor,
                    sync_path=sync_path,
                )

        worker["canonical_sync_state"] = state
        worker["canonical_sync_error"] = ""
        worker["canonical_sync_path"] = str(sync_path)
        worker["canonical_sync_branch"] = branch
        worker["canonical_sync_head"] = canonical_head
        worker["canonical_sync_merge_commit"] = merge_commit
        worker["canonical_synced_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return True

    def sync_existing_branch_worktree(sync_path: Path) -> bool:
        try:
            status = run_git(["status", "--porcelain"], timeout=20, cwd=sync_path)
        except Exception as exc:
            return fail(f"Existing branch worktree status failed: {exc}", sync_path=sync_path)
        if status.returncode != 0:
            return fail("Existing branch worktree status failed", status, sync_path=sync_path)
        if (status.stdout or "").strip():
            return fail("Existing branch worktree is dirty", sync_path=sync_path)

        for args, message, timeout in (
            (["fetch", "origin", "--prune"], "Existing branch worktree fetch failed", 120),
            (["pull", "--ff-only", "origin", branch], "Existing branch worktree fast-forward pull failed", 120),
        ):
            try:
                result = run_git(args, timeout=timeout, cwd=sync_path)
            except Exception as exc:
                return fail(f"{message}: {exc}", sync_path=sync_path)
            if result.returncode != 0:
                return fail(message, result, sync_path=sync_path)
        return verify_synced_checkout(sync_path, state="synced_existing_worktree")

    try:
        status = run_git(["status", "--porcelain"], timeout=20)
    except Exception as exc:
        return fail(f"Canonical checkout status failed: {exc}")
    if status.returncode != 0:
        return fail("Canonical checkout status failed", status)
    if (status.stdout or "").strip():
        return fail("Canonical checkout is dirty")

    for args, message, timeout in (
        (["fetch", "origin", "--prune"], "Canonical checkout fetch failed", 120),
        (["checkout", branch], "Canonical checkout branch checkout failed", 60),
        (["pull", "--ff-only", "origin", branch], "Canonical checkout fast-forward pull failed", 120),
    ):
        try:
            result = run_git(args, timeout=timeout)
        except Exception as exc:
            return fail(f"{message}: {exc}")
        if result.returncode != 0:
            if args[:1] == ["checkout"]:
                existing = find_existing_branch_worktree()
                if existing is not None:
                    return sync_existing_branch_worktree(existing)
            return fail(message, result)

    return verify_synced_checkout(project_path, state="synced")


def _ensure_pr(board: Optional[str], workspace: str) -> bool:
    if not board:
        return True
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY
    from hermes_cli.discord_worker_boards import effective_pr_policy_for_worker
    from hermes_cli.discord_worker_boards import _update_worker_meta

    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
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
    _reset_pr_status_fields(worker)
    try:
        skip_pr_lifecycle = False
        if open_policy == PR_OPEN_POLICY_NEVER:
            worker["pr_skipped_no_changes"] = True
            worker["pr_state"] = "not_needed"
            worker["pr_checks_status"] = "passed"
            worker["pr_checks_total"] = 0
            worker["pr_checks_failed"] = []
            worker["pr_merge_state"] = "not_needed"
            worker["pr_mergeable"] = "not_needed"
            worker["pr_merge_skipped"] = True
            worker["pr_merge_skipped_reason"] = "pr_open_policy_never"
            worker["pr_blocker"] = ""
            skip_pr_lifecycle = True
        if not skip_pr_lifecycle:
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
            assert repo is not None
            target_error = _validate_pr_amend_target(worker, repo=repo, base=base)
            if target_error:
                worker["pr_error"] = target_error
                worker["pr_checks_status"] = "not checked"
                worker["pr_merge_state"] = "unknown"
                worker["pr_blocker"] = target_error
                raise RuntimeError(target_error)
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
                opened = _ensure_pr_open(
                    worker,
                    root=root,
                    repo=repo,
                    branch=branch,
                    base=base,
                    board=board,
                )
                if opened and policy == MERGE_POLICY_AUTO:
                    if _ensure_pr_merged(worker, root=root, repo=repo):
                        _sync_canonical_checkout_after_merge(worker, branch=base)
                elif opened and policy in {MERGE_POLICY_MANUAL, MERGE_POLICY_NEVER}:
                    worker["pr_merge_skipped"] = True
                    worker["pr_merge_skipped_reason"] = policy
                    worker["pr_blocker"] = _pr_open_blocker(worker)
                elif not worker.get("pr_skipped_no_changes"):
                    worker.setdefault("pr_checks_status", "not checked")
                    worker.setdefault("pr_merge_state", "unknown")
                    worker["pr_blocker"] = _pr_open_blocker(worker)
        if not worker.get("pr_blocker") and not _verify_pr_amend_head_advanced(worker, root=root):
            worker.setdefault("pr_checks_status", "not checked")
            worker.setdefault("pr_merge_state", "unknown")
    except Exception as exc:
        worker.setdefault("pr_error", str(exc))
        worker.setdefault("pr_checks_status", "not checked")
        worker.setdefault("pr_merge_state", "unknown")
        worker["pr_blocker"] = _pr_blocker(worker)
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
            )
            if key in worker
        },
    )
    if worker.get("pr_blocker"):
        return False
    if worker.get("pr_skipped_no_changes"):
        return not bool(worker.get("pr_error"))
    if policy == MERGE_POLICY_AUTO:
        return bool(worker.get("pr_url")) and _pr_is_merged(worker) and not bool(worker.get("pr_error"))
    return bool(worker.get("pr_url")) and _pr_is_open_or_merged(worker) and not bool(_pr_open_blocker(worker))


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
