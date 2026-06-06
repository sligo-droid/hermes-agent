from __future__ import annotations

from pathlib import Path


def _home(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    return root


def _prepare_board(monkeypatch, tmp_path: Path, *, thread_id: str, running_role: str | None = "dev"):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    monkeypatch.setattr(dwb, "ensure_code_island_for_board", lambda _board: True)
    board = dwb.set_goal(thread_id=thread_id, goal=f"Ship worker board {thread_id}")
    conn = kanban_db.connect(board=board.slug)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        planner_id = tasks[0].id
        planner_is_running = False
        if running_role:
            if running_role == dwb.ROLE_PLANNER:
                running_id = planner_id
                planner_is_running = True
            else:
                running_id = kanban_db.create_task(
                    conn,
                    title=f"Running dev {thread_id}",
                    assignee=dwb.ROLE_DEV,
                    tenant=board.slug,
                )
            conn.execute(
                "UPDATE tasks SET status = 'running', assignee = ? WHERE id = ?",
                (running_role, running_id),
            )
        if not planner_is_running:
            conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (planner_id,))
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
        max_dev_workers_per_board=2,
        spawn_fn=spawn_fn,
    )

    assert len(spawned) == 1
    assert spawned[0][0] == board.slug
    assert len(results[0][1].spawned) == 1


def test_discord_worker_dispatch_passes_stale_timeout(monkeypatch, tmp_path):
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    board = _prepare_board(monkeypatch, tmp_path, thread_id="stale-timeout")
    captured = {}

    def fake_dispatch_once(conn, **kwargs):
        captured.update(kwargs)
        return kanban_db.DispatchResult()

    monkeypatch.setattr(kanban_db, "dispatch_once", fake_dispatch_once)

    dispatch_discord_worker_boards(
        [board.slug],
        max_global_workers=4,
        max_workers_per_board=2,
        stale_timeout_seconds=60,
    )

    assert captured["stale_timeout_seconds"] == 60


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
        max_dev_workers_per_board=2,
        spawn_fn=spawn_fn,
    )

    assert len(spawned) == 1
    assert sum(len(result.spawned) for _board, result in results if result is not None) == 1


def test_discord_worker_dispatch_default_dev_cap_keeps_boards_serial(monkeypatch, tmp_path):
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

    assert len(spawned) == 0
    assert sum(len(result.spawned) for _board, result in results if result is not None) == 0


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
        max_dev_workers_per_board=0,
        spawn_fn=spawn_fn,
    )

    assert len(spawned) == 0
    assert sum(len(result.spawned) for _board, result in results if result is not None) == 0


def test_discord_worker_dispatch_dev_cap_allows_distinct_workspace_parallelism(monkeypatch, tmp_path):
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    board = _prepare_board(monkeypatch, tmp_path, thread_id="parallel", running_role=None)
    conn = None
    try:
        from hermes_cli import kanban_db

        conn = kanban_db.connect(board=board.slug)
        for idx, task in enumerate(
            task for task in kanban_db.list_tasks(conn, include_archived=False) if task.assignee == "dev"
        ):
            conn.execute(
                "UPDATE tasks SET status = 'ready', workspace_path = ? WHERE id = ?",
                (str(tmp_path / f"workspace-{idx}"), task.id),
            )
        conn.commit()
    finally:
        if conn is not None:
            conn.close()
    spawned = []

    def spawn_fn(task, _workspace, board=None):
        spawned.append((board, task.id))
        return 5000 + len(spawned)

    results = dispatch_discord_worker_boards(
        [board.slug],
        max_global_workers=4,
        max_workers_per_board=3,
        max_dev_workers_per_board=2,
        spawn_fn=spawn_fn,
    )

    assert len(spawned) == 2
    assert sum(len(result.spawned) for _board, result in results if result is not None) == 2


def test_discord_worker_dispatch_shared_workspace_caps_dev_fanout(monkeypatch, tmp_path):
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    board = _prepare_board(monkeypatch, tmp_path, thread_id="shared", running_role=None)
    conn = None
    try:
        from hermes_cli import kanban_db

        conn = kanban_db.connect(board=board.slug)
        conn.execute(
            "UPDATE tasks SET workspace_path = ? WHERE assignee = 'dev'",
            (str(tmp_path / "shared-worktree"),),
        )
        conn.commit()
    finally:
        if conn is not None:
            conn.close()
    spawned = []

    def spawn_fn(task, _workspace, board=None):
        spawned.append((board, task.id))
        return 5500 + len(spawned)

    results = dispatch_discord_worker_boards(
        [board.slug],
        max_global_workers=4,
        max_workers_per_board=3,
        max_dev_workers_per_board=2,
        spawn_fn=spawn_fn,
    )

    assert len(spawned) == 1
    assert sum(len(result.spawned) for _board, result in results if result is not None) == 1


def test_discord_worker_dispatch_planner_running_blocks_dev_spawn(monkeypatch, tmp_path):
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    board = _prepare_board(monkeypatch, tmp_path, thread_id="planner", running_role="planner")
    spawned = []

    results = dispatch_discord_worker_boards(
        [board.slug],
        max_global_workers=4,
        max_workers_per_board=3,
        max_dev_workers_per_board=2,
        spawn_fn=lambda task, workspace, board=None: spawned.append((task.assignee, board)) or 6000,
    )

    assert spawned == []
    assert sum(len(result.spawned) for _board, result in results if result is not None) == 0


def test_discord_worker_dispatch_reviewer_lane_is_singleton(monkeypatch, tmp_path):
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    board = _prepare_board(monkeypatch, tmp_path, thread_id="reviewer", running_role=None)
    conn = kanban_db.connect(board=board.slug)
    try:
        conn.execute("UPDATE tasks SET status = 'done' WHERE assignee = ?", (dwb.ROLE_DEV,))
        for idx in range(2):
            task_id = kanban_db.create_task(
                conn,
                title=f"Review task {idx}",
                assignee=dwb.ROLE_REVIEWER,
                tenant=board.slug,
            )
            conn.execute("UPDATE tasks SET status = 'review', claim_lock = NULL WHERE id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    spawned = []

    results = dispatch_discord_worker_boards(
        [board.slug],
        max_global_workers=4,
        max_workers_per_board=3,
        max_dev_workers_per_board=2,
        spawn_fn=lambda task, workspace, board=None: spawned.append((task.assignee, board)) or 7000,
    )

    assert spawned == [(dwb.ROLE_REVIEWER, board.slug)]
    assert sum(len(result.spawned) for _board, result in results if result is not None) == 1


def test_discord_worker_dispatch_ready_reviewer_is_control_ready(monkeypatch, tmp_path):
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    board = _prepare_board(monkeypatch, tmp_path, thread_id="ready-reviewer", running_role=None)
    conn = kanban_db.connect(board=board.slug)
    try:
        conn.execute("UPDATE tasks SET status = 'done' WHERE assignee = ?", (dwb.ROLE_DEV,))
        task_id = kanban_db.create_task(
            conn,
            title="Ready reviewer task",
            assignee=dwb.ROLE_REVIEWER,
            tenant=board.slug,
        )
        conn.execute("UPDATE tasks SET status = 'ready', claim_lock = NULL WHERE id = ?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    spawned = []

    results = dispatch_discord_worker_boards(
        [board.slug],
        max_global_workers=4,
        max_workers_per_board=2,
        max_dev_workers_per_board=1,
        spawn_fn=lambda task, workspace, board=None: spawned.append((task.assignee, board)) or 7500,
    )

    assert spawned == [(dwb.ROLE_REVIEWER, board.slug)]
    assert sum(len(result.spawned) for _board, result in results if result is not None) == 1


def test_discord_worker_dispatch_records_corrupt_open_once_then_skips(
    monkeypatch,
    tmp_path,
    caplog,
):
    import logging
    import sqlite3

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    board = "discord-corrupt-dispatch"
    db_path = tmp_path / "kanban.db"
    db_path.write_text("not sqlite", encoding="utf-8")
    incidents = {}
    calls = {"connect": 0, "record": 0, "dispatch": 0}

    monkeypatch.setattr(dwb, "is_discord_worker_board", lambda candidate: candidate == board)
    monkeypatch.setattr(dwb, "reconcile_board", lambda candidate: None)
    monkeypatch.setattr(dwb, "ensure_code_island_for_board", lambda candidate: True)
    monkeypatch.setattr(dwb, "is_executable_worker_board", lambda candidate: True)
    monkeypatch.setattr(dwb, "is_paused_or_cancelled", lambda candidate: False)
    monkeypatch.setattr(kanban_db, "kanban_db_path", lambda candidate=None: db_path)
    monkeypatch.setattr(kanban_db, "is_board_paused_for_corruption", lambda candidate=None: incidents.get(candidate))

    def connect(*args, **kwargs):
        calls["connect"] += 1
        raise sqlite3.DatabaseError("file is not a database")

    def record_incident(candidate, db_path_arg, reason, *, backup_path=None, fingerprint=None):
        calls["record"] += 1
        incident = {
            "pause_reason": "kanban_db_corruption",
            "db_path": str(db_path_arg),
            "fingerprint": fingerprint,
            "quarantine_path": str(backup_path) if backup_path is not None else None,
            "reason": reason,
        }
        incidents[candidate] = incident
        return incident

    def dispatch_once(*args, **kwargs):
        calls["dispatch"] += 1
        return kanban_db.DispatchResult()

    monkeypatch.setattr(kanban_db, "connect", connect)
    monkeypatch.setattr(kanban_db, "record_corrupt_board_incident", record_incident)
    monkeypatch.setattr(kanban_db, "dispatch_once", dispatch_once)

    with caplog.at_level(logging.DEBUG, logger="hermes_cli.discord_worker_dispatch"):
        first = dispatch_discord_worker_boards([board], spawn_fn=lambda *args, **kwargs: 999)
        second = dispatch_discord_worker_boards([board], spawn_fn=lambda *args, **kwargs: 999)

    assert first == [(board, None)]
    assert second == [(board, None)]
    assert calls == {"connect": 1, "record": 1, "dispatch": 0}
    messages = [record.getMessage() for record in caplog.records]
    assert sum("Discord worker board discord-corrupt-dispatch database corruption incident" in msg for msg in messages) == 1
    assert any("paused for unchanged DB corruption" in msg for msg in messages)
    assert not any(record.exc_info for record in caplog.records)
