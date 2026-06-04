"""Command Center read model for Sligo Labs operator surfaces.

The Command Center deliberately treats self-improvement proposals, Discord
threads, and manual Kanban tasks as *sources* of work, and worker boards / task
runs as execution detail.  It is a read-only aggregate over the existing durable
stores so the dashboard can present one coherent ledger without changing the
underlying proposal/Kanban/Discord lifecycles.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

from hermes_cli import kanban_db
from hermes_cli.discord_worker_roles import DISCORD_WORKER_META_KEY
from self_improvement import proposal_storage

_TERMINAL_TASK_STATUSES = {"done", "archived"}
_WAITING_TASK_STATUSES = {"triage", "todo", "scheduled", "ready"}
_RUNNING_TASK_STATUSES = {"running"}
_REVIEW_TASK_STATUSES = {"review"}
_BLOCKED_TASK_STATUSES = {"blocked"}


def _epoch_or_none(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            return int(value)
        text = str(value).strip()
        if not text:
            return None
        if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
            return int(text)
        iso_text = text.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(iso_text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    except (TypeError, ValueError):
        return None


def _json_loads(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed if parsed is not None else default


def _priority_rank(value: Any) -> int:
    if isinstance(value, int):
        return value
    normalized = str(value or "").strip().lower()
    return {
        "critical": 4,
        "urgent": 4,
        "high": 3,
        "medium": 2,
        "normal": 2,
        "low": 1,
    }.get(normalized, 0)


def _text_preview(*parts: Any, limit: int = 260) -> str:
    text = " ".join(" ".join(str(part or "").split()) for part in parts if str(part or "").strip()).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _worker_board_url(board: str | None, public_url: str | None = None) -> str | None:
    if public_url:
        return str(public_url).rstrip("/")
    if board and board != kanban_db.DEFAULT_BOARD:
        return f"/workers/{quote(str(board), safe='')}"
    return "/workers" if board == kanban_db.DEFAULT_BOARD else None


def _worker_ticket_url(task_id: str, *, board: str | None, public_url: str | None = None) -> str:
    board_url = _worker_board_url(board, public_url)
    if board and board != kanban_db.DEFAULT_BOARD and board_url:
        return f"{board_url}/tickets/{quote(task_id, safe='')}"
    return f"/workers?task={quote(task_id, safe='')}"


def _worker_console_url(task_id: str, *, board: str | None) -> str | None:
    if not board or board == kanban_db.DEFAULT_BOARD:
        return None
    return f"/workers/{quote(str(board), safe='')}/tickets/{quote(task_id, safe='')}/console"


def _canonical_status_from_task(task: dict[str, Any] | None) -> tuple[str, str]:
    if not task:
        return "blocked", "missing"
    status = str(task.get("status") or "").lower()
    if status in _RUNNING_TASK_STATUSES:
        return "running", status
    if status in _REVIEW_TASK_STATUSES:
        return "review", status
    if status in _BLOCKED_TASK_STATUSES:
        return "blocked", status
    if status == "done":
        return "shipped", status
    if status == "archived":
        return "archived", status
    if status in _WAITING_TASK_STATUSES:
        return "queued", status
    return "accepted", status or "unknown"


def _canonical_status_from_board(
    tasks: list[dict[str, Any]],
    *,
    board_meta: dict[str, Any],
) -> tuple[str, str]:
    worker = _board_worker_meta(board_meta)
    goal_status = str(worker.get("goal_status") or "").lower()
    if board_meta.get("archived"):
        return "archived", "archived"
    counts = _task_status_counts(tasks)
    if counts.get("blocked", 0):
        return "blocked", "blocked"
    if counts.get("running", 0):
        return "running", "running"
    if counts.get("review", 0):
        return "review", "review"
    if sum(counts.get(status, 0) for status in _WAITING_TASK_STATUSES):
        return "queued", "queued"
    active_tasks = [task for task in tasks if str(task.get("status") or "").lower() != "archived"]
    if active_tasks and all(str(task.get("status") or "").lower() == "done" for task in active_tasks):
        return "shipped", goal_status or "done"
    if goal_status in {"done", "shipped", "complete", "completed"}:
        return "shipped", goal_status
    return "accepted", goal_status or "board"


def _canonical_status_from_proposal(card: dict[str, Any], task: dict[str, Any] | None) -> tuple[str, str]:
    status = str(card.get("status") or "").lower()
    if status == "rejected" or card.get("archived_at"):
        return "rejected", status or "rejected"
    if card.get("kanban_task_id"):
        return _canonical_status_from_task(task)
    if status in {"approved", "enqueued"}:
        return "accepted", status
    return "proposed", status or "proposed"


def _latest_self_improvement_metadata(proposal_id: str) -> dict[str, Any]:
    """Return durable proposal metadata without losing approval artifacts.

    Follow-up audit events such as halt/undo often carry small metadata payloads
    that omit the Discord thread/public worker-board URLs from approval. The
    Command Center needs the latest values when present, but blank later events
    must not erase useful approval provenance, so merge chronologically instead
    of returning only the last non-empty event.
    """

    try:
        events = proposal_storage.list_audit_events(proposal_id)
    except Exception:
        return {}
    merged: dict[str, Any] = {}
    for event in events:
        raw_metadata = event.get("metadata")
        if not isinstance(raw_metadata, dict):
            continue
        for key, value in raw_metadata.items():
            if value is not None and value != "":
                merged[key] = value
    return merged


def _latest_self_improvement_board(proposal_id: str) -> str:
    metadata = _latest_self_improvement_metadata(proposal_id)
    board = metadata.get("board") or metadata.get("discord_board")
    return str(board or kanban_db.DEFAULT_BOARD)


def _task_to_dict(task: kanban_db.Task) -> dict[str, Any]:
    data = asdict(task)
    return data


def _run_to_dict(run: kanban_db.Run, *, board: str) -> dict[str, Any]:
    data = asdict(run)
    data["board"] = board
    return data


def _task_runs(conn: sqlite3.Connection, task_id: str, *, board: str, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM task_runs WHERE task_id = ? "
        "ORDER BY COALESCE(ended_at, started_at) DESC, id DESC LIMIT ?",
        (task_id, int(limit)),
    ).fetchall()
    return [_run_to_dict(kanban_db.Run.from_row(row), board=board) for row in rows]


def _recent_board_runs(conn: sqlite3.Connection, *, board: str, limit: int = 20) -> list[dict[str, Any]]:
    base_sql = (
        "SELECT r.*, t.title AS task_title, t.status AS task_status, t.assignee AS task_assignee "
        "FROM task_runs r LEFT JOIN tasks t ON t.id = r.task_id "
    )
    active_rows = conn.execute(
        base_sql
        + "WHERE r.ended_at IS NULL "
        + "ORDER BY r.started_at DESC, r.id DESC"
    ).fetchall()
    recent_rows = conn.execute(
        base_sql
        + "ORDER BY COALESCE(r.ended_at, r.started_at) DESC, r.id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    runs: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for row in [*active_rows, *recent_rows]:
        row_id = int(row["id"])
        if row_id in seen_ids:
            continue
        seen_ids.add(row_id)
        run = _run_to_dict(kanban_db.Run.from_row(row), board=board)
        run["task_title"] = row["task_title"]
        run["task_status"] = row["task_status"]
        run["task_assignee"] = row["task_assignee"]
        runs.append(run)
    return runs


def _board_worker_meta(board_meta: dict[str, Any]) -> dict[str, Any]:
    worker = board_meta.get(DISCORD_WORKER_META_KEY)
    return worker if isinstance(worker, dict) else {}


def _is_discord_board(board: str, board_meta: dict[str, Any]) -> bool:
    worker = _board_worker_meta(board_meta)
    return bool(worker) or str(board).startswith("discord-")


def _source_from_task_board(board: str, board_meta: dict[str, Any]) -> dict[str, Any]:
    worker = _board_worker_meta(board_meta)
    if _is_discord_board(board, board_meta):
        return {
            "id": f"source:discord:{board}",
            "kind": "discord",
            "label": "Discord worker thread",
            "ref": {
                "board": board,
                "thread_id": worker.get("thread_id") or worker.get("chat_id"),
                "source_message_id": worker.get("source_message_id") or worker.get("request_id"),
                "summary_message_id": worker.get("summary_message_id"),
                "guild_id": worker.get("guild_id"),
                "parent_channel_id": worker.get("parent_channel_id"),
                "public_url": worker.get("public_url"),
            },
        }
    return {
        "id": f"source:kanban-board:{board}",
        "kind": "kanban_board",
        "label": "Kanban board",
        "ref": {"board": board},
    }


def _execution_from_task(task: dict[str, Any], *, board: str, board_meta: dict[str, Any]) -> dict[str, Any]:
    worker = _board_worker_meta(board_meta)
    public_url = worker.get("public_url") if isinstance(worker, dict) else None
    task_id = str(task.get("id") or "")
    return {
        "board": board,
        "board_name": board_meta.get("name") or board,
        "task_id": task_id,
        "task_status": task.get("status"),
        "task_url": _worker_ticket_url(task_id, board=board, public_url=public_url),
        "worker_url": _worker_board_url(board, public_url),
        "console_url": _worker_console_url(task_id, board=board),
        "active_run_id": task.get("current_run_id"),
        "worker_unit": task.get("worker_unit"),
        "worker_pid": task.get("worker_pid"),
        "workspace_kind": task.get("workspace_kind"),
        "workspace_path": task.get("workspace_path"),
    }


def _execution_from_board(
    *,
    board: str,
    board_meta: dict[str, Any],
    tasks: list[dict[str, Any]],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    worker = _board_worker_meta(board_meta)
    public_url = worker.get("public_url") if isinstance(worker, dict) else None
    active_run = next((run for run in runs if _is_active_run(run)), None)
    return {
        "board": board,
        "board_name": board_meta.get("name") or board,
        "task_id": None,
        "task_status": None,
        "task_url": None,
        "worker_url": _worker_board_url(board, public_url),
        "console_url": None,
        "active_run_id": active_run.get("id") if active_run else None,
        "archive_action": f"/api/plugins/kanban/boards/{quote(board, safe='')}",
        "archiveable": board != kanban_db.DEFAULT_BOARD and not bool(board_meta.get("archived")),
        "task_counts": _task_status_counts(tasks),
        "run_count": len(runs),
    }


def _proposal_source(card: dict[str, Any]) -> dict[str, Any]:
    proposal_id = str(card.get("proposal_id") or "")
    return {
        "id": f"source:self-improvement-proposal:{proposal_id}",
        "kind": "self_improvement",
        "label": "Self-improvement recommendation",
        "ref": {
            "proposal_id": card.get("proposal_id"),
            "run_db_id": card.get("run_db_id"),
            "project": card.get("project"),
            "prong": card.get("prong"),
            "run_id": card.get("run_id"),
            "cron_job_id": card.get("cron_job_id"),
            "cron_output_path": card.get("cron_output_path"),
            "source_key": card.get("source_key"),
        },
    }


def _proposal_work_item(
    card: dict[str, Any],
    *,
    task: dict[str, Any] | None,
    runs: list[dict[str, Any]],
    board: str | None,
    board_meta: dict[str, Any] | None,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    canonical_status, status_detail = _canonical_status_from_proposal(card, task)
    public_url = metadata.get("discord_board_public_url") or metadata.get("board_public_url")
    worker_url_fallback = metadata.get("worker_url") or card.get("worker_url")
    task_id = str(card.get("kanban_task_id") or "").strip()
    execution = None
    if task_id:
        effective_board = board or kanban_db.DEFAULT_BOARD
        effective_meta = board_meta or kanban_db.read_board_metadata(effective_board)
        execution = _execution_from_task(
            task or {"id": task_id, "status": status_detail},
            board=effective_board,
            board_meta={**effective_meta, DISCORD_WORKER_META_KEY: _board_worker_meta(effective_meta)},
        )
        if public_url:
            execution["worker_url"] = _worker_board_url(effective_board, str(public_url))
            execution["task_url"] = _worker_ticket_url(task_id, board=effective_board, public_url=str(public_url))
        elif worker_url_fallback:
            execution["worker_url"] = str(worker_url_fallback)
            execution["task_url"] = str(worker_url_fallback)
    return {
        "id": f"self-improvement:{card['proposal_id']}",
        "title": card.get("title") or card.get("proposal_id"),
        "summary": card.get("summary") or _text_preview(card.get("body")),
        "body_preview": _text_preview(card.get("body"), card.get("rationale")),
        "project": card.get("project"),
        "priority": card.get("priority") or "medium",
        "priority_rank": _priority_rank(card.get("priority")),
        "severity": card.get("severity"),
        "status": canonical_status,
        "status_detail": status_detail,
        "created_at": card.get("created_at"),
        "updated_at": card.get("updated_at"),
        "source": _proposal_source(card),
        "decision": {
            "needed": canonical_status == "proposed",
            "proposal_id": card.get("proposal_id"),
            "approve_action": f"/api/plugins/kanban/self-improvement/proposals/{quote(str(card['proposal_id']), safe='')}/approve",
            "reject_action": f"/api/plugins/kanban/self-improvement/proposals/{quote(str(card['proposal_id']), safe='')}/reject",
        },
        "execution": execution,
        "runs": runs,
        "artifacts": _artifacts_from_metadata(metadata, fallback_worker_url=card.get("worker_url")),
        "source_excerpts": card.get("source_excerpts") or [],
        "raw": {"proposal": card, "task": task, "approval_metadata": metadata},
    }


def _artifacts_from_metadata(metadata: dict[str, Any], *, fallback_worker_url: Any = None) -> list[dict[str, Any]]:
    enriched = dict(metadata)
    if fallback_worker_url and not enriched.get("worker_url"):
        enriched["worker_url"] = fallback_worker_url
    artifacts: list[dict[str, Any]] = []
    for key, kind, label in (
        ("discord_thread_url", "discord_thread", "Discord thread"),
        ("discord_board_public_url", "worker_board", "Worker board"),
        ("board_public_url", "worker_board", "Worker board"),
        ("worker_url", "worker_board", "Worker board"),
    ):
        url = enriched.get(key)
        if url:
            artifacts.append({"kind": kind, "label": label, "url": str(url)})
    return artifacts


def _task_work_item(
    task: dict[str, Any],
    *,
    board: str,
    board_meta: dict[str, Any],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    canonical_status, status_detail = _canonical_status_from_task(task)
    source = _source_from_task_board(board, board_meta)
    return {
        "id": f"kanban:{board}:{task['id']}",
        "title": task.get("title") or task.get("id"),
        "summary": _text_preview(task.get("latest_summary"), task.get("result"), task.get("body")),
        "body_preview": _text_preview(task.get("body")),
        "project": task.get("tenant"),
        "priority": task.get("priority"),
        "priority_rank": _priority_rank(task.get("priority")),
        "severity": None,
        "status": canonical_status,
        "status_detail": status_detail,
        "created_at": task.get("created_at"),
        "updated_at": task.get("completed_at") or task.get("started_at") or task.get("created_at"),
        "source": {
            **source,
            "ref": {
                **source.get("ref", {}),
                "task_id": task.get("id"),
                "idempotency_key": task.get("idempotency_key"),
                "session_id": task.get("session_id"),
            },
        },
        "decision": {"needed": False},
        "execution": _execution_from_task(task, board=board, board_meta=board_meta),
        "runs": runs,
        "artifacts": _artifacts_from_task_and_board(task, board_meta),
        "source_excerpts": [],
        "raw": {"task": task, "board": board_meta},
    }


def _first_text(*values: Any) -> str | None:
    for value in values:
        if str(value or "").strip():
            return str(value).strip()
    return None


def _board_title(board: str, board_meta: dict[str, Any], proposal_action_context: dict[str, Any] | None = None) -> str:
    if proposal_action_context:
        title = _first_text(proposal_action_context.get("title"))
        if title:
            return title
    worker = _board_worker_meta(board_meta)
    for key in ("summary_title", "root_goal", "initial_request"):
        value = worker.get(key)
        if str(value or "").strip():
            return str(value).strip()
    return str(board_meta.get("name") or board)


def _board_summary(board_meta: dict[str, Any], tasks: list[dict[str, Any]], proposal_action_context: dict[str, Any] | None = None) -> str:
    if proposal_action_context:
        summary = _first_text(proposal_action_context.get("summary")) or _text_preview(proposal_action_context.get("body_preview"))
        if summary:
            return summary
    worker = _board_worker_meta(board_meta)
    summary = _text_preview(worker.get("root_goal"), worker.get("initial_request"), board_meta.get("description"))
    if summary:
        return summary
    counts = _task_status_counts(tasks)
    if counts:
        parts = [f"{count} {status}" for status, count in sorted(counts.items()) if count]
        return "Board rollup: " + ", ".join(parts)
    return "Worker board with no active task detail recorded."


def _task_status_counts(tasks: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.get("status") or "unknown").lower()
        counts[status] = counts.get(status, 0) + 1
    return counts


def _board_work_item(
    *,
    board: str,
    board_meta: dict[str, Any],
    tasks: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    proposal_action_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    canonical_status, status_detail = _canonical_status_from_board(tasks, board_meta=board_meta)
    source = _source_from_task_board(board, board_meta)
    created_candidates = [task.get("created_at") for task in tasks if task.get("created_at")]
    updated_candidates = [task.get("completed_at") or task.get("started_at") or task.get("created_at") for task in tasks]
    latest_updated = max((_epoch_or_none(value) or 0 for value in updated_candidates), default=0) or board_meta.get("created_at")
    proposal_source = proposal_action_context.get("source") if proposal_action_context else None
    decision = {"needed": False}
    if proposal_action_context:
        proposal_id = str(proposal_action_context.get("proposal_id") or "")
        if proposal_id:
            decision.update(
                {
                    "proposal_id": proposal_id,
                    "halt_action": f"/api/plugins/kanban/self-improvement/proposals/{quote(proposal_id, safe='')}/halt",
                    "undo_followup_action": f"/api/plugins/kanban/self-improvement/proposals/{quote(proposal_id, safe='')}/undo-followup",
                }
            )
    return {
        "id": f"kanban-board:{board}",
        "title": _board_title(board, board_meta, proposal_action_context),
        "summary": _board_summary(board_meta, tasks, proposal_action_context),
        "body_preview": proposal_action_context.get("body_preview") if proposal_action_context else None,
        "project": proposal_action_context.get("project") if proposal_action_context else None,
        "priority": proposal_action_context.get("priority") if proposal_action_context else None,
        "priority_rank": proposal_action_context.get("priority_rank") if proposal_action_context else 0,
        "severity": proposal_action_context.get("severity") if proposal_action_context else None,
        "status": canonical_status,
        "status_detail": status_detail,
        "created_at": min((_epoch_or_none(value) or 0 for value in created_candidates), default=0) or board_meta.get("created_at"),
        "updated_at": latest_updated,
        "source": proposal_source if isinstance(proposal_source, dict) else source,
        "decision": decision,
        "execution": _execution_from_board(board=board, board_meta=board_meta, tasks=tasks, runs=runs),
        "runs": runs,
        "artifacts": proposal_action_context.get("artifacts") if proposal_action_context else _artifacts_from_task_and_board({}, board_meta),
        "source_excerpts": proposal_action_context.get("source_excerpts") if proposal_action_context else [],
        "raw": {
            "board": board_meta,
            "rollup": {"task_counts": _task_status_counts(tasks), "task_count": len(tasks), "run_count": len(runs)},
            "proposal_action_context": proposal_action_context,
        },
    }


def _artifacts_from_task_and_board(task: dict[str, Any], board_meta: dict[str, Any]) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    worker = _board_worker_meta(board_meta)
    public_url = worker.get("public_url")
    if public_url:
        artifacts.append({"kind": "worker_board", "label": "Worker board", "url": str(public_url)})
    for key, kind, label in (
        ("pr_url", "pull_request", "Pull request"),
        ("discord_thread_url", "discord_thread", "Discord thread"),
    ):
        value = worker.get(key) or task.get(key)
        if value:
            artifacts.append({"kind": kind, "label": label, "url": str(value)})
    return artifacts


def _all_proposal_cards(*, include_archived: bool) -> list[dict[str, Any]]:
    try:
        return proposal_storage.list_cards(
            include_rejected=include_archived,
            include_archived=include_archived,
        ).get("cards", [])
    except AttributeError:
        return []


def _load_task_for_proposal(
    card: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str | None, dict[str, Any] | None]:
    task_id = str(card.get("kanban_task_id") or "").strip()
    if not task_id:
        return None, [], None, None
    board = _latest_self_improvement_board(str(card.get("proposal_id") or ""))
    if board != kanban_db.DEFAULT_BOARD and not kanban_db.board_exists(board):
        return None, [], board, kanban_db.read_board_metadata(board)
    try:
        kanban_db.init_db(board=board)
        conn = kanban_db.connect(board=board)
    except Exception:
        return None, [], board, None
    try:
        task = kanban_db.get_task(conn, task_id)
        task_dict = _task_to_dict(task) if task else None
        if task_dict:
            task_dict["latest_summary"] = kanban_db.latest_summary(conn, task_id)
        runs = _task_runs(conn, task_id, board=board)
        return task_dict, runs, board, kanban_db.read_board_metadata(board)
    finally:
        conn.close()


def _source_from_proposal_run(run: dict[str, Any]) -> dict[str, Any]:
    status = "parse_failed" if run.get("parse_error") or run.get("status") == "malformed" else str(run.get("status") or "ingested")
    return {
        "id": f"source:self-improvement-run:{run.get('id')}",
        "kind": "self_improvement_run",
        "label": "Self-improvement cron run",
        "title": f"{run.get('project') or 'unknown'} / {run.get('prong') or 'unknown'}",
        "status": status,
        "bucket": "inbox" if status == "parse_failed" else "sources",
        "created_at": run.get("created_at") or run.get("generated_at") or run.get("ingested_at"),
        "updated_at": run.get("updated_at"),
        "ref": {
            "run_db_id": run.get("id"),
            "source_key": run.get("source_key"),
            "run_id": run.get("run_id"),
            "cron_job_id": run.get("cron_job_id"),
            "cron_job_name": run.get("cron_job_name"),
            "cron_output_path": run.get("cron_output_path"),
            "source_url": run.get("source_url"),
            "card_count": run.get("card_count"),
            "parse_error": run.get("parse_error"),
        },
    }


def _source_from_discord_board(board: str, board_meta: dict[str, Any]) -> dict[str, Any]:
    worker = _board_worker_meta(board_meta)
    return {
        "id": f"source:discord:{board}",
        "kind": "discord_thread",
        "label": "Discord worker thread",
        "title": worker.get("summary_title") or worker.get("root_goal") or worker.get("initial_request") or board_meta.get("name") or board,
        "status": worker.get("goal_status") or worker.get("phase") or "active",
        "bucket": "sources",
        "created_at": worker.get("created_at") or board_meta.get("created_at"),
        "updated_at": worker.get("updated_at") or board_meta.get("created_at"),
        "ref": {
            "board": board,
            "thread_id": worker.get("thread_id") or worker.get("chat_id"),
            "source_message_id": worker.get("source_message_id") or worker.get("request_id"),
            "summary_message_id": worker.get("summary_message_id"),
            "guild_id": worker.get("guild_id"),
            "parent_channel_id": worker.get("parent_channel_id"),
            "public_url": worker.get("public_url"),
        },
    }


def build_command_center_snapshot(*, include_archived: bool = False, recent_run_limit_per_board: int = 20) -> dict[str, Any]:
    """Return the read-only Command Center snapshot.

    The response is intentionally denormalized: the frontend needs a fast,
    coherent operator view, while the existing proposal/Kanban/Discord stores
    remain the durable systems of record.
    """

    now = int(time.time())
    work_items: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    seen_execution_tasks: set[tuple[str, str]] = set()
    board_proposal_action_context: dict[str, dict[str, Any]] = {}

    for card in _all_proposal_cards(include_archived=include_archived):
        task, task_runs, board, board_meta = _load_task_for_proposal(card)
        metadata = _latest_self_improvement_metadata(str(card.get("proposal_id") or ""))
        item = _proposal_work_item(
            card,
            task=task,
            runs=task_runs,
            board=board,
            board_meta=board_meta,
            metadata=metadata,
        )
        has_board_rollup = bool(
            board
            and board != kanban_db.DEFAULT_BOARD
            and item.get("status") not in {"proposed", "rejected", "archived"}
            and kanban_db.board_exists(board)
        )
        if not has_board_rollup:
            work_items.append(item)
        elif board:
            proposal_id = card.get("proposal_id")
            if proposal_id and item.get("source", {}).get("kind") == "self_improvement":
                board_proposal_action_context.setdefault(
                    board,
                    {
                        "proposal_id": proposal_id,
                        "title": card.get("title"),
                        "summary": card.get("summary"),
                        "body_preview": item.get("body_preview"),
                        "project": item.get("project"),
                        "priority": item.get("priority"),
                        "priority_rank": item.get("priority_rank"),
                        "severity": item.get("severity"),
                        "source": item.get("source"),
                        "artifacts": item.get("artifacts") or [],
                        "source_excerpts": item.get("source_excerpts") or [],
                    },
                )
        if board and card.get("kanban_task_id"):
            seen_execution_tasks.add((board, str(card["kanban_task_id"])))

    proposal_runs = proposal_storage.list_runs().get("runs", [])
    for run in proposal_runs:
        sources.append(_source_from_proposal_run(run))

    boards = kanban_db.list_boards(include_archived=include_archived)
    for board_meta in boards:
        board = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        if _is_discord_board(board, board_meta):
            sources.append(_source_from_discord_board(board, board_meta))
        try:
            kanban_db.init_db(board=board)
            conn = kanban_db.connect(board=board)
        except Exception as exc:
            sources.append(
                {
                    "id": f"source:kanban-board-error:{board}",
                    "kind": "kanban_board",
                    "label": "Kanban board",
                    "title": board_meta.get("name") or board,
                    "status": "error",
                    "bucket": "inbox",
                    "created_at": board_meta.get("created_at"),
                    "updated_at": board_meta.get("created_at"),
                    "ref": {"board": board, "error": str(exc)},
                }
            )
            continue
        try:
            tasks = kanban_db.list_tasks(conn, include_archived=include_archived, order_by="updated")
            summaries = kanban_db.latest_summaries(conn, [task.id for task in tasks])
            task_dicts = []
            for task in tasks:
                task_dict = _task_to_dict(task)
                task_dict["latest_summary"] = summaries.get(task.id)
                task_dicts.append(task_dict)
            board_runs = _recent_board_runs(conn, board=board, limit=recent_run_limit_per_board)
            if board != kanban_db.DEFAULT_BOARD:
                work_items.append(
                    _board_work_item(
                        board=board,
                        board_meta=board_meta,
                        tasks=task_dicts,
                        runs=board_runs,
                        proposal_action_context=board_proposal_action_context.get(board),
                    )
                )
            else:
                for task_dict in task_dicts:
                    key = (board, str(task_dict.get("id")))
                    if key in seen_execution_tasks:
                        continue
                    task_runs = _task_runs(conn, str(task_dict.get("id")), board=board)
                    work_items.append(_task_work_item(task_dict, board=board, board_meta=board_meta, runs=task_runs))
            runs.extend(board_runs)
        finally:
            conn.close()

    work_items.sort(key=_work_item_sort_key)
    sources.sort(key=_source_sort_key)
    runs.sort(key=lambda run: _epoch_or_none(run.get("ended_at") or run.get("started_at")) or 0, reverse=True)

    metrics = _metrics(work_items=work_items, sources=sources, runs=runs, boards=boards)
    return {
        "schema_version": 1,
        "generated_at": now,
        "summary": "Sources create canonical Work Items; worker boards and task runs are execution detail.",
        "work_items": work_items,
        "sources": sources,
        "runs": runs,
        "boards": boards,
        "metrics": metrics,
    }


def _work_item_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    status_weight = {
        "running": 0,
        "proposed": 1,
        "blocked": 2,
        "review": 3,
        "queued": 4,
        "accepted": 5,
        "shipped": 6,
        "rejected": 7,
        "archived": 8,
    }.get(str(item.get("status") or ""), 9)
    updated = _epoch_or_none(item.get("updated_at")) or _epoch_or_none(item.get("created_at")) or 0
    return (status_weight, -updated, str(item.get("id") or ""))


def _source_sort_key(source: dict[str, Any]) -> tuple[int, int, str]:
    bucket_weight = 0 if source.get("bucket") == "inbox" else 1
    updated = _epoch_or_none(source.get("updated_at")) or _epoch_or_none(source.get("created_at")) or 0
    return (bucket_weight, -updated, str(source.get("id") or ""))


def _metrics(
    *,
    work_items: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    boards: list[dict[str, Any]],
) -> dict[str, Any]:
    by_status: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for item in work_items:
        status = str(item.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        source_kind = str((item.get("source") or {}).get("kind") or "unknown")
        by_source[source_kind] = by_source.get(source_kind, 0) + 1
    active_runs = [run for run in runs if _is_active_run(run)]
    return {
        "total_work_items": len(work_items),
        "inbox": sum(1 for item in work_items if _is_inbox_work_item(item))
        + sum(1 for source in sources if source.get("bucket") == "inbox"),
        "active_work": sum(1 for item in work_items if item.get("status") in {"queued", "running", "review"}),
        "blocked": by_status.get("blocked", 0),
        "review": by_status.get("review", 0),
        "shipped": by_status.get("shipped", 0),
        "recommendations": by_source.get("self_improvement", 0),
        "discord_origin": by_source.get("discord", 0),
        "parse_failures": sum(1 for source in sources if source.get("status") == "parse_failed"),
        "active_runs": len(active_runs),
        "recent_runs": len(runs),
        "boards": len(boards),
        "by_status": by_status,
        "by_source": by_source,
    }


def _is_inbox_work_item(item: dict[str, Any]) -> bool:
    return bool((item.get("decision") or {}).get("needed")) or item.get("status") == "proposed"


def _is_active_run(run: dict[str, Any]) -> bool:
    if run.get("ended_at") is not None:
        return False
    task_status = str(run.get("task_status") or "").lower()
    return task_status in _RUNNING_TASK_STATUSES
