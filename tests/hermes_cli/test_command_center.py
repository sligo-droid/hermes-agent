import json
from pathlib import Path

from hermes_cli import command_center, kanban_db
from self_improvement import proposal_storage

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "self_improvement"


def _fixture_payload() -> dict:
    return json.loads((FIXTURES / "proposal_run_pid_valid.json").read_text(encoding="utf-8"))


def _ingest_valid(monkeypatch, tmp_path) -> dict:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    payload = _fixture_payload()
    return proposal_storage.ingest_proposal_output(json.dumps(payload))


def _first_card() -> dict:
    cards = proposal_storage.list_cards()["cards"]
    assert cards
    return cards[0]


def test_snapshot_hides_rejected_cards_by_default_and_exposes_source_ids(tmp_path, monkeypatch):
    _ingest_valid(monkeypatch, tmp_path)
    card = _first_card()
    proposal_storage.record_rejection(card["proposal_id"], reason="not worth doing", actor="operator")

    default_snapshot = command_center.build_command_center_snapshot()
    assert [item for item in default_snapshot["work_items"] if item["id"].startswith("self-improvement:")] == []

    archived_snapshot = command_center.build_command_center_snapshot(include_archived=True)
    item = next(item for item in archived_snapshot["work_items"] if item["id"] == f"self-improvement:{card['proposal_id']}")
    assert item["status"] == "rejected"
    assert item["source"]["id"] == f"source:self-improvement-proposal:{card['proposal_id']}"
    assert item["source"]["kind"] == "self_improvement"


def test_snapshot_preserves_approval_artifacts_after_followup_audit_events(tmp_path, monkeypatch):
    _ingest_valid(monkeypatch, tmp_path)
    card = _first_card()
    proposal_storage.record_approval(
        card["proposal_id"],
        kanban_task_id="t_approved",
        worker_url="/workers/legacy-board/tickets/t_approved",
        actor="operator",
        metadata={
            "board": "discord-command-center",
            "discord_thread_url": "https://discord.com/channels/1/2/3",
            "discord_board_public_url": "/workers/discord-command-center",
        },
    )
    proposal_storage.record_audit_event(
        card["proposal_id"],
        action="halted",
        actor="operator",
        reason="pause after approval",
        metadata={"halted_by": "operator"},
    )

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"self-improvement:{card['proposal_id']}")
    artifact_urls = {artifact["url"] for artifact in item["artifacts"]}

    assert item["execution"]["board"] == "discord-command-center"
    assert item["execution"]["worker_url"] is None
    assert "https://discord.com/channels/1/2/3" in artifact_urls
    assert "/workers/discord-command-center" in artifact_urls
    assert item["raw"]["approval_metadata"]["halted_by"] == "operator"


def test_snapshot_uses_stored_worker_url_when_approval_metadata_is_absent(tmp_path, monkeypatch):
    _ingest_valid(monkeypatch, tmp_path)
    card = _first_card()
    proposal_storage.record_approval(
        card["proposal_id"],
        kanban_task_id="t_legacy",
        worker_url="/workers?task=t_legacy",
        actor="operator",
    )

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"self-improvement:{card['proposal_id']}")
    artifact_urls = {artifact["url"] for artifact in item["artifacts"]}

    assert item["execution"]["worker_url"] is None
    assert item["execution"]["task_url"] == "/workers?task=t_legacy"
    assert "/workers?task=t_legacy" in artifact_urls


def test_snapshot_marks_paused_default_proposal_task_resumable_and_archivable(tmp_path, monkeypatch):
    _ingest_valid(monkeypatch, tmp_path)
    card = _first_card()
    conn = kanban_db.connect()
    try:
        task_id = kanban_db.create_task(conn, title="Paused downstream task", initial_status="blocked")
    finally:
        conn.close()
    proposal_storage.record_approval(
        card["proposal_id"],
        kanban_task_id=task_id,
        worker_url=f"/workers?task={task_id}",
        actor="operator",
    )

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"self-improvement:{card['proposal_id']}")

    assert item["status"] == "blocked"
    assert item["execution"]["paused"] is True
    assert item["execution"]["resumable"] is True
    assert item["decision"]["pause_action"].endswith(f"/{card['proposal_id']}/pause")
    assert item["decision"]["resume_action"].endswith(f"/{card['proposal_id']}/resume")
    assert item["decision"]["archive_action"].endswith(f"/{card['proposal_id']}/halt")


def test_snapshot_always_includes_active_runs_outside_recent_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-active-run-test"
    kanban_db.write_board_metadata(board, name="Discord Active Run Test")
    conn = kanban_db.connect(board=board)
    try:
        active_task = kanban_db.create_task(conn, title="Old active worker", board=board)
        with conn:
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (active_task, "running", 100, None, None),
            )
            active_run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?",
                (active_run_id, active_task),
            )
        for index in range(25):
            task_id = kanban_db.create_task(conn, title=f"Completed worker {index}", board=board)
            with conn:
                conn.execute(
                    "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                    (task_id, "done", 1000 + index, 1001 + index, "done"),
                )
                conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (1001 + index, task_id))
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot(recent_run_limit_per_board=5)
    active_runs = [run for run in snapshot["runs"] if run["board"] == board and run["id"] == active_run_id]

    assert active_runs
    assert active_runs[0]["ended_at"] is None
    assert snapshot["metrics"]["active_runs"] == 1


def test_snapshot_rolls_named_discord_board_tasks_up_to_board_work_item(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-1512105486609289247"
    kanban_db.write_board_metadata(board, name="Discord Feature Thread")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Implement feature from Discord",
            body="Original Discord request details.",
            tenant="PID",
            board=board,
        )
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")
    source = next(source for source in snapshot["sources"] if source["id"] == f"source:discord:{board}")

    assert item["source"]["kind"] == "discord"
    assert item["source"]["id"] == f"source:discord:{board}"
    assert item["execution"]["board"] == board
    assert item["execution"]["task_id"] is None
    assert item["execution"]["archiveable"] is True
    assert item["execution"]["archive_action"] == f"/api/plugins/kanban/boards/{board}"
    assert item["execution"]["worker_url"] is None
    assert not any(row["id"] == f"kanban:{board}:{task_id}" for row in snapshot["work_items"])
    assert source["kind"] == "discord_thread"
    assert snapshot["metrics"]["discord_origin"] == 1


def test_snapshot_running_board_rollup_outranks_blocked_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    older_board = "discord-running-with-blocked"
    newer_board = "discord-blocked-only"
    kanban_db.write_board_metadata(older_board, name="Running Board")
    kanban_db.write_board_metadata(newer_board, name="Blocked Board")

    conn = kanban_db.connect(board=older_board)
    try:
        running_id = kanban_db.create_task(conn, title="Active worker", board=older_board, initial_status="running")
        kanban_db.create_task(conn, title="Paused sibling", board=older_board, initial_status="blocked")
        with conn:
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (running_id, "running", 100, None, None),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?", (run_id, running_id))
    finally:
        conn.close()

    conn = kanban_db.connect(board=newer_board)
    try:
        kanban_db.create_task(conn, title="Blocked worker", board=newer_board, initial_status="blocked")
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    running_item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{older_board}")
    blocked_item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{newer_board}")

    assert running_item["status"] == "running"
    assert blocked_item["status"] == "blocked"
    assert snapshot["work_items"].index(running_item) < snapshot["work_items"].index(blocked_item)


def test_worker_board_url_rejects_top_level_public_worker_urls():
    board = "discord-worker-board"

    for public_url in (
        "/workers",
        "/workers/",
        "/workers?filter=active",
        "/workers/#active",
        "https://hermes.sligolabs.com/workers",
        "https://hermes.sligolabs.com/workers/?filter=active#running",
    ):
        assert command_center._worker_board_url(board, public_url) == f"/workers/{board}"

    assert command_center._worker_board_url(board, "/workers/discord-worker-board") == "/workers/discord-worker-board"
    assert (
        command_center._worker_board_url(board, "https://hermes.sligolabs.com/workers/discord-worker-board")
        == "https://hermes.sligolabs.com/workers/discord-worker-board"
    )
    assert command_center._worker_board_url(kanban_db.DEFAULT_BOARD, "/workers") is None
    assert command_center._worker_board_url(None, "https://hermes.sligolabs.com/workers/") is None


def test_snapshot_worker_url_requires_started_execution_and_named_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    unstarted_board = "discord-worker-not-started"
    started_board = "discord-worker-started"
    kanban_db.write_board_metadata(unstarted_board, name="Unstarted Worker Board")
    kanban_db.write_board_metadata(started_board, name="Started Worker Board")

    conn = kanban_db.connect(board=unstarted_board)
    try:
        kanban_db.create_task(conn, title="Queued worker", board=unstarted_board)
    finally:
        conn.close()

    conn = kanban_db.connect(board=started_board)
    try:
        task_id = kanban_db.create_task(conn, title="Started worker", board=started_board, initial_status="running")
        with conn:
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (task_id, "running", 100, None, None),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
    finally:
        conn.close()

    default_conn = kanban_db.connect()
    try:
        default_task_id = kanban_db.create_task(default_conn, title="Default board started worker", initial_status="running")
        with default_conn:
            default_conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (default_task_id, "running", 100, None, None),
            )
            default_run_id = default_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            default_conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (default_run_id, default_task_id))
    finally:
        default_conn.close()

    snapshot = command_center.build_command_center_snapshot()
    unstarted_item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{unstarted_board}")
    started_item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{started_board}")
    default_item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban:default:{default_task_id}")

    assert unstarted_item["execution"]["worker_url"] is None
    assert started_item["execution"]["worker_url"] == f"/workers/{started_board}"
    assert default_item["execution"]["worker_url"] is None


def test_snapshot_skips_internal_default_board_foreman_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    conn = kanban_db.connect()
    try:
        foreman_task_id = kanban_db.create_task(
            conn,
            title="Recover discord-1512215349011943478/t_84004635",
            body="<foreman-metadata>{}</foreman-metadata>",
            created_by="discord-worker-foreman",
            idempotency_key="discord-foreman:discord-1512215349011943478:t_84004635",
        )
        ordinary_task_id = kanban_db.create_task(
            conn,
            title="Manual operator request",
            created_by="operator",
        )
        with conn:
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (foreman_task_id, "done", 100, 120, "completed"),
            )
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (ordinary_task_id, "done", 200, 220, "completed"),
            )
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item_ids = {item["id"] for item in snapshot["work_items"]}
    run_task_ids = {run["task_id"] for run in snapshot["runs"]}

    assert f"kanban:default:{foreman_task_id}" not in item_ids
    assert f"kanban:default:{ordinary_task_id}" in item_ids
    assert foreman_task_id not in run_task_ids
    assert ordinary_task_id in run_task_ids


def test_snapshot_inbox_metric_only_counts_pending_decisions_and_inbox_sources(tmp_path, monkeypatch):
    _ingest_valid(monkeypatch, tmp_path)
    proposed_card = _first_card()
    board = "discord-finished-not-inbox"
    kanban_db.write_board_metadata(board, name="Finished Discord Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Finished worker", board=board)
        with conn:
            conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (200, task_id))
    finally:
        conn.close()

    storage_conn = proposal_storage.connect()
    try:
        with storage_conn:
            storage_conn.execute(
                "UPDATE proposal_runs SET status = ?, parse_error = ? WHERE id = (SELECT MIN(id) FROM proposal_runs)",
                ("malformed", "bad json"),
            )
    finally:
        storage_conn.close()

    snapshot = command_center.build_command_center_snapshot()
    board_item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")

    assert board_item["status"] == "shipped"
    assert board_item["execution"]["archiveable"] is True
    assert snapshot["metrics"]["inbox"] == 2
    assert any(item["id"] == f"self-improvement:{proposed_card['proposal_id']}" for item in snapshot["work_items"])
    assert any(source["bucket"] == "inbox" and source["status"] == "parse_failed" for source in snapshot["sources"])


def test_snapshot_exposes_issue_pulse_status_points(tmp_path, monkeypatch):
    _ingest_valid(monkeypatch, tmp_path)
    board = "discord-blocked-pulse"
    kanban_db.write_board_metadata(board, name="Blocked Pulse Board")
    conn = kanban_db.connect(board=board)
    try:
        kanban_db.create_task(conn, title="Blocked worker", board=board, initial_status="blocked")
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    pulse = {point["status"]: point for point in snapshot["metrics"]["issue_pulse"]}

    assert pulse["blocked"]["count"] == snapshot["metrics"]["by_status"]["blocked"]
    assert pulse["blocked"]["label"] == "Blocked"
    assert pulse["proposed"]["count"] >= 1


def test_self_improvement_board_rollup_preserves_proposal_controls(tmp_path, monkeypatch):
    _ingest_valid(monkeypatch, tmp_path)
    card = _first_card()
    board = "self-improvement-worker-board"
    kanban_db.write_board_metadata(board, name="Self Improvement Worker Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Approved recommendation worker", board=board, initial_status="blocked")
    finally:
        conn.close()
    proposal_storage.record_approval(
        card["proposal_id"],
        kanban_task_id=task_id,
        worker_url=f"/workers/{board}/tickets/{task_id}",
        actor="operator",
        metadata={"board": board},
    )

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")

    assert not any(row["id"] == f"self-improvement:{card['proposal_id']}" for row in snapshot["work_items"])
    assert item["decision"]["needed"] is False
    assert item["decision"]["proposal_id"] == card["proposal_id"]
    assert item["decision"]["halt_action"].endswith(f"/{card['proposal_id']}/halt")
    assert item["decision"]["pause_action"].endswith(f"/{card['proposal_id']}/pause")
    assert item["decision"]["resume_action"].endswith(f"/{card['proposal_id']}/resume")
    assert item["decision"]["undo_followup_action"].endswith(f"/{card['proposal_id']}/undo-followup")


def test_self_improvement_board_rollup_preserves_proposal_naming_and_context(tmp_path, monkeypatch):
    _ingest_valid(monkeypatch, tmp_path)
    card = _first_card()
    board = "self-improvement-named-worker-board"
    kanban_db.write_board_metadata(board, name="Verbose Generic Worker Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Generic downstream task title", board=board, initial_status="running")
        with conn:
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (task_id, "running", 100, None, None),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?", (run_id, task_id))
    finally:
        conn.close()
    proposal_storage.record_approval(
        card["proposal_id"],
        kanban_task_id=task_id,
        worker_url=f"/workers/{board}/tickets/{task_id}",
        actor="operator",
        metadata={"board": board},
    )

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")

    assert snapshot["work_items"][0]["id"] == f"kanban-board:{board}"
    assert item["status"] == "running"
    assert item["title"] == card["title"]
    assert item["summary"] == card["summary"]
    assert item["project"] == card["project"]
    assert item["priority"] == card["priority"]
    assert item["priority_rank"] == command_center._priority_rank(card["priority"])
    assert item["severity"] == card["severity"]
    assert item["source"]["kind"] == "self_improvement"
    assert item["raw"]["proposal_action_context"]["title"] == card["title"]


def test_direct_discord_board_rollup_has_no_self_improvement_controls(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-direct-no-proposal"
    kanban_db.write_board_metadata(board, name="Direct Discord Board")
    conn = kanban_db.connect(board=board)
    try:
        kanban_db.create_task(conn, title="Direct Discord worker", board=board, initial_status="blocked")
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")

    assert item["source"]["kind"] == "discord"
    assert item["decision"] == {"needed": False}


def test_snapshot_active_runs_ignores_stale_open_runs_for_terminal_tasks(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-stale-open-run"
    kanban_db.write_board_metadata(board, name="Discord Stale Run")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Finished worker", board=board)
        with conn:
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (task_id, "running", 100, None, None),
            )
            conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (200, task_id))
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    stale_run = next(run for run in snapshot["runs"] if run["board"] == board and run["task_id"] == task_id)
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")

    assert stale_run["ended_at"] is None
    assert stale_run["task_status"] == "done"
    assert snapshot["metrics"]["active_runs"] == 0
    assert item["status"] == "shipped"


def test_snapshot_sorts_iso_timestamped_work_items_by_recency(tmp_path, monkeypatch):
    _ingest_valid(monkeypatch, tmp_path)
    payload = _fixture_payload()
    payload["run"]["run_id"] = "older-run"
    payload["cards"][0]["idempotency_key"] = "older-card"
    payload["cards"][0]["title"] = "Older recommendation"
    payload["cards"][0]["created_at"] = "2026-06-01T00:00:00Z"
    proposal_storage.ingest_proposal_output(json.dumps(payload), source={"run_id": "older-run"})

    conn = proposal_storage.connect()
    try:
        with conn:
            conn.execute("UPDATE proposal_cards SET updated_at = ? WHERE title = ?", ("2026-06-01T00:00:00Z", "Older recommendation"))
            conn.execute(
                "UPDATE proposal_cards SET updated_at = ? WHERE title != ?",
                ("2026-06-04T02:04:10Z", "Older recommendation"),
            )
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    self_improvement_titles = [
        item["title"]
        for item in snapshot["work_items"]
        if item["source"]["kind"] == "self_improvement"
    ]

    assert self_improvement_titles[:2] == [
        "Add backoff logging to PID scraper timeout retries",
        "Older recommendation",
    ]
