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
    assert item["execution"]["worker_url"] == "/workers/discord-command-center"
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

    assert item["execution"]["worker_url"] == "/workers?task=t_legacy"
    assert item["execution"]["task_url"] == "/workers?task=t_legacy"
    assert "/workers?task=t_legacy" in artifact_urls


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


def test_snapshot_promotes_discord_board_tasks_to_discord_origin_work_items(tmp_path, monkeypatch):
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
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban:{board}:{task_id}")
    source = next(source for source in snapshot["sources"] if source["id"] == f"source:discord:{board}")

    assert item["source"]["kind"] == "discord"
    assert item["source"]["id"] == f"source:discord:{board}"
    assert item["execution"]["board"] == board
    assert item["execution"]["worker_url"] == f"/workers/{board}"
    assert source["kind"] == "discord_thread"
    assert snapshot["metrics"]["discord_origin"] == 1


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
