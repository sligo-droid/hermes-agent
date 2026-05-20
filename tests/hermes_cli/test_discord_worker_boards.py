from __future__ import annotations

import json
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


def test_set_goal_repairs_unmapped_board_workspace(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    def fake_worktree_path(project_path, thread_id):
        name = Path(project_path or "unmapped").name
        return str(tmp_path / f"{name}-discord-{thread_id}")

    monkeypatch.setattr(dwb, "_default_worktree_path", fake_worktree_path)
    monkeypatch.setattr(dwb, "_ensure_code_island", lambda worker: None)

    board = dwb.set_goal(thread_id="7780", goal="Fix worker routing")
    old_worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    old_worktree = old_worker["worktree_path"]
    assert old_worktree.endswith("unmapped-discord-7780")

    conn = kanban_db.connect(board=board.slug)
    try:
        task = kanban_db.list_tasks(conn, include_archived=False)[0]
        assert task.workspace_path == old_worktree
    finally:
        conn.close()

    project = tmp_path / "repo"
    project.mkdir()
    repaired = dwb.set_goal(
        thread_id="7780",
        goal="Fix worker routing",
        project_context={"project_path": str(project)},
    )
    worker = kanban_db.read_board_metadata(repaired.slug)["discord_worker"]
    new_worktree = worker["worktree_path"]

    assert worker["project_path"] == str(project)
    assert new_worktree.endswith("repo-discord-7780")
    assert new_worktree != old_worktree

    conn = kanban_db.connect(board=repaired.slug)
    try:
        task = kanban_db.list_tasks(conn, include_archived=False)[0]
        assert task.workspace_path == new_worktree
    finally:
        conn.close()


def test_board_thread_state_reflects_kanban_tasks(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.set_goal(thread_id="780", goal="Track thread emoji state")
    conn = kanban_db.connect(board=board.slug)
    try:
        task = kanban_db.list_tasks(conn, include_archived=False)[0]
        assert dwb.board_thread_state(board.slug) == "active"

        claimed = kanban_db.claim_task(conn, task.id)
        assert claimed is not None
        kanban_db.block_task(
            conn,
            task.id,
            reason="needs user input",
            expected_run_id=claimed.current_run_id,
        )
        assert dwb.board_thread_state(board.slug) == "blocked"

        kanban_db.unblock_task(conn, task.id)
        claimed = kanban_db.claim_task(conn, task.id)
        assert claimed is not None
        kanban_db.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
        assert dwb.board_thread_state(board.slug) == "active"
        dwb._update_worker_meta(board.slug, {"goal_status": "done", "phase": "complete"})
        assert dwb.board_thread_state(board.slug) == "done"

        failed = kanban_db.create_task(conn, title="Broken ticket", tenant=board.slug)
        conn.execute(
            "UPDATE tasks SET status='blocked', last_failure_error='worker crashed' WHERE id=?",
            (failed,),
        )
        conn.commit()
        assert dwb.board_thread_state(board.slug) == "errored"
    finally:
        conn.close()


def test_start_direct_goal_activates_board_without_planner(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="778",
        goal="Follow up on the todos from this meeting.",
    )
    conn = kanban_db.connect(board=board.slug)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()

    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert tasks == []
    assert worker["execution_mode"] == "kanban_pipeline"
    assert worker["goal_status"] == "active"
    assert worker["phase"] == "dev"
    assert worker["root_goal"] == "Follow up on the todos from this meeting."


def test_set_goal_preserves_nested_subgoal_text_for_planner(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    body = "/subgoal inspect logs\nThen implement the smallest fix"
    board = dwb.set_goal(thread_id="779", goal=body, request_id="msg-779")
    conn = kanban_db.connect(board=board.slug)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()

    assert len(tasks) == 1
    assert tasks[0].assignee == "planner"
    assert tasks[0].created_by == "discord-goal"
    payload = json.loads(tasks[0].body or "{}")
    assert payload["request"] == body
    assert "/subgoal inspect logs" in payload["request"]
    assert payload["planner_instructions"]
    instructions = "\n".join(payload["planner_instructions"])
    assert "fewest coherent dev tickets" in instructions
    assert "Do not create standalone discovery, audit, polish, or verification tickets" in instructions
    assert "one deduplicated canonical list" in instructions


def test_feature_request_starts_distinct_planner_tickets(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_planner_request(
        thread_id="780",
        request="Build the drilldown page",
        request_id="msg-a",
    )
    dwb.start_planner_request(
        thread_id="780",
        request="Also add CSV export",
        request_id="msg-b",
    )
    dwb.start_planner_request(
        thread_id="780",
        request="Also add CSV export",
        request_id="msg-b",
    )

    conn = kanban_db.connect(board=board.slug)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()

    assert [task.assignee for task in tasks] == ["planner", "planner"]
    assert [json.loads(task.body or "{}")["request"] for task in tasks] == [
        "Build the drilldown page",
        "Also add CSV export",
    ]


def test_goal_reuses_existing_feature_summary_planner(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_planner_request(
        thread_id="780",
        request="/goal\n\nBuild the drilldown page",
        request_id="feature-summary",
    )
    reused = dwb.set_goal(
        thread_id="780",
        goal="Build the drilldown page",
        request_id="goal-message",
    )

    conn = kanban_db.connect(board=board.slug)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()

    assert reused.slug == board.slug
    assert len(tasks) == 1
    assert tasks[0].assignee == "planner"
    payload = json.loads(tasks[0].body or "{}")
    assert payload["request"] == "Build the drilldown page"


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

    dwb.set_goal(thread_id="5151", goal="Build the thing", guild_id="111")

    html = dwb.render_public_board_index_html()

    assert "Hermes Kanban" in html
    assert '<a class="brand" href="/">Hermes<br>Kanban</a>' in html
    assert "/workers/5151" in html
    assert (
        '<a href="https://discord.com/channels/111/5151" target="_blank" '
        'rel="noopener noreferrer"><code>5151</code></a>'
    ) in html
    assert "Build the thing" in html


def test_public_board_index_lists_operational_row_data(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    monkeypatch.setattr(dwb, "_now", lambda: 100)
    board = dwb.ensure_discord_thread_board(
        thread_id="5152",
        initial_request="Build private thing",
        project_context={"project_path": "/repo/app"},
    )
    conn = kanban_db.connect(board=board.slug)
    try:
        kanban_db.create_task(conn, title="Ready task")
        running = kanban_db.create_task(conn, title="Running task")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (running,))
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(dwb, "_now", lambda: 200)
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "active",
            "phase": "dev",
            "execution_mode": "kanban_pipeline",
            "pr_url": "https://github.example/pull/42",
            "pr_number": 42,
            "review_loop_count": 2,
            "review_loop_limit": 5,
            "paused": True,
            "blocked_reason": "waiting for review",
        },
    )

    html = dwb.render_public_board_index_html()

    assert "active / dev" in html
    assert 'class="runtime runtime-paused">paused</strong>' in html
    assert "ready:1" in html
    assert "running:1" in html
    assert "Running: idle" in html
    assert "Branch: discord/5152" in html
    assert 'PR: <a href="https://github.example/pull/42">#42</a>' in html
    assert "Review: 2/5" in html
    assert "Created: 1970-01-01 00:01:40 UTC" in html
    assert "Updated: 1970-01-01 00:03:20 UTC" in html
    assert "paused blocked: waiting for review" in html
    assert 'action="/workers/5152/start"' in html
    assert ">Resume</button>" in html
    assert "/repo/app" not in html
    assert "app-discord-5152" not in html
    assert "share_token" not in html


def test_public_board_index_shows_pause_control_for_active_board(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb

    dwb.set_goal(thread_id="5153", goal="Build the thing")

    html = dwb.render_public_board_index_html()

    assert 'class="runtime runtime-queued">queued</strong>' in html
    assert "Queue: awaiting next dispatcher tick" in html
    assert "Running: idle" in html
    assert 'action="/workers/5153/pause"' in html
    assert ">Pause Queue</button>" in html


def test_public_board_index_shows_running_runtime(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    monkeypatch.setattr(kanban_db, "_pid_alive", lambda pid: pid == 123)
    board = dwb.set_goal(thread_id="5154", goal="Run the thing")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(conn, title="Active dev ticket")
        conn.execute("UPDATE tasks SET status='running', worker_pid=123 WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()

    html = dwb.render_public_board_index_html()

    assert 'class="runtime runtime-running">running</strong>' in html
    assert "Running: Active dev ticket" in html
    assert "pid=123" in html


def test_public_board_index_live_worker_overrides_stale_blocked_meta(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    monkeypatch.setattr(kanban_db, "_pid_alive", lambda pid: pid == 123)
    board = dwb.set_goal(thread_id="5157", goal="Run despite stale meta")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(conn, title="Live worker ticket")
        conn.execute("UPDATE tasks SET status='running', worker_pid=123 WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()
    dwb._update_worker_meta(board.slug, {"blocked_reason": "old blocker"})

    html = dwb.render_public_board_index_html()

    assert 'class="runtime runtime-running">running</strong>' in html
    assert "Running: Live worker ticket" in html
    assert "Status: Live worker ticket" in html


def test_public_board_index_does_not_show_dead_pid_as_running(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    monkeypatch.setattr(kanban_db, "_pid_alive", lambda _pid: False)
    board = dwb.set_goal(thread_id="5155", goal="Run the thing")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(conn, title="Dead worker ticket")
        conn.execute("UPDATE tasks SET status='running', worker_pid=123 WHERE id=?", (task_id,))
        conn.commit()
    finally:
        conn.close()

    html = dwb.render_public_board_index_html()

    assert 'class="runtime runtime-stale">stale</strong>' in html
    assert "running ticket has no live worker" in html
    assert "Pause</button>" not in html


def test_public_board_index_done_board_has_no_pause_action(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.set_goal(thread_id="5156", goal="Finish the thing")
    conn = kanban_db.connect(board=board.slug)
    try:
        task = kanban_db.list_tasks(conn, include_archived=False)[0]
        claimed = kanban_db.claim_task(conn, task.id)
        assert claimed is not None
        kanban_db.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(board.slug, {"goal_status": "done", "phase": "complete"})

    html = dwb.render_public_board_index_html()

    assert 'class="runtime runtime-done">done</strong>' in html
    assert 'action="/workers/5156/pause"' not in html


def test_public_board_index_lists_newest_sessions_first(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb

    monkeypatch.setattr(dwb, "_now", lambda: 100)
    dwb.set_goal(thread_id="1001", goal="Older worker")
    monkeypatch.setattr(dwb, "_now", lambda: 200)
    dwb.set_goal(thread_id="1002", goal="Newer worker")

    snapshot = dwb.public_board_index_snapshot()
    html = dwb.render_public_board_index_html()

    assert [board["session_id"] for board in snapshot["boards"][:2]] == [
        "1002",
        "1001",
    ]
    assert html.index("Newer worker") < html.index("Older worker")


def test_public_session_board_auto_refreshes(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.set_goal(thread_id="6160", goal="Watch the board")
    conn = kanban_db.connect(board=board.slug)
    try:
        kanban_db.create_task(conn, title="Second ticket")
    finally:
        conn.close()

    html = dwb.render_public_session_board_html("6160")

    assert '<meta http-equiv="refresh" content="15">' in html
    assert html.count('data-ticket-terminal-url="/workers/6160/tickets/') == 2
    assert 'id="ticket-modal"' in html
    assert "Terminal Log" in html
    assert "setInterval" in html
    assert "Unable to load ticket terminal" in html
    assert '<a class="brand" href="/workers">Hermes<br>Kanban</a>' in html
    assert "Codex result" not in html
    assert "Recent internals" not in html
    assert "codex_state" not in html
    assert "JSON.stringify" not in html


def test_public_session_board_shows_runtime_controls(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(thread_id="6163", goal="Control the board")

    active_html = dwb.render_public_session_board_html("6163")

    assert 'class="runtime runtime-queued">queued</strong>' in active_html
    assert 'action="/workers/6163/pause?return_to=/workers/6163"' in active_html
    assert ">Pause Queue</button>" in active_html

    dwb.pause_board(board.slug)
    paused_html = dwb.render_public_session_board_html("6163")

    assert 'class="runtime runtime-paused">paused</strong>' in paused_html
    assert 'action="/workers/6163/start?return_to=/workers/6163"' in paused_html
    assert ">Resume</button>" in paused_html


def test_public_session_board_links_discord_thread_and_workers_index(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb

    dwb.set_goal(thread_id="6162", goal="Watch the board", guild_id="111")

    html = dwb.render_public_session_board_html("6162")

    assert '<a class="back-link" href="/workers">Worker Boards</a>' in html
    assert (
        '<span>Discord: <a href="https://discord.com/channels/111/6162" '
        'target="_blank" rel="noopener noreferrer"><code>6162</code></a></span>'
    ) in html


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


def test_worker_routes_require_dashboard_auth(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.web_server import app

    board = dwb.set_goal(thread_id="7171", goal="Public workers stay public")
    token = board.worker["share_token"]
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.list_tasks(conn, include_archived=False)[0].id
    finally:
        conn.close()
    client = TestClient(app)

    dashboard = client.get("/")
    dashboard_kanban = client.get("/kanban")
    index = client.get("/workers")
    session = client.get("/workers/7171")
    ticket_state = client.get(f"/workers/7171/tickets/{task_id}/state")
    ticket_terminal = client.get(f"/workers/7171/tickets/{task_id}/terminal")
    root_legacy = client.get("/7171", follow_redirects=False)
    kanban_legacy = client.get("/kanban/7171", follow_redirects=False)
    token_resp = client.get(f"/workers/public/kanban/{token}")
    old_token_resp = client.get(f"/public/kanban/{token}")
    nested_worker = client.get("/workers/7171/extra")
    nested_kanban = client.get("/kanban/7171/extra")
    nested_token = client.get(f"/workers/public/kanban/{token}/extra")
    start = client.post("/workers/7171/start", follow_redirects=False)
    pause = client.post("/workers/7171/pause", follow_redirects=False)

    assert dashboard.status_code == 401
    assert dashboard_kanban.status_code == 401
    assert index.status_code == 401
    assert session.status_code == 401
    assert ticket_state.status_code == 401
    assert ticket_terminal.status_code == 401
    assert root_legacy.status_code == 401
    assert kanban_legacy.status_code == 401
    assert token_resp.status_code == 401
    assert old_token_resp.status_code == 401
    assert start.status_code == 401
    assert pause.status_code == 401
    assert nested_worker.status_code == 401
    assert nested_kanban.status_code == 401
    assert nested_token.status_code == 401


def test_worker_index_start_and_pause_actions(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    board = dwb.set_goal(thread_id="7272", goal="Toggle board")
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    pause_resp = client.post("/workers/7272/pause", follow_redirects=False)
    paused = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    start_resp = client.post("/workers/7272/start", follow_redirects=False)
    started = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    missing = client.post("/workers/missing/start", follow_redirects=False)

    assert pause_resp.status_code == 303
    assert pause_resp.headers["location"] == "/workers"
    assert paused["paused"] is True
    assert paused["goal_status"] == "paused"
    assert paused["phase"] == "paused"
    assert paused["phase_before_pause"] == "planning"
    assert start_resp.status_code == 303
    assert started["paused"] is False
    assert started["cancelled"] is False
    assert started["goal_status"] == "active"
    assert started["phase"] == "planning"
    assert missing.status_code == 404


def test_worker_detail_start_and_pause_actions_redirect_back(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    board = dwb.set_goal(thread_id="7373", goal="Toggle board from detail")
    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    pause_resp = client.post(
        "/workers/7373/pause?return_to=/workers/7373",
        follow_redirects=False,
    )
    paused = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    start_resp = client.post(
        "/workers/7373/start?return_to=/workers/7373",
        follow_redirects=False,
    )
    started = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    external_resp = client.post(
        "/workers/7373/pause?return_to=https%3A%2F%2Fexample.test%2Fbad",
        follow_redirects=False,
    )

    assert pause_resp.status_code == 303
    assert pause_resp.headers["location"] == "/workers/7373"
    assert paused["paused"] is True
    assert start_resp.status_code == 303
    assert start_resp.headers["location"] == "/workers/7373"
    assert started["paused"] is False
    assert external_resp.status_code == 303
    assert external_resp.headers["location"] == "/workers"


def test_worker_ticket_state_endpoint_returns_redacted_state(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    board = dwb.set_goal(thread_id="8181", goal="Inspect state")
    conn = kanban_db.connect(board=board.slug)
    try:
        task = kanban_db.list_tasks(conn, include_archived=False)[0]
        claimed = kanban_db.claim_task(conn, task.id)
        assert claimed is not None
        kanban_db.complete_task(
            conn,
            task.id,
            summary="Read /home/droid/private/config.yaml",
            metadata={"path": "/home/droid/private/config.yaml"},
            expected_run_id=claimed.current_run_id,
        )
    finally:
        conn.close()
    kanban_db._append_worker_log_line(
        kanban_db.worker_log_path(task.id, board=board.slug),
        "ran cat /home/droid/private/config.yaml with key sk-proj-A1B2C3D4E5F6G7H8I9J0",
    )
    dwb.record_codex_worker_event(
        task.id,
        board=board.slug,
        event={
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "commandExecution",
                    "cwd": "/home/droid/private",
                    "aggregatedOutput": "token sk-proj-A1B2C3D4E5F6G7H8I9J0",
                }
            },
        },
    )

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    resp = client.get(f"/workers/8181/tickets/{task.id}/state")
    missing = client.get("/workers/8181/tickets/t_missing/state")

    assert resp.status_code == 200
    data = resp.json()
    rendered = json.dumps(data)
    assert data["task"]["id"] == task.id
    assert data["current_run"]["id"] == claimed.current_run_id
    assert data["runs"][0]["summary"] == "Read [REDACTED_PATH]"
    assert "[REDACTED_PATH]" in rendered
    assert "/home/droid/private" not in rendered
    assert "sk-proj-A1B2C3D4E5F6G7H8I9J0" not in rendered
    assert data["codex_state"]["available"] is True
    assert data["codex_state"]["events"][0]["item_type"] == "commandExecution"
    assert missing.status_code == 404


def test_worker_ticket_terminal_endpoint_returns_sanitized_feed(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    board = dwb.set_goal(thread_id="8183", goal="Inspect terminal")
    conn = kanban_db.connect(board=board.slug)
    try:
        task = kanban_db.list_tasks(conn, include_archived=False)[0]
        claimed = kanban_db.claim_task(conn, task.id)
        assert claimed is not None
        kanban_db._set_worker_pid(conn, task.id, 12345)
        kanban_db.complete_task(
            conn,
            task.id,
            summary="Read /home/droid/private/config.yaml",
            metadata={"path": "/home/droid/private/config.yaml"},
            expected_run_id=claimed.current_run_id,
        )
    finally:
        conn.close()
    log_path = kanban_db.worker_log_path(task.id, board=board.slug)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kanban_db._append_worker_log_line(
        log_path,
        "[kanban dispatcher] scheduled Codex role worker: role=planner reasoning=high mode=fast",
    )
    kanban_db._append_worker_log_line(
        log_path,
        "[kanban dispatcher] spawning Codex role worker: hermes chat -q secret prompt",
    )
    kanban_db._append_worker_log_line(
        log_path,
        "ran cat /home/droid/private/config.yaml with key sk-proj-A1B2C3D4E5F6G7H8I9J0",
    )
    dwb.record_codex_worker_event(
        task.id,
        board=board.slug,
        event={
            "method": "item/completed",
            "params": {
                "item": {
                    "type": "commandExecution",
                    "cwd": "/home/droid/private",
                    "aggregatedOutput": "token sk-proj-A1B2C3D4E5F6G7H8I9J0",
                }
            },
        },
    )

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    resp = client.get(f"/workers/8183/tickets/{task.id}/terminal")
    missing = client.get("/workers/8183/tickets/t_missing/terminal")

    assert resp.status_code == 200
    data = resp.json()
    rendered = json.dumps(data)
    assert data["task"]["id"] == task.id
    assert data["current_run"]["id"] == claimed.current_run_id
    assert data["lines"][0] == f"$ ticket {task.id}"
    assert "scheduled Codex role worker: role=planner reasoning=high mode=fast" in "\n".join(data["lines"])
    assert "completed: Read [REDACTED_PATH]" in "\n".join(data["lines"])
    assert "codex_state" not in data
    assert "events" not in data
    assert "aggregatedOutput" not in rendered
    assert "commandExecution" not in rendered
    assert "spawning Codex role worker" not in rendered
    assert "secret prompt" not in rendered
    assert "/home/droid/private" not in rendered
    assert "sk-proj-A1B2C3D4E5F6G7H8I9J0" not in rendered
    assert missing.status_code == 404


def test_worker_ticket_state_endpoint_reports_empty_codex_state(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from fastapi.testclient import TestClient
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    board = dwb.set_goal(thread_id="8182", goal="Inspect empty internals")
    conn = kanban_db.connect(board=board.slug)
    try:
        task = kanban_db.list_tasks(conn, include_archived=False)[0]
        claimed = kanban_db.claim_task(conn, task.id)
        assert claimed is not None
    finally:
        conn.close()

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    resp = client.get(f"/workers/8182/tickets/{task.id}/state")

    assert resp.status_code == 200
    data = resp.json()
    assert data["current_run"]["id"] == claimed.current_run_id
    assert data["codex_state"] == {
        "available": False,
        "message": "No Codex app-server internals captured for this ticket yet.",
    }


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


def test_add_subgoal_activates_direct_dev_ticket_board(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.ensure_discord_thread_board(
        thread_id="1001",
        initial_request="/subgoal Add regression tests",
    )
    idx, text = dwb.add_subgoal(board.slug, "Add regression tests")

    conn = kanban_db.connect(board=board.slug)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()

    meta = kanban_db.read_board_metadata(board.slug)
    worker = meta["discord_worker"]
    assert (idx, text) == (1, "Add regression tests")
    assert worker["execution_mode"] == "kanban_pipeline"
    assert worker["goal_status"] == "active"
    assert worker["phase"] == "dev"
    assert len(tasks) == 1
    assert tasks[0].assignee == "dev"
    assert tasks[0].created_by == "discord-subgoal"


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


def test_running_worker_thread_targets_returns_running_role_boards(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.set_goal(
        thread_id="2401",
        goal="Ship typed workers",
        chat_id="parent-1",
    )
    other = dwb.set_goal(thread_id="2402", goal="Idle board", chat_id="parent-2")
    conn = kanban_db.connect(board=board.slug)
    try:
        role_task = _create_ready_dev_task(board.slug)
        non_role_task = kanban_db.create_task(
            conn,
            title="Running non-role task",
            assignee="ordinary-worker",
            tenant=board.slug,
        )
        kanban_db.claim_task(conn, role_task)
        kanban_db.claim_task(conn, non_role_task)
    finally:
        conn.close()

    targets = dwb.running_worker_thread_targets()

    assert targets == [
        {
            "board": board.slug,
            "thread_id": "2401",
            "chat_id": "parent-1",
            "running": 1,
        }
    ]
    assert other.slug not in {target["board"] for target in targets}


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
