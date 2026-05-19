from __future__ import annotations

from pathlib import Path


def _home(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://example.test")
    return root


def test_ensure_discord_thread_board_creates_public_metadata(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.ensure_discord_thread_board(
        thread_id="12345",
        chat_id="999",
        guild_id="111",
        parent_channel_id="222",
        initial_request="Build the thing",
        project_context={"project_path": "/repo/app"},
    )

    assert board.slug == "discord-12345"
    assert board.public_url.startswith("https://example.test/public/kanban/")
    meta = kanban_db.read_board_metadata(board.slug)
    worker = meta["discord_worker"]
    assert worker["thread_id"] == "12345"
    assert worker["initial_request"] == "Build the thing"
    assert worker["worktree_path"].endswith("app-discord-12345")


def test_set_goal_creates_planner_task_for_role_lane(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.set_goal(thread_id="777", goal="Implement durable workers")
    conn = kanban_db.connect(board=board.slug)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()

    assert len(tasks) == 1
    assert tasks[0].assignee == "planner"
    assert tasks[0].status == "ready"
    assert tasks[0].workspace_kind == "dir"


def test_public_snapshot_does_not_expose_share_token(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(thread_id="888", goal="Ship it")
    token = board.worker["share_token"]

    snapshot = dwb.public_board_snapshot(token)

    assert snapshot["board"] == board.slug
    assert "share_token" not in snapshot["worker"]
    assert snapshot["worker"]["public_url"].endswith(token)


def test_subgoal_remove_deactivates_and_archives_unstarted_task(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.set_goal(thread_id="999", goal="Root goal")
    idx, text = dwb.add_subgoal(board.slug, "Add regression tests")
    assert idx == 1
    assert text == "Add regression tests"

    removed = dwb.deactivate_subgoal(board.slug, 1)
    assert removed == "Add regression tests"

    conn = kanban_db.connect(board=board.slug)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=True)
        subgoal_tasks = [t for t in tasks if t.created_by == "discord-subgoal"]
    finally:
        conn.close()
    assert len(subgoal_tasks) == 1
    assert subgoal_tasks[0].status == "archived"


def test_dispatch_once_allows_explicit_role_lane_assignees(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.set_goal(thread_id="123", goal="Plan work")
    spawned = []

    def fake_spawn(task, workspace, board=None):
        spawned.append((task.id, task.assignee, workspace, board))
        return 4242

    conn = kanban_db.connect(board=board.slug)
    try:
        without_extra = kanban_db.dispatch_once(conn, dry_run=True, board=board.slug)
        with_extra = kanban_db.dispatch_once(
            conn,
            spawn_fn=fake_spawn,
            board=board.slug,
            additional_spawnable_assignees=dwb.ROLE_ASSIGNEES,
        )
    finally:
        conn.close()

    assert without_extra.skipped_nonspawnable
    assert with_extra.spawned
    assert spawned[0][1] == "planner"
    assert spawned[0][3] == board.slug

