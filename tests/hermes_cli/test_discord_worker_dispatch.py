from __future__ import annotations

from pathlib import Path


def _home(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    return root


def _prepare_board(monkeypatch, tmp_path: Path, *, thread_id: str):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    monkeypatch.setattr(dwb, "ensure_code_island_for_board", lambda _board: True)
    board = dwb.set_goal(thread_id=thread_id, goal=f"Ship worker board {thread_id}")
    conn = kanban_db.connect(board=board.slug)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        running_id = tasks[0].id
        conn.execute(
            "UPDATE tasks SET status = 'running', assignee = ? WHERE id = ?",
            (dwb.ROLE_PLANNER, running_id),
        )
        for idx in range(2):
            ready_id = kanban_db.create_task(
                conn,
                title=f"Ready dev {thread_id}-{idx}",
                assignee=dwb.ROLE_DEV,
                tenant=board.slug,
            )
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?",
                (ready_id,),
            )
        conn.commit()
    finally:
        conn.close()
    return board


def test_discord_worker_board_cap_counts_already_running(monkeypatch, tmp_path):
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    board = _prepare_board(monkeypatch, tmp_path, thread_id="cap-1")
    spawned = []

    def spawn_fn(task, _workspace, board=None):
        spawned.append((board, task.id))
        return 1000 + len(spawned)

    results = dispatch_discord_worker_boards(
        [board.slug],
        max_global_workers=4,
        max_workers_per_board=2,
        spawn_fn=spawn_fn,
    )

    assert len(spawned) == 1
    assert spawned[0][0] == board.slug
    assert len(results[0][1].spawned) == 1


def test_discord_worker_dispatch_respects_global_live_cap(monkeypatch, tmp_path):
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    board_a = _prepare_board(monkeypatch, tmp_path, thread_id="global-a")
    board_b = _prepare_board(monkeypatch, tmp_path, thread_id="global-b")
    spawned = []

    def spawn_fn(task, _workspace, board=None):
        spawned.append((board, task.id))
        return 2000 + len(spawned)

    results = dispatch_discord_worker_boards(
        [board_a.slug, board_b.slug],
        max_global_workers=3,
        max_workers_per_board=2,
        spawn_fn=spawn_fn,
    )

    assert len(spawned) == 1
    assert sum(len(result.spawned) for _board, result in results if result is not None) == 1


def test_discord_worker_dispatch_defaults_allow_eight_global_workers(monkeypatch, tmp_path):
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    boards = [_prepare_board(monkeypatch, tmp_path, thread_id=f"default-{idx}") for idx in range(5)]
    spawned = []

    def spawn_fn(task, _workspace, board=None):
        spawned.append((board, task.id))
        return 3000 + len(spawned)

    results = dispatch_discord_worker_boards(
        [board.slug for board in boards],
        spawn_fn=spawn_fn,
    )

    assert len(spawned) == 3
    assert sum(len(result.spawned) for _board, result in results if result is not None) == 3


def test_discord_worker_dispatch_invalid_caps_fall_back_to_defaults(monkeypatch, tmp_path):
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    boards = [_prepare_board(monkeypatch, tmp_path, thread_id=f"fallback-{idx}") for idx in range(5)]
    spawned = []

    def spawn_fn(task, _workspace, board=None):
        spawned.append((board, task.id))
        return 4000 + len(spawned)

    results = dispatch_discord_worker_boards(
        [board.slug for board in boards],
        max_global_workers=0,
        max_workers_per_board=0,
        spawn_fn=spawn_fn,
    )

    assert len(spawned) == 3
    assert sum(len(result.spawned) for _board, result in results if result is not None) == 3
