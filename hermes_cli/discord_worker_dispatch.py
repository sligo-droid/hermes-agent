"""Cross-board dispatcher policy for Discord Codex worker boards."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Callable, Optional

from hermes_cli import discord_worker_boards as dwb
from hermes_cli import kanban_db


logger = logging.getLogger(__name__)


def _paused_corrupt_incident(board: str) -> Optional[dict]:
    try:
        incident = kanban_db.is_board_paused_for_corruption(board)
    except Exception:
        return None
    if not incident:
        return None
    db_path = Path(str(incident.get("db_path") or kanban_db.kanban_db_path(board)))
    try:
        fingerprint = kanban_db._db_content_fingerprint(db_path)
    except Exception:
        fingerprint = None
    if incident.get("fingerprint") == fingerprint:
        return incident
    logger.info(
        "kanban dispatcher: Discord worker board %s database changed since corruption incident; retrying",
        board,
    )
    return None


def _is_corrupt_board_db_error(exc: Exception) -> bool:
    if isinstance(exc, kanban_db.KanbanDbCorruptError):
        return True
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    msg = str(exc).lower()
    return "file is not a database" in msg or "database disk image is malformed" in msg


def _record_corrupt_board(board: str, exc: Exception) -> Optional[dict]:
    incident = getattr(exc, "incident", None)
    if isinstance(incident, dict):
        return incident
    db_path = kanban_db.kanban_db_path(board)
    try:
        resolved = db_path.resolve()
    except OSError:
        resolved = db_path
    try:
        fingerprint = kanban_db._db_content_fingerprint(resolved)
    except Exception:
        fingerprint = None
    return kanban_db.record_corrupt_board_incident(
        board,
        resolved,
        str(getattr(exc, "reason", None) or exc),
        backup_path=getattr(exc, "backup_path", None),
        fingerprint=fingerprint,
    )


def _log_corrupt_board_incident(board: str, incident: Optional[dict], exc: Exception) -> None:
    incident = incident or {}
    logger.error(
        "kanban dispatcher: Discord worker board %s database corruption incident; "
        "db_path=%s quarantine_path=%s reason=%s. Dispatch is paused for this "
        "board while the DB fingerprint is unchanged. Repair guidance: restore "
        "a known-good backup or run `hermes kanban repair --board %s`, then "
        "retry after integrity checks pass.",
        board,
        incident.get("db_path") or str(kanban_db.kanban_db_path(board)),
        incident.get("quarantine_path") or getattr(exc, "backup_path", None) or "<unavailable>",
        incident.get("reason") or getattr(exc, "reason", None) or str(exc),
        board,
    )


def _coerce_positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def running_role_count(board: str) -> int:
    """Return the number of running Discord role-lane tasks on one board."""
    conn = kanban_db.connect(board=board)
    try:
        placeholders = ",".join("?" for _ in dwb.ROLE_ASSIGNEES)
        row = conn.execute(
            "SELECT COUNT(*) FROM tasks "
            f"WHERE status = 'running' AND lower(assignee) IN ({placeholders})",
            tuple(sorted(dwb.ROLE_ASSIGNEES)),
        ).fetchone()
        return int(row[0] or 0)
    finally:
        conn.close()


def running_role_counts(board: str) -> dict[str, int]:
    """Return running Discord role-lane counts keyed by role."""
    conn = kanban_db.connect(board=board)
    try:
        placeholders = ",".join("?" for _ in dwb.ROLE_ASSIGNEES)
        rows = conn.execute(
            "SELECT lower(assignee) AS role, COUNT(*) AS count FROM tasks "
            f"WHERE status = 'running' AND lower(assignee) IN ({placeholders}) "
            "GROUP BY lower(assignee)",
            tuple(sorted(dwb.ROLE_ASSIGNEES)),
        ).fetchall()
        counts = {role: 0 for role in dwb.ROLE_ASSIGNEES}
        for row in rows:
            counts[str(row["role"] or "")] = int(row["count"] or 0)
        return counts
    finally:
        conn.close()


def _ready_role_counts(board: str) -> dict[str, int]:
    conn = kanban_db.connect(board=board)
    try:
        rows = conn.execute(
            "SELECT status, lower(assignee) AS role, COUNT(*) AS count FROM tasks "
            "WHERE status IN ('ready', 'review') AND claim_lock IS NULL "
            "GROUP BY status, lower(assignee)"
        ).fetchall()
        counts = {role: 0 for role in dwb.ROLE_ASSIGNEES}
        for row in rows:
            role = str(row["role"] or "")
            status = str(row["status"] or "")
            if role == dwb.ROLE_REVIEWER and status in {"ready", "review"}:
                counts[role] += int(row["count"] or 0)
            elif role in {dwb.ROLE_PLANNER, dwb.ROLE_DEV} and status == "ready":
                counts[role] += int(row["count"] or 0)
        return counts
    finally:
        conn.close()


def _dev_shared_workspace_limited(board: str) -> bool:
    """True when queued/running dev tasks share one checkout path."""
    conn = kanban_db.connect(board=board)
    try:
        rows = conn.execute(
            "SELECT workspace_path FROM tasks "
            "WHERE status IN ('ready', 'running') AND lower(assignee) = ?",
            (dwb.ROLE_DEV,),
        ).fetchall()
    finally:
        conn.close()
    paths = [str(row["workspace_path"] or "").strip() for row in rows]
    paths = [path for path in paths if path]
    return len(paths) > 1 and len(set(paths)) == 1


def _configured_dev_cap(value: object, max_workers_per_board: int) -> int:
    if value is not None:
        return _coerce_positive_int(value, 1)
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        worker_cfg = ((cfg.get("kanban") or {}).get("discord_worker") or {})
        configured = worker_cfg.get("max_dev_workers_per_board")
        if configured is not None:
            return _coerce_positive_int(configured, 1)
    except Exception:
        pass
    return min(1, max_workers_per_board)


def _configured_stale_timeout(value: object = None) -> int:
    if value is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
            value = (cfg.get("kanban") or {}).get("dispatch_stale_timeout_seconds", 0)
        except Exception:
            value = 0
    try:
        return max(0, int(str(value or 0)))
    except (TypeError, ValueError):
        return 0


def _dispatch_board(
    board: str,
    *,
    max_spawn: int,
    failure_limit: int,
    stale_timeout_seconds: int,
    spawn_fn: Optional[Callable],
) -> kanban_db.DispatchResult:
    conn = kanban_db.connect(board=board)
    try:
        return kanban_db.dispatch_once(
            conn,
            board=board,
            max_spawn=max_spawn,
            failure_limit=failure_limit,
            stale_timeout_seconds=stale_timeout_seconds,
            spawn_fn=spawn_fn,
            additional_spawnable_assignees=dwb.ROLE_ASSIGNEES,
        )
    finally:
        conn.close()


def dispatch_discord_worker_boards(
    boards: list[str],
    *,
    max_global_workers: int = 8,
    max_workers_per_board: int = 2,
    max_dev_workers_per_board: Optional[int] = None,
    failure_limit: int = kanban_db.DEFAULT_FAILURE_LIMIT,
    stale_timeout_seconds: Optional[int] = None,
    spawn_fn: Optional[Callable] = None,
) -> list[tuple[str, Optional[kanban_db.DispatchResult]]]:
    """Run one Discord worker-board dispatch pass.

    Each board gets a small live-concurrency cap by default so independent
    dev-lane tickets from one Discord /goal can progress together. Planner /
    reviewer ordering is still enforced by task dependencies, while the global
    pool prevents one busy board from monopolizing the dispatcher loop.
    """
    max_global_workers = _coerce_positive_int(max_global_workers, 8)
    max_workers_per_board = _coerce_positive_int(max_workers_per_board, 2)
    max_dev_workers_per_board = _configured_dev_cap(max_dev_workers_per_board, max_workers_per_board)
    configured_stale_timeout = _configured_stale_timeout(stale_timeout_seconds)
    if spawn_fn is None:
        from hermes_cli.kanban_codex_workers import spawn_or_default

        spawn_fn = spawn_or_default

    out: list[tuple[str, Optional[kanban_db.DispatchResult]]] = []
    eligible: list[str] = []
    running_by_board: dict[str, int] = {}
    running_roles_by_board: dict[str, dict[str, int]] = {}
    ready_roles_by_board: dict[str, dict[str, int]] = {}

    for board in boards:
        if _paused_corrupt_incident(board):
            logger.debug(
                "kanban dispatcher: Discord worker board %s paused for unchanged DB corruption; skipping dispatch",
                board,
            )
            out.append((board, None))
            continue
        try:
            if not dwb.is_discord_worker_board(board):
                continue
            dwb.reconcile_board(board)
            if not dwb.ensure_code_island_for_board(board):
                out.append((board, None))
                continue
            if not dwb.is_executable_worker_board(board):
                out.append((board, None))
                continue
            if dwb.is_paused_or_cancelled(board):
                out.append((board, None))
                continue
            running = running_role_count(board)
            running_by_board[board] = running
            running_roles_by_board[board] = running_role_counts(board)
            ready_roles_by_board[board] = _ready_role_counts(board)
            eligible.append(board)
        except Exception as exc:
            if _is_corrupt_board_db_error(exc):
                _log_corrupt_board_incident(board, _record_corrupt_board(board, exc), exc)
                out.append((board, None))
                continue
            logger.exception("kanban dispatcher: Discord worker prep failed on board %s", board)
            out.append((board, None))

    remaining_global_slots = max(0, max_global_workers - sum(running_by_board.values()))
    for board in eligible:
        if _paused_corrupt_incident(board):
            logger.debug(
                "kanban dispatcher: Discord worker board %s paused for unchanged DB corruption; skipping dispatch",
                board,
            )
            out.append((board, None))
            continue
        if dwb.is_paused_or_cancelled(board):
            out.append((board, None))
            continue
        running = running_by_board.get(board, 0)
        running_roles = running_roles_by_board.get(board, {})
        ready_roles = ready_roles_by_board.get(board, {})
        control_running = int(running_roles.get(dwb.ROLE_PLANNER, 0) or 0) + int(
            running_roles.get(dwb.ROLE_REVIEWER, 0) or 0
        )
        control_ready = int(ready_roles.get(dwb.ROLE_PLANNER, 0) or 0) + int(
            ready_roles.get(dwb.ROLE_REVIEWER, 0) or 0
        )
        running_dev = int(running_roles.get(dwb.ROLE_DEV, 0) or 0)
        effective_dev_cap = max_dev_workers_per_board
        if effective_dev_cap > 1 and _dev_shared_workspace_limited(board):
            effective_dev_cap = 1
        board_slots = max(0, max_workers_per_board - running)
        dev_slots = max(0, effective_dev_cap - running_dev)
        if remaining_global_slots <= 0 or board_slots <= 0:
            # Still run maintenance paths: reclaim stale/crashed/timed-out work
            # and promote dependency-satisfied tasks, but do not spawn.
            max_spawn = 0
        elif control_running:
            max_spawn = running
        elif control_ready:
            max_spawn = running + min(1, remaining_global_slots, board_slots)
        else:
            max_spawn = running + min(dev_slots, remaining_global_slots, board_slots)
        try:
            result = _dispatch_board(
                board,
                max_spawn=max_spawn,
                failure_limit=failure_limit,
                stale_timeout_seconds=configured_stale_timeout,
                spawn_fn=spawn_fn,
            )
            if result.spawned:
                remaining_global_slots = max(0, remaining_global_slots - len(result.spawned))
            out.append((board, result))
        except Exception as exc:
            if _is_corrupt_board_db_error(exc):
                _log_corrupt_board_incident(board, _record_corrupt_board(board, exc), exc)
                out.append((board, None))
                continue
            logger.exception("kanban dispatcher: Discord worker tick failed on board %s", board)
            out.append((board, None))

    return out
