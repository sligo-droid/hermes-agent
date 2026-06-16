"""Command Center read model for Sligo Labs operator surfaces.

The Command Center deliberately treats self-improvement proposals, Discord
threads, and manual Kanban tasks as *sources* of work, and worker boards / task
runs as execution detail.  It is a read-only aggregate over the existing durable
stores so the dashboard can present one coherent ledger without changing the
underlying proposal/Kanban/Discord lifecycles.
"""

from __future__ import annotations

import copy
import json
import re
import sqlite3
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

from hermes_constants import get_hermes_home
from hermes_cli import kanban_db
from hermes_cli import command_center_annotations
from hermes_cli.discord_worker_roles import DISCORD_WORKER_META_KEY
from self_improvement import proposal_storage

COMMAND_CENTER_REPAIR_CREATED_BY = "command-center-repair"
COMMAND_CENTER_REVERT_CREATED_BY = "command-center-revert"
COMMAND_CENTER_REPAIR_PRIORITY = 1_000_000
COMMAND_CENTER_REPAIR_ASSIGNEE = "foreman"
COMMAND_CENTER_REPAIR_ACTIVE_STATUSES = {"triage", "todo", "ready", "running", "review", "blocked", "scheduled"}

_TERMINAL_TASK_STATUSES = {"done", "archived"}
_TERMINAL_SUCCESS_STATUSES = {"done", "shipped", "complete", "completed"}
_WAITING_TASK_STATUSES = {"triage", "todo", "scheduled", "ready"}
_RUNNING_TASK_STATUSES = {"running"}
_REVIEW_TASK_STATUSES = {"review"}
_BLOCKED_TASK_STATUSES = {"blocked"}
_PROJECT_ALIASES = {
    "hermes": "hermes",
    "hermes-agent": "hermes",
    "dev": "hermes",
    "#dev": "hermes",
    "pid": "pid",
}
_ARCHIVED_BOARD_DIR_RE = re.compile(r"^(?P<slug>.+)-(?P<timestamp>\d{9,})(?:-\d+)?$")
_ARCHIVED_BOARD_METADATA_CACHE: tuple[tuple[str, int], list[dict[str, Any]]] | None = None
_SNAPSHOT_CACHE_TTL_SECONDS = 3.0
_SNAPSHOT_CACHE: dict[tuple[str | None, bool, int], tuple[float, dict[str, Any]]] = {}
_SNAPSHOT_CACHE_HOME: Path | None = None


def invalidate_snapshot_cache() -> None:
    """Clear cached Command Center snapshots after durable state changes."""

    _SNAPSHOT_CACHE.clear()


def _invalidate_snapshot_cache_if_home_changed() -> None:
    global _SNAPSHOT_CACHE_HOME

    home = get_hermes_home()
    if _SNAPSHOT_CACHE_HOME != home:
        invalidate_snapshot_cache()
        _SNAPSHOT_CACHE_HOME = home


def _normalize_project_key(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    normalized = text.replace(" ", "-").replace("_", "-")
    return _PROJECT_ALIASES.get(normalized, normalized)


def _configured_projects() -> list[dict[str, Any]]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
    except Exception:
        cfg = {}
    section = cfg.get("self_improvement") if isinstance(cfg, dict) else None
    configured = section.get("projects") if isinstance(section, dict) and isinstance(section.get("projects"), dict) else {}
    projects: list[dict[str, Any]] = []
    for raw_key, raw_project in configured.items():
        key = _normalize_project_key(raw_key)
        if not key or not isinstance(raw_project, dict):
            continue
        project = {
            "key": key,
            "label": str(raw_project.get("label") or raw_key),
            "description": raw_project.get("description"),
            "source_hint": raw_project.get("source_hint") or raw_project.get("discord_channel_name"),
            "discord_channel_id": raw_project.get("discord_channel_id"),
        }
        projects.append(project)
    seen = {project["key"] for project in projects}
    for key, label, hint in (("hermes", "Hermes", "#dev"), ("pid", "PID", None)):
        if key not in seen:
            projects.append({"key": key, "label": label, "source_hint": hint})
    return projects


def _project_from_worker_meta(worker: dict[str, Any]) -> str | None:
    context = worker.get("project_context") if isinstance(worker.get("project_context"), dict) else {}
    for value in (
        worker.get("project"),
        worker.get("project_key"),
        worker.get("project_name"),
        context.get("project_key"),
        context.get("project_name"),
        context.get("self_improvement_project"),
    ):
        project = _normalize_project_key(value)
        if project:
            return project
    return None


def _project_from_board(board: str, board_meta: dict[str, Any], tasks: list[dict[str, Any]] | None = None) -> str | None:
    worker = _board_worker_meta(board_meta)
    for value in (board_meta.get("project"), board_meta.get("project_key"), board_meta.get("tenant")):
        project = _normalize_project_key(value)
        if project:
            return project
    project = _project_from_worker_meta(worker)
    if project:
        return project
    if tasks:
        task_projects = {_normalize_project_key(task.get("tenant") or task.get("project")) for task in tasks}
        task_projects.discard(None)
        if len(task_projects) == 1:
            return next(iter(task_projects))
    return None


def _with_project(value: dict[str, Any], project: str | None) -> dict[str, Any]:
    if project:
        value["project"] = project
        ref = value.get("ref")
        if isinstance(ref, dict):
            ref.setdefault("project", project)
    return value


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


def _archived_board_slug_and_timestamp(path: Path) -> tuple[str, int | None]:
    match = _ARCHIVED_BOARD_DIR_RE.match(path.name)
    if not match:
        return path.name, None
    return match.group("slug"), int(match.group("timestamp"))


def _read_archived_board_metadata(path: Path) -> dict[str, Any] | None:
    """Return metadata for a board moved under ``boards/_archived``.

    Archived boards are stored as ``_archived/<slug>-<timestamp>/``. The
    normal ``kanban_db.read_board_metadata(slug)`` path cannot read them because
    it resolves only active board directories, so the Command Center has to read
    the moved directory directly to make Archive a real historical ledger.
    """

    if not path.is_dir():
        return None
    has_db = (path / "kanban.db").exists()
    has_meta = (path / "board.json").exists()
    if not (has_db or has_meta):
        return None

    inferred_slug, archived_at = _archived_board_slug_and_timestamp(path)
    raw: dict[str, Any] = {}
    if has_meta:
        try:
            parsed = json.loads((path / "board.json").read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                raw = parsed
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raw = {}

    slug = str(raw.get("slug") or inferred_slug).strip() or inferred_slug
    meta: dict[str, Any] = {
        "slug": slug,
        "name": raw.get("name") or slug,
        "description": raw.get("description") or "",
        "icon": raw.get("icon") or "",
        "color": raw.get("color") or "",
        "default_workdir": raw.get("default_workdir"),
        "created_at": raw.get("created_at"),
    }
    meta.update(raw)
    meta["slug"] = slug
    meta["archived"] = True
    meta["archive_dir"] = path.name
    meta["archive_path"] = str(path)
    meta["archive_id"] = f"archive:{path.name}"
    meta["archived_at"] = raw.get("archived_at") or archived_at or int(path.stat().st_mtime)
    meta["db_path"] = str(path / "kanban.db")
    return meta


def _archived_board_metadata() -> list[dict[str, Any]]:
    global _ARCHIVED_BOARD_METADATA_CACHE

    archive_root = kanban_db.boards_root() / "_archived"
    if not archive_root.is_dir():
        _ARCHIVED_BOARD_METADATA_CACHE = None
        return []
    cache_key = (str(archive_root), archive_root.stat().st_mtime_ns)
    if _ARCHIVED_BOARD_METADATA_CACHE and _ARCHIVED_BOARD_METADATA_CACHE[0] == cache_key:
        return copy.deepcopy(_ARCHIVED_BOARD_METADATA_CACHE[1])

    entries: list[dict[str, Any]] = []
    for child in sorted(archive_root.iterdir(), key=lambda p: p.name.lower()):
        meta = _read_archived_board_metadata(child)
        if meta is not None:
            entries.append(meta)
    _ARCHIVED_BOARD_METADATA_CACHE = (cache_key, copy.deepcopy(entries))
    return copy.deepcopy(entries)


def _archived_boards_by_slug() -> dict[str, list[dict[str, Any]]]:
    archived: dict[str, list[dict[str, Any]]] = {}
    for meta in _archived_board_metadata():
        slug = str(meta.get("slug") or "").strip()
        if slug:
            archived.setdefault(slug, []).append(meta)
    for entries in archived.values():
        entries.sort(key=lambda item: _epoch_or_none(item.get("archived_at")) or 0, reverse=True)
    return archived


def _archived_board_has_nonterminal_evidence(board_meta: dict[str, Any]) -> bool:
    worker = _board_worker_meta(board_meta)
    goal_status = str(worker.get("goal_status") or "").lower()
    phase = str(worker.get("phase") or "").lower()
    if goal_status in {"done", "shipped", "complete", "completed", "cancelled", "canceled"} or phase == "complete":
        return False
    db_path = board_meta.get("db_path")
    if not db_path or not Path(str(db_path)).exists():
        return bool(goal_status or phase)
    try:
        conn = kanban_db.connect(db_path=Path(str(db_path)))
    except Exception:
        return bool(goal_status or phase)
    try:
        tasks = [_task_to_dict(task) for task in kanban_db.list_tasks(conn, include_archived=True, order_by="updated")]
        runs = _recent_board_runs(conn, board=str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD), limit=20)
    except Exception:
        return bool(goal_status or phase)
    finally:
        conn.close()
    if any(_is_active_run(run) for run in runs):
        return True
    statuses = {str(task.get("status") or "").lower() for task in tasks}
    return any(status and status not in _TERMINAL_TASK_STATUSES for status in statuses)


def _matching_nonterminal_archive(board: str, archived_by_slug: dict[str, list[dict[str, Any]]]) -> dict[str, Any] | None:
    for archived_meta in archived_by_slug.get(board, []):
        if _archived_board_has_nonterminal_evidence(archived_meta):
            return archived_meta
    return None


def _metadata_file_is_readable(board: str) -> bool:
    path = kanban_db.board_metadata_path(board)
    if not path.exists():
        return False
    try:
        return isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False


def _active_board_repair_context(
    board: str,
    board_meta: dict[str, Any],
    *,
    archived_by_slug: dict[str, list[dict[str, Any]]],
    error: Exception | None = None,
) -> dict[str, Any] | None:
    if board == kanban_db.DEFAULT_BOARD or board_meta.get("archived"):
        return None
    reason: str | None = None
    if error is not None:
        reason = f"active worker board cannot be opened: {error}"
    elif not kanban_db.board_dir(board).is_dir():
        reason = "active worker board directory is missing."
    elif not _metadata_file_is_readable(board):
        reason = "active worker board metadata is missing or unreadable."
    elif not Path(str(board_meta.get("db_path") or kanban_db.kanban_db_path(board))).exists():
        reason = "active worker board Kanban database is missing."
    if not reason:
        return None

    context: dict[str, Any] = {"repair_required": True, "repair_reason": reason}
    archived_meta = _matching_nonterminal_archive(board, archived_by_slug)
    if archived_meta:
        context["archived_board_path"] = archived_meta.get("archive_path") or archived_meta.get("archived_path")
        context["repair_reason"] = f"{reason} Matching non-terminal board evidence is archived."
    return context


def _empty_active_board_repair_context(
    board: str,
    board_meta: dict[str, Any],
    *,
    tasks: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    archived_by_slug: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    if board == kanban_db.DEFAULT_BOARD or board_meta.get("archived") or tasks or runs:
        return None
    worker = _board_worker_meta(board_meta)
    goal_status = str(worker.get("goal_status") or "").lower()
    phase = str(worker.get("phase") or "").lower()
    if goal_status in {"done", "shipped", "complete", "completed", "cancelled", "canceled"} or phase == "complete":
        return None
    archived_meta = _matching_nonterminal_archive(board, archived_by_slug)
    if not archived_meta:
        return None
    reason = "active worker board Kanban database has no tasks or runs."
    return {
        "repair_required": True,
        "archived_board_path": archived_meta.get("archive_path") or archived_meta.get("archived_path"),
        "repair_reason": f"{reason} Matching non-terminal board evidence is archived.",
    }


def _board_identity(board: str, board_meta: dict[str, Any]) -> str:
    return str(board_meta.get("archive_id") or board)


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
        candidate = str(public_url).rstrip("/")
        path = urlsplit(candidate).path.rstrip("/")
        if path not in {"/workers", "workers"}:
            return candidate
    if board and board != kanban_db.DEFAULT_BOARD:
        return f"/workers/{quote(str(board), safe='')}"
    return None


def _worker_ticket_url(task_id: str, *, board: str | None, public_url: str | None = None) -> str:
    board_url = _worker_board_url(board, public_url)
    if board and board != kanban_db.DEFAULT_BOARD and board_url:
        return f"{board_url}/tickets/{quote(task_id, safe='')}"
    return f"/workers?task={quote(task_id, safe='')}"


def _worker_console_url(task_id: str, *, board: str | None) -> str | None:
    if not board or board == kanban_db.DEFAULT_BOARD:
        return None
    return f"/workers/{quote(str(board), safe='')}/tickets/{quote(task_id, safe='')}/console"


def command_center_repair_idempotency_key(board: str, task_id: str | None = None) -> str:
    target = str(task_id or board or kanban_db.DEFAULT_BOARD).strip() or kanban_db.DEFAULT_BOARD
    return f"command-center-repair:{board}:{target}"


def _repair_task_for_key(tasks: list[dict[str, Any]], idempotency_key: str) -> dict[str, Any] | None:
    parts = idempotency_key.split(":", 2)
    board_prefix = f"{parts[0]}:{parts[1]}:" if len(parts) == 3 and parts[2] == parts[1] else ""
    attempt_prefix = f"{idempotency_key}:"
    matches = [
        task for task in tasks
        if (
            task.get("idempotency_key") == idempotency_key
            or str(task.get("idempotency_key") or "").startswith(attempt_prefix)
            or (board_prefix and str(task.get("idempotency_key") or "").startswith(board_prefix))
        )
        and str(task.get("status") or "").lower() in COMMAND_CENTER_REPAIR_ACTIVE_STATUSES
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda item: (_epoch_or_none(item.get("created_at")) or 0, str(item.get("id") or "")), reverse=True)[0]


def _with_repair_metadata(execution: dict[str, Any], *, status: str, tasks: list[dict[str, Any]]) -> dict[str, Any]:
    if status != "blocked":
        return execution
    board = str(execution.get("board") or "").strip()
    if not board:
        return execution
    task_id = str(execution.get("task_id") or "").strip() or None
    idempotency_key = command_center_repair_idempotency_key(board, task_id)
    repair_task = _repair_task_for_key(tasks, idempotency_key)
    execution = dict(execution)
    execution["repair_action"] = f"/api/plugins/kanban/boards/{quote(board, safe='')}/repair"
    execution["repairable"] = repair_task is None
    if repair_task:
        repair_task_id = str(repair_task.get("id") or "")
        repair_task_status = str(repair_task.get("status") or "").lower()
        execution["repair_task_id"] = repair_task_id
        execution["repair_task_status"] = repair_task.get("status")
        execution["repair_worker_url"] = _worker_ticket_url(repair_task_id, board=board) if repair_task_id else None
        if repair_task_status in _BLOCKED_TASK_STATUSES:
            execution["repair_blocked"] = True
            execution["repair_status_detail"] = "Active Command Center repair task is blocked."
    return execution


def _discord_urls(ref: dict[str, Any]) -> dict[str, str]:
    guild_id = str(ref.get("guild_id") or "").strip()
    thread_id = str(ref.get("thread_id") or ref.get("chat_id") or "").strip()
    if not guild_id or not thread_id:
        return {}
    thread_url = f"https://discord.com/channels/{guild_id}/{thread_id}"
    message_id = str(ref.get("source_message_id") or ref.get("request_id") or "").strip()
    discord_url = f"{thread_url}/{message_id}" if message_id else thread_url
    return {"discord_url": discord_url, "source_url": discord_url, "discord_thread_url": thread_url}


def _has_started_execution(task: dict[str, Any] | None = None, *, runs: list[dict[str, Any]] | None = None) -> bool:
    if runs:
        return True
    if not task:
        return False
    return any(task.get(key) for key in ("current_run_id", "worker_unit", "worker_pid"))


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
    runs: list[dict[str, Any]] | None = None,
) -> tuple[str, str]:
    worker = _board_worker_meta(board_meta)
    goal_status = str(worker.get("goal_status") or "").lower()
    if board_meta.get("archived"):
        return "archived", "archived"
    phase = str(worker.get("phase") or "").lower()
    if goal_status in {"done", "shipped", "complete", "completed"} or phase == "complete":
        return "shipped", goal_status or phase or "done"
    counts = _task_status_counts(tasks)
    if counts.get("running", 0) or any(_is_active_run(run) for run in runs or []):
        return "running", "running"
    if board_meta.get("command_center_paused") or worker.get("paused") or goal_status == "paused" or phase == "paused":
        return "paused", str(worker.get("paused_reason") or "paused")
    if counts.get("blocked", 0):
        return "blocked", "blocked"
    if counts.get("review", 0):
        return "review", "review"
    if sum(counts.get(status, 0) for status in _WAITING_TASK_STATUSES):
        return "queued", "queued"
    active_tasks = [task for task in tasks if str(task.get("status") or "").lower() != "archived"]
    if active_tasks and all(str(task.get("status") or "").lower() == "done" for task in active_tasks):
        return "shipped", goal_status or "done"
    return "accepted", goal_status or "board"


def _canonical_status_from_proposal(card: dict[str, Any], task: dict[str, Any] | None) -> tuple[str, str]:
    status = str(card.get("status") or "").lower()
    if status == "completed":
        return "shipped", status
    if status == "archived" or (card.get("archived_at") and status != "rejected"):
        return "archived", status or "archived"
    if status == "rejected":
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


def _board_from_worker_url(worker_url: Any) -> str | None:
    text = str(worker_url or "").strip()
    if not text:
        return None
    path = urlsplit(text).path.rstrip("/")
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[0] == "workers":
        return parts[1]
    return None


def _proposal_board_candidates(card: dict[str, Any], metadata: dict[str, Any]) -> list[str]:
    candidates: list[str] = []
    for value in (
        metadata.get("board"),
        metadata.get("discord_board"),
        _board_from_worker_url(metadata.get("discord_board_public_url")),
        _board_from_worker_url(metadata.get("board_public_url")),
        _board_from_worker_url(metadata.get("worker_url")),
        _board_from_worker_url(card.get("worker_url")),
    ):
        text = str(value or "").strip()
        if text and text not in candidates:
            candidates.append(text)
    if kanban_db.DEFAULT_BOARD not in candidates:
        candidates.append(kanban_db.DEFAULT_BOARD)
    return candidates


def _task_from_board_db(task_id: str, *, board: str, db_path: Path) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    conn = kanban_db.connect(db_path=db_path)
    try:
        task = kanban_db.get_task(conn, task_id)
        task_dict = _task_to_dict(task) if task else None
        if task_dict:
            task_dict["latest_summary"] = kanban_db.latest_summary(conn, task_id)
        return task_dict, _task_runs(conn, task_id, board=board)
    finally:
        conn.close()


def _proposal_terminal_metadata(
    card: dict[str, Any],
    *,
    task: dict[str, Any] | None,
    board: str | None,
    board_meta: dict[str, Any] | None,
) -> dict[str, Any] | None:
    task_id = str(card.get("kanban_task_id") or "").strip()
    if task_id and task and str(task.get("status") or "").lower() == "done":
        return {
            "proposal_id": card.get("proposal_id"),
            "kanban_task_id": task_id,
            "board": board or kanban_db.DEFAULT_BOARD,
            "board_db_path": (board_meta or {}).get("db_path"),
            "archive_path": (board_meta or {}).get("archive_path") or (board_meta or {}).get("archived_path"),
            "observed_terminal_status": "done",
            "evidence_kind": "kanban_task",
        }
    worker = _board_worker_meta(board_meta or {})
    goal_status = str(worker.get("goal_status") or "").lower()
    phase = str(worker.get("phase") or "").lower()
    observed = goal_status if goal_status in _TERMINAL_SUCCESS_STATUSES else phase if phase in _TERMINAL_SUCCESS_STATUSES else ""
    if board and observed:
        return {
            "proposal_id": card.get("proposal_id"),
            "kanban_task_id": task_id or None,
            "board": board,
            "board_db_path": (board_meta or {}).get("db_path"),
            "archive_path": (board_meta or {}).get("archive_path") or (board_meta or {}).get("archived_path"),
            "observed_terminal_status": observed,
            "evidence_kind": "worker_board",
        }
    return None


def _reconcile_terminal_proposal(
    card: dict[str, Any],
    *,
    task: dict[str, Any] | None,
    board: str | None,
    board_meta: dict[str, Any] | None,
) -> dict[str, Any]:
    if str(card.get("status") or "").lower() != "approved":
        return card
    metadata = _proposal_terminal_metadata(card, task=task, board=board, board_meta=board_meta)
    if not metadata:
        return card
    return proposal_storage.record_completion(
        str(card.get("proposal_id") or ""),
        actor="command-center-reconciliation",
        reason="mapped Kanban worker reached terminal success",
        kanban_task_id=str(card.get("kanban_task_id") or "") or None,
        metadata=metadata,
    )


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
        "SELECT r.*, t.title AS task_title, t.status AS task_status, t.assignee AS task_assignee, "
        "t.created_by AS task_created_by, t.idempotency_key AS task_idempotency_key, t.body AS task_body "
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
        if board == kanban_db.DEFAULT_BOARD and _is_internal_discord_foreman_task(
            {
                "created_by": row["task_created_by"],
                "idempotency_key": row["task_idempotency_key"],
                "body": row["task_body"],
            }
        ):
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


def _is_internal_discord_foreman_task(task: dict[str, Any]) -> bool:
    if task.get("created_by") == "discord-worker-foreman":
        return True
    idempotency_key = str(task.get("idempotency_key") or "")
    if idempotency_key.startswith("discord-foreman:"):
        return True
    body = str(task.get("body") or "")
    return "<foreman-metadata>" in body


def _source_from_task_board(board: str, board_meta: dict[str, Any]) -> dict[str, Any]:
    worker = _board_worker_meta(board_meta)
    project = _project_from_board(board, board_meta)
    source_identity = _board_identity(board, board_meta)
    if _is_discord_board(board, board_meta):
        ref = {
            "board": board,
            "thread_id": worker.get("thread_id") or worker.get("chat_id"),
            "source_message_id": worker.get("source_message_id") or worker.get("request_id"),
            "summary_message_id": worker.get("summary_message_id"),
            "guild_id": worker.get("guild_id"),
            "parent_channel_id": worker.get("parent_channel_id"),
            "public_url": worker.get("public_url"),
        }
        ref.update(_discord_urls(ref))
        return _with_project({
            "id": f"source:discord:{source_identity}",
            "kind": "discord",
            "label": "Discord worker thread",
            "ref": ref,
        }, project)
    return _with_project({
        "id": f"source:kanban-board:{source_identity}",
        "kind": "kanban_board",
        "label": "Kanban board",
        "ref": {"board": board},
    }, project)


def _execution_from_task(
    task: dict[str, Any],
    *,
    board: str,
    board_meta: dict[str, Any],
    runs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    worker = _board_worker_meta(board_meta)
    public_url = worker.get("public_url") if isinstance(worker, dict) else None
    task_id = str(task.get("id") or "")
    task_status = str(task.get("status") or "").lower()
    paused = task_status in {"blocked", "scheduled"}
    started = _has_started_execution(task, runs=runs)
    execution = {
        "board": board,
        "board_name": board_meta.get("name") or board,
        "task_id": task_id,
        "task_status": task.get("status"),
        "task_url": _worker_ticket_url(task_id, board=board, public_url=public_url),
        "worker_url": _worker_board_url(board, public_url) if started else None,
        "console_url": _worker_console_url(task_id, board=board),
        "active_run_id": task.get("current_run_id"),
        "paused": paused,
        "resumable": paused,
        "worker_unit": task.get("worker_unit"),
        "worker_pid": task.get("worker_pid"),
        "workspace_kind": task.get("workspace_kind"),
        "workspace_path": task.get("workspace_path"),
    }
    return _with_repair_metadata(execution, status=_canonical_status_from_task(task)[0], tasks=[task])


def _execution_from_board(
    *,
    board: str,
    board_meta: dict[str, Any],
    tasks: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    repair_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    worker = _board_worker_meta(board_meta)
    public_url = worker.get("public_url") if isinstance(worker, dict) else None
    archived = bool(board_meta.get("archived"))
    active_run = next((run for run in runs if _is_active_run(run)), None)
    paused = bool(
        worker.get("paused")
        or board_meta.get("command_center_paused")
        or str(worker.get("goal_status") or "").lower() == "paused"
        or str(worker.get("phase") or "").lower() == "paused"
    )
    active_statuses = {str(task.get("status") or "").lower() for task in tasks}
    resumable = paused or bool(active_statuses & {"blocked", "scheduled"})
    canonical_status = _canonical_status_from_board(tasks, board_meta=board_meta, runs=runs)[0]
    execution = {
        "board": board,
        "board_name": board_meta.get("name") or board,
        "task_id": None,
        "task_status": None,
        "task_url": None,
        "worker_url": None if archived or repair_context else _worker_board_url(board, public_url),
        "console_url": None,
        "active_run_id": active_run.get("id") if active_run else None,
        "pause_action": f"/api/plugins/kanban/boards/{quote(board, safe='')}/pause",
        "resume_action": f"/api/plugins/kanban/boards/{quote(board, safe='')}/resume",
        "archive_action": f"/api/plugins/kanban/boards/{quote(board, safe='')}",
        "paused": paused,
        "resumable": board != kanban_db.DEFAULT_BOARD and not archived and not repair_context and resumable,
        "archiveable": board != kanban_db.DEFAULT_BOARD and not archived and not repair_context,
        "task_counts": _task_status_counts(tasks),
        "run_count": len(runs),
    }
    if repair_context:
        execution.update(repair_context)
    if board != kanban_db.DEFAULT_BOARD and not archived and canonical_status == "shipped":
        execution["undo_followup_action"] = f"/api/plugins/kanban/boards/{quote(board, safe='')}/undo-followup"
    return _with_repair_metadata(execution, status=canonical_status, tasks=tasks)


def _proposal_source(card: dict[str, Any]) -> dict[str, Any]:
    proposal_id = str(card.get("proposal_id") or "")
    project = _normalize_project_key(card.get("project"))
    return _with_project({
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
    }, project)


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
            runs=runs,
        )
        if public_url:
            if _has_started_execution(task, runs=runs):
                execution["worker_url"] = _worker_board_url(effective_board, str(public_url))
            execution["task_url"] = _worker_ticket_url(task_id, board=effective_board, public_url=str(public_url))
        elif worker_url_fallback:
            execution["task_url"] = str(worker_url_fallback)
    return {
        "id": f"self-improvement:{card['proposal_id']}",
        "title": card.get("title") or card.get("proposal_id"),
        "summary": card.get("summary") or _text_preview(card.get("body")),
        "body_preview": _text_preview(card.get("body"), card.get("rationale")),
        "project": _normalize_project_key(card.get("project")) or card.get("project"),
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
            "pause_action": f"/api/plugins/kanban/self-improvement/proposals/{quote(str(card['proposal_id']), safe='')}/pause",
            "resume_action": f"/api/plugins/kanban/self-improvement/proposals/{quote(str(card['proposal_id']), safe='')}/resume",
            "archive_action": f"/api/plugins/kanban/self-improvement/proposals/{quote(str(card['proposal_id']), safe='')}/archive" if canonical_status == "proposed" else f"/api/plugins/kanban/self-improvement/proposals/{quote(str(card['proposal_id']), safe='')}/halt",
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
    board = enriched.get("discord_board") or enriched.get("board")
    artifacts: list[dict[str, Any]] = []
    for key, kind, label in (
        ("discord_thread_url", "discord_thread", "Discord thread"),
        ("discord_board_public_url", "worker_board", "Worker board"),
        ("board_public_url", "worker_board", "Worker board"),
        ("worker_url", "worker_board", "Worker board"),
    ):
        url = enriched.get(key)
        if url:
            if kind == "worker_board":
                url = _worker_board_url(str(board or "") or None, str(url)) or url
            artifacts.append({"kind": kind, "label": label, "url": str(url)})
    return artifacts


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
    repair_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    canonical_status, status_detail = _canonical_status_from_board(tasks, board_meta=board_meta, runs=runs)
    if repair_context and canonical_status in {"running", "accepted", "queued", "review"}:
        canonical_status = "blocked"
        status_detail = "repair_required"
    execution = _execution_from_board(board=board, board_meta=board_meta, tasks=tasks, runs=runs, repair_context=repair_context)
    if canonical_status == "blocked" and execution.get("repair_blocked"):
        canonical_status = "mega_blocked"
        status_detail = "repair_blocked"
    source = _source_from_task_board(board, board_meta)
    project = (
        _normalize_project_key(proposal_action_context.get("project"))
        if proposal_action_context
        else None
    ) or _project_from_board(board, board_meta, tasks)
    created_candidates = [task.get("created_at") for task in tasks if task.get("created_at")]
    updated_candidates = [task.get("completed_at") or task.get("started_at") or task.get("created_at") for task in tasks]
    latest_updated = board_meta.get("archived_at") or max((_epoch_or_none(value) or 0 for value in updated_candidates), default=0) or board_meta.get("created_at")
    proposal_source = proposal_action_context.get("source") if proposal_action_context else None
    decision = {"needed": False}
    if proposal_action_context:
        proposal_id = str(proposal_action_context.get("proposal_id") or "")
        if proposal_id:
            decision.update(
                {
                    "proposal_id": proposal_id,
                    "halt_action": f"/api/plugins/kanban/self-improvement/proposals/{quote(proposal_id, safe='')}/halt",
                    "pause_action": f"/api/plugins/kanban/self-improvement/proposals/{quote(proposal_id, safe='')}/pause",
                    "resume_action": f"/api/plugins/kanban/self-improvement/proposals/{quote(proposal_id, safe='')}/resume",
                    "undo_followup_action": f"/api/plugins/kanban/self-improvement/proposals/{quote(proposal_id, safe='')}/undo-followup",
                }
            )
    return {
        "id": f"kanban-board:{_board_identity(board, board_meta)}",
        "title": _board_title(board, board_meta, proposal_action_context),
        "summary": _board_summary(board_meta, tasks, proposal_action_context),
        "body_preview": proposal_action_context.get("body_preview") if proposal_action_context else None,
        "project": project,
        "priority": proposal_action_context.get("priority") if proposal_action_context else None,
        "priority_rank": proposal_action_context.get("priority_rank") if proposal_action_context else 0,
        "severity": proposal_action_context.get("severity") if proposal_action_context else None,
        "status": canonical_status,
        "status_detail": status_detail,
        "created_at": min((_epoch_or_none(value) or 0 for value in created_candidates), default=0) or board_meta.get("created_at"),
        "updated_at": latest_updated,
        "source": proposal_source if isinstance(proposal_source, dict) else source,
        "decision": decision,
        "execution": execution,
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
    discord_ref = {
        "thread_id": worker.get("thread_id") or worker.get("chat_id"),
        "source_message_id": worker.get("source_message_id") or worker.get("request_id"),
        "guild_id": worker.get("guild_id"),
    }
    discord_url = _discord_urls(discord_ref).get("discord_url")
    if discord_url:
        artifacts.append({"kind": "discord_source", "label": "Discord", "url": discord_url})
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
    proposal_id = str(card.get("proposal_id") or "")
    metadata = _latest_self_improvement_metadata(proposal_id)
    board_candidates = _proposal_board_candidates(card, metadata)
    board_metas = kanban_db.list_boards(include_archived=True)
    by_slug: dict[str, list[dict[str, Any]]] = {}
    for board_meta in [*board_metas, *[meta for entries in _archived_boards_by_slug().values() for meta in entries]]:
        slug = str(board_meta.get("slug") or "").strip()
        if slug:
            by_slug.setdefault(slug, []).append(board_meta)
    fallback: tuple[dict[str, Any] | None, list[dict[str, Any]], str | None, dict[str, Any] | None] | None = None
    for board in board_candidates:
        metas = by_slug.get(board)
        if not metas:
            if board != kanban_db.DEFAULT_BOARD and not kanban_db.board_exists(board):
                fallback = fallback or (None, [], board, kanban_db.read_board_metadata(board))
                continue
            metas = [kanban_db.read_board_metadata(board)]
        for board_meta in metas:
            db_path_value = board_meta.get("db_path")
            try:
                if board_meta.get("archived") or board_meta.get("archive_path") or board_meta.get("archived_path"):
                    if not db_path_value:
                        continue
                    db_path = Path(str(db_path_value))
                    if not db_path.exists():
                        continue
                    task, runs = _task_from_board_db(task_id, board=board, db_path=db_path)
                else:
                    kanban_db.init_db(board=board)
                    task, runs = _task_from_board_db(task_id, board=board, db_path=kanban_db.kanban_db_path(board))
            except Exception:
                fallback = fallback or (None, [], board, None)
                continue
            if task:
                return task, runs, board, board_meta
            fallback = fallback or (None, runs, board, board_meta)
    return fallback or (None, [], board_candidates[0] if board_candidates else None, None)


def _source_from_proposal_run(run: dict[str, Any]) -> dict[str, Any]:
    status = "parse_failed" if run.get("parse_error") or run.get("status") == "malformed" else str(run.get("status") or "ingested")
    project = _normalize_project_key(run.get("project"))
    return _with_project({
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
    }, project)


def _source_from_discord_board(board: str, board_meta: dict[str, Any]) -> dict[str, Any]:
    worker = _board_worker_meta(board_meta)
    project = _project_from_board(board, board_meta)
    source_identity = _board_identity(board, board_meta)
    ref = {
        "board": board,
        "thread_id": worker.get("thread_id") or worker.get("chat_id"),
        "source_message_id": worker.get("source_message_id") or worker.get("request_id"),
        "summary_message_id": worker.get("summary_message_id"),
        "guild_id": worker.get("guild_id"),
        "parent_channel_id": worker.get("parent_channel_id"),
        "public_url": worker.get("public_url"),
    }
    ref.update(_discord_urls(ref))
    return _with_project({
        "id": f"source:discord:{source_identity}",
        "kind": "discord_thread",
        "label": "Discord worker thread",
        "title": worker.get("summary_title") or worker.get("root_goal") or worker.get("initial_request") or board_meta.get("name") or board,
        "status": "archived" if board_meta.get("archived") else worker.get("goal_status") or worker.get("phase") or "active",
        "bucket": "sources",
        "created_at": worker.get("created_at") or board_meta.get("created_at"),
        "updated_at": board_meta.get("archived_at") or worker.get("updated_at") or board_meta.get("created_at"),
        "ref": ref,
    }, project)


def get_cached_command_center_snapshot(
    *,
    include_archived: bool = False,
    recent_run_limit_per_board: int = 20,
    project: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Return a short-lived cached Command Center snapshot for API polling."""

    cache_key = (_normalize_project_key(project), bool(include_archived), int(recent_run_limit_per_board))
    _invalidate_snapshot_cache_if_home_changed()
    monotonic_now = time.monotonic()
    if not force_refresh:
        cached = _SNAPSHOT_CACHE.get(cache_key)
        if cached and monotonic_now - cached[0] < _SNAPSHOT_CACHE_TTL_SECONDS:
            return copy.deepcopy(cached[1])

    snapshot = build_command_center_snapshot(
        include_archived=include_archived,
        recent_run_limit_per_board=recent_run_limit_per_board,
        project=project,
    )
    _SNAPSHOT_CACHE[cache_key] = (monotonic_now, copy.deepcopy(snapshot))
    return snapshot


def build_command_center_snapshot(*, include_archived: bool = False, recent_run_limit_per_board: int = 20, project: str | None = None) -> dict[str, Any]:
    """Return the read-only Command Center snapshot.

    The response is intentionally denormalized: the frontend needs a fast,
    coherent operator view, while the existing proposal/Kanban/Discord stores
    remain the durable systems of record.
    """

    now = int(time.time())
    work_items: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    runs: list[dict[str, Any]] = []
    board_proposal_action_context: dict[str, dict[str, Any]] = {}

    for card in _all_proposal_cards(include_archived=include_archived):
        task, task_runs, board, board_meta = _load_task_for_proposal(card)
        card = _reconcile_terminal_proposal(card, task=task, board=board, board_meta=board_meta)
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
        if not has_board_rollup and item.get("status") in {"proposed", "rejected", "archived"}:
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
    proposal_runs = proposal_storage.list_runs().get("runs", [])
    for run in proposal_runs:
        sources.append(_source_from_proposal_run(run))

    archived_by_slug = _archived_boards_by_slug()
    boards = kanban_db.list_boards(include_archived=include_archived)
    if include_archived:
        boards.extend(meta for entries in archived_by_slug.values() for meta in entries)
    for board_meta in boards:
        board = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        if _is_discord_board(board, board_meta):
            sources.append(_source_from_discord_board(board, board_meta))
        corrupt_state = None
        if not board_meta.get("archive_path"):
            try:
                corrupt_state = kanban_db.corrupt_board_quarantine_state(board)
            except Exception:
                corrupt_state = None
        if corrupt_state and corrupt_state.get("skipped"):
            incident = corrupt_state.get("incident") if isinstance(corrupt_state.get("incident"), dict) else {}
            sources.append(
                {
                    "id": f"source:kanban-board-error:{_board_identity(board, board_meta)}",
                    "kind": "kanban_board",
                    "label": "Kanban board",
                    "title": board_meta.get("name") or board,
                    "status": "degraded",
                    "bucket": "inbox",
                    "created_at": board_meta.get("created_at"),
                    "updated_at": incident.get("last_seen") or board_meta.get("created_at"),
                    "ref": {
                        "board": board,
                        "error": corrupt_state.get("reason") or incident.get("reason"),
                        "corruption": {
                            "db_path": corrupt_state.get("db_path") or incident.get("db_path"),
                            "first_seen": incident.get("first_seen"),
                            "last_seen": incident.get("last_seen"),
                            "next_retry": corrupt_state.get("next_retry") or incident.get("next_retry"),
                            "quarantine_path": incident.get("quarantine_path"),
                        },
                    },
                }
            )
            continue
        repair_context = _active_board_repair_context(board, board_meta, archived_by_slug=archived_by_slug)
        if repair_context and board != kanban_db.DEFAULT_BOARD and not board_meta.get("archived"):
            work_items.append(
                _board_work_item(
                    board=board,
                    board_meta=board_meta,
                    tasks=[],
                    runs=[],
                    proposal_action_context=board_proposal_action_context.get(board),
                    repair_context=repair_context,
                )
            )
            continue
        try:
            db_path = board_meta.get("db_path")
            if board_meta.get("archive_path") and db_path:
                archive_db_path = Path(str(db_path))
                if not archive_db_path.exists():
                    raise FileNotFoundError(str(archive_db_path))
                conn = kanban_db.connect(db_path=archive_db_path)
            else:
                conn = kanban_db.connect(board=board)
        except Exception as exc:
            repair_context = _active_board_repair_context(board, board_meta, archived_by_slug=archived_by_slug, error=exc)
            if repair_context and board != kanban_db.DEFAULT_BOARD and not board_meta.get("archived"):
                work_items.append(
                    _board_work_item(
                        board=board,
                        board_meta=board_meta,
                        tasks=[],
                        runs=[],
                        proposal_action_context=board_proposal_action_context.get(board),
                        repair_context=repair_context,
                    )
                )
            sources.append(
                {
                    "id": f"source:kanban-board-error:{_board_identity(board, board_meta)}",
                    "kind": "kanban_board",
                    "label": "Kanban board",
                    "title": board_meta.get("name") or board,
                    "status": "error",
                    "bucket": "inbox",
                    "created_at": board_meta.get("created_at"),
                    "updated_at": board_meta.get("archived_at") or board_meta.get("created_at"),
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
            repair_context = repair_context or _empty_active_board_repair_context(
                board,
                board_meta,
                tasks=task_dicts,
                runs=board_runs,
                archived_by_slug=archived_by_slug,
            )
            if board != kanban_db.DEFAULT_BOARD:
                work_items.append(
                    _board_work_item(
                        board=board,
                        board_meta=board_meta,
                        tasks=task_dicts,
                        runs=board_runs,
                        proposal_action_context=board_proposal_action_context.get(board),
                        repair_context=repair_context,
                    )
                )
            runs.extend(board_runs)
        finally:
            conn.close()

    projects = _configured_projects()
    project_filter = _normalize_project_key(project)
    if project_filter:
        work_items, sources, runs, boards = _filter_snapshot_by_project(
            project_filter,
            work_items=work_items,
            sources=sources,
            runs=runs,
            boards=boards,
        )

    command_center_annotations.enrich_work_items(work_items)

    work_items.sort(key=_work_item_sort_key)
    sources.sort(key=_source_sort_key)
    runs.sort(key=lambda run: _epoch_or_none(run.get("ended_at") or run.get("started_at")) or 0, reverse=True)

    metrics = _metrics(work_items=work_items, sources=sources, runs=runs, boards=boards)
    return {
        "schema_version": 1,
        "generated_at": now,
        "summary": "Sources create canonical Work Items; worker boards and task runs are execution detail.",
        "projects": projects,
        "current_project": project_filter,
        "work_items": work_items,
        "sources": sources,
        "runs": runs,
        "boards": boards,
        "metrics": metrics,
    }


def _item_project(item: dict[str, Any]) -> str | None:
    project = _normalize_project_key(item.get("project"))
    if project:
        return project
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    project = _normalize_project_key(source.get("project"))
    if project:
        return project
    ref = source.get("ref") if isinstance(source.get("ref"), dict) else {}
    return _normalize_project_key(ref.get("project"))


def _source_project(source: dict[str, Any]) -> str | None:
    project = _normalize_project_key(source.get("project"))
    if project:
        return project
    ref = source.get("ref") if isinstance(source.get("ref"), dict) else {}
    return _normalize_project_key(ref.get("project"))


def _run_project(run: dict[str, Any], board_projects: dict[str, str | None], task_projects: dict[tuple[str, str], str | None]) -> str | None:
    board = str(run.get("board") or kanban_db.DEFAULT_BOARD)
    task_id = str(run.get("task_id") or "")
    return task_projects.get((board, task_id)) or board_projects.get(board)


def _filter_snapshot_by_project(
    project: str,
    *,
    work_items: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    boards: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    included_items = [item for item in work_items if _item_project(item) == project]
    source_ids = {str((item.get("source") or {}).get("id") or "") for item in included_items}
    included_boards = {
        str((item.get("execution") or {}).get("board") or "")
        for item in included_items
        if isinstance(item.get("execution"), dict) and (item.get("execution") or {}).get("board")
    }
    included_tasks = {
        (
            str((item.get("execution") or {}).get("board") or kanban_db.DEFAULT_BOARD),
            str((item.get("execution") or {}).get("task_id") or ""),
        )
        for item in included_items
        if isinstance(item.get("execution"), dict) and (item.get("execution") or {}).get("task_id")
    }
    board_projects = {
        str(board.get("slug") or kanban_db.DEFAULT_BOARD): _project_from_board(str(board.get("slug") or kanban_db.DEFAULT_BOARD), board)
        for board in boards
    }
    task_projects: dict[tuple[str, str], str | None] = {}
    for item in work_items:
        execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
        board = str(execution.get("board") or kanban_db.DEFAULT_BOARD)
        task_id = str(execution.get("task_id") or "")
        if task_id:
            task_projects[(board, task_id)] = _item_project(item)
    included_sources = [
        source for source in sources
        if _source_project(source) == project or str(source.get("id") or "") in source_ids
    ]
    included_runs = [
        run for run in runs
        if _run_project(run, board_projects, task_projects) == project
        or str(run.get("board") or kanban_db.DEFAULT_BOARD) in included_boards
        or (str(run.get("board") or kanban_db.DEFAULT_BOARD), str(run.get("task_id") or "")) in included_tasks
    ]
    included_board_rows = [
        board for board in boards
        if _project_from_board(str(board.get("slug") or kanban_db.DEFAULT_BOARD), board) == project
        or str(board.get("slug") or kanban_db.DEFAULT_BOARD) in included_boards
    ]
    return included_items, included_sources, included_runs, included_board_rows


def _work_item_sort_key(item: dict[str, Any]) -> tuple[int, str]:
    created = _epoch_or_none(item.get("created_at"))
    updated = _epoch_or_none(item.get("updated_at"))
    recency = created if created is not None else updated or 0
    return (-recency, str(item.get("id") or ""))


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
    terminal_boards = {
        str((item.get("execution") or {}).get("board") or "")
        for item in work_items
        if item.get("status") in {"shipped", "archived"}
    }
    active_runs = [run for run in runs if _is_active_run(run) and str(run.get("board") or "") not in terminal_boards]
    return {
        "total_work_items": len(work_items),
        "inbox": sum(1 for item in work_items if _is_inbox_work_item(item))
        + sum(1 for source in sources if source.get("bucket") == "inbox"),
        "active_work": sum(1 for item in work_items if item.get("status") in {"queued", "running", "review"}),
        "blocked": by_status.get("blocked", 0),
        "archived": by_status.get("archived", 0),
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
        "issue_pulse": [
            {"status": status, "count": count, "label": status.replace("_", " ").title()}
            for status, count in sorted(by_status.items())
        ],
    }


def _is_inbox_work_item(item: dict[str, Any]) -> bool:
    return bool((item.get("decision") or {}).get("needed")) or item.get("status") == "proposed"


def _is_active_run(run: dict[str, Any]) -> bool:
    if run.get("ended_at") is not None:
        return False
    task_status = str(run.get("task_status") or "").lower()
    return task_status in _RUNNING_TASK_STATUSES
