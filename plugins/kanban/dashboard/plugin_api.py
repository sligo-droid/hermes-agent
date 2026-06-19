"""Kanban dashboard plugin — backend API routes.

Mounted at /api/plugins/kanban/ by the dashboard plugin system.

This layer is intentionally thin: every handler is a small wrapper around
``hermes_cli.kanban_db`` or a direct SQL query. Writes use the same code
paths the CLI and gateway ``/kanban`` command use, so the three surfaces
cannot drift.

Live updates arrive via the ``/events`` WebSocket, which tails the
append-only ``task_events`` table on a short poll interval (WAL mode lets
reads run alongside the dispatcher's IMMEDIATE write transactions).

Security note
-------------
Plugin HTTP routes go through the dashboard's session-token auth middleware
(``web_server.auth_middleware``) just like core API routes — every
``/api/plugins/...`` request must present the session bearer token (or the
session cookie set when you load the dashboard HTML). The token is the
random per-process ``_SESSION_TOKEN`` printed at startup; the dashboard's
own pages inject it via ``window.__HERMES_SESSION_TOKEN__`` so logged-in
browsers don't have to handle it manually.

For the ``/events`` WebSocket we still require the session token as a
``?token=`` query parameter (browsers cannot set the ``Authorization``
header on an upgrade request), matching the established pattern used by
the in-browser PTY bridge in ``hermes_cli/web_server.py``.

This means ``hermes dashboard --host 0.0.0.0`` is safe to run on a LAN:
plugin routes are no longer an unauthenticated exception. The auth still
isn't multi-user — anyone who can read the printed URL+token gets full
dashboard access — but they can't ride along just because they can reach
the port.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect, status as http_status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from hermes_cli import command_center, command_center_annotations, kanban_db
from hermes_cli.discord_worker_roles import DISCORD_WORKER_META_KEY, ROLE_FOREMAN, ROLE_PLANNER
from hermes_cli import kanban_diagnostics as kd
from self_improvement import discord_publish, proposal_storage

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Auth helper — WebSocket only (HTTP routes live behind the dashboard's
# existing plugin-bypass; this is documented above).
# ---------------------------------------------------------------------------

def _check_ws_token(provided: Optional[str]) -> bool:
    """Constant-time compare against the dashboard session token.

    Imported lazily so the plugin still loads in test contexts where the
    dashboard web_server module isn't importable (e.g. the bare-FastAPI
    test harness).
    """
    if not provided:
        return False
    try:
        from hermes_cli import web_server as _ws
    except Exception:
        # No dashboard context (tests). Accept so the tail loop is still
        # testable; in production the dashboard module always imports
        # cleanly because it's the caller.
        return True
    expected = getattr(_ws, "_SESSION_TOKEN", None)
    if not expected:
        return True
    return hmac.compare_digest(str(provided), str(expected))


def _resolve_board(board: Optional[str]) -> Optional[str]:
    """Validate and normalise a board slug from a query param.

    Raises :class:`HTTPException` 400 on malformed slugs so the browser
    sees a clean error instead of a 500. Returns the normalised slug,
    or ``None`` when the caller omitted the param (which then falls
    through to the active board inside ``kb.connect()``).
    """
    if board is None or board == "":
        return None
    try:
        normed = kanban_db._normalize_board_slug(board)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if normed and normed != kanban_db.DEFAULT_BOARD and not kanban_db.board_exists(normed):
        raise HTTPException(
            status_code=404,
            detail=f"board {normed!r} does not exist",
        )
    return normed


def _conn(board: Optional[str] = None):
    """Open a kanban_db connection, creating the schema on first use.

    Every handler that mutates the DB goes through this. ``kanban_db.connect``
    is the dashboard source of truth for first-use schema initialization and
    corruption checks, so callers do not run a second explicit ``init_db`` pass.

    ``board`` is the query-param slug (already normalised by
    :func:`_resolve_board`). When ``None`` the active board is used
    via the resolution chain (env var → ``current`` file → ``default``).
    """
    _raise_if_corrupt_quarantined(board)
    return kanban_db.connect(board=board)


def _corrupt_board_payload(state: dict[str, Any]) -> dict[str, Any]:
    incident = state.get("incident") if isinstance(state.get("incident"), dict) else {}
    return {
        "status": "degraded",
        "reason": state.get("reason") or incident.get("reason") or "kanban DB corruption incident",
        "db_path": state.get("db_path") or incident.get("db_path"),
        "fingerprint": state.get("fingerprint") or incident.get("fingerprint"),
        "first_seen": incident.get("first_seen"),
        "last_seen": incident.get("last_seen"),
        "next_retry": state.get("next_retry") or incident.get("next_retry"),
        "quarantine_path": incident.get("quarantine_path"),
        "repair_command": incident.get("repair_command") or f"hermes kanban repair --board {state.get('board') or kanban_db.DEFAULT_BOARD}",
    }


def _corrupt_board_state(board: Optional[str]) -> dict[str, Any] | None:
    try:
        state = kanban_db.corrupt_board_quarantine_state(board)
    except Exception:
        return None
    return state if state.get("skipped") else None


def _raise_if_corrupt_quarantined(board: Optional[str]) -> None:
    state = _corrupt_board_state(board)
    if not state:
        return
    raise HTTPException(
        status_code=503,
        detail={
            "error": "kanban_board_corrupt",
            "board": state.get("board") or board or kanban_db.DEFAULT_BOARD,
            "corruption": _corrupt_board_payload(state),
        },
    )


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

# Columns shown by the dashboard, in left-to-right order. "archived" is
# available via a filter toggle rather than a visible column.
#
# Keep this in sync with kanban_db.VALID_STATUSES.  In particular,
# ``scheduled`` is a first-class waiting column used for time-based follow-ups;
# if it is omitted here, the board-level fallback below mis-buckets scheduled
# tasks into ``todo`` and makes the dashboard look like the Scheduled column
# disappeared.
BOARD_COLUMNS: list[str] = [
    "triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done",
]


_CARD_SUMMARY_PREVIEW_CHARS = 200


def _task_dict(
    task: kanban_db.Task,
    *,
    latest_summary: Optional[str] = None,
) -> dict[str, Any]:
    d = asdict(task)
    # Add derived age metrics so the UI can colour stale cards without
    # computing deltas client-side.
    try:
        d["age"] = kanban_db.task_age(task)
    except Exception:
        d["age"] = {"created_age_seconds": None, "started_age_seconds": None, "time_to_complete_seconds": None}
    # Surface the latest non-null run summary so dashboards don't show
    # blank cards/drawers for tasks where the worker handed off via
    # ``task_runs.summary`` (the kanban-worker pattern) instead of
    # ``tasks.result``. ``None`` when no run has produced a summary yet.
    d["latest_summary"] = latest_summary
    # Keep body short on list endpoints; full body comes from /tasks/:id.
    return d


def _event_dict(event: kanban_db.Event) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "kind": event.kind,
        "payload": event.payload,
        "created_at": event.created_at,
        "run_id": event.run_id,
    }


def _comment_dict(c: kanban_db.Comment) -> dict[str, Any]:
    return {
        "id": c.id,
        "task_id": c.task_id,
        "author": c.author,
        "body": c.body,
        "created_at": c.created_at,
    }


def _attachment_dict(a: kanban_db.Attachment) -> dict[str, Any]:
    """Serialise an Attachment for the drawer. ``stored_path`` is the
    absolute on-disk path workers read; the UI uses ``id`` for download."""
    return {
        "id": a.id,
        "task_id": a.task_id,
        "filename": a.filename,
        "content_type": a.content_type,
        "size": a.size,
        "uploaded_by": a.uploaded_by,
        "stored_path": a.stored_path,
        "created_at": a.created_at,
    }


def _run_dict(r: kanban_db.Run) -> dict[str, Any]:
    """Serialise a Run for the drawer's Run history section."""
    return {
        "id": r.id,
        "task_id": r.task_id,
        "profile": r.profile,
        "step_key": r.step_key,
        "status": r.status,
        "claim_lock": r.claim_lock,
        "claim_expires": r.claim_expires,
        "worker_pid": r.worker_pid,
        "max_runtime_seconds": r.max_runtime_seconds,
        "last_heartbeat_at": r.last_heartbeat_at,
        "started_at": r.started_at,
        "ended_at": r.ended_at,
        "outcome": r.outcome,
        "summary": r.summary,
        "metadata": r.metadata,
        "error": r.error,
    }


# Hallucination-warning event kinds — see complete_task() in kanban_db.py.
# completion_blocked_hallucination: kernel rejected created_cards with
#   phantom ids; task stays in prior state.
# suspected_hallucinated_references: prose scan found t_<hex> in summary
#   that doesn't resolve; completion succeeded, advisory only.
_WARNING_EVENT_KINDS = (
    "completion_blocked_hallucination",
    "suspected_hallucinated_references",
)


def _compute_task_diagnostics(
    conn: sqlite3.Connection,
    task_ids: Optional[list[str]] = None,
) -> dict[str, list[dict]]:
    """Run the diagnostic rule engine against every task (or a subset)
    and return ``{task_id: [diagnostic_dict, ...]}``.

    Tasks with no active diagnostics are omitted from the result.
    Uses ``hermes_cli.kanban_diagnostics`` — see that module for the
    rule definitions.
    """
    from hermes_cli import kanban_diagnostics as kd
    from hermes_cli.config import load_config

    diag_config = kd.config_from_runtime_config(load_config())

    # Build the candidate task list. We need each task's row + its
    # events + its runs. Doing N separate queries works but scales
    # poorly; do three aggregate queries instead.
    if task_ids is not None:
        if not task_ids:
            return {}
        placeholders = ",".join(["?"] * len(task_ids))
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE id IN ({placeholders})",
            tuple(task_ids),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE status != 'archived'",
        ).fetchall()

    if not rows:
        return {}

    # Index events + runs by task id. For very large boards this will
    # slurp a lot — acceptable on the dashboard's typical working set
    # (hundreds of tasks), but we can add pagination / filtering later
    # if profiling shows it's a hotspot.
    row_ids = [r["id"] for r in rows]
    placeholders = ",".join(["?"] * len(row_ids))
    events_by_task: dict[str, list] = {tid: [] for tid in row_ids}
    for ev_row in conn.execute(
        f"SELECT * FROM task_events WHERE task_id IN ({placeholders}) ORDER BY id",
        tuple(row_ids),
    ).fetchall():
        events_by_task.setdefault(ev_row["task_id"], []).append(ev_row)
    runs_by_task: dict[str, list] = {tid: [] for tid in row_ids}
    for run_row in conn.execute(
        f"SELECT * FROM task_runs WHERE task_id IN ({placeholders}) ORDER BY id",
        tuple(row_ids),
    ).fetchall():
        runs_by_task.setdefault(run_row["task_id"], []).append(run_row)

    out: dict[str, list[dict]] = {}
    for r in rows:
        tid = r["id"]
        diags = kd.compute_task_diagnostics(
            r,
            events_by_task.get(tid, []),
            runs_by_task.get(tid, []),
            config=diag_config,
        )
        if diags:
            out[tid] = [d.to_dict() for d in diags]
    return out


def _warnings_summary_from_diagnostics(
    diagnostics: list[dict],
) -> Optional[dict]:
    """Compact summary for cards: {count, highest_severity, kinds,
    latest_at}. Replaces the old hallucination-only ``warnings`` object
    — same shape additions plus ``highest_severity`` so the UI can color
    badges per diagnostic severity.

    Returns None when ``diagnostics`` is empty.
    """
    if not diagnostics:
        return None
    from hermes_cli.kanban_diagnostics import SEVERITY_ORDER

    kinds: dict[str, int] = {}
    latest = 0
    highest_idx = -1
    highest_sev: Optional[str] = None
    count = 0
    for d in diagnostics:
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + d.get("count", 1)
        count += d.get("count", 1)
        la = d.get("last_seen_at") or 0
        if la > latest:
            latest = la
        sev = d.get("severity")
        if sev in SEVERITY_ORDER:
            idx = SEVERITY_ORDER.index(sev)
            if idx > highest_idx:
                highest_idx = idx
                highest_sev = sev
    return {
        "count": count,
        "kinds": kinds,
        "latest_at": latest,
        "highest_severity": highest_sev,
    }


def _links_for(conn: sqlite3.Connection, task_id: str) -> dict[str, list[str]]:
    """Return {'parents': [...], 'children': [...]} for a task."""
    parents = [
        r["parent_id"]
        for r in conn.execute(
            "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
            (task_id,),
        )
    ]
    children = [
        r["child_id"]
        for r in conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
            (task_id,),
        )
    ]
    return {"parents": parents, "children": children}


# ---------------------------------------------------------------------------
# GET /board
# ---------------------------------------------------------------------------

@router.get("/board")
def get_board(
    tenant: Optional[str] = Query(None, description="Filter to a single tenant"),
    include_archived: bool = Query(False),
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
    workflow_template_id: Optional[str] = Query(
        None, description="Restrict to tasks using this workflow template id",
    ),
    current_step_key: Optional[str] = Query(
        None, description="Restrict to tasks at this workflow step key",
    ),
):
    """Return the full board grouped by status column.

    ``_conn()`` auto-initializes ``kanban.db`` on first call so a fresh
    install doesn't surface a "failed to load" error on the plugin tab.

    ``board`` selects which board to read from. Omitting it falls
    through to the active board (``HERMES_KANBAN_BOARD`` env → on-disk
    ``current`` pointer → ``default``).
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        tasks = kanban_db.list_tasks(
            conn,
            tenant=tenant,
            include_archived=include_archived,
            workflow_template_id=workflow_template_id,
            current_step_key=current_step_key,
        )
        # Pre-fetch link counts per task (cheap: one query).
        link_counts: dict[str, dict[str, int]] = {}
        for row in conn.execute(
            "SELECT parent_id, child_id FROM task_links"
        ).fetchall():
            link_counts.setdefault(row["parent_id"], {"parents": 0, "children": 0})[
                "children"
            ] += 1
            link_counts.setdefault(row["child_id"], {"parents": 0, "children": 0})[
                "parents"
            ] += 1

        # Comment + event counts (both cheap aggregates).
        comment_counts: dict[str, int] = {
            r["task_id"]: r["n"]
            for r in conn.execute(
                "SELECT task_id, COUNT(*) AS n FROM task_comments GROUP BY task_id"
            )
        }

        # Progress rollup: for each parent, how many children are done / total.
        # One pass over task_links joined with child status — cheaper than
        # N per-task queries and the plugin uses it to render "N/M".
        progress: dict[str, dict[str, int]] = {}
        for row in conn.execute(
            "SELECT l.parent_id AS pid, t.status AS cstatus "
            "FROM task_links l JOIN tasks t ON t.id = l.child_id"
        ).fetchall():
            p = progress.setdefault(row["pid"], {"done": 0, "total": 0})
            p["total"] += 1
            if row["cstatus"] == "done":
                p["done"] += 1

        # Diagnostics rollup for this board — see kanban_diagnostics.
        # We get the full structured list per task AND a compact
        # summary for the card badge (so cards don't carry the detail
        # text; the drawer fetches that via /tasks/:id or /diagnostics).
        diagnostics_per_task = _compute_task_diagnostics(conn, task_ids=None)

        latest_event_id = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM task_events"
        ).fetchone()["m"]

        columns: dict[str, list[dict]] = {c: [] for c in BOARD_COLUMNS}
        if include_archived:
            columns["archived"] = []

        # Batch-fetch the latest non-null run summary per task in one
        # window-function query (avoids N+1 ``latest_summary`` calls
        # for boards with hundreds of tasks). Truncated to a card-size
        # preview here — the full text is available via /tasks/:id.
        summary_map = kanban_db.latest_summaries(conn, [t.id for t in tasks])

        for t in tasks:
            full = summary_map.get(t.id)
            preview = (
                full[:_CARD_SUMMARY_PREVIEW_CHARS] if full else None
            )
            d = _task_dict(t, latest_summary=preview)
            d["link_counts"] = link_counts.get(t.id, {"parents": 0, "children": 0})
            d["comment_count"] = comment_counts.get(t.id, 0)
            d["progress"] = progress.get(t.id)  # None when the task has no children
            diags = diagnostics_per_task.get(t.id)
            if diags:
                # Full list goes into the payload so the drawer can render
                # without a second round-trip. The board-level badge only
                # needs the summary.
                d["diagnostics"] = diags
                d["warnings"] = _warnings_summary_from_diagnostics(diags)
            col = t.status if t.status in columns else "todo"
            columns[col].append(d)

        # Stable per-column ordering already applied by list_tasks
        # (priority DESC, created_at ASC), keep as-is.

        # List of known tenants for the UI filter dropdown.
        tenants = [
            r["tenant"]
            for r in conn.execute(
                "SELECT DISTINCT tenant FROM tasks WHERE tenant IS NOT NULL ORDER BY tenant"
            )
        ]
        # List of distinct assignees for the lane-by-profile sub-grouping.
        assignees = [
            r["assignee"]
            for r in conn.execute(
                "SELECT DISTINCT assignee FROM tasks WHERE assignee IS NOT NULL "
                "AND status != 'archived' ORDER BY assignee"
            )
        ]

        return {
            "columns": [
                {"name": name, "tasks": columns[name]} for name in columns.keys()
            ],
            "tenants": tenants,
            "assignees": assignees,
            "latest_event_id": int(latest_event_id),
            "now": int(time.time()),
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# GET /tasks/:id
# ---------------------------------------------------------------------------

@router.get("/tasks/{task_id}")
def get_task(
    task_id: str,
    board: Optional[str] = Query(None),
    run_state_type: Optional[str] = Query(
        None, description="With run_state_name: filter runs by column 'status' or 'outcome'",
    ),
    run_state_name: Optional[str] = Query(
        None, description="With run_state_type: exact value for that run column",
    ),
):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if (run_state_type is None) ^ (run_state_name is None):
            raise HTTPException(
                status_code=400,
                detail="run_state_type and run_state_name must be passed together or omitted",
            )
        if run_state_type is not None and run_state_type not in ("status", "outcome"):
            raise HTTPException(
                status_code=400,
                detail="run_state_type must be 'status' or 'outcome'",
            )
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        # Drawer/detail view returns the FULL summary (no truncation) so
        # operators can read the complete worker handoff without making
        # a second round-trip. Cards on /board carry a 200-char preview.
        full_summary = kanban_db.latest_summary(conn, task_id)
        task_d = _task_dict(task, latest_summary=full_summary)
        # Attach diagnostics so the drawer's Diagnostics section can
        # render recovery actions without a second round-trip.
        diags = _compute_task_diagnostics(conn, task_ids=[task_id])
        diag_list = diags.get(task_id) or []
        if diag_list:
            task_d["diagnostics"] = diag_list
            task_d["warnings"] = _warnings_summary_from_diagnostics(diag_list)
        return {
            "task": task_d,
            "comments": [_comment_dict(c) for c in kanban_db.list_comments(conn, task_id)],
            "events": [_event_dict(e) for e in kanban_db.list_events(conn, task_id)],
            "attachments": [_attachment_dict(a) for a in kanban_db.list_attachments(conn, task_id)],
            "links": _links_for(conn, task_id),
            "runs": [
                _run_dict(r)
                for r in kanban_db.list_runs(
                    conn,
                    task_id,
                    state_type=run_state_type,
                    state_name=run_state_name,
                )
            ],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# POST /tasks
# ---------------------------------------------------------------------------

class CreateTaskBody(BaseModel):
    title: str
    body: Optional[str] = None
    assignee: Optional[str] = None
    tenant: Optional[str] = None
    priority: int = 0
    workspace_kind: str = "scratch"
    workspace_path: Optional[str] = None
    parents: list[str] = Field(default_factory=list)
    triage: bool = False
    idempotency_key: Optional[str] = None
    max_runtime_seconds: Optional[int] = None
    skills: Optional[list[str]] = None


class ProposalRejectBody(BaseModel):
    reason: str = Field(..., min_length=1, max_length=2000)


class ProposalFollowupBody(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=2000)


class BoardRepairBody(BaseModel):
    task_id: Optional[str] = None
    work_item_id: Optional[str] = None
    title: Optional[str] = None
    status: Optional[str] = None
    detail: Optional[str] = None


class BoardUndoFollowupBody(BaseModel):
    reason: Optional[str] = Field(default=None, max_length=2000)


class CommandCenterAnnotationBody(BaseModel):
    mode: str
    text: str = Field(..., min_length=1, max_length=4000)
    title: Optional[str] = Field(default=None, max_length=200)
    pause_current: bool = False


def _proposal_actor() -> str:
    return os.environ.get("USER") or os.environ.get("USERNAME") or "dashboard"


def _proposal_task_body(card: dict[str, Any]) -> str:
    task = card.get("kanban_task") if isinstance(card.get("kanban_task"), dict) else {}
    source_lines = []
    for excerpt in card.get("source_excerpts") or []:
        if not isinstance(excerpt, dict):
            continue
        label = excerpt.get("label") or "source"
        text = str(excerpt.get("text") or "").strip()
        if text:
            source_lines.append(f"- {label}: {text[:500]}")
    source_block = "\n".join(source_lines[:3]) or "- No source excerpt recorded."
    tags = task.get("tags") if isinstance(task.get("tags"), list) else []
    tag_line = ", ".join(str(tag) for tag in tags) if tags else "none"
    parts = [
        str(task.get("body") or card.get("body") or "").strip(),
        "",
        "Self-improvement proposal metadata:",
        f"- proposal_id: {card['proposal_id']}",
        f"- project: {card['project']}",
        f"- prong: {card['prong']}",
        f"- priority: {card.get('priority') or 'medium'}",
        f"- severity: {card.get('severity') or 'unspecified'}",
        f"- cron_job_id: {card.get('cron_job_id') or 'unknown'}",
        f"- run_id: {card.get('run_id') or card.get('run_db_id')}",
        f"- cron_output_path: {card.get('cron_output_path') or 'unknown'}",
        f"- tags: {tag_line}",
        "",
        "Rationale:",
        str(card.get("rationale") or "No rationale recorded.").strip(),
        "",
        "Source excerpts:",
        source_block,
    ]
    annotation_context = command_center_annotations.operator_context_block(f"self-improvement:{card['proposal_id']}")
    if annotation_context:
        parts.extend(["", annotation_context])
    return "\n".join(parts)


def _annotation_target_from_work_item(item: dict[str, Any]) -> tuple[str, str]:
    work_item_id = str(item.get("id") or "")
    execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
    if work_item_id.startswith("self-improvement:"):
        return "self_improvement_proposal", work_item_id.split(":", 1)[1]
    if work_item_id.startswith("kanban-board:"):
        return "kanban_board", str(execution.get("board") or work_item_id.split(":", 1)[1])
    if work_item_id.startswith("kanban:"):
        return "kanban_task", str(execution.get("task_id") or work_item_id.rsplit(":", 1)[-1])
    raise HTTPException(status_code=409, detail="unsupported Command Center Work Item kind")


def _find_command_center_work_item(work_item_id: str) -> dict[str, Any]:
    snapshot = command_center.build_command_center_snapshot(include_archived=True)
    for item in snapshot.get("work_items", []):
        if str(item.get("id") or "") == work_item_id:
            return item
    if work_item_id.startswith("self-improvement:"):
        proposal_id = work_item_id.split(":", 1)[1]
        card = proposal_storage.get_card(proposal_id)
        if card and card.get("kanban_task_id"):
            task_id = str(card.get("kanban_task_id") or "")
            board = _latest_self_improvement_board(proposal_id)
            board_meta = kanban_db.read_board_metadata(board)
            task = None
            runs: list[dict[str, Any]] = []
            try:
                conn = _conn(board=_resolve_board(board))
                try:
                    task = kanban_db.get_task(conn, task_id)
                    runs = [{"id": run.id, "status": run.status, "ended_at": run.ended_at} for run in kanban_db.list_runs(conn, task_id)]
                finally:
                    conn.close()
            except HTTPException:
                task = None
            status = "blocked"
            if task is not None:
                if task.status == "done":
                    status = "shipped"
                elif task.status == "archived":
                    status = "archived"
                elif task.status in {"running", "review", "blocked"}:
                    status = task.status
                else:
                    status = "queued"
            return {
                "id": work_item_id,
                "title": card.get("title") or proposal_id,
                "summary": card.get("summary") or card.get("body"),
                "status": status,
                "source": {"kind": "self_improvement", "ref": {"proposal_id": proposal_id}},
                "execution": {
                    "board": board,
                    "board_name": board_meta.get("name") or board,
                    "task_id": task_id,
                    "task_status": task.status if task else None,
                    "workspace_path": task.workspace_path if task else None,
                    "runs": runs,
                },
            }
    raise HTTPException(status_code=404, detail=f"Command Center Work Item {work_item_id!r} not found")


def _annotation_followup_body(*, annotation: dict[str, Any], item: dict[str, Any]) -> str:
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
    return "\n".join(
        [
            "Implement operator correction follow-up for a Command Center Work Item.",
            "",
            "Original Work Item:",
            f"- work_item_id: {item.get('id')}",
            f"- source_ref: {json.dumps(source.get('ref') or source, sort_keys=True)}",
            f"- previous_title: {annotation.get('previous_title') or ''}",
            f"- previous_summary: {annotation.get('previous_summary') or ''}",
            f"- previous_status: {annotation.get('previous_status') or ''}",
            f"- execution: {json.dumps(execution, sort_keys=True)}",
            "",
            "Operator correction:",
            f"- title: {annotation.get('title') or 'none'}",
            f"- text: {annotation.get('text')}",
            "",
            "Instructions:",
            "- Treat this correction as audited operator context.",
            "- Do not mutate original source text, proposal payload JSON, board root goals, or existing task bodies silently.",
            "- Create the smallest safe follow-up work needed to honor the correction and report the outcome.",
        ]
    )


def _create_annotation_followup(*, item: dict[str, Any], annotation: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
    board = str(execution.get("board") or "").strip()
    if not board:
        return None, None, "Work Item has no active execution board for correction follow-up"
    if board == kanban_db.DEFAULT_BOARD:
        return None, None, "default board correction follow-up is not supported for Command Center Work Items"
    if not kanban_db.board_exists(board):
        return None, None, "Work Item execution board does not exist for correction follow-up"
    board_meta = kanban_db.read_board_metadata(board)
    worker = _discord_worker_meta(board)
    idempotency_key = f"command-center-correction:{annotation['id']}"
    conn = _conn(board=board)
    try:
        workspace = _dashboard_worker_workspace(board_meta, execution.get("workspace_path"))
        title = annotation.get("title") or f"Operator correction: {item.get('title') or item.get('id')}"
        task_id = kanban_db.create_task(
            conn,
            title=str(title)[:200],
            body=_annotation_followup_body(annotation=annotation, item=item),
            assignee=ROLE_FOREMAN,
            created_by="command-center-correction",
            workspace_kind=workspace["workspace_kind"],
            workspace_path=workspace["workspace_path"],
            tenant=str(board_meta.get("project") or board_meta.get("tenant") or board),
            priority=command_center.COMMAND_CENTER_REPAIR_PRIORITY,
            idempotency_key=idempotency_key,
            max_runtime_seconds=1800,
        )
        task = kanban_db.get_task(conn, task_id)
    finally:
        conn.close()
    if worker:
        try:
            from hermes_cli import discord_worker_boards as dwb

            dwb.mark_dispatch_dirty(board=board, reason="command-center-correction")
        except Exception:
            pass
    return _task_dict(task) if task else None, _worker_ticket_url(task_id, board=board, board_public_url=worker.get("public_url") if worker else None), None


def _pause_annotation_target(item: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
    board = str(execution.get("board") or "").strip()
    task_id = str(execution.get("task_id") or "").strip()
    if not board:
        return None, "Work Item has no execution board to pause"
    try:
        worker = _discord_worker_meta(board)
        if worker:
            from hermes_cli import discord_worker_boards as dwb

            return dwb.pause_board(board, reason="command-center-correction"), None
        if task_id:
            conn = _conn(board=board)
            try:
                return {"board": board, "task_id": task_id, "paused": _pause_generic_task(conn, task_id, reason="command-center-correction")}, None
            finally:
                conn.close()
        return _pause_generic_board(board, reason="command-center-correction"), None
    except Exception as exc:
        return None, str(exc)


def _repair_task_body(
    *,
    board: str,
    board_meta: dict[str, Any],
    payload: BoardRepairBody,
    source_task: kanban_db.Task | None,
    task_counts: dict[str, int],
) -> str:
    worker = _discord_worker_meta(board)
    board_url = _worker_board_url(board=board, board_public_url=worker.get("public_url") if worker else None)
    source_title = payload.title or (source_task.title if source_task else None) or board_meta.get("name") or board
    return "\n".join(
        [
            "Diagnose and repair a blocked Command Center worker board.",
            "",
            "Use the foreman profile/role with the broadest available context and permissions. Inspect the board, task state, recent runs, logs, and persisted metadata; safely unblock or repair stuck tickets; wake dispatch if needed; and report exactly what changed.",
            "",
            "Board:",
            f"- slug: {board}",
            f"- name: {board_meta.get('name') or board}",
            f"- worker_url: {board_url or 'unavailable'}",
            f"- task_counts: {json.dumps(task_counts, sort_keys=True)}",
            "",
            "Source work item:",
            f"- work_item_id: {payload.work_item_id or 'unknown'}",
            f"- task_id: {payload.task_id or (source_task.id if source_task else 'board-rollup')}",
            f"- title: {source_title}",
            f"- status: {payload.status or (source_task.status if source_task else 'blocked')}",
            f"- detail: {payload.detail or 'blocked Command Center item'}",
            "",
            "Instructions:",
            "- Do not create duplicate Foreman repair tickets for this source.",
            "- Prefer repairing task/board state over deleting evidence.",
            "- If a task is safely resumable, move it back to ready and mark dispatch dirty.",
            "- If the block is legitimate, leave it blocked with a clear explanation and next action.",
            "",
            "Post-repair root-cause follow-up:",
            "- After fixing or unblocking the board, assess whether the stuck board reveals a durable or fundamental Hermes repository fix.",
            "- Do not auto-apply repository code changes inside this repair unless they are directly required to unstick the board.",
            "- If a durable repo fix appears warranted, create exactly one separate human-decision Command Center row as a proposed self-improvement card for `project: hermes` and `prong: system-doctor`.",
            "- Use the existing self-improvement proposal path: persist a `self_improvement.proposal_run.v1` proposal run with `self_improvement.proposal_storage.ingest_proposal_output(...)`, not a new repair endpoint or ad hoc dashboard row.",
            "- The proposed card must specify the repo fix for human approval and include title, summary/body, rationale, scope, and verification; its `kanban_task` should contain the implementation brief humans would approve into a follow-up job.",
            "- Report any created follow-up proposal id/title/url in your final JSON `follow_up_proposals`; if no durable repo fix is needed, say so in verification.",
        ]
    )


def _board_undo_followup_task_body(*, board: str, board_meta: dict[str, Any], reason: str | None, task_counts: dict[str, int]) -> str:
    worker = _discord_worker_meta(board)
    board_url = _worker_board_url(board=board, board_public_url=worker.get("public_url") if worker else None)
    return "\n".join(
        [
            "Review the completed Command Center worker board and prepare the safest revert path if needed.",
            "",
            "Board:",
            f"- slug: {board}",
            f"- name: {board_meta.get('name') or board}",
            f"- worker_url: {board_url or 'unavailable'}",
            f"- task_counts: {json.dumps(task_counts, sort_keys=True)}",
            "",
            f"Reason: {reason or 'operator requested follow-up'}",
            "",
            "Instructions:",
            "- Inspect completed work, artifacts, commits/PRs, and worker summaries before proposing any revert.",
            "- Prefer the smallest safe compensating follow-up over destructive state changes.",
            "- Do not mutate live production state unless the task explicitly requires it and verification is available.",
        ]
    )


def _active_repair_task(conn, idempotency_key: str) -> kanban_db.Task | None:
    exact_keys = [idempotency_key]
    like_keys = [f"{idempotency_key}:%"]
    parts = idempotency_key.split(":", 2)
    if len(parts) == 3 and parts[2] == parts[1]:
        # Board-rollup repair uses command-center-repair:<board>:<board>.
        # Suppress it when any active task-specific repair already exists on
        # that board, even if the UI snapshot is stale.
        like_keys.append(f"{parts[0]}:{parts[1]}:%")
    clauses = ["idempotency_key = ?" for _ in exact_keys] + ["idempotency_key LIKE ?" for _ in like_keys]
    rows = conn.execute(
        "SELECT id FROM tasks WHERE " + " OR ".join(clauses) + " ORDER BY created_at DESC, id DESC",
        (*exact_keys, *like_keys),
    ).fetchall()
    for row in rows:
        task = kanban_db.get_task(conn, row["id"])
        if task and task.status in command_center.COMMAND_CENTER_REPAIR_ACTIVE_STATUSES:
            return task
    return None


def _repair_attempt_idempotency_key(conn, base_key: str, *, now: int | None = None) -> str:
    row = conn.execute(
        "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' ORDER BY created_at DESC LIMIT 1",
        (base_key,),
    ).fetchone()
    if not row:
        return base_key
    attempt_second = int(now if now is not None else time.time())
    for suffix in [str(attempt_second), *(f"{attempt_second}-{index}" for index in range(1, 100))]:
        candidate = f"{base_key}:{suffix}"
        existing = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' LIMIT 1",
            (candidate,),
        ).fetchone()
        if not existing:
            return candidate
    return f"{base_key}:{attempt_second}-overflow"


def _latest_self_improvement_board(proposal_id: str) -> str:
    try:
        events = proposal_storage.list_audit_events(proposal_id)
    except Exception:
        return "default"
    for event in reversed(events):
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        board = metadata.get("board")
        if board:
            return str(board)
    return "default"


def _latest_self_improvement_recovery_metadata(proposal_id: str) -> dict[str, Any]:
    try:
        events = proposal_storage.list_audit_events(proposal_id)
    except Exception:
        return {}
    for event in reversed(events):
        if event.get("action") != "recovery_needed":
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        return metadata if isinstance(metadata, dict) else {}
    return {}


def _self_improvement_resume_task_id(card: dict[str, Any]) -> str:
    task_id = str(card.get("kanban_task_id") or "").strip()
    if task_id:
        return task_id
    proposal_id = str(card.get("proposal_id") or "")
    recovery_metadata = _latest_self_improvement_recovery_metadata(proposal_id)
    return str(recovery_metadata.get("kanban_task_id") or "").strip()


def _discord_worker_meta(board: str | None) -> dict[str, Any]:
    board = str(board or "").strip()
    if not board or board == "default":
        return {}
    try:
        worker = kanban_db.read_board_metadata(board).get(DISCORD_WORKER_META_KEY)
    except Exception:
        return {}
    if not isinstance(worker, dict) or worker.get("kind") != "discord_worker_board":
        return {}
    return worker


def _runtime_checkout_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _is_runtime_checkout_path(path: str) -> bool:
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
        root = _runtime_checkout_root().resolve(strict=False)
    except Exception:
        return False
    return candidate == root or root in candidate.parents


def _dashboard_worker_workspace(board_meta: dict[str, Any] | None, *candidates: Any) -> dict[str, str | None]:
    """Return safe workspace args for dashboard-created worker tasks.

    Worker jobs must not run in the checkout that happens to be serving the
    dashboard/gateway. For Hermes that runtime checkout is usually the canonical
    ``main`` tree; foreground/dashboard/cron repair work still needs the same
    per-board worktree isolation as Discord-originated workers.
    """
    meta = board_meta if isinstance(board_meta, dict) else {}
    worker = meta.get(DISCORD_WORKER_META_KEY)
    paths: list[Any] = []
    if isinstance(worker, dict):
        paths.append(worker.get("worktree_path"))
    paths.extend(candidates)
    for value in paths:
        path = str(value or "").strip()
        if not path:
            continue
        expanded = Path(path).expanduser()
        if expanded.is_absolute() and not _is_runtime_checkout_path(path):
            return {"workspace_kind": "dir", "workspace_path": path}
    return {"workspace_kind": "scratch", "workspace_path": None}


def _discord_worker_board_status(worker: dict[str, Any]) -> str:
    status = str(worker.get("goal_status") or "").strip().lower()
    phase = str(worker.get("phase") or "").strip().lower()
    if status == "done" or phase == "complete":
        return "done"
    if status:
        return status
    if worker.get("cancelled"):
        return "cancelled"
    if worker.get("paused"):
        return "paused"
    return phase


def _discord_worker_board_is_terminal(worker: dict[str, Any]) -> bool:
    return _discord_worker_board_status(worker) in {"done", "blocked", "cancelled"}


def _discord_worker_board_is_cancelled(worker: dict[str, Any]) -> bool:
    # Done boards still owe their green terminal reaction/completion notice before
    # archive; a stale cancellation flag must not bypass those delivery guards.
    status = str(worker.get("goal_status") or "").strip().lower()
    phase = str(worker.get("phase") or "").strip().lower()
    if status == "done" or phase == "complete":
        return False
    return _discord_worker_board_status(worker) == "cancelled" or bool(worker.get("cancelled"))


def _pause_generic_task(conn: sqlite3.Connection, task_id: str, *, reason: str) -> bool:
    task = kanban_db.get_task(conn, task_id)
    if task is None or task.status in {"done", "archived"}:
        return False
    if task.status in {"blocked", "scheduled"}:
        return True
    if task.status == "running" or task.claim_lock:
        kanban_db.reclaim_task(conn, task_id, reason=reason)
        task = kanban_db.get_task(conn, task_id)
        if task is None or task.status in {"done", "archived"}:
            return False
    return kanban_db.block_task(conn, task_id, reason=reason)


def _resume_generic_task(conn: sqlite3.Connection, task_id: str) -> bool:
    task = kanban_db.get_task(conn, task_id)
    if task is None or task.status in {"done", "archived"}:
        return False
    if task.status in {"ready", "running", "review"}:
        return True
    if task.status in {"blocked", "scheduled"}:
        return kanban_db.unblock_task(conn, task_id)
    if task.status in {"todo", "triage"}:
        return kanban_db.set_status_direct(conn, task_id, "ready", source="dashboard/command-center-resume")
    return False


def _pause_generic_board(board: str, *, reason: str) -> dict[str, Any]:
    conn = _conn(board=_resolve_board(board))
    paused: list[str] = []
    try:
        for task in kanban_db.list_tasks(conn, include_archived=False):
            if task.status in {"done", "archived"}:
                continue
            if _pause_generic_task(conn, task.id, reason=reason):
                paused.append(task.id)
    finally:
        conn.close()
    meta = kanban_db.read_board_metadata(board)
    meta.pop("db_path", None)
    meta["command_center_paused"] = True
    meta["command_center_pause_reason"] = reason
    meta["command_center_paused_at"] = int(time.time())
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"board": board, "paused": paused}


def _resume_generic_board(board: str) -> dict[str, Any]:
    conn = _conn(board=_resolve_board(board))
    resumed: list[str] = []
    try:
        for task in kanban_db.list_tasks(conn, include_archived=False):
            if task.status in {"blocked", "scheduled"} and _resume_generic_task(conn, task.id):
                resumed.append(task.id)
    finally:
        conn.close()
    meta = kanban_db.read_board_metadata(board)
    meta.pop("db_path", None)
    meta["command_center_paused"] = False
    meta.pop("command_center_pause_reason", None)
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"board": board, "resumed": resumed}


def _board_recovery_resume_blocker(board: str) -> str | None:
    try:
        cards = proposal_storage.list_cards(include_rejected=True, include_archived=True).get("cards", [])
    except Exception:
        return None
    for card in cards:
        if str(card.get("status") or "").lower() != "recovery_needed":
            continue
        proposal_id = str(card.get("proposal_id") or "")
        if _latest_self_improvement_board(proposal_id) != board:
            continue
        metadata = _latest_self_improvement_recovery_metadata(proposal_id)
        return str(metadata.get("recovery_reason") or "recovery is required").strip()
    return None


def _require_command_center_board_resumable(board: str, worker: dict[str, Any] | None = None) -> None:
    if board == kanban_db.DEFAULT_BOARD:
        raise HTTPException(status_code=409, detail="default board is not resumable through board replay")
    meta = kanban_db.read_board_metadata(board)
    if meta.get("archived"):
        raise HTTPException(status_code=409, detail="archived board is not resumable")
    recovery_reason = _board_recovery_resume_blocker(board)
    if recovery_reason:
        raise HTTPException(status_code=409, detail=f"board requires recovery and cannot be resumed directly: {recovery_reason}")
    worker = worker or _discord_worker_meta(board)
    paused = bool(
        (worker or {}).get("paused")
        or meta.get("command_center_paused")
        or str((worker or {}).get("goal_status") or "").lower() == "paused"
        or str((worker or {}).get("phase") or "").lower() == "paused"
    )
    conn = _conn(board=board)
    try:
        statuses = {str(task.status or "").lower() for task in kanban_db.list_tasks(conn, include_archived=False)}
    finally:
        conn.close()
    if not paused and not (statuses & {"blocked", "scheduled"}):
        raise HTTPException(status_code=409, detail="board is not resumable from current Command Center state")


def _stop_generic_board_for_archive(board: str, *, reason: str) -> dict[str, Any]:
    conn = _conn(board=_resolve_board(board))
    archived: list[str] = []
    reclaimed: list[str] = []
    try:
        for task in kanban_db.list_tasks(conn, include_archived=False):
            if task.status == "done":
                continue
            if task.status == "running" or task.claim_lock:
                if kanban_db.reclaim_task(conn, task.id, reason=reason):
                    reclaimed.append(task.id)
            latest = kanban_db.get_task(conn, task.id)
            if latest and latest.status != "archived" and kanban_db.archive_task(conn, task.id):
                archived.append(task.id)
    finally:
        conn.close()
    return {"board": board, "archived_tasks": archived, "reclaimed": reclaimed}


def _board_is_completed(board: str, board_meta: dict[str, Any]) -> bool:
    worker_status = _discord_worker_board_status(_discord_worker_meta(board))
    if worker_status == "done":
        return True
    conn = _conn(board=board)
    try:
        active_tasks = [task for task in kanban_db.list_tasks(conn, include_archived=False) if task.status != "archived"]
    finally:
        conn.close()
    if active_tasks and all(task.status == "done" for task in active_tasks):
        return True
    worker = board_meta.get(DISCORD_WORKER_META_KEY) if isinstance(board_meta.get(DISCORD_WORKER_META_KEY), dict) else {}
    return str(worker.get("goal_status") or "").lower() in {"done", "shipped", "complete", "completed"} or str(worker.get("phase") or "").lower() == "complete"


def _planner_task_matches_card(task: kanban_db.Task, card: dict[str, Any]) -> bool:
    if task.assignee != ROLE_PLANNER or task.created_by != "self-improvement":
        return False
    expected = " ".join(discord_publish._initial_request(card).split())
    if not expected:
        return True
    try:
        payload = json.loads(task.body or "{}")
    except Exception:
        return False
    if not isinstance(payload, dict):
        return False
    observed = " ".join(str(payload.get("request") or payload.get("root_goal") or "").split())
    return observed == expected


def _self_improvement_planner_task(board: str | None, card: dict[str, Any]) -> kanban_db.Task | None:
    board = str(board or "default")
    worker = _discord_worker_meta(board)
    preferred_id = str(worker.get("latest_planner_task_id") or "").strip()
    conn = _conn(board=board)
    try:
        if preferred_id:
            task = kanban_db.get_task(conn, preferred_id)
            if task is not None and _planner_task_matches_card(task, card):
                return task
        matches = [
            task
            for task in kanban_db.list_tasks(conn, include_archived=False)
            if _planner_task_matches_card(task, card)
        ]
        if not matches:
            return None
        return sorted(matches, key=lambda item: (int(item.created_at or 0), item.id), reverse=True)[0]
    finally:
        conn.close()


def _self_improvement_card_with_downstream(card: dict[str, Any] | None) -> dict[str, Any] | None:
    if card is None:
        return None
    enriched = dict(card)
    proposal_id = str(enriched.get("proposal_id") or "")
    if str(enriched.get("status") or "").lower() == "recovery_needed":
        recovery_metadata = _latest_self_improvement_recovery_metadata(proposal_id)
        if recovery_metadata:
            enriched["recovery_metadata"] = recovery_metadata
            enriched["recovery_required"] = True
            enriched["recovery_reason"] = recovery_metadata.get("recovery_reason")
            enriched["recovery_evidence_kind"] = recovery_metadata.get("evidence_kind")
            enriched["observed_terminal_status"] = recovery_metadata.get("observed_terminal_status")
            if recovery_metadata.get("observed_task_status") is not None:
                enriched["observed_task_status"] = recovery_metadata.get("observed_task_status")
    task_id = enriched.get("kanban_task_id")
    if not task_id:
        return enriched
    _repair_card_worker_url(enriched)
    board = _latest_self_improvement_board(proposal_id)
    enriched["downstream_board"] = board
    worker = _discord_worker_meta(board)
    board_status = ""
    if worker:
        board_status = _discord_worker_board_status(worker)
        enriched["downstream_board_status"] = board_status
        enriched["downstream_board_phase"] = str(worker.get("phase") or "")
    try:
        conn = _conn(board=_resolve_board(board))
    except HTTPException:
        enriched["downstream_task_status"] = "missing"
        enriched["downstream_task_missing"] = True
        return enriched
    try:
        task = kanban_db.get_task(conn, str(task_id))
        if task is None:
            enriched["downstream_task_status"] = "missing"
            enriched["downstream_task_missing"] = True
        else:
            enriched["downstream_task_status"] = board_status or task.status
            enriched["downstream_task"] = _task_dict(task)
    finally:
        conn.close()
    return enriched


def _self_improvement_grouped_with_downstream() -> dict[str, Any]:
    from hermes_cli import command_center

    # Keep proposal endpoint reads in sync with the Command Center lifecycle
    # projection so terminal worker evidence is reflected outside snapshots too.
    command_center.build_command_center_snapshot()
    grouped = proposal_storage.grouped_cards()
    for project in grouped.get("projects", []):
        for prong in project.get("prongs", []):
            prong["cards"] = [
                _self_improvement_card_with_downstream(card) or card
                for card in prong.get("cards", [])
            ]
    return grouped


def _proposal_priority(value: Any) -> int:
    return {"critical": 4, "urgent": 4, "high": 3, "medium": 2, "low": 1}.get(str(value or "").lower(), 0)


def _worker_url(task_id: str) -> str:
    return f"/workers?task={quote(task_id, safe='')}"


def _worker_board_url(*, board: str | None = None, board_public_url: str | None = None) -> str:
    public_url = str(board_public_url or "").strip()
    if public_url:
        candidate = public_url.rstrip("/")
        path = urlsplit(candidate).path.rstrip("/")
        if path not in {"/workers", "workers"}:
            return candidate
    if board:
        return f"/workers/{quote(str(board), safe='')}"
    return ""


def _worker_ticket_url(task_id: str, *, board: str | None = None, board_public_url: str | None = None) -> str:
    board_url = _worker_board_url(board=board, board_public_url=board_public_url)
    if board_url:
        return f"{board_url}/tickets/{quote(task_id, safe='')}"
    return _worker_url(task_id)


def _approval_worker_url(task_id: str, discord_route: discord_publish.DiscordApprovalRoute | None, board: str | None) -> str:
    if discord_route and (discord_route.board or discord_route.board_public_url):
        return _worker_board_url(
            board=discord_route.board or board,
            board_public_url=discord_route.board_public_url,
        ) or _worker_url(task_id)
    return _worker_url(task_id)


def _approval_worker_url_from_metadata(task_id: str, metadata: dict[str, Any], board: str | None) -> str:
    return _worker_board_url(
        board=str(metadata.get("discord_board") or board or "") or None,
        board_public_url=str(metadata.get("discord_board_public_url") or "") or None,
    ) or _worker_url(task_id)


@router.post("/tasks")
def create_task(payload: CreateTaskBody, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title=payload.title,
            body=payload.body,
            assignee=payload.assignee,
            created_by="dashboard",
            workspace_kind=payload.workspace_kind,
            workspace_path=payload.workspace_path,
            tenant=payload.tenant,
            priority=payload.priority,
            parents=payload.parents,
            triage=payload.triage,
            idempotency_key=payload.idempotency_key,
            max_runtime_seconds=payload.max_runtime_seconds,
            skills=payload.skills,
        )
        task = kanban_db.get_task(conn, task_id)
        body: dict[str, Any] = {"task": _task_dict(task) if task else None}
        # Surface a dispatcher-presence warning so the UI can show a
        # banner when a `ready` task would otherwise sit idle because no
        # gateway is running (or dispatch_in_gateway=false). Only emit
        # for ready+assigned tasks; triage/todo are expected to wait,
        # and unassigned tasks can't be dispatched regardless.
        if task and task.status == "ready" and task.assignee:
            try:
                from hermes_cli.kanban import _check_dispatcher_presence
                running, message = _check_dispatcher_presence()
                if not running and message:
                    body["warning"] = message
            except Exception:
                # Probe failure must never block the create itself.
                pass
        return body
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Attachments — upload / list / download / delete (#35338)
# ---------------------------------------------------------------------------

# Cap a single upload so a runaway request can't fill the disk. 25 MB
# comfortably covers PDFs, images, and source docs — the kanban use case.
_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


def _safe_attachment_name(raw: str) -> str:
    """Reduce a client-supplied filename to a safe basename.

    Strips any directory components (``os.path.basename`` on both
    separators) so a malicious ``../../etc/passwd`` or ``C:\\x`` collapses
    to its leaf. Rejects empty / dotfile-only names. The result is only
    ever joined under the per-task attachments dir, never used verbatim
    as a path from the client.
    """
    name = (raw or "").replace("\\", "/").split("/")[-1].strip()
    # Drop control chars and leading dots so we never write a dotfile or
    # a name with embedded NULs/newlines.
    name = "".join(ch for ch in name if ch.isprintable() and ch not in '\x00').strip()
    name = name.lstrip(".").strip()
    if not name:
        raise HTTPException(status_code=400, detail="invalid attachment filename")
    return name[:200]


@router.get("/tasks/{task_id}/attachments")
def list_task_attachments(task_id: str, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if kanban_db.get_task(conn, task_id) is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        return {
            "attachments": [
                _attachment_dict(a) for a in kanban_db.list_attachments(conn, task_id)
            ]
        }
    finally:
        conn.close()


@router.post("/tasks/{task_id}/attachments")
async def upload_task_attachment(
    task_id: str,
    file: UploadFile = File(...),
    board: Optional[str] = Query(None),
    uploaded_by: Optional[str] = Form(None),
):
    """Store an uploaded file for a task and record its metadata.

    The blob lands under ``attachments_root(board)/<task_id>/`` with a
    sanitised, collision-resolved name. The worker reads it via the
    absolute path surfaced in ``build_worker_context``.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if kanban_db.get_task(conn, task_id) is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")

        safe_name = _safe_attachment_name(file.filename or "")

        # Stream to disk with a hard size cap so a huge upload can't fill
        # the disk. Read in chunks; abort + clean up if the cap is hit.
        dest_dir = kanban_db.task_attachments_dir(task_id, board=board)
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Resolve name collisions: foo.pdf → foo (1).pdf, foo (2).pdf, …
        stem, dot, ext = safe_name.partition(".")
        candidate = safe_name
        n = 1
        while (dest_dir / candidate).exists():
            candidate = f"{stem} ({n}){dot}{ext}"
            n += 1
        dest_path = dest_dir / candidate

        total = 0
        try:
            with open(dest_path, "wb") as out:
                while True:
                    chunk = await file.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_ATTACHMENT_BYTES:
                        out.close()
                        dest_path.unlink(missing_ok=True)
                        raise HTTPException(
                            status_code=413,
                            detail=(
                                f"attachment exceeds {_MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB limit"
                            ),
                        )
                    out.write(chunk)
        except HTTPException:
            raise
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"failed to store attachment: {exc}")

        att_id = kanban_db.add_attachment(
            conn,
            task_id,
            filename=candidate,
            stored_path=str(dest_path.resolve()),
            content_type=file.content_type,
            size=total,
            uploaded_by=(uploaded_by or "dashboard"),
        )
        att = kanban_db.get_attachment(conn, att_id)
        return {"attachment": _attachment_dict(att) if att else None}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@router.get("/attachments/{attachment_id}")
def download_attachment(attachment_id: int, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        att = kanban_db.get_attachment(conn, attachment_id)
        if att is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        # Confirm the blob still lives under the board's attachments root
        # before serving — defense in depth against a tampered DB row.
        root = kanban_db.attachments_root(board=board).resolve()
        try:
            stored = Path(att.stored_path).resolve()
            stored.relative_to(root)
        except (ValueError, OSError):
            raise HTTPException(status_code=404, detail="attachment file unavailable")
        if not stored.is_file():
            raise HTTPException(status_code=404, detail="attachment file missing on disk")
        return FileResponse(
            path=str(stored),
            filename=att.filename,
            media_type=att.content_type or "application/octet-stream",
        )
    finally:
        conn.close()


@router.delete("/attachments/{attachment_id}")
def remove_attachment(attachment_id: int, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        att = kanban_db.delete_attachment(conn, attachment_id)
        if att is None:
            raise HTTPException(status_code=404, detail="attachment not found")
        return {"ok": True, "id": attachment_id}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# PATCH /tasks/:id  (status / assignee / priority / title / body)
# ---------------------------------------------------------------------------

class UpdateTaskBody(BaseModel):
    status: Optional[str] = None
    assignee: Optional[str] = None
    priority: Optional[int] = None
    title: Optional[str] = None
    body: Optional[str] = None
    result: Optional[str] = None
    block_reason: Optional[str] = None
    # Structured handoff fields — forwarded to complete_task when status
    # transitions to 'done'. Dashboard parity with ``hermes kanban
    # complete --summary ... --metadata ...``.
    summary: Optional[str] = None
    metadata: Optional[dict] = None


@router.patch("/tasks/{task_id}")
def update_task(task_id: str, payload: UpdateTaskBody, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")

        # --- assignee ----------------------------------------------------
        if payload.assignee is not None:
            try:
                ok = kanban_db.assign_task(
                    conn, task_id, payload.assignee or None,
                )
            except RuntimeError as e:
                raise HTTPException(status_code=409, detail=str(e))
            if not ok:
                raise HTTPException(status_code=404, detail="task not found")

        # --- status -------------------------------------------------------
        if payload.status is not None:
            s = payload.status
            try:
                ok = kanban_db.move_task_status(
                    conn,
                    task_id,
                    s,
                    result=payload.result,
                    summary=payload.summary,
                    metadata=payload.metadata,
                    block_reason=payload.block_reason,
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            if not ok:
                # For ``ready``, name the blocking parent(s) so the dashboard
                # can render an actionable toast instead of a silent no-op.
                # See #26744.
                if s == "ready":
                    blockers = kanban_db.parents_blocking_ready(conn, task_id)
                    if blockers:
                        names = ", ".join(
                            f"{p['title']!r} ({p['id']}, status={p['status']})"
                            for p in blockers
                        )
                        raise HTTPException(
                            status_code=409,
                            detail=(
                                f"Cannot move to 'ready': blocked by parent(s) "
                                f"not done — {names}"
                            ),
                        )
                raise HTTPException(
                    status_code=409,
                    detail=f"status transition to {s!r} not valid from current state",
                )

        # --- priority -----------------------------------------------------
        if payload.priority is not None:
            with kanban_db.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET priority = ? WHERE id = ?",
                    (int(payload.priority), task_id),
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES (?, 'reprioritized', ?, ?)",
                    (task_id, json.dumps({"priority": int(payload.priority)}),
                     int(time.time())),
                )

        # --- title / body -------------------------------------------------
        if payload.title is not None or payload.body is not None:
            with kanban_db.write_txn(conn):
                sets, vals = [], []
                if payload.title is not None:
                    if not payload.title.strip():
                        raise HTTPException(status_code=400, detail="title cannot be empty")
                    sets.append("title = ?")
                    vals.append(payload.title.strip())
                if payload.body is not None:
                    sets.append("body = ?")
                    vals.append(payload.body)
                vals.append(task_id)
                conn.execute(
                    f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?", vals,
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, kind, payload, created_at) "
                    "VALUES (?, 'edited', NULL, ?)",
                    (task_id, int(time.time())),
                )

        updated = kanban_db.get_task(conn, task_id)
        return {"task": _task_dict(updated) if updated else None}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# DELETE /tasks/:id
# ---------------------------------------------------------------------------

@router.delete("/tasks/{task_id}")
def delete_task(task_id: str, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        ok = kanban_db.delete_task(conn, task_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        return {"deleted": True, "task_id": task_id}
    finally:
        conn.close()


def _parents_blocking_ready(
    conn: sqlite3.Connection, task_id: str,
) -> list:
    """Compatibility wrapper for older tests/importers."""
    return kanban_db.parents_blocking_ready(conn, task_id)


def _set_status_direct(
    conn: sqlite3.Connection, task_id: str, new_status: str,
) -> bool:
    """Compatibility wrapper for older tests/importers."""
    return kanban_db.set_status_direct(conn, task_id, new_status)


# ---------------------------------------------------------------------------
# Comments
# ---------------------------------------------------------------------------

class CommentBody(BaseModel):
    body: str
    author: Optional[str] = "dashboard"


@router.post("/tasks/{task_id}/comments")
def add_comment(task_id: str, payload: CommentBody, board: Optional[str] = Query(None)):
    if not payload.body.strip():
        raise HTTPException(status_code=400, detail="body is required")
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        if kanban_db.get_task(conn, task_id) is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        kanban_db.add_comment(
            conn, task_id, author=payload.author or "dashboard", body=payload.body,
        )
        return {"ok": True}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Links
# ---------------------------------------------------------------------------

class LinkBody(BaseModel):
    parent_id: str
    child_id: str


@router.post("/links")
def add_link(payload: LinkBody, board: Optional[str] = Query(None)):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        kanban_db.link_tasks(conn, payload.parent_id, payload.child_id)
        return {"ok": True}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        conn.close()


@router.delete("/links")
def delete_link(
    parent_id: str = Query(...),
    child_id: str = Query(...),
    board: Optional[str] = Query(None),
):
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        ok = kanban_db.unlink_tasks(conn, parent_id, child_id)
        return {"ok": bool(ok)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Bulk actions (multi-select on the board)
# ---------------------------------------------------------------------------

class BulkTaskBody(BaseModel):
    ids: list[str]
    status: Optional[str] = None
    assignee: Optional[str] = None  # "" or None = unassign
    priority: Optional[int] = None
    archive: bool = False
    result: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[dict] = None
    reclaim_first: bool = False


@router.post("/tasks/bulk")
def bulk_update(payload: BulkTaskBody, board: Optional[str] = Query(None)):
    """Apply the same patch to every id in ``payload.ids``.

    This is an *independent* iteration — per-task failures don't abort
    siblings. Returns per-id outcome so the UI can surface partials.
    """
    ids = [i for i in (payload.ids or []) if i]
    if not ids:
        raise HTTPException(status_code=400, detail="ids is required")
    results: list[dict] = []
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        for tid in ids:
            entry: dict[str, Any] = {"id": tid, "ok": True}
            try:
                task = kanban_db.get_task(conn, tid)
                if task is None:
                    entry.update(ok=False, error="not found")
                    results.append(entry)
                    continue
                if payload.archive:
                    if not kanban_db.archive_task(conn, tid):
                        entry.update(ok=False, error="archive refused")
                if payload.status is not None and not payload.archive:
                    s = payload.status
                    try:
                        ok = kanban_db.move_task_status(
                            conn,
                            tid,
                            s,
                            result=payload.result,
                            summary=payload.summary,
                            metadata=payload.metadata,
                        )
                    except ValueError as exc:
                        entry.update(ok=False, error=f"unknown status {s!r}")
                        if str(exc).startswith("Cannot set status"):
                            entry["error"] = str(exc)
                        results.append(entry)
                        continue
                    if not ok:
                        entry.update(ok=False, error=f"transition to {s!r} refused")
                if payload.assignee is not None:
                    try:
                        if payload.reclaim_first:
                            ok = kanban_db.reassign_task(
                                conn, tid, payload.assignee or None,
                                reclaim_first=True,
                            )
                        else:
                            ok = kanban_db.assign_task(
                                conn, tid, payload.assignee or None,
                            )
                        if not ok:
                            entry.update(ok=False, error="assign refused")
                    except RuntimeError as e:
                        entry.update(ok=False, error=str(e))
                if payload.priority is not None:
                    with kanban_db.write_txn(conn):
                        conn.execute(
                            "UPDATE tasks SET priority = ? WHERE id = ?",
                            (int(payload.priority), tid),
                        )
                        conn.execute(
                            "INSERT INTO task_events (task_id, kind, payload, created_at) "
                            "VALUES (?, 'reprioritized', ?, ?)",
                            (tid, json.dumps({"priority": int(payload.priority)}),
                             int(time.time())),
                        )
            except Exception as e:  # defensive — one bad id shouldn't kill the batch
                entry.update(ok=False, error=str(e))
            results.append(entry)
        return {"results": results}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Diagnostics — fleet-wide distress signals (hallucinations, crashes,
# spawn failures, stuck-blocked). See hermes_cli.kanban_diagnostics for
# the rule engine.
# ---------------------------------------------------------------------------

@router.get("/diagnostics")
def list_diagnostics(
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
    severity: Optional[str] = Query(
        None,
        description="Filter by severity: warning|error|critical",
    ),
):
    """Return ``[{task_id, task_title, task_status, task_assignee,
    diagnostics: [...]}, ...]`` for every task on the board with at
    least one active diagnostic.

    Severity-filterable so the UI can render "just the critical ones"
    or the CLI can grep. Useful for the board-header attention strip
    AND for ``hermes kanban diagnostics`` which shells to this
    endpoint when the dashboard's running, or invokes the engine
    directly when it isn't.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        diags_by_task = _compute_task_diagnostics(conn, task_ids=None)
        if not diags_by_task:
            return {"diagnostics": [], "count": 0}

        # Narrow by severity if asked.
        if severity:
            filtered: dict[str, list[dict]] = {}
            for tid, dl in diags_by_task.items():
                keep = [d for d in dl if kd.severity_at_or_above(d.get("severity"), severity)]
                if keep:
                    filtered[tid] = keep
            diags_by_task = filtered
            if not diags_by_task:
                return {"diagnostics": [], "count": 0}

        # Pull the task rows we need in one query so we can include
        # titles/statuses without a per-task lookup.
        ids = list(diags_by_task.keys())
        placeholders = ",".join(["?"] * len(ids))
        rows = {
            r["id"]: r
            for r in conn.execute(
                f"SELECT id, title, status, assignee FROM tasks WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
        }

        out = []
        for tid, dl in diags_by_task.items():
            r = rows.get(tid)
            out.append({
                "task_id": tid,
                "task_title": r["title"] if r else None,
                "task_status": r["status"] if r else None,
                "task_assignee": r["assignee"] if r else None,
                "diagnostics": dl,
            })
        # Sort: highest severity first, then most recent.
        from hermes_cli.kanban_diagnostics import SEVERITY_ORDER
        sev_idx = {s: i for i, s in enumerate(SEVERITY_ORDER)}
        def _sort_key(row):
            top = row["diagnostics"][0]
            return (
                -sev_idx.get(top.get("severity"), -1),
                -(top.get("last_seen_at") or 0),
            )
        out.sort(key=_sort_key)

        return {
            "diagnostics": out,
            "count": sum(len(d["diagnostics"]) for d in out),
        }
    finally:
        conn.close()



# ---------------------------------------------------------------------------
# Command Center aggregate — one operator read model across sources/work/runs
# ---------------------------------------------------------------------------

@router.get("/command-center/snapshot")
def command_center_snapshot(
    include_archived: bool = Query(False),
    recent_run_limit_per_board: int = Query(20, ge=0, le=100),
    project: str | None = Query(None),
    include_details: bool = Query(True),
    force_refresh: bool = Query(False),
):
    """Return Sligo Labs' canonical operator read model.

    Self-improvement proposals, Discord threads, and manual Kanban entries are
    sources of Work Items; worker boards and task runs are execution detail.
    """

    return command_center.get_cached_command_center_snapshot(
        include_archived=include_archived,
        recent_run_limit_per_board=recent_run_limit_per_board,
        project=project,
        include_details=include_details,
        force_refresh=force_refresh,
    )


@router.get("/command-center/work-items/{work_item_id}")
def command_center_work_item_detail(work_item_id: str):
    """Return a full-detail Command Center Work Item for lazy row expansion."""

    return {"work_item": _find_command_center_work_item(work_item_id)}


@router.post("/command-center/work-items/{work_item_id}/annotations")
def command_center_work_item_annotation(work_item_id: str, payload: CommandCenterAnnotationBody):
    try:
        mode, text, title = command_center_annotations.validate_annotation(
            mode=payload.mode,
            text=payload.text,
            title=payload.title,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    item = _find_command_center_work_item(work_item_id)
    status = str(item.get("status") or "").lower()
    if mode == "correction" and status == "archived":
        raise HTTPException(status_code=409, detail="archived Work Items must be reopened before correction")
    target_kind, target_id = _annotation_target_from_work_item(item)
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
    annotation = command_center_annotations.record_annotation(
        work_item_id=work_item_id,
        mode=mode,
        text=text,
        title=title,
        actor=_proposal_actor(),
        target_kind=target_kind,
        target_id=target_id,
        previous_title=item.get("title"),
        previous_summary=item.get("summary"),
        previous_status=item.get("status"),
        source_ref=source.get("ref") if isinstance(source.get("ref"), dict) else source,
        execution_snapshot=execution,
    )
    followup_task = None
    worker_url = None
    errors: dict[str, str] = {}
    if mode == "correction" and status not in {"proposed", "queued", "accepted", "paused", "rejected"}:
        followup_task, worker_url, error = _create_annotation_followup(item=item, annotation=annotation)
        if error:
            errors["followup_task"] = error
    if mode == "correction" and payload.pause_current:
        pause_result, pause_error = _pause_annotation_target(item)
        if pause_error:
            errors["pause_current"] = pause_error
        else:
            annotation["pause_result"] = pause_result
    response = {
        "annotation": annotation,
        "work_item_id": work_item_id,
        "followup_task": followup_task,
        "worker_url": worker_url,
    }
    if errors:
        response["errors"] = errors
    command_center.invalidate_snapshot_cache()
    return response


# ---------------------------------------------------------------------------
# Worker visibility — cross-task active-worker list and per-run inspection
# ---------------------------------------------------------------------------

try:
    import psutil as _psutil
except ImportError:
    _psutil = None  # type: ignore[assignment]


@router.get("/workers/active")
def list_active_workers(
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Return every currently-running worker on the board.

    A worker is a ``task_runs`` row whose ``ended_at`` is NULL and whose
    ``worker_pid`` is non-NULL, belonging to a task with ``status='running'``.

    Returns ``{workers: [...], count: N, checked_at: <epoch>}``.  Each
    worker entry carries enough context for the dashboard to link back to
    its task without a second round-trip.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        rows = conn.execute(
            """
            SELECT
                r.id          AS run_id,
                r.task_id,
                t.title       AS task_title,
                t.status      AS task_status,
                t.assignee    AS task_assignee,
                r.profile,
                r.worker_pid,
                r.started_at,
                r.claim_lock,
                r.claim_expires,
                r.last_heartbeat_at,
                r.max_runtime_seconds
            FROM task_runs r
            JOIN tasks t ON t.id = r.task_id
            WHERE r.ended_at IS NULL
              AND r.worker_pid IS NOT NULL
              AND t.status = 'running'
            ORDER BY r.started_at ASC
            """,
        ).fetchall()
        workers = [
            {
                "run_id": row["run_id"],
                "task_id": row["task_id"],
                "task_title": row["task_title"],
                "task_status": row["task_status"],
                "task_assignee": row["task_assignee"],
                "profile": row["profile"],
                "worker_pid": row["worker_pid"],
                "started_at": row["started_at"],
                "claim_lock": row["claim_lock"],
                "claim_expires": row["claim_expires"],
                "last_heartbeat_at": row["last_heartbeat_at"],
                "max_runtime_seconds": row["max_runtime_seconds"],
            }
            for row in rows
        ]
        return {"workers": workers, "count": len(workers), "checked_at": int(time.time())}
    finally:
        conn.close()


@router.get("/self-improvement/proposals")
def self_improvement_proposals_endpoint():
    """Grouped self-improvement proposal cards for the dashboard.

    Response shape: ``{projects:[{project, prongs:[{prong, cards:[...]}]}]}``.
    Cards include source cron identifiers and the embedded future Kanban task
    payload, but this endpoint is read-only and never creates Kanban tasks.
    """

    return _self_improvement_grouped_with_downstream()


@router.get("/self-improvement/runs")
def self_improvement_runs_endpoint():
    """All self-improvement proposal runs, including empty and malformed runs."""

    return proposal_storage.list_runs()


@router.get("/self-improvement/proposals/{proposal_id}")
def self_improvement_proposal_detail_endpoint(proposal_id: str):
    from hermes_cli import command_center

    command_center.build_command_center_snapshot()
    card = proposal_storage.get_card(proposal_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"proposal {proposal_id!r} not found")
    return {"card": _self_improvement_card_with_downstream(card)}


@router.post("/self-improvement/proposals/{proposal_id}/approve")
def self_improvement_proposal_approve_endpoint(proposal_id: str):
    card = proposal_storage.get_card(proposal_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"proposal {proposal_id!r} not found")
    if card.get("status") == "rejected":
        raise HTTPException(status_code=409, detail="rejected proposals cannot be approved")

    task_payload = card.get("kanban_task") if isinstance(card.get("kanban_task"), dict) else {}
    existing_route = _latest_discord_approval_metadata(proposal_id)
    existing_task_id = str(card.get("kanban_task_id") or "").strip()
    if card.get("status") == "approved" and existing_task_id:
        board = str(existing_route.get("discord_board") or existing_route.get("board") or task_payload.get("board") or "").strip()
        if board == "default":
            board = ""
        task = None
        try:
            conn = _conn(board=_resolve_board(board or None))
            try:
                task = kanban_db.get_task(conn, existing_task_id)
            finally:
                conn.close()
        except HTTPException:
            task = None
        enriched = _self_improvement_card_with_downstream(card)
        worker_url = str((enriched or {}).get("worker_url") or "").strip() or _approval_worker_url_from_metadata(existing_task_id, existing_route, board or None)
        return {
            "card": enriched,
            "task": _task_dict(task) if task else None,
            "worker_url": worker_url,
        }

    channel_id = discord_publish.configured_project_channel_id(card.get("project"))
    discord_route = (
        discord_publish.publish_approved_proposal(
            card,
            channel_id=channel_id,
            existing=existing_route,
        )
        if channel_id
        else None
    )
    idempotency_key = f"self-improvement:{proposal_id}"
    task = None
    task_id = ""
    if discord_route and discord_route.thread_id and (discord_route.error or not discord_route.board):
        metadata = {
            "idempotency_key": idempotency_key,
            "board": discord_route.board or "",
            **discord_route.metadata(),
        }
        proposal_storage.record_audit_event(
            proposal_id,
            action="approval_discord_worker_failed",
            actor=_proposal_actor(),
            reason=discord_route.error or "Discord worker board was not created",
            metadata=metadata,
        )
        raise HTTPException(
            status_code=500,
            detail=f"Discord worker board failed: {discord_route.error or 'board was not created'}",
        )
    if discord_route and discord_route.board:
        route_metadata = {
            "idempotency_key": idempotency_key,
            "board": discord_route.board or "",
            **discord_route.metadata(),
        }
        proposal_storage.record_audit_event(
            proposal_id,
            action="approval_route_created",
            actor=_proposal_actor(),
            metadata=route_metadata,
        )
        discord_route = discord_publish.activate_approved_proposal(card, discord_route)
        board = discord_route.board if discord_route and discord_route.board else _resolve_board(str(task_payload.get("board") or "") or None)
        if discord_route and discord_route.error:
            proposal_storage.record_audit_event(
                proposal_id,
                action="approval_discord_worker_failed",
                actor=_proposal_actor(),
                reason=discord_route.error,
                metadata={
                    "idempotency_key": idempotency_key,
                    "board": board or "",
                    **discord_route.metadata(),
                },
            )
            raise HTTPException(status_code=500, detail=f"Discord worker activation failed: {discord_route.error}")
        task = _self_improvement_planner_task(board, card)
        if task is None:
            raise HTTPException(status_code=500, detail="Discord planner task was not created")
        task_id = task.id
    else:
        board = _resolve_board(str(task_payload.get("board") or "") or None)
        workspace = _dashboard_worker_workspace(
            kanban_db.read_board_metadata(board),
            task_payload.get("workspace_path"),
            task_payload.get("project_path"),
            card.get("workspace_path"),
            card.get("project_path"),
        )
        conn = _conn(board=board)
        try:
            task_id = kanban_db.create_task(
                conn,
                title=str(task_payload.get("title") or card["title"]),
                body=_proposal_task_body(card),
                assignee=str(task_payload.get("assignee") or "dev"),
                created_by="self-improvement",
                workspace_kind=workspace["workspace_kind"],
                workspace_path=workspace["workspace_path"],
                tenant=str(task_payload.get("tenant") or card.get("project") or "self-improvement"),
                priority=_proposal_priority(card.get("priority")),
                idempotency_key=idempotency_key,
            )
            task = kanban_db.get_task(conn, task_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        finally:
            conn.close()

    if discord_route and discord_route.board:
        board = discord_route.board

    worker_url = _approval_worker_url(task_id, discord_route, board)
    approval_metadata = {
        "idempotency_key": idempotency_key,
        "board": board or "default",
        **(discord_route.metadata() if discord_route else {}),
        **({"discord_channel_id": channel_id, "discord_publish": "unavailable"} if channel_id and not discord_route else {}),
    }
    try:
        approved = proposal_storage.record_approval(
            proposal_id,
            kanban_task_id=task_id,
            worker_url=worker_url,
            actor=_proposal_actor(),
            metadata=approval_metadata,
        )
    except Exception as exc:
        try:
            proposal_storage.record_audit_event(
                proposal_id,
                action="approval_record_failed",
                actor=_proposal_actor(),
                kanban_task_id=task_id,
                reason=str(exc),
                metadata={**approval_metadata, "worker_url": worker_url},
            )
        except Exception:
            log.exception("failed to record approval_record_failed audit event for proposal %s", proposal_id)
        raise HTTPException(
            status_code=500,
            detail="Approval route was created but final approval persistence failed; retry will reattach to the existing route.",
        ) from exc
    command_center.invalidate_snapshot_cache()
    return {"card": _self_improvement_card_with_downstream(approved), "task": _task_dict(task) if task else None, "worker_url": worker_url}


@router.post("/self-improvement/proposals/{proposal_id}/halt")
def self_improvement_proposal_halt_endpoint(proposal_id: str, payload: ProposalFollowupBody | None = None):
    card = proposal_storage.get_card(proposal_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"proposal {proposal_id!r} not found")
    task_id = card.get("kanban_task_id")
    if not task_id:
        raise HTTPException(status_code=409, detail="proposal has no downstream task")
    board = _latest_self_improvement_board(proposal_id)
    worker = _discord_worker_meta(board)
    if worker:
        board_status = _discord_worker_board_status(worker)
        if board_status in {"done", "cancelled"}:
            raise HTTPException(status_code=409, detail="downstream board is no longer in flight")
        reason = payload.reason.strip() if payload and payload.reason else "self-improvement-halted"
        from hermes_cli import discord_worker_boards as dwb

        stop_result = dwb.stop_board_execution(board, reason=reason)
        conn = _conn(board=board)
        try:
            task = kanban_db.get_task(conn, str(task_id))
            next_task = task
        finally:
            conn.close()
        proposal_storage.record_audit_event(
            proposal_id,
            action="halted",
            actor=_proposal_actor(),
            kanban_task_id=str(task_id),
            reason=(payload.reason.strip() if payload and payload.reason else None),
            metadata={"board": board, "previous_status": board_status, "stop_result": stop_result},
        )
        command_center.invalidate_snapshot_cache()
        return {"card": _self_improvement_card_with_downstream(proposal_storage.get_card(proposal_id)), "task": _task_dict(next_task) if next_task else None}

    conn = _conn(board=_resolve_board(board))
    try:
        task = kanban_db.get_task(conn, str(task_id))
        if task is None:
            raise HTTPException(status_code=404, detail=f"downstream task {task_id!r} not found")
        if task.status in {"done", "archived"}:
            raise HTTPException(status_code=409, detail="downstream task is no longer in flight")
        kanban_db.archive_task(conn, str(task_id))
        next_task = kanban_db.get_task(conn, str(task_id))
    finally:
        conn.close()
    proposal_storage.record_audit_event(
        proposal_id,
        action="halted",
        actor=_proposal_actor(),
        kanban_task_id=str(task_id),
        reason=(payload.reason.strip() if payload and payload.reason else None),
        metadata={"board": board, "previous_status": task.status},
    )
    command_center.invalidate_snapshot_cache()
    return {"card": _self_improvement_card_with_downstream(proposal_storage.get_card(proposal_id)), "task": _task_dict(next_task) if next_task else None}


@router.post("/self-improvement/proposals/{proposal_id}/archive")
def self_improvement_proposal_archive_endpoint(proposal_id: str):
    card = proposal_storage.get_card(proposal_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"proposal {proposal_id!r} not found")
    if card.get("kanban_task_id") or card.get("status") == "approved":
        raise HTTPException(status_code=409, detail="approved proposals must be halted or archived through their downstream worker")
    archived = proposal_storage.record_archive(
        proposal_id,
        actor=_proposal_actor(),
        metadata={"source": "command-center"},
    )
    command_center.invalidate_snapshot_cache()
    return {"card": _self_improvement_card_with_downstream(archived)}


@router.post("/self-improvement/proposals/{proposal_id}/pause")
def self_improvement_proposal_pause_endpoint(proposal_id: str, payload: ProposalFollowupBody | None = None):
    card = proposal_storage.get_card(proposal_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"proposal {proposal_id!r} not found")
    task_id = card.get("kanban_task_id")
    if not task_id:
        raise HTTPException(status_code=409, detail="proposal has no downstream task")
    board = _latest_self_improvement_board(proposal_id)
    reason = payload.reason.strip() if payload and payload.reason else "command-center-paused"
    worker = _discord_worker_meta(board)
    result: dict[str, Any]
    if worker:
        from hermes_cli import discord_worker_boards as dwb

        dwb.pause_board(board, reason=reason)
        result = {"board": board, "paused": True}
        conn = _conn(board=board)
    else:
        conn = _conn(board=_resolve_board(board))
        result = {"board": board, "paused": False}
    try:
        task = kanban_db.get_task(conn, str(task_id))
        if task is None:
            raise HTTPException(status_code=404, detail=f"downstream task {task_id!r} not found")
        if not worker and task.status not in {"done", "archived"}:
            result["paused"] = _pause_generic_task(conn, str(task_id), reason=reason)
        next_task = kanban_db.get_task(conn, str(task_id))
    finally:
        conn.close()
    proposal_storage.record_audit_event(
        proposal_id,
        action="paused",
        actor=_proposal_actor(),
        kanban_task_id=str(task_id),
        reason=(payload.reason.strip() if payload and payload.reason else None),
        metadata={"board": board, "previous_status": task.status, "result": result},
    )
    return {"card": _self_improvement_card_with_downstream(proposal_storage.get_card(proposal_id)), "task": _task_dict(next_task) if next_task else None}


@router.post("/self-improvement/proposals/{proposal_id}/resume")
def self_improvement_proposal_resume_endpoint(proposal_id: str):
    card = proposal_storage.get_card(proposal_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"proposal {proposal_id!r} not found")
    task_id = _self_improvement_resume_task_id(card)
    if not task_id:
        raise HTTPException(status_code=409, detail="proposal has no downstream task")
    board = _latest_self_improvement_board(proposal_id)
    worker = _discord_worker_meta(board)
    result: dict[str, Any]
    if worker:
        from hermes_cli import discord_worker_boards as dwb

        result = dwb.resume_board(board)
        conn = _conn(board=board)
    else:
        conn = _conn(board=_resolve_board(board))
        result = {"board": board, "resumed": False}
    try:
        task = kanban_db.get_task(conn, str(task_id))
        if task is None:
            raise HTTPException(status_code=404, detail=f"downstream task {task_id!r} not found")
        if not worker:
            result["resumed"] = _resume_generic_task(conn, str(task_id))
        next_task = kanban_db.get_task(conn, str(task_id))
    finally:
        conn.close()
    proposal_storage.record_audit_event(
        proposal_id,
        action="resumed",
        actor=_proposal_actor(),
        kanban_task_id=str(task_id),
        metadata={"board": board, "previous_status": task.status, "result": result},
    )
    return {"card": _self_improvement_card_with_downstream(proposal_storage.get_card(proposal_id)), "task": _task_dict(next_task) if next_task else None}


@router.post("/self-improvement/proposals/{proposal_id}/undo-followup")
def self_improvement_proposal_undo_followup_endpoint(proposal_id: str, payload: ProposalFollowupBody | None = None):
    card = proposal_storage.get_card(proposal_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"proposal {proposal_id!r} not found")
    task_id = card.get("kanban_task_id")
    if not task_id:
        raise HTTPException(status_code=409, detail="proposal has no downstream task")
    board = _latest_self_improvement_board(proposal_id)
    worker = _discord_worker_meta(board)
    if worker and _discord_worker_board_status(worker) != "done":
        raise HTTPException(status_code=409, detail="downstream board is not fully implemented")
    conn = _conn(board=_resolve_board(board))
    try:
        task = kanban_db.get_task(conn, str(task_id))
        if task is None:
            raise HTTPException(status_code=404, detail=f"downstream task {task_id!r} not found")
        if task.status != "done":
            raise HTTPException(status_code=409, detail="downstream task is not fully implemented")
        idempotency_key = f"self-improvement:{proposal_id}:undo-followup"
        workspace = _dashboard_worker_workspace(
            kanban_db.read_board_metadata(board),
            task.workspace_path,
            card.get("workspace_path"),
            card.get("project_path"),
        )
        followup_id = kanban_db.create_task(
            conn,
            title=f"Review undo path for: {card.get('title') or proposal_id}",
            body="\n".join(
                [
                    "Review the approved self-improvement change and prepare the safest undo path if needed.",
                    "",
                    f"Original proposal: {proposal_id}",
                    f"Completed downstream task: {task_id}",
                    f"Reason: {(payload.reason.strip() if payload and payload.reason else 'operator requested follow-up')}",
                ]
            ),
            assignee="dev",
            created_by="self-improvement",
            workspace_kind=workspace["workspace_kind"],
            workspace_path=workspace["workspace_path"],
            tenant=str(card.get("project") or "self-improvement"),
            priority=2,
            idempotency_key=idempotency_key,
            initial_status="blocked",
        )
        followup = kanban_db.get_task(conn, followup_id)
    finally:
        conn.close()
    proposal_storage.record_audit_event(
        proposal_id,
        action="undo_followup_requested",
        actor=_proposal_actor(),
        kanban_task_id=str(task_id),
        reason=(payload.reason.strip() if payload and payload.reason else None),
        metadata={"board": board, "followup_task_id": followup_id, "idempotency_key": idempotency_key},
    )
    return {"card": _self_improvement_card_with_downstream(proposal_storage.get_card(proposal_id)), "task": _task_dict(followup) if followup else None}


def _repair_grouped_worker_urls(grouped: dict[str, Any]) -> dict[str, Any]:
    for project in grouped.get("projects") or []:
        for prong in project.get("prongs") or []:
            for card in prong.get("cards") or []:
                if isinstance(card, dict):
                    _repair_card_worker_url(card)
    return grouped


def _repair_card_worker_url(card: dict[str, Any]) -> None:
    task_id = str(card.get("kanban_task_id") or "").strip()
    if not task_id:
        return
    current = str(card.get("worker_url") or "").strip()
    metadata = _latest_discord_approval_metadata(str(card.get("proposal_id") or ""))
    board = metadata.get("discord_board") or metadata.get("board")
    if board == "default":
        board = None
    board_public_url = metadata.get("discord_board_public_url")
    board_url = _worker_board_url(board=board, board_public_url=board_public_url)
    if not board_url:
        return

    legacy_url = _worker_url(task_id)
    ticket_url = _worker_ticket_url(task_id, board=board, board_public_url=board_public_url)
    repairable = (
        not current
        or current in {legacy_url, "/workers", board_url, ticket_url}
        or current.startswith("/workers?")
        or current.startswith(f"{board_url}/tickets/")
    )
    if repairable:
        card["worker_url"] = board_url


_DISCORD_APPROVAL_METADATA_ACTIONS = {"approved", "approval_route_created", "approval_discord_worker_failed"}


def _latest_discord_approval_metadata(proposal_id: str) -> dict[str, Any]:
    try:
        events = proposal_storage.list_audit_events(proposal_id)
    except Exception:
        return {}
    merged: dict[str, Any] = {}
    for event in events:
        if event.get("action") not in _DISCORD_APPROVAL_METADATA_ACTIONS:
            continue
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        for key, value in metadata.items():
            if value is not None and value != "":
                merged[key] = value
    return merged


@router.post("/self-improvement/proposals/{proposal_id}/reject")
def self_improvement_proposal_reject_endpoint(proposal_id: str, payload: ProposalRejectBody):
    card = proposal_storage.get_card(proposal_id)
    if card is None:
        raise HTTPException(status_code=404, detail=f"proposal {proposal_id!r} not found")
    if card.get("status") == "approved" or card.get("kanban_task_id"):
        raise HTTPException(status_code=409, detail="approved proposals cannot be rejected")
    rejected = proposal_storage.record_rejection(
        proposal_id,
        reason=payload.reason.strip(),
        actor=_proposal_actor(),
    )
    command_center.invalidate_snapshot_cache()
    return {"card": _self_improvement_card_with_downstream(rejected)}


@router.get("/self-improvement/proposals/{proposal_id}/audit")
def self_improvement_proposal_audit_endpoint(proposal_id: str):
    if proposal_storage.get_card(proposal_id) is None:
        raise HTTPException(status_code=404, detail=f"proposal {proposal_id!r} not found")
    return {"events": proposal_storage.list_audit_events(proposal_id)}


@router.get("/self-improvement/runs/{run_id}")
def self_improvement_run_detail_endpoint(run_id: str):
    run = proposal_storage.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"proposal run {run_id!r} not found")
    return {"run": run}


@router.get("/self-improvement/parse-failures")
def self_improvement_parse_failures_endpoint():
    return proposal_storage.list_parse_failures()


@router.get("/runs/{run_id}")
def get_run_endpoint(
    run_id: int,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Direct lookup of a ``task_runs`` row by its integer id.

    Returns ``{run: {...}}`` using the same serialisation as the
    per-task run history embedded in ``GET /tasks/{task_id}``.
    404 when no such run exists.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        r = kanban_db.get_run(conn, run_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        return {"run": _run_dict(r)}
    finally:
        conn.close()


@router.get("/runs/{run_id}/inspect")
def inspect_run_endpoint(
    run_id: int,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Live PID stats for a run's worker process via psutil.

    If the run has already ended, or has no recorded ``worker_pid``,
    returns ``{alive: false}`` with a human-readable ``reason``.

    When the process is live, returns CPU, memory, thread count, fd count,
    status, create_time, and cmdline.  ``access_denied`` is set when the
    OS refuses inspection rather than raising a 500.

    psutil availability: if psutil is not installed the endpoint still
    works but ``alive`` is always returned as ``false`` with
    ``reason="psutil not available"``.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        r = kanban_db.get_run(conn, run_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    finally:
        conn.close()

    if r.ended_at is not None:
        return {"run_id": run_id, "alive": False, "reason": "run already ended"}
    if r.worker_pid is None:
        return {"run_id": run_id, "alive": False, "reason": "no worker_pid recorded"}

    pid = r.worker_pid

    if _psutil is None:
        return {"run_id": run_id, "alive": False, "pid": pid, "reason": "psutil not available"}

    try:
        proc = _psutil.Process(pid)
        info = proc.as_dict(attrs=[
            "cpu_percent", "memory_info", "num_threads",
            "status", "create_time", "cmdline",
        ])
        # num_fds is POSIX-only; skip gracefully on Windows.
        try:
            num_fds = proc.num_fds()
        except AttributeError:
            num_fds = None
        mem = info.get("memory_info")
        return {
            "run_id": run_id,
            "alive": True,
            "pid": pid,
            "cpu_percent": info.get("cpu_percent"),
            "memory_rss_bytes": mem.rss if mem else None,
            "memory_vms_bytes": mem.vms if mem else None,
            "num_threads": info.get("num_threads"),
            "num_fds": num_fds,
            "status": info.get("status"),
            "create_time": info.get("create_time"),
            "cmdline": info.get("cmdline"),
        }
    except _psutil.NoSuchProcess:
        return {"run_id": run_id, "alive": False, "pid": pid, "reason": "process not found"}
    except _psutil.AccessDenied:
        return {"run_id": run_id, "alive": True, "pid": pid, "error": "access denied"}


class TerminateRunBody(BaseModel):
    reason: Optional[str] = None


@router.post("/runs/{run_id}/terminate")
def terminate_run_endpoint(
    run_id: int,
    payload: TerminateRunBody,
    board: Optional[str] = Query(None, description="Kanban board slug (omit for current)"),
):
    """Terminate the worker process backing an in-flight run.

    Resolves ``run_id`` to its parent ``task_id`` and routes through
    :func:`kanban_db.reclaim_task` so the SIGTERM->SIGKILL flow,
    run-outcome bookkeeping, and event-log append all match what the
    existing ``POST /tasks/{task_id}/reclaim`` endpoint does.

    Responses:
      * 200 ``{"ok": true, "run_id": ..., "task_id": ...}`` on success.
      * 404 when ``run_id`` is unknown.
      * 409 when the run has already ended, or the task is no longer in
        a claimable state.

    Closes the gap left by PR #28432, which shipped the read-only
    sibling endpoints (``/workers/active``, ``/runs/{run_id}``,
    ``/runs/{run_id}/inspect``) but no termination control surface.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        r = kanban_db.get_run(conn, run_id)
        if r is None:
            raise HTTPException(status_code=404, detail=f"run {run_id} not found")
        if r.ended_at is not None:
            raise HTTPException(
                status_code=409,
                detail=f"run {run_id} already ended",
            )
        ok = kanban_db.reclaim_task(conn, r.task_id, reason=payload.reason)
        if not ok:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cannot terminate run {run_id}: task {r.task_id} is no "
                    "longer in a reclaimable state"
                ),
            )
        return {"ok": True, "run_id": run_id, "task_id": r.task_id}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Recovery actions — reclaim a running claim, reassign to a new profile
# ---------------------------------------------------------------------------

class ReclaimBody(BaseModel):
    reason: Optional[str] = None


@router.post("/tasks/{task_id}/reclaim")
def reclaim_task_endpoint(
    task_id: str,
    payload: ReclaimBody,
    board: Optional[str] = Query(None),
):
    """Release an active worker claim on a running task.

    Used by the dashboard recovery popover when an operator wants to
    abort a stuck worker (e.g. one that keeps hallucinating card ids)
    without waiting for the claim TTL. Maps 1:1 to
    ``hermes kanban reclaim <task_id> --reason ...``.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        ok = kanban_db.reclaim_task(conn, task_id, reason=payload.reason)
        if not ok:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cannot reclaim {task_id}: not in a claimable state "
                    "(not running, or unknown id)"
                ),
            )
        return {"ok": True, "task_id": task_id}
    finally:
        conn.close()


class SpecifyBody(BaseModel):
    """Optional author override. Nothing else is configurable from the
    dashboard — model + prompt come from ``auxiliary.triage_specifier``
    in config.yaml, same as the CLI."""

    author: Optional[str] = None


@router.post("/tasks/{task_id}/specify")
def specify_task_endpoint(
    task_id: str,
    payload: SpecifyBody,
    board: Optional[str] = Query(None),
):
    """Flesh out a triage-column task via the auxiliary LLM and promote
    it to ``todo``. Maps 1:1 to ``hermes kanban specify <task_id>``.

    Returns the outcome shape used by the CLI: ``{ok, task_id, reason,
    new_title}``. A non-OK outcome is NOT an HTTP error — the UI renders
    the reason inline (e.g. "no auxiliary client configured") so the
    operator knows what to fix, and retries without a page reload.

    This endpoint runs in FastAPI's threadpool (sync ``def``) because
    the underlying LLM call can take tens of seconds to minutes on
    reasoning models, which would block the event loop if we used
    ``async def`` without an explicit ``run_in_executor``.
    """
    board = _resolve_board(board)
    _raise_if_corrupt_quarantined(board)
    # Pin the board for the duration of this call so the specifier module
    # (which calls ``kb.connect()`` with no args) hits the right DB.
    prev_env = os.environ.get("HERMES_KANBAN_BOARD")
    try:
        os.environ["HERMES_KANBAN_BOARD"] = board or kanban_db.DEFAULT_BOARD
        # Import lazily so a missing auxiliary client at import time
        # doesn't break plugin load.
        from hermes_cli import kanban_specify  # noqa: WPS433 (intentional)

        outcome = kanban_specify.specify_task(
            task_id,
            author=(payload.author or None),
        )
    finally:
        if prev_env is None:
            os.environ.pop("HERMES_KANBAN_BOARD", None)
        else:
            os.environ["HERMES_KANBAN_BOARD"] = prev_env

    return {
        "ok": bool(outcome.ok),
        "task_id": outcome.task_id,
        "reason": outcome.reason,
        "new_title": outcome.new_title,
    }


class ReassignBody(BaseModel):
    profile: Optional[str] = None  # "" or None = unassign
    reclaim_first: bool = False
    reason: Optional[str] = None


@router.post("/tasks/{task_id}/reassign")
def reassign_task_endpoint(
    task_id: str,
    payload: ReassignBody,
    board: Optional[str] = Query(None),
):
    """Reassign a task to a different profile, optionally reclaiming first.

    Used by the dashboard recovery popover when an operator wants to
    retry a task with a different worker profile (e.g. switch to a
    smarter model after the assigned profile keeps hallucinating).
    Maps 1:1 to ``hermes kanban reassign <task_id> <profile> [--reclaim]``.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        ok = kanban_db.reassign_task(
            conn, task_id,
            payload.profile or None,
            reclaim_first=bool(payload.reclaim_first),
            reason=payload.reason,
        )
        if not ok:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"cannot reassign {task_id}: unknown id, or still "
                    "running (pass reclaim_first=true to release the claim first)"
                ),
            )
        return {"ok": True, "task_id": task_id, "assignee": payload.profile or None}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Plugin config (read dashboard.kanban.* defaults from config.yaml)
# ---------------------------------------------------------------------------

@router.get("/config")
def get_config():
    """Return kanban dashboard preferences from ~/.hermes/config.yaml.

    Reads the ``dashboard.kanban`` section if present; defaults otherwise.
    Used by the UI to pre-select tenant filters, toggle markdown rendering,
    or set column-width preferences without a round-trip per page load.
    """
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    dash_cfg = (cfg.get("dashboard") or {})
    # dashboard.kanban may itself be a dict; fall back to {}.
    k_cfg = dash_cfg.get("kanban") or {}
    return {
        "default_tenant": k_cfg.get("default_tenant") or "",
        "lane_by_profile": bool(k_cfg.get("lane_by_profile", True)),
        "include_archived_by_default": bool(k_cfg.get("include_archived_by_default", False)),
        "render_markdown": bool(k_cfg.get("render_markdown", True)),
    }


# ---------------------------------------------------------------------------
# Home-channel subscriptions (per-task, per-platform toggles)
# ---------------------------------------------------------------------------
#
# Home channels are a first-class gateway concept — each configured platform
# can have exactly one (chat_id, thread_id, name) it considers "home". The
# dashboard surfaces these as per-task toggles so a user can opt a specific
# task into receiving terminal notifications (completed / blocked / gave_up)
# at their telegram/discord/slack home, without touching the CLI.
#
# The wire format mirrors kanban_db.add_notify_sub — (task_id, platform,
# chat_id, thread_id) — so toggle-on creates exactly the same row the
# `/kanban create` slash command would, and the existing gateway notifier
# watcher delivers events without any additional plumbing.


def _configured_home_channels() -> list[dict]:
    """Return every platform that has a home_channel set, fully hydrated.

    Reads the live GatewayConfig so env-var overlays (``TELEGRAM_HOME_CHANNEL``
    etc.) are honored alongside config.yaml. Returns platforms in a stable
    order and drops platforms without a home.
    """
    try:
        from gateway.config import load_gateway_config
    except Exception:
        return []
    try:
        gw_cfg = load_gateway_config()
    except Exception:
        return []
    result: list[dict] = []
    for platform, pcfg in gw_cfg.platforms.items():
        if not pcfg or not pcfg.home_channel:
            continue
        hc = pcfg.home_channel
        result.append({
            "platform": platform.value,
            "chat_id": hc.chat_id,
            "thread_id": hc.thread_id or "",
            "name": hc.name or "Home",
        })
    # Stable order for deterministic UI — platform name alphabetical.
    result.sort(key=lambda r: r["platform"])
    return result


def _active_profile_name() -> str:
    """Return the current Hermes profile name for notify-sub ownership."""
    try:
        from hermes_cli.profiles import get_active_profile_name
        return get_active_profile_name() or "default"
    except Exception:
        return "default"


def _home_sub_matches(sub: dict, home: dict) -> bool:
    """True if a notify_subs row corresponds to the given home channel."""
    return (
        sub.get("platform") == home["platform"]
        and str(sub.get("chat_id", "")) == str(home["chat_id"])
        and str(sub.get("thread_id") or "") == str(home["thread_id"] or "")
    )


@router.get("/home-channels")
def get_home_channels(
    task_id: Optional[str] = Query(None),
    board: Optional[str] = Query(None),
):
    """List every platform with a home channel, plus whether *task_id*
    (if given) is currently subscribed to that home.

    When ``task_id`` is omitted, every entry's ``subscribed`` is ``false``
    — useful for the "no task selected" state of the UI.
    """
    homes = _configured_home_channels()
    subscribed_homes: set[tuple[str, str, str]] = set()
    if task_id:
        board = _resolve_board(board)
        conn = _conn(board=board)
        try:
            subs = kanban_db.list_notify_subs(conn, task_id)
        finally:
            conn.close()
        for sub in subs:
            key = (
                str(sub.get("platform") or ""),
                str(sub.get("chat_id") or ""),
                str(sub.get("thread_id") or ""),
            )
            subscribed_homes.add(key)
    result = []
    for home in homes:
        key = (home["platform"], home["chat_id"], home["thread_id"])
        result.append({**home, "subscribed": key in subscribed_homes})
    return {"home_channels": result}


@router.post("/tasks/{task_id}/home-subscribe/{platform}")
def subscribe_home(task_id: str, platform: str, board: Optional[str] = Query(None)):
    """Subscribe *task_id* to notifications routed to *platform*'s home channel.

    Idempotent — re-subscribing is a no-op at the DB layer. 404 if the
    platform has no home channel configured. 404 if the task doesn't exist.
    """
    homes = _configured_home_channels()
    home = next((h for h in homes if h["platform"] == platform), None)
    if not home:
        raise HTTPException(
            status_code=404,
            detail=f"No home channel configured for platform {platform!r}. "
                   f"Set one from the messenger via /sethome, or configure "
                   f"gateway.platforms.{platform}.home_channel in config.yaml.",
        )
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id} not found")
        kanban_db.add_notify_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=home["chat_id"],
            thread_id=home["thread_id"] or None,
            notifier_profile=_active_profile_name(),
        )
        return {"ok": True, "task_id": task_id, "home_channel": home}
    finally:
        conn.close()


@router.delete("/tasks/{task_id}/home-subscribe/{platform}")
def unsubscribe_home(task_id: str, platform: str, board: Optional[str] = Query(None)):
    """Remove any notify subscription on *task_id* that matches *platform*'s home."""
    homes = _configured_home_channels()
    home = next((h for h in homes if h["platform"] == platform), None)
    if not home:
        raise HTTPException(
            status_code=404,
            detail=f"No home channel configured for platform {platform!r}.",
        )
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        kanban_db.remove_notify_sub(
            conn,
            task_id=task_id,
            platform=platform,
            chat_id=home["chat_id"],
            thread_id=home["thread_id"] or None,
        )
        return {"ok": True, "task_id": task_id, "home_channel": home}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stats (per-profile / per-status counts + oldest-ready age)
# ---------------------------------------------------------------------------

@router.get("/stats")
def get_stats(board: Optional[str] = Query(None)):
    """Per-status + per-assignee counts + oldest-ready age.

    Designed for the dashboard HUD and for router profiles that need to
    answer "is this specialist overloaded?" without scanning the whole
    board themselves.
    """
    board = _resolve_board(board)
    _raise_if_corrupt_quarantined(board)
    conn = _conn(board=board)
    try:
        return kanban_db.board_stats(conn)
    finally:
        conn.close()


@router.get("/assignees")
def get_assignees(board: Optional[str] = Query(None)):
    """Known profiles + per-profile task counts.

    Returns the union of ``~/.hermes/profiles/*`` on disk and every
    distinct assignee currently used on the board. The dashboard uses
    this to populate its assignee dropdown so a freshly-created profile
    appears in the picker before it's been given any task.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        return {"assignees": kanban_db.known_assignees(conn)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Worker log (read-only; file written by _default_spawn)
# ---------------------------------------------------------------------------

@router.get("/tasks/{task_id}/log")
def get_task_log(
    task_id: str,
    tail: Optional[int] = Query(None, ge=1, le=2_000_000),
    board: Optional[str] = Query(None),
):
    """Return the worker's stdout/stderr log.

    ``tail`` caps the response size (bytes) so the dashboard drawer
    doesn't paginate megabytes into the browser. Returns 404 if the task
    has never spawned. The on-disk log is rotated at 2 MiB per
    ``_rotate_worker_log`` — a single ``.log.1`` is kept, no further
    generations, so disk usage per task is bounded at ~4 MiB.
    """
    board = _resolve_board(board)
    conn = _conn(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
    finally:
        conn.close()
    if task is None:
        raise HTTPException(status_code=404, detail=f"task {task_id} not found")
    content = kanban_db.read_worker_log(task_id, tail_bytes=tail, board=board)
    log_path = kanban_db.worker_log_path(task_id, board=board)
    size = log_path.stat().st_size if log_path.exists() else 0
    return {
        "task_id": task_id,
        "path": str(log_path),
        "exists": content is not None,
        "size_bytes": size,
        "content": content or "",
        # Truncated when the on-disk file was larger than the tail cap.
        "truncated": bool(tail and size > tail),
    }


# ---------------------------------------------------------------------------
# Dispatch nudge (optional quick-path so the UI doesn't wait 60 s)
# ---------------------------------------------------------------------------

@router.post("/dispatch")
def dispatch(
    dry_run: bool = Query(False),
    max_n: int = Query(8, alias="max"),
    board: Optional[str] = Query(None),
):
    board = _resolve_board(board)
    _raise_if_corrupt_quarantined(board)
    conn = _conn(board=board)
    try:
        result = kanban_db.dispatch_once(
            conn, dry_run=dry_run, max_spawn=max_n, board=board,
        )
        # DispatchResult is a dataclass.
        try:
            return asdict(result)
        except TypeError:
            return {"result": str(result)}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Boards CRUD (multi-project support)
# ---------------------------------------------------------------------------

class CreateBoardBody(BaseModel):
    slug: str
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    switch: bool = False


class RenameBoardBody(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None


def _board_counts(slug: str) -> dict[str, int]:
    """Return ``{status: count}`` for a board. Safe on an empty DB."""
    if _corrupt_board_state(slug):
        return {}
    try:
        path = kanban_db.kanban_db_path(board=slug)
        if not path.exists():
            return {}
        incident = kanban_db.is_board_paused_for_corruption(slug)
        if incident and incident.get("fingerprint") == kanban_db._db_content_fingerprint(path):
            return {}
        conn = kanban_db.connect(board=slug)
        try:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
            ).fetchall()
            return {r["status"]: int(r["n"]) for r in rows}
        finally:
            conn.close()
    except Exception:
        return {}


@router.get("/boards")
def list_boards(include_archived: bool = Query(False)):
    """Return every board on disk with task counts and the active slug."""
    boards = kanban_db.list_boards(include_archived=include_archived)
    current = kanban_db.get_current_board()
    for b in boards:
        b["is_current"] = (b["slug"] == current)
        state = _corrupt_board_state(b["slug"])
        if state:
            b["status"] = "degraded"
            b["corruption"] = _corrupt_board_payload(state)
            b["counts"] = {}
        else:
            b["counts"] = _board_counts(b["slug"])
        b["total"] = sum(b["counts"].values())
    return {"boards": boards, "current": current}


@router.post("/boards")
def create_board_endpoint(payload: CreateBoardBody):
    """Create a new board. Idempotent — ``slug`` collision returns existing."""
    try:
        meta = kanban_db.create_board(
            payload.slug,
            name=payload.name,
            description=payload.description,
            icon=payload.icon,
            color=payload.color,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if payload.switch:
        try:
            kanban_db.set_current_board(meta["slug"])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return {"board": meta, "current": kanban_db.get_current_board()}


@router.patch("/boards/{slug}")
def rename_board(slug: str, payload: RenameBoardBody):
    """Update a board's display metadata (slug is immutable — create a new one to rename the directory)."""
    try:
        normed = kanban_db._normalize_board_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not normed or not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {slug!r} does not exist")
    meta = kanban_db.write_board_metadata(
        normed,
        name=payload.name,
        description=payload.description,
        icon=payload.icon,
        color=payload.color,
    )
    return {"board": meta}


@router.post("/boards/{slug}/pause")
def pause_board(slug: str, payload: ProposalFollowupBody | None = None):
    """Pause a Command Center board without archiving it."""
    try:
        normed = kanban_db._normalize_board_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not normed or not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {slug!r} does not exist")
    _raise_if_corrupt_quarantined(normed)
    reason = payload.reason.strip() if payload and payload.reason else "command-center-paused"
    worker = _discord_worker_meta(normed)
    if worker:
        from hermes_cli import discord_worker_boards as dwb

        dwb.pause_board(normed, reason=reason)
        result = {"board": normed, "paused": True}
    else:
        result = _pause_generic_board(normed, reason=reason)
    return {"result": result, "board": kanban_db.read_board_metadata(normed)}


@router.post("/boards/{slug}/resume")
def resume_board(slug: str):
    """Replay blocked Command Center board tickets while keeping /resume compatibility."""
    try:
        normed = kanban_db._normalize_board_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not normed or not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {slug!r} does not exist")
    _raise_if_corrupt_quarantined(normed)
    worker = _discord_worker_meta(normed)
    _require_command_center_board_resumable(normed, worker=worker)
    if worker:
        from hermes_cli import discord_worker_boards as dwb

        result = dwb.resume_board(normed)
    else:
        result = _resume_generic_board(normed)
    return {"result": result, "board": kanban_db.read_board_metadata(normed)}


@router.post("/boards/{slug}/repair")
def repair_board(slug: str, payload: BoardRepairBody | None = None):
    """Create or return a high-priority Foreman repair ticket for a blocked board."""
    try:
        normed = kanban_db._normalize_board_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not normed or not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {slug!r} does not exist")
    payload = payload or BoardRepairBody()
    source_task_id = str(payload.task_id or "").strip() or None
    idempotency_key = command_center.command_center_repair_idempotency_key(normed, source_task_id)
    conn = _conn(board=normed)
    created = False
    try:
        source_task = kanban_db.get_task(conn, source_task_id) if source_task_id else None
        if source_task_id and source_task is None:
            raise HTTPException(status_code=404, detail=f"task {source_task_id!r} not found on board {normed!r}")
        board_meta = kanban_db.read_board_metadata(normed)
        task_counts = _board_counts(normed)
        task = _active_repair_task(conn, idempotency_key)
        if task:
            task_id = task.id
        else:
            create_key = _repair_attempt_idempotency_key(conn, idempotency_key)
            workspace = _dashboard_worker_workspace(
                board_meta,
                source_task.workspace_path if source_task and source_task.workspace_kind == "dir" else None,
            )
            task_id = kanban_db.create_task(
                conn,
                title=f"Repair blocked board: {payload.title or board_meta.get('name') or normed}",
                body=_repair_task_body(
                    board=normed,
                    board_meta=board_meta,
                    payload=payload,
                    source_task=source_task,
                    task_counts=task_counts,
                ),
                assignee=ROLE_FOREMAN,
                created_by=command_center.COMMAND_CENTER_REPAIR_CREATED_BY,
                workspace_kind=workspace["workspace_kind"],
                workspace_path=workspace["workspace_path"],
                tenant=str(board_meta.get("project") or board_meta.get("tenant") or normed),
                priority=command_center.COMMAND_CENTER_REPAIR_PRIORITY,
                idempotency_key=create_key,
                max_runtime_seconds=1800,
            )
            created = True
            task = kanban_db.get_task(conn, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        conn.close()

    worker = _discord_worker_meta(normed)
    if worker:
        try:
            from hermes_cli import discord_worker_boards as dwb

            dwb.mark_dispatch_dirty(board=normed, reason="command-center-repair")
        except Exception:
            pass
    return {
        "created": created,
        "task": _task_dict(task) if task else None,
        "worker_url": _worker_ticket_url(task_id, board=normed, board_public_url=worker.get("public_url") if worker else None),
        "board": kanban_db.read_board_metadata(normed),
        "idempotency_key": idempotency_key,
    }


@router.post("/boards/{slug}/undo-followup")
def board_undo_followup(slug: str, payload: BoardUndoFollowupBody | None = None):
    """Create or return a follow-up task for reviewing a completed board revert path."""
    try:
        normed = kanban_db._normalize_board_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not normed or normed == kanban_db.DEFAULT_BOARD or not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {slug!r} does not exist")
    board_meta = kanban_db.read_board_metadata(normed)
    conn = _conn(board=normed)
    created = False
    idempotency_key = f"command-center-revert:{normed}"
    try:
        existing = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? AND status != 'archived' ORDER BY created_at DESC LIMIT 1",
            (idempotency_key,),
        ).fetchone()
        if existing:
            task_id = str(existing["id"])
            task = kanban_db.get_task(conn, task_id)
        else:
            if not _board_is_completed(normed, board_meta):
                raise HTTPException(status_code=409, detail="downstream board is not fully implemented")
            reason = payload.reason.strip() if payload and payload.reason else None
            task_counts = _board_counts(normed)
            workspace = _dashboard_worker_workspace(board_meta)
            task_id = kanban_db.create_task(
                conn,
                title=f"Review revert path for completed board: {board_meta.get('name') or normed}",
                body=_board_undo_followup_task_body(board=normed, board_meta=board_meta, reason=reason, task_counts=task_counts),
                assignee=ROLE_FOREMAN,
                created_by=command_center.COMMAND_CENTER_REVERT_CREATED_BY,
                workspace_kind=workspace["workspace_kind"],
                workspace_path=workspace["workspace_path"],
                tenant=str(board_meta.get("project") or board_meta.get("tenant") or normed),
                priority=command_center.COMMAND_CENTER_REPAIR_PRIORITY,
                idempotency_key=idempotency_key,
                max_runtime_seconds=1800,
                initial_status="blocked",
            )
            created = True
            task = kanban_db.get_task(conn, task_id)
    finally:
        conn.close()
    return {
        "created": created,
        "task": _task_dict(task) if task else None,
        "worker_url": _worker_ticket_url(task_id, board=normed, board_public_url=_discord_worker_meta(normed).get("public_url") if _discord_worker_meta(normed) else None),
        "board": kanban_db.read_board_metadata(normed),
        "idempotency_key": idempotency_key,
    }


@router.delete("/boards/{slug}")
def delete_board(slug: str, delete: bool = Query(False, description="Hard-delete instead of archive")):
    """Archive (default) or hard-delete a board."""
    try:
        normed = kanban_db._normalize_board_slug(slug)
        if not delete and normed and kanban_db.board_exists(normed):
            _raise_if_corrupt_quarantined(normed)
            board_meta = kanban_db.read_board_metadata(normed)
            worker = _discord_worker_meta(normed)
            if worker:
                from hermes_cli import discord_worker_boards as dwb

                terminal_before_archive = _discord_worker_board_is_terminal(worker)
                cancelled_before_archive = terminal_before_archive and _discord_worker_board_is_cancelled(worker)
                completed_before_archive = _board_is_completed(normed, board_meta)
                stale_blocked_completion = _discord_worker_board_status(worker) == "blocked" and completed_before_archive
                if (
                    terminal_before_archive
                    and not cancelled_before_archive
                    and not stale_blocked_completion
                    and dwb.board_has_unsynced_terminal_reaction(normed)
                ):
                    dwb.mark_dispatch_dirty(board=normed, reason="archive-waiting-for-terminal-reaction-sync")
                    raise HTTPException(
                        status_code=409,
                        detail="Discord worker terminal reaction has not synced yet; retry archive after sync",
                    )
                if terminal_before_archive and dwb.board_has_pending_terminal_completion_notice(normed):
                    dwb.mark_dispatch_dirty(board=normed, reason="archive-waiting-for-terminal-completion-notice")
                    raise HTTPException(
                        status_code=409,
                        detail="Discord worker completion follow-up has not posted yet; retry archive after sync",
                    )
                if not terminal_before_archive:
                    dwb.stop_board_execution(normed, reason="archived-from-command-center")
                    if dwb.board_has_unsynced_terminal_reaction(normed):
                        dwb.mark_dispatch_dirty(board=normed, reason="archive-waiting-for-terminal-reaction-sync")
                        raise HTTPException(
                            status_code=409,
                            detail="Discord worker terminal reaction has not synced yet; retry archive after sync",
                        )
                    if dwb.board_has_pending_terminal_completion_notice(normed):
                        dwb.mark_dispatch_dirty(board=normed, reason="archive-waiting-for-terminal-completion-notice")
                        raise HTTPException(
                            status_code=409,
                            detail="Discord worker completion follow-up has not posted yet; retry archive after sync",
                        )
            else:
                _stop_generic_board_for_archive(normed, reason="archived-from-command-center")
        res = kanban_db.remove_board(slug, archive=not delete)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    command_center.invalidate_snapshot_cache()
    return {"result": res, "current": kanban_db.get_current_board()}


@router.post("/boards/{slug}/switch")
def switch_board(slug: str):
    """Persist ``slug`` as the active board for subsequent CLI / slash calls.

    Dashboard users pick boards via a client-side ``localStorage`` — this
    endpoint is for ``/kanban boards switch`` parity so gateway slash
    commands and the CLI share the same current-board pointer.
    """
    try:
        normed = kanban_db._normalize_board_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not normed or not kanban_db.board_exists(normed):
        raise HTTPException(status_code=404, detail=f"board {slug!r} does not exist")
    kanban_db.set_current_board(normed)
    return {"current": normed}


# ---------------------------------------------------------------------------
# WebSocket: /events?since=<event_id>
# ---------------------------------------------------------------------------

# Poll interval for the event tail loop. SQLite WAL + 300 ms polling is
# the simplest and most robust approach; it adds a fraction of a percent
# of CPU and has no shared state to synchronize across workers.
_EVENT_POLL_SECONDS = 0.3


# ---------------------------------------------------------------------------
# Profile metadata & description editing (consumed by the kanban orchestrator)
# ---------------------------------------------------------------------------

class DescribeBody(BaseModel):
    description: Optional[str] = None  # explicit user-authored text


class DescribeAutoBody(BaseModel):
    overwrite: bool = False


@router.get("/profiles")
def list_profile_roster():
    """Return every installed profile with its description.

    Consumed by the dashboard's settings panel (orchestrator picker)
    and the profile-description editing UI. Profiles without a
    description still appear here — they're routable on name alone,
    just less precisely.
    """
    try:
        from hermes_cli import profiles as profiles_mod
        profiles = profiles_mod.list_profiles()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to list profiles: {exc}")
    return {
        "profiles": [
            {
                "name": p.name,
                "is_default": bool(p.is_default),
                "model": p.model or "",
                "provider": p.provider or "",
                "description": p.description or "",
                "description_auto": bool(p.description_auto),
                "skill_count": int(p.skill_count or 0),
            }
            for p in profiles
        ],
    }


@router.patch("/profiles/{profile_name}")
def update_profile_description(profile_name: str, payload: DescribeBody):
    """Set or clear the description of a profile.

    Empty string clears the description; non-empty stores it as a
    user-authored description (``description_auto: false``) so the
    auto-describer won't overwrite it on a sweep without
    ``--overwrite``.
    """
    try:
        from hermes_cli import profiles as profiles_mod
        canon = profiles_mod.normalize_profile_name(profile_name)
        if canon == "default":
            from hermes_constants import get_hermes_home  # type: ignore
            from pathlib import Path as _Path
            profile_dir = _Path(get_hermes_home())
        else:
            profile_dir = profiles_mod.get_profile_dir(canon)
        if not profile_dir.is_dir():
            raise HTTPException(status_code=404, detail=f"profile '{profile_name}' not found")
        text = (payload.description or "").strip()
        profiles_mod.write_profile_meta(
            profile_dir,
            description=text,
            description_auto=False,
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to update profile: {exc}")
    return {"ok": True, "profile": canon, "description": text}


@router.post("/profiles/{profile_name}/describe-auto")
def auto_describe_profile(profile_name: str, payload: DescribeAutoBody):
    """Generate a description for the named profile via the auxiliary
    LLM (``auxiliary.profile_describer``). Persists with
    ``description_auto: true`` so the dashboard can surface a "review"
    badge.

    Maps 1:1 to ``hermes profile describe <name> --auto``. Non-OK
    outcomes are NOT HTTP errors — the UI renders the reason inline
    (e.g. "no auxiliary client configured") so the operator can fix
    config and retry without a page reload.
    """
    try:
        from hermes_cli import profile_describer  # noqa: WPS433 (intentional)
        outcome = profile_describer.describe_profile(
            profile_name,
            overwrite=bool(payload.overwrite),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"describer crashed: {exc}")
    return {
        "ok": bool(outcome.ok),
        "profile": outcome.profile_name,
        "reason": outcome.reason,
        "description": outcome.description,
    }


# ---------------------------------------------------------------------------
# Decompose endpoint (orchestrator-driven fan-out)
# ---------------------------------------------------------------------------

class DecomposeBody(BaseModel):
    author: Optional[str] = None


@router.post("/tasks/{task_id}/decompose")
def decompose_task_endpoint(
    task_id: str,
    payload: DecomposeBody,
    board: Optional[str] = Query(None),
):
    """Fan a triage-column task out into a graph of child tasks via the
    auxiliary LLM, routed to specialist profiles by description. Maps
    1:1 to ``hermes kanban decompose <task_id>``.

    Returns the outcome shape used by the CLI: ``{ok, task_id, reason,
    fanout, child_ids, new_title}``. A non-OK outcome is NOT an HTTP
    error — the UI renders the reason inline.

    Runs in FastAPI's threadpool (sync ``def``) because the LLM call
    can take minutes on reasoning models.
    """
    board = _resolve_board(board)
    _raise_if_corrupt_quarantined(board)
    from hermes_cli import kanban_decompose  # noqa: WPS433 (intentional)
    outcome = kanban_decompose.decompose_task(
        task_id,
        author=(payload.author or None),
        board=board,
    )

    return {
        "ok": bool(outcome.ok),
        "task_id": outcome.task_id,
        "reason": outcome.reason,
        "fanout": bool(outcome.fanout),
        "child_ids": outcome.child_ids or [],
        "new_title": outcome.new_title,
    }


# ---------------------------------------------------------------------------
# Orchestration settings (kanban.orchestrator_profile / default_assignee /
# auto_decompose) — surfaced to the dashboard's settings panel
# ---------------------------------------------------------------------------

class OrchestrationSettingsBody(BaseModel):
    orchestrator_profile: Optional[str] = None
    default_assignee: Optional[str] = None
    auto_decompose: Optional[bool] = None
    auto_promote_children: Optional[bool] = None


@router.get("/orchestration")
def get_orchestration_settings():
    """Return the current kanban orchestration knobs from config.yaml
    plus the resolved effective values (filling in fallbacks)."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
    except Exception:
        cfg = {}
    kanban_cfg = (cfg.get("kanban") or {}) if isinstance(cfg, dict) else {}
    explicit_orch = (kanban_cfg.get("orchestrator_profile") or "").strip()
    explicit_default = (kanban_cfg.get("default_assignee") or "").strip()
    auto_decompose = bool(kanban_cfg.get("auto_decompose", True))
    auto_promote_children = bool(kanban_cfg.get("auto_promote_children", True))

    # Resolve fallbacks the same way the decomposer does.
    resolved_orch = explicit_orch
    resolved_default = explicit_default
    try:
        from hermes_cli import profiles as profiles_mod
        active_default = profiles_mod.get_active_profile_name() or "default"
        if not resolved_orch or not profiles_mod.profile_exists(resolved_orch):
            resolved_orch = active_default
        if not resolved_default or not profiles_mod.profile_exists(resolved_default):
            resolved_default = active_default
    except Exception:
        active_default = "default"
        if not resolved_orch:
            resolved_orch = active_default
        if not resolved_default:
            resolved_default = active_default

    return {
        "orchestrator_profile": explicit_orch,
        "default_assignee": explicit_default,
        "auto_decompose": auto_decompose,
        "auto_promote_children": auto_promote_children,
        "resolved_orchestrator_profile": resolved_orch,
        "resolved_default_assignee": resolved_default,
        "active_profile": active_default,
    }


@router.put("/orchestration")
def set_orchestration_settings(payload: OrchestrationSettingsBody):
    """Update the kanban orchestration knobs in ~/.hermes/config.yaml.

    Each field is optional — only fields explicitly passed are
    written. ``orchestrator_profile`` / ``default_assignee`` accept
    empty strings to clear the override and fall back to the default
    profile.
    """
    try:
        from hermes_cli.config import load_config, save_config
        cfg = load_config() or {}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to load config: {exc}")

    kanban_section = cfg.setdefault("kanban", {})
    if not isinstance(kanban_section, dict):
        kanban_section = {}
        cfg["kanban"] = kanban_section

    # Validate any non-empty profile names exist before saving.
    try:
        from hermes_cli import profiles as profiles_mod
    except Exception:
        profiles_mod = None  # type: ignore

    if payload.orchestrator_profile is not None:
        name = (payload.orchestrator_profile or "").strip()
        if name and profiles_mod is not None:
            try:
                if not profiles_mod.profile_exists(name):
                    raise HTTPException(
                        status_code=400,
                        detail=f"profile '{name}' does not exist",
                    )
            except HTTPException:
                raise
            except Exception:
                pass  # fail open if the lookup itself errors
        kanban_section["orchestrator_profile"] = name

    if payload.default_assignee is not None:
        name = (payload.default_assignee or "").strip()
        if name and profiles_mod is not None:
            try:
                if not profiles_mod.profile_exists(name):
                    raise HTTPException(
                        status_code=400,
                        detail=f"profile '{name}' does not exist",
                    )
            except HTTPException:
                raise
            except Exception:
                pass
        kanban_section["default_assignee"] = name

    if payload.auto_decompose is not None:
        kanban_section["auto_decompose"] = bool(payload.auto_decompose)

    if payload.auto_promote_children is not None:
        kanban_section["auto_promote_children"] = bool(payload.auto_promote_children)

    try:
        save_config(cfg)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"failed to save config: {exc}")

    # Echo back the resolved state (callers usually re-render from it).
    return get_orchestration_settings()


@router.websocket("/events")
async def stream_events(ws: WebSocket):
    # Enforce the dashboard session token as a query param — browsers can't
    # set Authorization on a WS upgrade. This matches how the PTY bridge
    # authenticates in hermes_cli/web_server.py.
    token = ws.query_params.get("token")
    if not _check_ws_token(token):
        await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    try:
        since_raw = ws.query_params.get("since", "0")
        try:
            cursor = int(since_raw)
        except ValueError:
            cursor = 0

        # Board selection — pinned at the WS handshake; re-subscribe to
        # switch boards. Changing boards mid-stream would require
        # reconciling two cursors, so the UI just opens a new WS on
        # board change.
        ws_board_raw = ws.query_params.get("board")
        try:
            ws_board = kanban_db._normalize_board_slug(ws_board_raw) if ws_board_raw else None
        except ValueError:
            ws_board = None

        def _fetch_new(cursor_val: int) -> tuple[int, list[dict]]:
            state = _corrupt_board_state(ws_board)
            if state:
                return cursor_val, [{
                    "id": cursor_val,
                    "task_id": None,
                    "run_id": None,
                    "kind": "kanban_board_corrupt",
                    "payload": _corrupt_board_payload(state),
                    "created_at": int(time.time()),
                }]
            conn = kanban_db.connect(board=ws_board)
            try:
                rows = conn.execute(
                    "SELECT id, task_id, run_id, kind, payload, created_at "
                    "FROM task_events WHERE id > ? ORDER BY id ASC LIMIT 200",
                    (cursor_val,),
                ).fetchall()
                out: list[dict] = []
                new_cursor = cursor_val
                for r in rows:
                    try:
                        payload = json.loads(r["payload"]) if r["payload"] else None
                    except Exception:
                        payload = None
                    out.append({
                        "id": r["id"],
                        "task_id": r["task_id"],
                        "run_id": r["run_id"],
                        "kind": r["kind"],
                        "payload": payload,
                        "created_at": r["created_at"],
                    })
                    new_cursor = r["id"]
                return new_cursor, out
            finally:
                conn.close()

        while True:
            cursor, events = await asyncio.to_thread(_fetch_new, cursor)
            if events:
                await ws.send_json({"events": events, "cursor": cursor})
            await asyncio.sleep(_EVENT_POLL_SECONDS)
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        # Normal shutdown path: dashboard process exit (Ctrl-C) cancels the
        # websocket task while it is sleeping in the poll loop.
        # CancelledError is a BaseException in 3.8+ so the bare Exception
        # handler below would not catch it; without this clause Uvicorn
        # surfaces the cancellation as an application traceback. Quiet it.
        return
    except Exception as exc:  # defensive: never crash the dashboard worker
        log.warning("Kanban event stream error: %s", exc)
        try:
            await ws.close()
        except Exception:
            pass
