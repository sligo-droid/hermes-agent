"""Cross-board dispatcher policy for Discord Codex worker boards."""

from __future__ import annotations

import logging
from typing import Callable, Optional

from hermes_cli import discord_worker_boards as dwb
from hermes_cli import kanban_db


logger = logging.getLogger(__name__)


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


def _dispatch_board(
    board: str,
    *,
    max_spawn: int,
    failure_limit: int,
    spawn_fn: Optional[Callable],
) -> kanban_db.DispatchResult:
    conn = kanban_db.connect(board=board)
    try:
        return kanban_db.dispatch_once(
            conn,
            board=board,
            max_spawn=max_spawn,
            failure_limit=failure_limit,
            spawn_fn=spawn_fn,
            additional_spawnable_assignees=dwb.ROLE_ASSIGNEES,
        )
    finally:
        conn.close()


def dispatch_discord_worker_boards(
    boards: list[str],
    *,
    max_global_workers: int = 4,
    max_workers_per_board: int = 2,
    failure_limit: int = kanban_db.DEFAULT_FAILURE_LIMIT,
    spawn_fn: Optional[Callable] = None,
) -> list[tuple[str, Optional[kanban_db.DispatchResult]]]:
    """Run one Discord worker-board dispatch pass.

    Each board gets a small live-concurrency cap by default so independent
    dev-lane tickets from one Discord /goal can progress together. Planner /
    reviewer ordering is still enforced by task dependencies, while the global
    pool prevents one busy board from monopolizing the dispatcher loop.
    """
    max_global_workers = _coerce_positive_int(max_global_workers, 4)
    max_workers_per_board = _coerce_positive_int(max_workers_per_board, 2)
    if spawn_fn is None:
        from hermes_cli.kanban_codex_workers import spawn_or_default

        spawn_fn = spawn_or_default

    out: list[tuple[str, Optional[kanban_db.DispatchResult]]] = []
    eligible: list[str] = []
    running_by_board: dict[str, int] = {}

    for board in boards:
        try:
            if not dwb.is_discord_worker_board(board):
                continue
            dwb.reconcile_board(board)
            if not dwb.is_executable_worker_board(board):
                out.append((board, None))
                continue
            if dwb.is_paused_or_cancelled(board):
                out.append((board, None))
                continue
            running = running_role_count(board)
            running_by_board[board] = running
            eligible.append(board)
        except Exception:
            logger.exception("kanban dispatcher: Discord worker prep failed on board %s", board)
            out.append((board, None))

    remaining_global_slots = max(0, max_global_workers - sum(running_by_board.values()))
    for board in eligible:
        running = running_by_board.get(board, 0)
        board_slots = max(0, max_workers_per_board - running)
        if remaining_global_slots <= 0 or board_slots <= 0:
            # Still run maintenance paths: reclaim stale/crashed/timed-out work
            # and promote dependency-satisfied tasks, but do not spawn.
            max_spawn = 0
        else:
            max_spawn = min(max_workers_per_board, running + remaining_global_slots)
        try:
            result = _dispatch_board(
                board,
                max_spawn=max_spawn,
                failure_limit=failure_limit,
                spawn_fn=spawn_fn,
            )
            if result.spawned:
                remaining_global_slots = max(0, remaining_global_slots - len(result.spawned))
            out.append((board, result))
        except Exception:
            logger.exception("kanban dispatcher: Discord worker tick failed on board %s", board)
            out.append((board, None))

    return out
