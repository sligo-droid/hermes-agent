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
    assert board.public_url == "https://example.test/workers/12345"
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


def test_intake_board_reconcile_does_not_create_planner(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.ensure_discord_thread_board(
        thread_id="778",
        initial_request="Feature summary only",
    )

    assert dwb.reconcile_board(board.slug) is None
    conn = kanban_db.connect(board=board.slug)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()

    assert tasks == []


def test_public_snapshot_does_not_expose_share_token(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(thread_id="888", goal="Ship it")
    token = board.worker["share_token"]

    snapshot = dwb.public_board_snapshot(token)

    assert snapshot["board"] == board.slug
    assert "share_token" not in snapshot["worker"]
    assert "worktree_path" not in snapshot["worker"]
    assert "project_path" not in snapshot["worker"]
    assert snapshot["worker"]["public_url"] == "https://example.test/workers/888"


def test_public_session_snapshot_resolves_discord_thread_id(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(thread_id="4242", goal="Ship it")

    snapshot = dwb.public_board_snapshot_for_session("4242")

    assert snapshot["board"] == board.slug
    assert snapshot["worker"]["thread_id"] == "4242"


def test_public_session_url_accepts_workers_base(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://example.test/workers")
    from hermes_cli import discord_worker_boards as dwb

    assert dwb.public_session_board_url("4242") == "https://example.test/workers/4242"


def test_public_session_url_migrates_legacy_kanban_base(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://example.test/kanban")
    from hermes_cli import discord_worker_boards as dwb

    assert dwb.public_session_board_url("4242") == "https://example.test/workers/4242"


def test_public_board_index_lists_session_links(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb

    dwb.set_goal(thread_id="5151", goal="Build the thing")

    html = dwb.render_public_board_index_html()

    assert "Hermes Kanban" in html
    assert "/workers/5151" in html
    assert "Build the thing" in html


def test_public_kanban_web_routes(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    board = dwb.set_goal(thread_id="6161", goal="Build the thing")
    token = board.worker["share_token"]
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    index = client.get("/workers")
    dashboard_kanban = client.get("/kanban")
    root_session_redirect = client.get("/6161", follow_redirects=False)
    kanban_session_redirect = client.get("/kanban/6161", follow_redirects=False)
    session = client.get("/workers/6161")
    legacy_token = client.get(f"/kanban/public/kanban/{token}")
    token_resp = client.get(f"/workers/public/kanban/{token}")
    missing = client.get("/workers/does-not-exist")

    assert index.status_code == 200
    assert "/workers/6161" in index.text
    assert "public session boards" not in dashboard_kanban.text
    assert root_session_redirect.status_code == 307
    assert root_session_redirect.headers["location"] == "/workers/6161"
    assert kanban_session_redirect.status_code == 307
    assert kanban_session_redirect.headers["location"] == "/workers/6161"
    assert session.status_code == 200
    assert "Discord 6161" in session.text
    assert legacy_token.status_code == 200
    assert "Discord 6161" in legacy_token.text
    assert token_resp.status_code == 200
    assert "Discord 6161" in token_resp.text
    assert missing.status_code == 404


def test_public_worker_routes_do_not_require_dashboard_auth(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli.web_server import app

    board = dwb.set_goal(thread_id="7171", goal="Public workers stay public")
    token = board.worker["share_token"]
    client = TestClient(app)

    dashboard = client.get("/")
    dashboard_kanban = client.get("/kanban")
    index = client.get("/workers")
    session = client.get("/workers/7171")
    root_legacy = client.get("/7171", follow_redirects=False)
    kanban_legacy = client.get("/kanban/7171", follow_redirects=False)
    token_resp = client.get(f"/workers/public/kanban/{token}")
    old_token_resp = client.get(f"/public/kanban/{token}")
    nested_worker = client.get("/workers/7171/extra")
    nested_kanban = client.get("/kanban/7171/extra")
    nested_token = client.get(f"/workers/public/kanban/{token}/extra")

    assert dashboard.status_code == 401
    assert dashboard_kanban.status_code == 401
    assert index.status_code == 200
    assert "/workers/7171" in index.text
    assert session.status_code == 200
    assert "Discord 7171" in session.text
    assert root_legacy.status_code == 307
    assert root_legacy.headers["location"] == "/workers/7171"
    assert kanban_legacy.status_code == 307
    assert kanban_legacy.headers["location"] == "/workers/7171"
    assert token_resp.status_code == 200
    assert old_token_resp.status_code == 200
    assert nested_worker.status_code == 401
    assert nested_kanban.status_code == 401
    assert nested_token.status_code == 401


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


def _create_ready_dev_task(board_slug: str, title: str = "Implement task") -> str:
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=board_slug)
    try:
        return kanban_db.create_task(
            conn,
            title=title,
            assignee=dwb.ROLE_DEV,
            created_by="test",
            workspace_kind="dir",
            workspace_path=str(Path("/tmp") / board_slug),
            tenant=board_slug,
        )
    finally:
        conn.close()


def _make_discord_board(thread_id: str):
    from hermes_cli import discord_worker_boards as dwb

    return dwb.set_goal(thread_id=thread_id, goal=f"Work for {thread_id}")


def _make_intake_discord_board(thread_id: str):
    from hermes_cli import discord_worker_boards as dwb

    return dwb.ensure_discord_thread_board(
        thread_id=thread_id,
        initial_request=f"Work for {thread_id}",
    )


def test_discord_worker_dispatch_skips_intake_board(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    board = _make_intake_discord_board("1901")
    _create_ready_dev_task(board.slug)
    spawned = []

    dispatch_discord_worker_boards(
        [board.slug],
        max_global_workers=1,
        max_workers_per_board=1,
        spawn_fn=lambda task, workspace, board=None: spawned.append(board) or 1901,
    )

    assert spawned == []


def test_discord_worker_dispatch_spawns_across_two_boards(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    board_a = _make_discord_board("2001")
    board_b = _make_discord_board("2002")
    _create_ready_dev_task(board_a.slug)
    _create_ready_dev_task(board_b.slug)
    spawned = []

    def fake_spawn(task, workspace, board=None):
        spawned.append((task.id, task.assignee, board))
        return 1000 + len(spawned)

    results = dispatch_discord_worker_boards(
        [board_a.slug, board_b.slug],
        max_global_workers=2,
        max_workers_per_board=1,
        spawn_fn=fake_spawn,
    )

    assert sum(len(result.spawned) for _, result in results if result is not None) == 2
    assert {item[2] for item in spawned} == {board_a.slug, board_b.slug}


def test_discord_worker_dispatch_respects_global_limit(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    boards = [_make_discord_board(str(2100 + idx)) for idx in range(3)]
    for board in boards:
        _create_ready_dev_task(board.slug)

    results = dispatch_discord_worker_boards(
        [board.slug for board in boards],
        max_global_workers=2,
        max_workers_per_board=1,
        spawn_fn=lambda task, workspace, board=None: 2000,
    )

    assert sum(len(result.spawned) for _, result in results if result is not None) == 2


def test_discord_worker_dispatch_keeps_each_board_serial(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    board = _make_discord_board("2201")
    _create_ready_dev_task(board.slug, "Implement first task")
    _create_ready_dev_task(board.slug, "Implement second task")

    results = dispatch_discord_worker_boards(
        [board.slug],
        max_global_workers=4,
        max_workers_per_board=1,
        spawn_fn=lambda task, workspace, board=None: 3000,
    )

    assert sum(len(result.spawned) for _, result in results if result is not None) == 1


def test_discord_worker_dispatch_skips_paused_board(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli.discord_worker_dispatch import dispatch_discord_worker_boards

    paused = _make_discord_board("2301")
    active = _make_discord_board("2302")
    _create_ready_dev_task(paused.slug)
    _create_ready_dev_task(active.slug)
    dwb.pause_board(paused.slug)
    spawned = []

    def fake_spawn(task, workspace, board=None):
        spawned.append(board)
        return 4000 + len(spawned)

    dispatch_discord_worker_boards(
        [paused.slug, active.slug],
        max_global_workers=2,
        max_workers_per_board=1,
        spawn_fn=fake_spawn,
    )

    assert spawned == [active.slug]
