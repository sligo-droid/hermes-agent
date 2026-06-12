import json
import os
from pathlib import Path

from hermes_cli import command_center, command_center_annotations, command_center_verification, kanban_db
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


def test_snapshot_cache_reuses_snapshot_until_ttl_or_force_refresh(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    command_center.invalidate_snapshot_cache()
    call_count = 0
    monotonic_now = 100.0
    original_list_boards = kanban_db.list_boards

    def counted_list_boards(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_list_boards(*args, **kwargs)

    monkeypatch.setattr(kanban_db, "list_boards", counted_list_boards)
    monkeypatch.setattr(command_center.time, "monotonic", lambda: monotonic_now)

    first = command_center.get_cached_command_center_snapshot()
    first["work_items"].append({"id": "mutated-caller-copy"})
    second = command_center.get_cached_command_center_snapshot()

    assert call_count == 1
    assert all(item.get("id") != "mutated-caller-copy" for item in second["work_items"])

    command_center.get_cached_command_center_snapshot(force_refresh=True)
    assert call_count == 2

    monotonic_now += 3.1
    command_center.get_cached_command_center_snapshot()
    assert call_count == 3


def test_snapshot_cache_keys_include_project_archived_and_recent_run_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    command_center.invalidate_snapshot_cache()
    call_count = 0
    original_list_boards = kanban_db.list_boards

    monkeypatch.setattr(command_center.time, "monotonic", lambda: 200.0)

    def counted_list_boards(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return original_list_boards(*args, **kwargs)

    monkeypatch.setattr(kanban_db, "list_boards", counted_list_boards)

    command_center.get_cached_command_center_snapshot(project="hermes", include_archived=False, recent_run_limit_per_board=20)
    command_center.get_cached_command_center_snapshot(project="hermes", include_archived=False, recent_run_limit_per_board=20)
    command_center.get_cached_command_center_snapshot(project="pid", include_archived=False, recent_run_limit_per_board=20)
    command_center.get_cached_command_center_snapshot(project="hermes", include_archived=True, recent_run_limit_per_board=20)
    command_center.get_cached_command_center_snapshot(project="hermes", include_archived=False, recent_run_limit_per_board=5)

    assert call_count == 4


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


def test_snapshot_enriches_work_items_with_operator_annotations(tmp_path, monkeypatch):
    _ingest_valid(monkeypatch, tmp_path)
    card = _first_card()
    work_item_id = f"self-improvement:{card['proposal_id']}"

    command_center_annotations.record_annotation(
        work_item_id=work_item_id,
        mode="note",
        text="Operator note for later approval.",
        actor="operator",
        target_kind="self_improvement_proposal",
        target_id=card["proposal_id"],
        previous_title=card["title"],
        previous_summary=card["summary"],
        previous_status="proposed",
        source_ref={"proposal_id": card["proposal_id"]},
        execution_snapshot={},
        created_at=100,
    )
    command_center_annotations.record_annotation(
        work_item_id=work_item_id,
        mode="correction",
        title="Use safer approach",
        text="Redirect implementation to the safer path.",
        actor="operator",
        target_kind="self_improvement_proposal",
        target_id=card["proposal_id"],
        previous_title=card["title"],
        previous_summary=card["summary"],
        previous_status="proposed",
        source_ref={"proposal_id": card["proposal_id"]},
        execution_snapshot={},
        created_at=101,
    )

    original_connect = command_center_annotations._connect
    connect_count = 0

    def counted_connect():
        nonlocal connect_count
        connect_count += 1
        return original_connect()

    monkeypatch.setattr(command_center_annotations, "_connect", counted_connect)

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == work_item_id)

    assert connect_count == 1
    assert item["title"] == card["title"]
    assert item["summary"] == card["summary"]
    assert item["operator_note_count"] == 1
    assert item["latest_operator_note"]["text"] == "Operator note for later approval."
    assert item["latest_correction"]["title"] == "Use safer approach"
    assert [annotation["mode"] for annotation in item["annotations"]] == ["note", "correction"]


def test_batch_annotation_enrichment_handles_multiple_and_missing_items(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    first_id = "self-improvement:first"
    second_id = "kanban-board:second"
    missing_id = "kanban-board:missing"

    command_center_annotations.record_annotation(
        work_item_id=first_id,
        mode="note",
        text="First note.",
        actor="operator",
        target_kind="self_improvement_proposal",
        target_id="first",
        previous_title="First",
        previous_summary="First summary",
        previous_status="proposed",
        source_ref={},
        execution_snapshot={},
        created_at=100,
    )
    command_center_annotations.record_annotation(
        work_item_id=second_id,
        mode="correction",
        title="Second correction",
        text="Second correction text.",
        actor="operator",
        target_kind="kanban_board",
        target_id="second",
        previous_title="Second",
        previous_summary="Second summary",
        previous_status="running",
        source_ref={},
        execution_snapshot={},
        created_at=101,
    )

    items = [{"id": first_id}, {"id": second_id}, {"id": missing_id}]

    result = command_center_annotations.enrich_work_items(items)

    assert result is items
    assert items[0]["operator_note_count"] == 1
    assert items[0]["latest_operator_note"]["text"] == "First note."
    assert items[0]["latest_correction"] is None
    assert items[1]["operator_note_count"] == 0
    assert items[1]["latest_operator_note"] is None
    assert items[1]["latest_correction"]["title"] == "Second correction"
    assert items[2]["annotations"] == []
    assert items[2]["operator_note_count"] == 0
    assert items[2]["latest_operator_note"] is None
    assert items[2]["latest_correction"] is None


def test_batch_annotation_lookup_empty_list_does_not_query(monkeypatch):
    def fail_connect():
        raise AssertionError("empty annotation batch should not open sqlite")

    monkeypatch.setattr(command_center_annotations, "_connect", fail_connect)

    assert command_center_annotations.annotations_by_work_item([]) == {}
    assert command_center_annotations.enrich_work_items([]) == []


def test_snapshot_preserves_approval_artifacts_after_followup_audit_events(tmp_path, monkeypatch):
    _ingest_valid(monkeypatch, tmp_path)
    card = _first_card()
    board = "discord-command-center"
    kanban_db.write_board_metadata(board, name="Discord Command Center")
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
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")
    artifact_urls = {artifact["url"] for artifact in item["artifacts"]}

    assert "https://discord.com/channels/1/2/3" in artifact_urls
    assert "/workers/discord-command-center" in artifact_urls
    assert not any(row["id"] == f"self-improvement:{card['proposal_id']}" for row in snapshot["work_items"])


def test_snapshot_derives_worker_url_from_legacy_approval_board_metadata(tmp_path, monkeypatch):
    _ingest_valid(monkeypatch, tmp_path)
    card = _first_card()
    board = "1512960023947378698"
    kanban_db.write_board_metadata(board, name="Self Improvement Worker")
    proposal_storage.record_approval(
        card["proposal_id"],
        kanban_task_id="t_approved",
        worker_url="/workers",
        actor="operator",
        metadata={
            "board": board,
            "discord_board": board,
            "discord_thread_id": board,
            "discord_top_level_message_id": board,
            "discord_board_public_url": "https://sligo.sligolabs.com/workers",
        },
    )

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")
    artifact_urls = {artifact["url"] for artifact in item["artifacts"] if artifact["kind"] == "worker_board"}

    assert item["project"] == "pid"
    assert item["execution"]["worker_url"] == f"/workers/{board}"
    assert f"/workers/{board}" in artifact_urls
    assert "/workers" not in artifact_urls


def test_snapshot_omits_default_board_approved_proposal_rows(tmp_path, monkeypatch):
    _ingest_valid(monkeypatch, tmp_path)
    approved_card = _first_card()
    payload = _fixture_payload()
    payload["run"]["run_id"] = "second-run"
    payload["cards"][0]["idempotency_key"] = "second-card"
    payload["cards"][0]["title"] = "Pending recommendation"
    proposal_storage.ingest_proposal_output(json.dumps(payload), source={"run_id": "second-run"})
    proposal_storage.record_approval(
        approved_card["proposal_id"],
        kanban_task_id="t_legacy",
        worker_url="/workers?task=t_legacy",
        actor="operator",
    )

    snapshot = command_center.build_command_center_snapshot()
    item_ids = {item["id"] for item in snapshot["work_items"]}

    assert f"self-improvement:{approved_card['proposal_id']}" not in item_ids
    assert any(item["title"] == "Pending recommendation" and item["status"] == "proposed" for item in snapshot["work_items"])


def test_snapshot_omits_paused_default_proposal_task_work_item(tmp_path, monkeypatch):
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

    assert not any(item["id"] == f"self-improvement:{card['proposal_id']}" for item in snapshot["work_items"])


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
    assert item["execution"]["worker_url"] == f"/workers/{board}"
    assert not any(row["id"] == f"kanban:{board}:{task_id}" for row in snapshot["work_items"])
    assert source["kind"] == "discord_thread"
    assert snapshot["metrics"]["discord_origin"] == 1


def test_snapshot_archive_includes_moved_archived_worker_boards(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-archived-feature-thread"
    meta = kanban_db.write_board_metadata(board, name="Archived Feature Thread")
    meta.pop("db_path", None)
    meta[command_center.DISCORD_WORKER_META_KEY] = {
        "guild_id": "111",
        "thread_id": "222",
        "source_message_id": "333",
        "project_context": {"project_name": "Hermes"},
    }
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Archived worker", board=board)
        with conn:
            conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (200, task_id))
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (task_id, "done", 100, 200, "done"),
            )
    finally:
        conn.close()
    archived_result = kanban_db.remove_board(board)

    default_snapshot = command_center.build_command_center_snapshot(include_archived=False, project="hermes")
    archived_snapshot = command_center.build_command_center_snapshot(include_archived=True, project="hermes")

    assert Path(archived_result["new_path"]).exists()
    assert not any(item.get("execution", {}).get("board") == board for item in default_snapshot["work_items"])
    item = next(item for item in archived_snapshot["work_items"] if item.get("execution", {}).get("board") == board)
    source = next(source for source in archived_snapshot["sources"] if source["id"] == item["source"]["id"])
    run = next(run for run in archived_snapshot["runs"] if run["board"] == board and run["task_id"] == task_id)

    assert item["id"].startswith(f"kanban-board:archive:{board}-")
    assert item["status"] == "archived"
    assert item["project"] == "hermes"
    assert item["execution"]["archiveable"] is False
    assert item["execution"]["worker_url"] is None
    assert item["source"]["ref"]["discord_url"] == "https://discord.com/channels/111/222/333"
    assert source["status"] == "archived"
    assert run["outcome"] == "done"
    assert item["id"] in {row["id"] for row in archived_snapshot["work_items"] if row["status"] == "archived"}


def test_snapshot_exposes_project_tabs_and_filters_hermes_dev_intake(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    hermes_board = "discord-hermes-dev"
    pid_board = "discord-pid-work"
    unknown_board = "discord-unknown-work"
    for board, project in ((hermes_board, "Hermes"), (pid_board, "PID"), (unknown_board, None)):
        meta = kanban_db.write_board_metadata(board, name=board)
        meta.pop("db_path", None)
        worker = {"thread_id": f"thread-{board}", "guild_id": "guild"}
        if project:
            worker["project_context"] = {"project_name": project}
        meta[command_center.DISCORD_WORKER_META_KEY] = worker
        kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")
        conn = kanban_db.connect(board=board)
        try:
            kanban_db.create_task(conn, title=f"{board} task", board=board, tenant=project)
        finally:
            conn.close()

    snapshot = command_center.build_command_center_snapshot(project="hermes")

    assert {project["key"] for project in snapshot["projects"]} >= {"hermes", "pid"}
    assert snapshot["current_project"] == "hermes"
    assert {item["id"] for item in snapshot["work_items"]} == {f"kanban-board:{hermes_board}"}
    assert snapshot["work_items"][0]["project"] == "hermes"
    assert snapshot["sources"][0]["project"] == "hermes"
    assert not any(pid_board in item["id"] for item in snapshot["work_items"])
    assert not any(unknown_board in item["id"] for item in snapshot["work_items"])


def test_snapshot_omits_default_discord_intake_from_work_items(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    body = "\n".join(
        [
            "Discord default-board intake.",
            "",
            "Source URL: https://discord.com/channels/guild-1/thread-1/message-1",
            "Guild ID: guild-1",
            "Channel IDs: channel-1, thread-1",
            "Thread ID: thread-1",
            "Message ID: message-1",
            "User: dev-user",
            "Workspace: /home/droid/workspaces/hermes-agent",
            "GitHub: https://github.com/sligodroid/hermes-agent",
            "",
            "Message:",
            "make #dev feed the top board",
        ],
    )
    idempotency_key = "discord-default-intake:guild-1:thread-1:message-1"
    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="#dev intake: make #dev feed the top board",
            body=body,
            created_by="discord-default-intake",
            tenant="discord-default-intake",
            idempotency_key=idempotency_key,
            initial_status="blocked",
            board=kanban_db.DEFAULT_BOARD,
        )
    finally:
        conn.close()

    hermes_snapshot = command_center.build_command_center_snapshot(project="hermes")

    assert not any(item["id"] == f"kanban:{kanban_db.DEFAULT_BOARD}:{task_id}" for item in hermes_snapshot["work_items"])

    pid_snapshot = command_center.build_command_center_snapshot(project="pid")
    assert not any(item["id"] == f"kanban:{kanban_db.DEFAULT_BOARD}:{task_id}" for item in pid_snapshot["work_items"])


def test_snapshot_does_not_classify_arbitrary_default_discord_task_as_hermes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Discord task from another intake",
            body="Source URL: https://discord.com/channels/guild-1/channel-1/message-1",
            created_by="discord-default-intake",
            tenant="discord-default-intake",
            idempotency_key="discord-default-intake:guild-1:channel-1:message-1",
            initial_status="blocked",
            board=kanban_db.DEFAULT_BOARD,
        )
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot(project="hermes")

    assert not any(item["id"] == f"kanban:{kanban_db.DEFAULT_BOARD}:{task_id}" for item in snapshot["work_items"])


def test_snapshot_pid_filter_preserves_source_work_item_execution_relationship(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-pid-relationship"
    meta = kanban_db.write_board_metadata(board, name="PID Relationship Board")
    meta.pop("db_path", None)
    meta[command_center.DISCORD_WORKER_META_KEY] = {
        "guild_id": "111",
        "thread_id": "222",
        "project_context": {"project_name": "PID"},
    }
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="PID execution child", board=board, tenant="PID", initial_status="running")
        with conn:
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (task_id, "running", 100, None, None),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?", (run_id, task_id))
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot(project="pid")
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")
    source = next(source for source in snapshot["sources"] if source["id"] == item["source"]["id"])
    run = next(run for run in snapshot["runs"] if run["board"] == board and run["task_id"] == task_id)

    assert item["project"] == "pid"
    assert item["source"]["kind"] == "discord"
    assert item["execution"]["board"] == board
    assert item["execution"]["active_run_id"] == run["id"]
    assert source["project"] == "pid"
    assert snapshot["metrics"]["total_work_items"] == 1


def test_snapshot_adds_discord_source_urls_for_worker_boards(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-source-url-test"
    meta = kanban_db.write_board_metadata(board, name="Discord Source URL Test")
    meta.pop("db_path", None)
    meta[command_center.DISCORD_WORKER_META_KEY] = {
        "guild_id": "111",
        "thread_id": "222",
        "source_message_id": "333",
    }
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")
    conn = kanban_db.connect(board=board)
    try:
        kanban_db.create_task(conn, title="Discord linked worker", board=board)
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")
    source = next(source for source in snapshot["sources"] if source["id"] == f"source:discord:{board}")
    artifact_urls = {artifact["url"] for artifact in item["artifacts"]}

    assert item["source"]["ref"]["discord_url"] == "https://discord.com/channels/111/222/333"
    assert item["source"]["ref"]["source_url"] == "https://discord.com/channels/111/222/333"
    assert item["source"]["ref"]["discord_thread_url"] == "https://discord.com/channels/111/222"
    assert source["ref"]["discord_url"] == "https://discord.com/channels/111/222/333"
    assert "https://discord.com/channels/111/222/333" in artifact_urls


def test_discord_url_helper_requires_guild_and_thread():
    assert command_center._discord_urls({"guild_id": "111", "thread_id": "222"}) == {
        "discord_url": "https://discord.com/channels/111/222",
        "source_url": "https://discord.com/channels/111/222",
        "discord_thread_url": "https://discord.com/channels/111/222",
    }
    assert command_center._discord_urls({"guild_id": "111"}) == {}
    assert command_center._discord_urls({"thread_id": "222"}) == {}


def test_snapshot_board_rollups_include_running_and_blocked_statuses(tmp_path, monkeypatch):
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


def test_snapshot_completed_discord_worker_board_is_shipped_not_active(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-1512532369897160735"
    meta = kanban_db.write_board_metadata(board, name="Completed Worker Board")
    meta[command_center.DISCORD_WORKER_META_KEY] = {
        "kind": "discord_worker_board",
        "thread_id": "1512532369897160735",
        "guild_id": "111",
        "source_message_id": "1512532369897160735",
        "goal_status": "done",
        "phase": "complete",
        "public_url": "https://sligo.sligolabs.com/workers/1512532369897160735",
    }
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Completed worker", board=board, initial_status="running")
        with conn:
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (task_id, "done", 100, 200, "done"),
            )
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")

    assert item["status"] == "shipped"
    assert item["execution"]["task_counts"] == {"running": 1}
    assert snapshot["metrics"]["active_runs"] == 0
    assert snapshot["metrics"]["active_work"] == 0
    assert snapshot["metrics"]["shipped"] == 1


def test_snapshot_phase_complete_discord_worker_board_is_shipped_not_active(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-phase-complete"
    meta = kanban_db.write_board_metadata(board, name="Phase Complete Worker Board")
    meta[command_center.DISCORD_WORKER_META_KEY] = {
        "kind": "discord_worker_board",
        "thread_id": "1512532369897160735",
        "phase": "complete",
        "public_url": "https://sligo.sligolabs.com/workers/1512532369897160735",
    }
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Stale active worker", board=board, initial_status="running")
        with conn:
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (task_id, "running", 100, None, None),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?", (run_id, task_id))
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")

    assert item["status"] == "shipped"
    assert item["execution"]["task_counts"] == {"running": 1}
    assert snapshot["metrics"]["active_runs"] == 0
    assert snapshot["metrics"]["active_work"] == 0


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


def test_snapshot_worker_url_exposes_named_board_even_before_execution_starts(tmp_path, monkeypatch):
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
            conn.execute("UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?", (run_id, task_id))
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
    assert unstarted_item["execution"]["worker_url"] == f"/workers/{unstarted_board}"
    assert started_item["execution"]["worker_url"] == f"/workers/{started_board}"
    assert not any(item["id"] == f"kanban:default:{default_task_id}" for item in snapshot["work_items"])


def test_snapshot_running_board_without_run_is_still_clickable_and_pauseable(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-running-without-run"
    kanban_db.write_board_metadata(board, name="Running Without Run")

    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Running task without run row", board=board)
        with conn:
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")

    assert item["status"] == "running"
    assert item["execution"]["board"] == board
    assert item["execution"]["pause_action"].endswith(f"/{board}/pause")
    assert item["execution"]["resume_action"].endswith(f"/{board}/resume")
    assert item["execution"]["archive_action"].endswith(f"/{board}")
    assert item["execution"]["worker_url"] == f"/workers/{board}"


def test_snapshot_valid_running_board_keeps_worker_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-valid-running-board"
    kanban_db.write_board_metadata(board, name="Valid Running Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Active worker", board=board, initial_status="running")
        with conn:
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (task_id, "running", 100, None, None),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE tasks SET status = 'running', current_run_id = ? WHERE id = ?", (run_id, task_id))
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")

    assert item["status"] == "running"
    assert item["execution"]["worker_url"] == f"/workers/{board}"
    assert "repair_required" not in item["execution"]


def test_snapshot_active_stub_with_nonterminal_archive_requires_repair(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-stub-with-archive"
    kanban_db.write_board_metadata(board, name="Archived Active Work")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Still active", board=board, initial_status="running")
        with conn:
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (task_id, "running", 100, None, None),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
    finally:
        conn.close()
    archived_result = kanban_db.remove_board(board)
    kanban_db.board_dir(board).mkdir(parents=True, exist_ok=True)
    kanban_db.board_metadata_path(board).write_text(
        json.dumps({"slug": board, "name": "Archived Active Work"}),
        encoding="utf-8",
    )

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")

    assert item["status"] == "blocked"
    assert item["status_detail"] == "repair_required"
    assert item["execution"]["worker_url"] is None
    assert item["execution"]["repair_required"] is True
    assert item["execution"]["archived_board_path"] == archived_result["new_path"]
    assert "Matching non-terminal board evidence is archived" in item["execution"]["repair_reason"]


def test_snapshot_readable_active_stub_with_empty_db_and_nonterminal_archive_requires_repair(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-readable-stub-empty-db"
    kanban_db.write_board_metadata(board, name="Archived Empty Stub")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Archived active task", board=board, initial_status="running")
        with conn:
            conn.execute(
                "INSERT INTO task_runs(task_id, status, started_at, ended_at, outcome) VALUES (?, ?, ?, ?, ?)",
                (task_id, "running", 100, None, None),
            )
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("UPDATE tasks SET current_run_id = ? WHERE id = ?", (run_id, task_id))
    finally:
        conn.close()
    archived_result = kanban_db.remove_board(board)

    kanban_db.write_board_metadata(board, name="Archived Empty Stub")
    empty_conn = kanban_db.connect(board=board)
    empty_conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")

    assert item["status"] == "blocked"
    assert item["status_detail"] == "repair_required"
    assert item["execution"]["worker_url"] is None
    assert item["execution"]["repair_required"] is True
    assert item["execution"]["archived_board_path"] == archived_result["new_path"]
    assert "Kanban database has no tasks or runs" in item["execution"]["repair_reason"]
    assert "Matching non-terminal board evidence is archived" in item["execution"]["repair_reason"]


def test_snapshot_terminal_archived_board_stays_historical(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-terminal-archive"
    meta = kanban_db.write_board_metadata(board, name="Terminal Archive")
    meta[command_center.DISCORD_WORKER_META_KEY] = {"goal_status": "done", "phase": "complete"}
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Finished worker", board=board)
        with conn:
            conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (200, task_id))
    finally:
        conn.close()
    kanban_db.remove_board(board)

    default_snapshot = command_center.build_command_center_snapshot()
    archived_snapshot = command_center.build_command_center_snapshot(include_archived=True)

    assert not any(item.get("execution", {}).get("board") == board for item in default_snapshot["work_items"])
    item = next(item for item in archived_snapshot["work_items"] if item.get("execution", {}).get("board") == board)
    assert item["status"] == "archived"
    assert item["execution"]["worker_url"] is None
    assert "repair_required" not in item["execution"]


def test_archived_board_metadata_cache_reuses_until_archive_root_mtime_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    archive_root = kanban_db.boards_root() / "_archived"
    archive_root.mkdir(parents=True)
    first = archive_root / "discord-cache-1000000000"
    first.mkdir()
    (first / "kanban.db").touch()
    (first / "board.json").write_text(json.dumps({"slug": "discord-cache", "name": "Cached"}), encoding="utf-8")
    ignored = archive_root / "ignored-entry"
    ignored.mkdir()
    malformed = archive_root / "discord-malformed-1000000001"
    malformed.mkdir()
    (malformed / "kanban.db").touch()
    (malformed / "board.json").write_text("{not json", encoding="utf-8")

    original_read = command_center._read_archived_board_metadata
    read_paths: list[str] = []

    def counted_read(path: Path):
        read_paths.append(path.name)
        return original_read(path)

    monkeypatch.setattr(command_center, "_read_archived_board_metadata", counted_read)

    first_result = command_center._archived_board_metadata()
    first_result[0]["name"] = "mutated caller copy"
    second_result = command_center._archived_board_metadata()

    assert read_paths == ["discord-cache-1000000000", "discord-malformed-1000000001", "ignored-entry"]
    assert [meta["slug"] for meta in second_result] == ["discord-cache", "discord-malformed"]
    assert second_result[0]["name"] == "Cached"
    assert second_result[1]["name"] == "discord-malformed"

    new_archived = archive_root / "discord-cache-1000000002"
    new_archived.mkdir()
    (new_archived / "kanban.db").touch()
    (new_archived / "board.json").write_text(json.dumps({"slug": "discord-cache", "archived_at": 1000000002}), encoding="utf-8")
    next_ns = archive_root.stat().st_mtime_ns + 1_000_000_000
    os.utime(archive_root, ns=(next_ns, next_ns))

    refreshed_by_slug = command_center._archived_boards_by_slug()

    assert read_paths == [
        "discord-cache-1000000000",
        "discord-malformed-1000000001",
        "ignored-entry",
        "discord-cache-1000000000",
        "discord-cache-1000000002",
        "discord-malformed-1000000001",
        "ignored-entry",
    ]
    assert [meta["archive_dir"] for meta in refreshed_by_slug["discord-cache"]] == [
        "discord-cache-1000000002",
        "discord-cache-1000000000",
    ]


def test_archived_board_metadata_cache_handles_missing_archive_root(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    archive_root = kanban_db.boards_root() / "_archived"
    archive_root.mkdir(parents=True)
    archived = archive_root / "discord-cache-1000000000"
    archived.mkdir()
    (archived / "kanban.db").touch()

    assert command_center._archived_board_metadata()
    for child in archive_root.iterdir():
        for file_path in child.iterdir():
            file_path.unlink()
        child.rmdir()
    archive_root.rmdir()

    assert command_center._archived_board_metadata() == []


def test_snapshot_corrupt_active_board_metadata_requires_repair_without_dead_worker_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-corrupt-metadata"
    board_dir = kanban_db.board_dir(board)
    board_dir.mkdir(parents=True, exist_ok=True)
    kanban_db.board_metadata_path(board).write_text("{not json", encoding="utf-8")
    kanban_db.init_db(board=board)

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")

    assert item["status"] == "blocked"
    assert item["status_detail"] == "repair_required"
    assert item["execution"]["worker_url"] is None
    assert item["execution"]["repair_required"] is True
    assert "metadata is missing or unreadable" in item["execution"]["repair_reason"]


def test_snapshot_blocked_board_repair_metadata_suppresses_existing_ticket(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "blocked-repair-board"
    kanban_db.write_board_metadata(board, name="Blocked Repair Board")
    conn = kanban_db.connect(board=board)
    try:
        blocked_task_id = kanban_db.create_task(
            conn,
            title="Blocked task",
            assignee="dev",
            initial_status="blocked",
        )
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")
    assert item["status"] == "blocked"
    assert item["execution"]["repair_action"].endswith(f"/{board}/repair")
    assert item["execution"]["repairable"] is True
    assert "repair_task_id" not in item["execution"]

    conn = kanban_db.connect(board=board)
    try:
        repair_task_id = kanban_db.create_task(
            conn,
            title="Repair blocked board",
            assignee="foreman",
            created_by="command-center-repair",
            priority=1_000_000,
            idempotency_key=command_center.command_center_repair_idempotency_key(board, blocked_task_id),
        )
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")
    assert item["execution"]["repairable"] is False
    assert item["status"] == "blocked"
    assert item["execution"]["repair_task_id"] == repair_task_id
    assert item["execution"]["repair_task_status"] == "ready"
    assert item["execution"]["repair_worker_url"].endswith(f"/workers/{board}/tickets/{repair_task_id}")

    conn = kanban_db.connect(board=board)
    try:
        kanban_db.complete_task(conn, repair_task_id, summary="old repair done")
        kanban_db.set_status_direct(conn, blocked_task_id, "blocked")
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")
    assert item["execution"]["repairable"] is True
    assert "repair_task_id" not in item["execution"]


def test_snapshot_blocked_board_with_blocked_repair_is_mega_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "mega-blocked-repair-board"
    kanban_db.write_board_metadata(board, name="Mega Blocked Repair Board")
    conn = kanban_db.connect(board=board)
    try:
        blocked_task_id = kanban_db.create_task(
            conn,
            title="Blocked task",
            assignee="dev",
            initial_status="blocked",
        )
        repair_task_id = kanban_db.create_task(
            conn,
            title="Repair blocked board",
            assignee="foreman",
            created_by="command-center-repair",
            priority=1_000_000,
            idempotency_key=command_center.command_center_repair_idempotency_key(board, blocked_task_id),
        )
        kanban_db.set_status_direct(conn, repair_task_id, "blocked")
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")

    assert item["status"] == "mega_blocked"
    assert item["status_detail"] == "repair_blocked"
    assert item["execution"]["repairable"] is False
    assert item["execution"]["repair_task_id"] == repair_task_id
    assert item["execution"]["repair_task_status"] == "blocked"
    assert item["execution"]["repair_blocked"] is True
    assert item["execution"]["repair_status_detail"] == "Active Command Center repair task is blocked."


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
    assert f"kanban:default:{ordinary_task_id}" not in item_ids
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


def test_completed_board_rollup_exposes_revert_and_archive_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-completed-revert-actions"
    kanban_db.write_board_metadata(board, name="Completed Revert Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Completed worker", tenant="hermes", board=board)
        with conn:
            conn.execute("UPDATE tasks SET status = 'done', completed_at = ? WHERE id = ?", (200, task_id))
    finally:
        conn.close()

    snapshot = command_center.build_command_center_snapshot(project="hermes")
    item = next(item for item in snapshot["work_items"] if item["id"] == f"kanban-board:{board}")
    summary = command_center_verification.summarize_completed_actions(snapshot, project="hermes")

    assert item["status"] == "shipped"
    assert item["decision"]["needed"] is False
    assert item["execution"]["undo_followup_action"].endswith(f"/boards/{board}/undo-followup")
    assert item["execution"]["archiveable"] is True
    assert summary["completed_count"] == 1
    assert summary["revertable_completed_ids"] == [f"kanban-board:{board}"]
    assert summary["missing_revert_count"] == 0


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


def test_snapshot_omits_individual_kanban_task_work_items(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    board = "discord-board-rollup-only"
    kanban_db.write_board_metadata(board, name="Board Rollup Only")

    default_conn = kanban_db.connect()
    try:
        default_task_id = kanban_db.create_task(default_conn, title="Default board task")
    finally:
        default_conn.close()

    board_conn = kanban_db.connect(board=board)
    try:
        board_task_id = kanban_db.create_task(board_conn, title="Named board task", board=board)
    finally:
        board_conn.close()

    snapshot = command_center.build_command_center_snapshot()
    item_ids = {item["id"] for item in snapshot["work_items"]}

    assert f"kanban-board:{board}" in item_ids
    assert f"kanban:{kanban_db.DEFAULT_BOARD}:{default_task_id}" not in item_ids
    assert f"kanban:{board}:{board_task_id}" not in item_ids
    assert not any(item_id.startswith("kanban:") for item_id in item_ids)
