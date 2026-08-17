import asyncio
import json
import logging
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock


from gateway.config import Platform
from gateway.run import (
    GatewayRunner,
    _format_gateway_flow_telemetry,
    _gateway_flow_route_type,
    _gateway_flow_telemetry_fields,
)
from hermes_cli import kanban_db as kb


DISCORD_EPOCH_SECONDS = 1_420_070_400.0


def _discord_snowflake_at(timestamp: float) -> str:
    return str(int((timestamp - DISCORD_EPOCH_SECONDS) * 1000) << 22)


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({"chat_id": chat_id, "text": text, "metadata": metadata or {}})


class TypingAdapter:
    def __init__(self):
        self.typing = []

    async def send_typing_once(self, chat_id, metadata=None):
        self.typing.append({"chat_id": chat_id, "metadata": metadata or {}})


class FeatureSummarySyncAdapter:
    def __init__(self):
        self.synced = []

    async def sync_kanban_feature_summary(self, target):
        self.synced.append(dict(target))
        from hermes_cli import discord_worker_boards as dwb

        dwb.mark_thread_status_synced(
            str(target.get("board") or ""),
            summary=True,
            metadata_path=target.get("metadata_path"),
        )
        return target.get("sync_key") or target.get("board")


class ReactionSyncAdapter:
    def __init__(self):
        self.synced = []

    async def sync_kanban_thread_reaction(self, target):
        self.synced.append(dict(target))
        return target.get("state")


class DiscordStatusSyncAdapter(FeatureSummarySyncAdapter, ReactionSyncAdapter):
    pass


class CompletionNoticeAdapter(DiscordStatusSyncAdapter):
    def __init__(self):
        super().__init__()
        self.completions = []

    async def send_kanban_completion_notice(self, target):
        self.completions.append(dict(target))
        from hermes_cli import discord_worker_boards as dwb

        dwb.mark_thread_completion_notice_sent(
            str(target.get("board") or ""),
            message_id="completion-message-1",
            metadata_path=target.get("metadata_path"),
        )
        return target.get("board")


class WorkerThreadAdapter(DiscordStatusSyncAdapter):
    def __init__(self):
        super().__init__()
        self.created_threads = []

    async def create_worker_task_thread(self, parent_chat_id, **kwargs):
        self.created_threads.append({"parent_chat_id": parent_chat_id, **kwargs})
        return {
            "thread_id": f"thread-{len(self.created_threads)}",
            "thread_name": kwargs.get("name") or "worker task",
            "message_id": f"message-{len(self.created_threads)}",
        }

    async def send_worker_task_embed(self, thread_chat_id, **kwargs):
        self.created_threads.append({"thread_chat_id": thread_chat_id, **kwargs})
        return {
            "thread_id": str(thread_chat_id),
            "thread_name": kwargs.get("title") or "worker task",
            "message_id": f"message-{len(self.created_threads)}",
        }


class DisconnectedAdapters(dict):
    """Expose a platform during collection, then simulate disconnect on get()."""

    def get(self, key, default=None):
        return None


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


async def _run_one_discord_typing_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._discord_kanban_typing_watcher(interval=1)


def _make_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._kanban_sub_fail_counts = {}
    return runner


def _make_discord_runner(adapter):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.DISCORD: adapter}
    return runner


def test_gateway_flow_telemetry_formats_ids_and_durations_only():
    source = SimpleNamespace(
        platform=Platform.DISCORD,
        chat_id="chat-1",
        thread_id="thread-2",
        user_id="user-3",
    )

    fields = _gateway_flow_telemetry_fields(
        route_type="discord_worker_goal",
        source=source,
        session_id="session-4",
        admission_ts=10.0,
        dispatch_start_ts=10.25,
        finished_ts=11.0,
        phase_timestamps={
            "promotion_handoff_ts": 8.0,
            "request_ts": 9.0,
            "adapter_dispatch_ts": 9.1,
            "intake_ts": 9.2,
            "admitted_ts": 9.3,
            "agent_handler_start_ts": 9.5,
            "model_start_ts": 9.8,
            "first_commentary_ts": 10.4,
        },
        phase_durations={
            "ledger_claim_ms": 7,
            "ledger_claim_calls": 1,
            "ledger_agent_running_ms": 11,
            "ledger_agent_running_calls": 1,
        },
    )
    line = _format_gateway_flow_telemetry(fields)

    assert _gateway_flow_route_type(SimpleNamespace(text="hello"), None) == "mainline"
    assert _gateway_flow_route_type(SimpleNamespace(text="/goal ship"), "goal") == "slash_goal"
    assert "route_type=discord_worker_goal" in line
    assert "platform=discord" in line
    assert "chat_id=chat-1" in line
    assert "thread_id=thread-2" in line
    assert "user_id=user-3" in line
    assert "session_id=session-4" in line
    assert "admission_to_dispatch_ms=250" in line
    assert "dispatch_to_finish_ms=750" in line
    assert "handoff_to_intake_ms=1199" in line
    assert "handoff_to_admission_ms=1300" in line
    assert "handoff_to_model_ms=1800" in line
    assert "handoff_to_first_commentary_ms=2400" in line
    assert "request_to_adapter_dispatch_ms=99" in line
    assert "adapter_dispatch_to_intake_ms=99" in line
    assert "intake_to_admission_ms=100" in line
    assert "admission_to_agent_handler_ms=199" in line
    assert "agent_handler_to_model_ms=300" in line
    assert "model_to_first_commentary_ms=599" in line
    assert "request_to_first_commentary_ms=1400" in line
    assert "ledger_claim_ms=7" in line
    assert "ledger_claim_calls=1" in line
    assert "ledger_agent_running_ms=11" in line
    assert "ledger_agent_running_calls=1" in line
    assert "ship" not in line


def test_kanban_dispatch_dirty_signal_wakes_sleep():
    async def run():
        runner = GatewayRunner.__new__(GatewayRunner)
        assert runner._signal_kanban_dispatcher_dirty() is True
        assert runner._kanban_dispatch_dirty_event.is_set()
        woke = await runner._sleep_until_kanban_dispatch_due(1.0)
        assert woke is True
        assert not runner._kanban_dispatch_dirty_event.is_set()

    asyncio.run(run())


def test_discord_worker_dirty_marker_wakes_dispatch_sleep(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))

    async def run():
        from hermes_cli import discord_worker_boards as dwb

        runner = GatewayRunner.__new__(GatewayRunner)
        runner._running = True
        runner._kanban_dispatch_dirty_marker_ns = dwb.dispatch_dirty_marker_mtime_ns()
        dwb.mark_dispatch_dirty(board="discord-1", reason="test")
        woke = await runner._sleep_until_kanban_dispatch_due(1.0)
        assert woke is True

    asyncio.run(run())


def test_discord_worker_task_threads_are_enabled_by_default_independent_of_foreman():
    from hermes_cli.config import DEFAULT_CONFIG

    worker_cfg = DEFAULT_CONFIG["kanban"]["discord_worker"]
    assert worker_cfg["task_threads"]["enabled"] is True
    assert worker_cfg["foreman"]["enabled"] is False


def test_discord_worker_dirty_marker_polls_below_old_one_second_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))

    async def run():
        from hermes_cli import discord_worker_boards as dwb

        runner = GatewayRunner.__new__(GatewayRunner)
        runner._running = True
        runner._kanban_dispatch_dirty_marker_ns = dwb.dispatch_dirty_marker_mtime_ns()

        async def mark_soon():
            await asyncio.sleep(0.05)
            dwb.mark_dispatch_dirty(board="discord-1", reason="test")

        marker_task = asyncio.create_task(mark_soon())
        started = asyncio.get_running_loop().time()
        woke = await runner._sleep_until_kanban_dispatch_due(5.0)
        elapsed = asyncio.get_running_loop().time() - started
        await marker_task

        assert woke is True
        assert elapsed < 0.7

    asyncio.run(run())


def test_kanban_notifier_skips_board_missing_notify_subs_without_log_storm(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(kb.time, "time", lambda: 8000)
    board = "notifier-missing-subs"
    db_path = kb.kanban_db_path(board)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    conn = kb._sqlite_connect(db_path)
    try:
        conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT)")
        conn.commit()
    finally:
        conn.close()
    kb._write_board_metadata_raw(board, {"slug": board, "name": "Missing Subs"})
    kb._INITIALIZED_PATHS.add(str(db_path.resolve()))

    runner = _make_runner(RecordingAdapter())

    with caplog.at_level(logging.ERROR):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    messages = [record.getMessage() for record in caplog.records]
    schema_events = [msg for msg in messages if "schema readiness failed for notifier" in msg]
    assert len(schema_events) == 1
    assert "missing required table(s): kanban_notify_subs" in schema_events[0]
    assert kb.corrupt_board_quarantine_state(board, now=8001)["skipped"] is True


def test_discord_worker_spawned_task_records_worker_thread_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://hermes.example.test")
    import hermes_cli.config as cfg

    monkeypatch.setattr(
        cfg,
        "load_config",
        lambda: {
            "kanban": {
                "discord_worker": {
                    "task_threads": {
                        "enabled": True,
                    }
                }
            }
        },
    )

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_state import read_codex_worker_state

    board = dwb.ensure_discord_thread_board(
        thread_id="123",
        chat_id="parent-123",
        guild_id="guild-1",
        parent_channel_id="dev-parent",
        initial_request="/goal Ship the dashboard",
        project_context={"project_name": "Hermes", "project_path": "/repo/hermes"},
    )
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Build dashboard filters",
            body="Add filter controls and verify them.",
            assignee=dwb.ROLE_DEV,
            tenant=board.slug,
            initial_status="running",
            board=board.slug,
        )
    finally:
        conn.close()

    adapter = WorkerThreadAdapter()
    runner = _make_discord_runner(adapter)
    result = SimpleNamespace(spawned=[(task_id, dwb.ROLE_DEV, "/tmp/hermes-worktree")])

    asyncio.run(runner._discord_worker_announce_spawned_tasks([(board.slug, result)]))
    asyncio.run(runner._discord_worker_announce_spawned_tasks([(board.slug, result)]))

    assert adapter.created_threads == []

    state = read_codex_worker_state(task_id, board=board.slug)
    assert state["worker_task_thread"]["thread_id"] == "123"
    assert state["worker_task_thread"]["message_id"] == ""
    assert "foreman_thread" not in state

    conn = kanban_db.connect(board=board.slug)
    try:
        subs = kanban_db.list_notify_subs(conn, task_id)
    finally:
        conn.close()
    assert subs == []


def test_discord_worker_reads_legacy_foreman_thread_but_rewrites_worker_key(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    import hermes_cli.config as cfg

    monkeypatch.setattr(
        cfg,
        "load_config",
        lambda: {"kanban": {"discord_worker": {"task_threads": {"enabled": True}}}},
    )

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_state import read_codex_worker_state, write_codex_worker_state

    board = dwb.ensure_discord_thread_board(
        thread_id="source-thread",
        chat_id="source-thread",
        guild_id="guild-1",
        parent_channel_id="dev-parent",
        initial_request="/goal Migrate state",
    )
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Legacy task",
            assignee=dwb.ROLE_DEV,
            tenant=board.slug,
            initial_status="running",
            board=board.slug,
        )
    finally:
        conn.close()
    write_codex_worker_state(
        task_id,
        board=board.slug,
        update={
            "foreman_thread": {
                "thread_id": "legacy-thread",
                "parent_channel_id": "legacy-parent",
                "message_id": "legacy-message",
                "thread_name": "Legacy worker task",
            }
        },
    )

    runner = _make_discord_runner(WorkerThreadAdapter())
    result = SimpleNamespace(spawned=[(task_id, dwb.ROLE_DEV, "")])

    asyncio.run(runner._discord_worker_announce_spawned_tasks([(board.slug, result)]))

    state = read_codex_worker_state(task_id, board=board.slug)
    assert state["worker_task_thread"]["thread_id"] == "legacy-thread"
    assert state["worker_task_thread"]["message_id"] == "legacy-message"
    assert "foreman_thread" not in state


def test_kanban_dispatcher_announces_spawned_tasks_with_worker_lifecycle_path(monkeypatch):
    import hermes_cli.config as cfg
    from hermes_cli import kanban_db
    from hermes_cli import discord_worker_dispatch
    from hermes_cli.discord_worker_roles import DISCORD_WORKER_META_KEY

    monkeypatch.setattr(
        cfg,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "dispatch_interval_seconds": 1,
                "auto_decompose": False,
                "discord_worker": {
                    "max_global_workers": 8,
                    "max_workers_per_board": 2,
                },
            }
        },
    )
    monkeypatch.setattr(kanban_db, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kanban_db, "list_boards", lambda include_archived=False: [{"slug": "discord-1"}])
    monkeypatch.setattr(
        kanban_db,
        "read_board_metadata",
        lambda board: {DISCORD_WORKER_META_KEY: {"kind": "discord_worker_board"}},
    )
    monkeypatch.setattr(discord_worker_dispatch, "running_role_count", lambda board: 0)
    result = SimpleNamespace(
        spawned=[("task-1", "dev", "/tmp/hermes-worktree")],
        reclaimed=0,
        crashed=[],
        timed_out=[],
        promoted=0,
        auto_blocked=[],
    )
    monkeypatch.setattr(
        discord_worker_dispatch,
        "dispatch_discord_worker_boards",
        lambda *args, **kwargs: [("discord-1", result)],
    )

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {}
    announced = []

    async def announce(results):
        announced.extend(results)

    async def stop_after_tick(interval):
        runner._running = False
        return False

    runner._discord_worker_announce_spawned_tasks = announce
    runner._sleep_until_kanban_dispatch_due = stop_after_tick

    asyncio.run(runner._kanban_dispatcher_watcher())

    assert announced == [("discord-1", result)]


def test_kanban_dispatcher_missing_interval_falls_back_to_five_seconds(monkeypatch):
    import hermes_cli.config as cfg
    from hermes_cli import kanban_db
    from hermes_cli import discord_worker_dispatch

    monkeypatch.setattr(
        cfg,
        "load_config",
        lambda: {
            "kanban": {
                "dispatch_in_gateway": True,
                "auto_decompose": False,
                "discord_worker": {
                    "max_global_workers": 8,
                    "max_workers_per_board": 2,
                },
            }
        },
    )
    monkeypatch.setattr(kanban_db, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kanban_db, "list_boards", lambda include_archived=False: [])
    monkeypatch.setattr(discord_worker_dispatch, "running_role_count", lambda board: 0)
    monkeypatch.setattr(
        discord_worker_dispatch,
        "dispatch_discord_worker_boards",
        lambda *args, **kwargs: [],
    )

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {}
    intervals = []

    async def stop_after_tick(interval):
        intervals.append(interval)
        runner._running = False
        return False

    runner._discord_worker_announce_spawned_tasks = AsyncMock()
    runner._sleep_until_kanban_dispatch_due = stop_after_tick

    asyncio.run(runner._kanban_dispatcher_watcher())

    assert intervals == [5.0]


def test_kanban_cli_embedded_dispatcher_guidance_uses_five_second_default(monkeypatch):
    import argparse
    import gateway.status as status
    from hermes_cli.kanban import build_parser, _check_dispatcher_presence

    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    kanban_parser = build_parser(subparsers)
    daemon_interval = kanban_parser.parse_args(["daemon", "--force"]).interval

    monkeypatch.setattr(status, "get_running_pid", lambda: None)
    ok, guidance = _check_dispatcher_presence()

    assert ok is False
    assert daemon_interval == 5.0
    assert "tick interval 5s by default" in guidance
    assert "60 seconds" not in guidance


def _create_completed_subscription(summary="done once"):
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="notify once", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        kb.complete_task(conn, tid, summary=summary)
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-1",
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_kanban_notifier_dedupes_board_slugs_pointing_to_same_db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    kb.init_db()
    kb.write_board_metadata("alias-a", name="Alias A")
    kb.write_board_metadata("alias-b", name="Alias B")

    tid = _create_completed_subscription()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 1
    assert "Kanban" in adapter.sent[0]["text"]
    assert tid in adapter.sent[0]["text"]


def test_kanban_notifier_skips_unchanged_corrupt_paused_board(tmp_path, monkeypatch, caplog):
    import logging

    db_path = tmp_path / "kanban.db"
    db_path.write_text("not sqlite", encoding="utf-8")
    fingerprint = kb._db_content_fingerprint(db_path)
    monkeypatch.setattr(kb, "list_boards", lambda include_archived=False: [{"slug": "bad-board"}])
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: db_path)
    monkeypatch.setattr(
        kb,
        "is_board_paused_for_corruption",
        lambda board=None: {
            "pause_reason": "kanban_db_corruption",
            "db_path": str(db_path),
            "fingerprint": fingerprint,
            "quarantine_path": str(db_path.with_suffix(".db.corrupt.test.bak")),
            "reason": "sqlite refused to open file",
        },
    )
    connect = Mock(side_effect=AssertionError("paused corrupt board should not open"))
    monkeypatch.setattr(kb, "connect", connect)

    runner = _make_runner(RecordingAdapter())

    with caplog.at_level(logging.DEBUG, logger="gateway.run"):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert connect.call_count == 0
    messages = [record.getMessage() for record in caplog.records]
    assert any("paused for unchanged DB corruption" in msg for msg in messages)
    assert not any("cannot open board" in msg for msg in messages)


def test_kanban_notifier_records_corrupt_open_once_then_skips(tmp_path, monkeypatch, caplog):
    import logging
    import sqlite3

    db_path = tmp_path / "kanban.db"
    db_path.write_text("not sqlite", encoding="utf-8")
    incidents = {}
    calls = {"connect": 0, "record": 0}

    monkeypatch.setattr(kb, "list_boards", lambda include_archived=False: [{"slug": "bad-board"}])
    monkeypatch.setattr(kb, "kanban_db_path", lambda board=None: db_path)
    monkeypatch.setattr(kb, "is_board_paused_for_corruption", lambda board=None: incidents.get(board))

    def connect(*args, **kwargs):
        calls["connect"] += 1
        raise sqlite3.DatabaseError("file is not a database")

    def record_incident(board, db_path_arg, reason, *, backup_path=None, fingerprint=None, error_class=None):
        calls["record"] += 1
        incident = {
            "pause_reason": "kanban_db_corruption",
            "db_path": str(db_path_arg),
            "fingerprint": fingerprint,
            "quarantine_path": str(backup_path) if backup_path is not None else None,
            "error_class": error_class,
            "reason": reason,
        }
        incidents[board] = incident
        return incident

    monkeypatch.setattr(kb, "connect", connect)
    monkeypatch.setattr(kb, "record_corrupt_board_incident", record_incident)

    runner = _make_runner(RecordingAdapter())

    with caplog.at_level(logging.DEBUG, logger="gateway.run"):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        runner._running = True
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    messages = [record.getMessage() for record in caplog.records]
    assert sum("kanban notifier: board bad-board database corruption incident" in msg for msg in messages) == 1
    alert = next(msg for msg in messages if "database corruption incident" in msg)
    assert "bad-board" in alert
    assert str(db_path) in alert
    assert "quarantine_path=" in alert
    assert "reason=" in alert
    assert "repair --board bad-board" in alert
    assert any("paused for unchanged DB corruption" in msg for msg in messages)
    assert not any("cannot open board" in msg for msg in messages)
    assert not any(record.exc_info for record in caplog.records)
    assert calls == {"connect": 1, "record": 1}
    assert incidents["bad-board"]["error_class"] == "DatabaseError"


def test_kanban_notifier_quarantines_invalid_header_open_then_skips(tmp_path, monkeypatch, caplog):
    import logging

    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    board = "bad-header-board"
    kb.create_board(board)
    db_path = kb.kanban_db_path(board)
    original = b"not sqlite\x00" * 32
    db_path.write_bytes(original)
    (db_path.parent / "kanban.db-wal").write_bytes(b"wal bytes")
    (db_path.parent / "kanban.db-shm").write_bytes(b"shm bytes")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    monkeypatch.setattr(kb, "list_boards", lambda include_archived=False: [{"slug": board}])

    runner = _make_runner(RecordingAdapter())

    with caplog.at_level(logging.DEBUG, logger="gateway.run"):
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))
        runner._running = True
        asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    messages = [record.getMessage() for record in caplog.records]
    assert sum("kanban notifier: board bad-header-board database corruption incident" in msg for msg in messages) == 1
    assert any("paused for unchanged DB corruption" in msg for msg in messages)
    assert not any("cannot open board" in msg for msg in messages)
    assert not any(record.exc_info for record in caplog.records)

    incident = kb.is_board_paused_for_corruption(board)
    assert incident is not None
    assert incident["quarantine_path"] is not None
    backup = Path(incident["quarantine_path"])
    assert backup.read_bytes() == original
    assert db_path.read_bytes() == original
    assert (backup.parent / (backup.name + "-wal")).read_bytes() == b"wal bytes"
    assert (backup.parent / (backup.name + "-shm")).read_bytes() == b"shm bytes"
    assert list(db_path.parent.glob("kanban.db.corrupt.*.bak")) == [backup]


def test_discord_kanban_typing_watcher_pulses_running_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99001",
        goal="Show Discord typing while active",
        chat_id="parent-990",
    )
    conn = kb.connect(board=board.slug)
    try:
        tid = kb.create_task(
            conn,
            title="Active dev task",
            assignee=dwb.ROLE_DEV,
            tenant=board.slug,
        )
        kb.claim_task(conn, tid)
    finally:
        conn.close()

    adapter = TypingAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.typing == [
        {
            "chat_id": "parent-990",
            "metadata": {"thread_id": "99001"},
        }
    ]

    targets = dwb.thread_status_targets()
    assert [target["board"] for target in targets] == [board.slug]
    assert targets[0]["state"] == "running"


def test_discord_kanban_typing_watcher_pulses_when_source_task_is_running(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    conn = kb.connect()
    try:
        source_tid = kb.create_task(conn, title="Default-board source work", assignee="default")
        claimed = kb.claim_task(conn, source_tid)
        assert claimed is not None
    finally:
        conn.close()

    board = dwb.set_goal(
        thread_id="99030",
        goal="Worker board backed by a running source task",
        chat_id="parent-99030",
    )
    dwb._update_worker_meta(
        board.slug,
        {
            "source_board": kb.DEFAULT_BOARD,
            "source_task_id": source_tid,
        },
    )

    adapter = TypingAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.typing == [
        {
            "chat_id": "parent-99030",
            "metadata": {"thread_id": "99030"},
        }
    ]


def test_discord_kanban_typing_watcher_pulses_running_notify_thread(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="Dev intake work", assignee="default")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="discord",
            chat_id="parent-dev",
            thread_id="thread-dev",
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
    finally:
        conn.close()

    adapter = TypingAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.typing == [
        {
            "chat_id": "parent-dev",
            "metadata": {"thread_id": "thread-dev"},
        }
    ]


def test_discord_kanban_typing_watcher_pulses_old_manual_rerun_board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    old_message_id = _discord_snowflake_at(time.time() - (8 * 24 * 60 * 60))
    board = dwb.set_goal(
        thread_id="1519801883701543175",
        goal="Manual rerun should keep typing while active",
        chat_id="1519801883701543175",
        request_id="manual-rerun",
        board_slug="discord-1519801883701543175-m-1519918246990712904-manual-rerun",
    )
    worker = dict(kb.read_board_metadata(board.slug)[dwb.DISCORD_WORKER_META_KEY])
    worker["source_message_id"] = old_message_id
    dwb._update_worker_meta(board.slug, worker)

    conn = kb.connect(board=board.slug)
    try:
        tid = kb.create_task(
            conn,
            title="Manual rerun dev work",
            assignee=dwb.ROLE_DEV,
            tenant=board.slug,
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
    finally:
        conn.close()

    adapter = TypingAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.typing == [
        {
            "chat_id": "1519801883701543175",
            "metadata": {"thread_id": "1519801883701543175"},
        }
    ]


def test_discord_kanban_typing_watcher_continues_status_sync_after_typing_collection_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99031",
        goal="Keep status sync independent",
        chat_id="parent-99031",
    )

    monkeypatch.setattr(
        dwb,
        "running_discord_thread_typing_targets",
        lambda: (_ for _ in ()).throw(RuntimeError("bad board")),
    )
    monkeypatch.setattr(
        dwb,
        "thread_status_targets",
        lambda: [
            {
                "board": board.slug,
                "thread_id": "99031",
                "chat_id": "parent-99031",
                "state": "active",
                "reaction_state": "active",
            }
        ],
    )

    adapter = ReactionSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.synced == [
        {
            "board": board.slug,
            "thread_id": "99031",
            "chat_id": "parent-99031",
            "state": "active",
            "reaction_state": "active",
        }
    ]


def test_discord_kanban_typing_watcher_continues_typing_after_status_collection_error(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    monkeypatch.setattr(
        dwb,
        "running_discord_thread_typing_targets",
        lambda: [
            {
                "thread_id": "99032",
                "chat_id": "parent-99032",
            }
        ],
    )
    monkeypatch.setattr(
        dwb,
        "thread_status_targets",
        lambda: (_ for _ in ()).throw(RuntimeError("bad status board")),
    )

    adapter = TypingAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.typing == [
        {
            "chat_id": "parent-99032",
            "metadata": {"thread_id": "99032"},
        }
    ]


def test_discord_kanban_typing_watcher_default_refreshes_before_discord_expiry(monkeypatch):
    from hermes_cli import discord_worker_boards as dwb

    monkeypatch.setattr(dwb, "running_discord_thread_typing_targets", lambda: [])
    monkeypatch.setattr(dwb, "thread_status_targets", lambda: [])
    runner = _make_discord_runner(TypingAdapter())
    sleeps = []
    real_sleep = asyncio.sleep

    async def patched_sleep(delay):
        sleeps.append(delay)
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", patched_sleep)

    asyncio.run(runner._discord_kanban_typing_watcher())

    assert sleeps == [5.0]


def test_discord_kanban_typing_watcher_rate_limits_repeated_collection_errors(monkeypatch, caplog):
    from hermes_cli import discord_worker_boards as dwb

    monkeypatch.setattr(
        dwb,
        "running_discord_thread_typing_targets",
        lambda: (_ for _ in ()).throw(RuntimeError("same bad board")),
    )
    monkeypatch.setattr(dwb, "thread_status_targets", lambda: [])
    monkeypatch.setattr(time, "monotonic", Mock(return_value=100.0))

    adapter = TypingAdapter()
    runner = _make_discord_runner(adapter)
    real_sleep = asyncio.sleep
    sleeps = 0

    async def patched_sleep(delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", patched_sleep)

    with caplog.at_level(logging.ERROR, logger="gateway.run"):
        asyncio.run(runner._discord_kanban_typing_watcher(interval=1))

    exception_records = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and "target collection failed" in record.getMessage()
    ]
    assert len(exception_records) == 1


def test_discord_kanban_typing_watcher_logs_again_after_collection_recovery(monkeypatch, caplog):
    from hermes_cli import discord_worker_boards as dwb

    calls = 0

    def typing_targets():
        nonlocal calls
        calls += 1
        if calls in {1, 3}:
            raise RuntimeError("flaky board")
        return [
            {
                "thread_id": "99033",
                "chat_id": "parent-99033",
            }
        ]

    monkeypatch.setattr(dwb, "running_discord_thread_typing_targets", typing_targets)
    monkeypatch.setattr(dwb, "thread_status_targets", lambda: [])
    monkeypatch.setattr(time, "monotonic", Mock(return_value=100.0))

    real_sleep = asyncio.sleep
    sleeps = 0

    async def patched_sleep(delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps >= 3:
            runner._running = False
        await real_sleep(0)

    adapter = TypingAdapter()
    runner = _make_discord_runner(adapter)
    monkeypatch.setattr(asyncio, "sleep", patched_sleep)

    with caplog.at_level(logging.ERROR, logger="gateway.run"):
        asyncio.run(runner._discord_kanban_typing_watcher(interval=1))

    exception_records = [
        record
        for record in caplog.records
        if record.levelno >= logging.ERROR and "target collection failed" in record.getMessage()
    ]
    assert len(exception_records) == 2
    assert adapter.typing
    assert adapter.typing[-1] == {
        "chat_id": "parent-99033",
        "metadata": {"thread_id": "99033"},
    }


def test_discord_kanban_typing_watcher_syncs_feature_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99002",
        goal="Sync the feature summary card",
        chat_id="parent-991",
        guild_id="guild-991",
        parent_channel_id="parent-991",
    )

    adapter = FeatureSummarySyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 1
    target = adapter.synced[0]
    assert target["board"] == board.slug
    assert target["thread_id"] == "99002"
    assert target["guild_id"] == "guild-991"
    assert target["parent_channel_id"] == "parent-991"
    assert target["state"] == "active"
    assert target["fallback_title"] == "Sync the feature summary card"
    assert target["outcome"]
    assert target["sync_key"]


def test_discord_kanban_typing_watcher_suppresses_repeated_summary_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    dwb.set_goal(
        thread_id="99003",
        goal="Sync the feature summary card once",
        chat_id="parent-992",
    )

    adapter = FeatureSummarySyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))
    runner._running = True
    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 1


def test_discord_kanban_target_cache_key_includes_thread_identity():
    runner = GatewayRunner.__new__(GatewayRunner)

    first = runner._discord_kanban_target_cache_key(
        {
            "board": "discord-99002",
            "thread_id": "99002",
            "guild_id": "guild-1",
            "parent_channel_id": "parent-1",
        }
    )
    second = runner._discord_kanban_target_cache_key(
        {
            "board": "discord-99002",
            "thread_id": "99002",
            "guild_id": "guild-2",
            "parent_channel_id": "parent-1",
        }
    )

    assert first != second


def test_discord_kanban_typing_watcher_skips_completed_status_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99007",
        goal="Do not touch completed Discord thread",
        chat_id="parent-996",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(board.slug, {"goal_status": "done", "phase": "complete"})
    dwb.mark_thread_status_synced(board.slug, reaction=True, summary=True)

    adapter = DiscordStatusSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.synced == []


def test_discord_completed_worker_board_reaction_stays_done_with_stale_active_source_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    thread_id = _discord_snowflake_at(time.time())
    board = dwb.set_goal(
        thread_id=thread_id,
        goal="Quarantine and self-heal Kanban SQLite corruption instead of log-storming",
        chat_id=f"parent-{thread_id}",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    source_conn = kb.connect()
    try:
        source_task_id = kb.create_task(source_conn, title="Source task still active")
        with source_conn:
            source_conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (source_task_id,))
    finally:
        source_conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "source_board": kb.DEFAULT_BOARD,
            "source_task_id": source_task_id,
        },
    )

    assert dwb.board_thread_state(board.slug) == "done"
    assert dwb.board_thread_reaction_state(board.slug) == "done"


def test_discord_completed_worker_board_reaction_ignores_stale_active_source_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    source_conn = kb.connect()
    try:
        source_task_id = kb.create_task(source_conn, title="Source task still active")
        with source_conn:
            source_conn.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (source_task_id,))
    finally:
        source_conn.close()

    thread_id = _discord_snowflake_at(time.time())
    board = dwb.set_goal(
        thread_id=thread_id,
        goal=f"Completed worker with stale source\n- Board: {kb.DEFAULT_BOARD}\n- Task: {source_task_id}",
        chat_id=f"parent-{thread_id}",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
        },
    )

    target = next(target for target in dwb.thread_status_targets() if target["board"] == board.slug)
    assert target["state"] == "done"
    assert target.get("reaction_state", target["state"]) == "done"


def test_discord_status_targets_keep_source_thread_hammer_during_active_foreman(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    source = dwb.set_goal(
        thread_id="99020",
        goal="Original worker request",
        chat_id="parent-99020",
    )
    conn = kb.connect(board=source.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(source.slug, {"goal_status": "done", "phase": "complete"})

    foreman = dwb.set_goal(
        thread_id="99021",
        goal=f"/goal Foreman escalation: resolve worker issue\n- Board: {source.slug}",
        chat_id="parent-99021",
    )

    targets = dwb.thread_status_targets()
    by_board = {target["board"]: target for target in targets}

    assert by_board[source.slug]["state"] == "done"
    assert by_board[source.slug]["reaction_state"] == "foreman"
    assert by_board[foreman.slug]["hide_source_links"] is True


def test_discord_status_targets_keep_source_thread_hammer_during_master_foreman_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli.discord_worker_foreman import ForemanIssue, create_foreman_master_task

    source = dwb.set_goal(
        thread_id="99022",
        goal="Original worker request",
        chat_id="parent-99022",
    )
    conn = kb.connect(board=source.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(source.slug, {"goal_status": "done", "phase": "complete"})

    create_foreman_master_task(
        ForemanIssue(
            kind="worker_errored",
            board=source.slug,
            task_id="source-task",
            severity="error",
            title="Worker execution failed",
            evidence={"thread_id": "99022", "task_status": "blocked"},
        ),
        master_board="default",
        assignee="default",
    )

    targets = dwb.thread_status_targets()
    by_board = {target["board"]: target for target in targets}

    assert by_board[source.slug]["state"] == "done"
    assert by_board[source.slug]["reaction_state"] == "foreman"


def test_discord_terminal_status_target_syncs_only_when_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99017",
        goal="Sync final emoji once",
        chat_id="parent-99017",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
        },
    )

    targets = dwb.thread_status_targets()
    assert [target["board"] for target in targets] == [board.slug]
    assert targets[0]["state"] == "done"

    dwb.mark_thread_status_synced(board.slug, reaction=True)
    assert [target["board"] for target in dwb.thread_status_targets()] == [board.slug]

    dwb.mark_thread_status_synced(board.slug, summary=True)
    targets = dwb.thread_status_targets()
    assert [target["board"] for target in targets] == [board.slug]
    assert targets[0]["terminal_completion_message_pending"] is True

    dwb.mark_thread_completion_notice_sent(board.slug, message_id="completion-message-1")
    assert dwb.thread_status_targets() == []


def test_discord_terminal_preview_refresh_requeues_summary_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99023",
        goal="Refresh published PR preview status",
        chat_id="parent-99023",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "pr_url": "https://github.com/acme/hermes/pull/185",
            "pr_checks_status": "pending",
            "pr_checks_total": 2,
        },
    )
    dwb.mark_thread_status_synced(board.slug, reaction=True, summary=True)
    targets = dwb.thread_status_targets()
    assert [target["board"] for target in targets] == [board.slug]
    assert targets[0]["terminal_completion_message_pending"] is True

    dwb.mark_thread_completion_notice_sent(board.slug, message_id="completion-message-1")
    assert dwb.thread_status_targets() == []

    dwb._update_worker_meta(
        board.slug,
        {
            "pr_checks_status": "passed",
            "pr_state": "OPEN",
            "preview_url": "https://hermes-pr-185.vercel.app",
            "preview_status": "ready",
        },
    )

    targets = dwb.thread_status_targets()
    assert [target["board"] for target in targets] == [board.slug]
    assert targets[0]["state"] == "done"
    assert targets[0]["terminal_summary_sync_pending"] is True
    assert targets[0]["terminal_reaction_sync_pending"] is False

    worker = kb.read_board_metadata(board.slug)["discord_worker"]
    assert worker["terminal_summary_sync_pending"] is True
    assert worker["board_summary"]["pr"]["checks_status"] == "passed"
    assert worker["board_summary"]["pr"]["state"] == "OPEN"
    assert worker["board_summary"]["preview"]["url"] == "https://hermes-pr-185.vercel.app"
    assert worker["board_summary"]["deployment_status"] == "ready"


def test_discord_stale_completion_notice_flag_keeps_terminal_target_until_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99018",
        goal="Clear a stale completion notice flag",
        chat_id="parent-99018",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
            "terminal_completion_message_pending": True,
        },
    )

    target = dwb.thread_status_targets()[0]
    assert target["terminal_completion_message_pending"] is True

    dwb.mark_thread_status_synced(board.slug, reaction=True, summary=True)
    target = dwb.thread_status_targets()[0]
    assert target["terminal_completion_message_pending"] is True

    dwb.mark_thread_status_synced(board.slug, completion_message=True)
    assert dwb.thread_status_targets() == []
    worker = kb.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_completion_message_pending" not in worker
    assert "terminal_completion_message_sent_at" not in worker


def test_discord_kanban_typing_watcher_keeps_completion_notice_pending_without_sender(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99019",
        goal="Keep completed goal notice pending until Discord can send it",
        chat_id="parent-99019",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
            "terminal_completion_message_pending": True,
        },
    )

    adapter = DiscordStatusSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    worker = kb.read_board_metadata(board.slug)["discord_worker"]
    assert worker["terminal_completion_message_pending"] is True
    assert "terminal_completion_message_sent_at" not in worker


def test_discord_kanban_typing_watcher_sends_summary_fallback_completion_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99020",
        goal="Announce completed goal",
        chat_id="parent-99020",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
            "terminal_completion_message_pending": True,
        },
    )

    adapter = CompletionNoticeAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.completions) == 1
    assert adapter.completions[0]["board"] == board.slug
    assert adapter.completions[0]["board_summary"]["final_response"]["text"] == ""
    assert adapter.completions[0]["board_summary"]["task_counts"]["total"] == 1
    worker = kb.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_completion_message_pending" not in worker
    assert worker["terminal_completion_message_id"] == "completion-message-1"
    assert isinstance(worker["terminal_completion_message_sent_at"], int)


def test_discord_kanban_typing_watcher_prefers_recorded_final_response_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99020-final",
        goal="Announce completed goal with final response",
        chat_id="parent-99020-final",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        kb.complete_task(conn, task.id, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
            "terminal_completion_message_pending": True,
        },
    )
    dwb.record_final_discord_response(
        board.slug,
        final_response="✅ Done. Completed goal with verified final response.",
        session_id="session-99020",
    )

    adapter = CompletionNoticeAdapter()
    runner = _make_discord_runner(adapter)
    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.completions) == 1
    assert adapter.completions[0]["board"] == board.slug
    assert adapter.completions[0]["board_summary"]["final_response"]["text"] == "✅ Done. Completed goal with verified final response."
    worker = kb.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_completion_message_pending" not in worker
    assert worker["terminal_completion_message_id"] == "completion-message-1"
    assert isinstance(worker["terminal_completion_message_sent_at"], int)


def test_record_final_discord_response_rearms_done_board_completion_notice(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99023",
        goal="Announce completed goal after final response arrives late",
        chat_id="parent-99023",
    )
    dwb._update_worker_meta(board.slug, {"goal_status": "done", "phase": "complete"})
    dwb.mark_thread_status_synced(board.slug, completion_message=True)

    worker = kb.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_completion_message_pending" not in worker

    dwb.record_final_discord_response(
        board.slug,
        final_response="Done after the ledger produced the final Discord response.",
        session_id="session-99023",
    )

    adapter = CompletionNoticeAdapter()
    runner = _make_discord_runner(adapter)
    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.completions) == 1
    assert adapter.completions[0]["board_summary"]["final_response"]["text"] == "Done after the ledger produced the final Discord response."
    worker = kb.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_completion_message_pending" not in worker
    assert worker["terminal_completion_message_id"] == "completion-message-1"


def test_archived_pending_terminal_board_is_completion_notice_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    thread_id = str(int((time.time() + 1_000 - 1_420_070_400) * 1000) << 22)
    board = dwb.set_goal(
        thread_id=thread_id,
        goal="Archived board still owes final Discord response",
        chat_id="parent-99024",
    )
    dwb._update_worker_meta(board.slug, {"goal_status": "done", "phase": "complete"})
    dwb.record_final_discord_response(board.slug, final_response="Archived board completed.")
    kb.remove_board(board.slug, archive=True)

    targets = dwb.thread_status_targets()

    assert len(targets) == 1
    assert targets[0]["board"] == board.slug
    assert targets[0]["archived"] is True
    assert targets[0]["metadata_path"].endswith("/board.json")
    assert targets[0]["terminal_completion_message_pending"] is True
    assert targets[0]["board_summary"]["final_response"]["text"] == "Archived board completed."

    dwb.mark_thread_completion_notice_sent(
        board.slug,
        message_id="archived-completion-message",
        metadata_path=targets[0]["metadata_path"],
    )

    assert dwb.thread_status_targets() == []
    archived_meta = json.loads(Path(targets[0]["metadata_path"]).read_text(encoding="utf-8"))
    archived_worker = archived_meta["discord_worker"]
    assert "terminal_completion_message_pending" not in archived_worker
    assert archived_worker["terminal_completion_message_id"] == "archived-completion-message"


def test_discord_worker_new_goal_clears_prior_completion_notice_proof(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99022",
        goal="First completed goal",
        chat_id="parent-99022",
    )
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_completion_message_pending": True,
            "terminal_completion_message_sent_at": 123,
            "terminal_completion_message_id": "old-completion-message",
        },
    )

    board = dwb.set_goal(
        thread_id="99022",
        goal="Second goal should not inherit completion notice proof",
        chat_id="parent-99022",
    )

    worker = kb.read_board_metadata(board.slug)["discord_worker"]
    assert worker["goal_status"] == "active"
    assert "terminal_completion_message_pending" not in worker
    assert "terminal_completion_message_sent_at" not in worker
    assert "terminal_completion_message_id" not in worker


def test_discord_thread_status_targets_mark_foreman_generated_completion(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.start_direct_goal(
        thread_id="99021",
        goal="Foreman escalation: resolve a Discord worker issue.",
        chat_id="parent-99021",
        board_slug="foreman-success-target",
    )
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
            "terminal_completion_message_pending": True,
        },
    )

    target = dwb.thread_status_targets()[0]

    assert target["board"] == board.slug
    assert target["state"] == "done"
    assert target["foreman_generated"] is True
    assert target["hide_source_links"] is True
    assert target["terminal_completion_message_pending"] is True


def test_start_direct_goal_marks_initial_summary_sync_without_terminal_reaction(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="99023",
        goal="Ship direct kickoff visibility",
        chat_id="parent-99023",
    )

    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert worker["goal_status"] == "active"
    assert worker["phase"] == "dev"
    assert worker["terminal_summary_sync_pending"] is True
    assert "terminal_reaction_sync_pending" not in worker


def test_discord_kanban_typing_watcher_forces_pending_summary_when_cache_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="99024",
        goal="Force direct kickoff summary sync",
        chat_id="parent-99024",
    )
    target = dwb.thread_status_targets()[0]
    assert target["terminal_summary_sync_pending"] is True

    adapter = FeatureSummarySyncAdapter()
    runner = _make_discord_runner(adapter)
    runner._discord_kanban_summary_states = {
        runner._discord_kanban_target_cache_key(target): runner._discord_kanban_summary_sync_key(target)
    }

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 1
    assert adapter.synced[0]["board"] == board.slug
    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_summary_sync_pending" not in worker


def test_discord_kanban_typing_watcher_reuses_normal_active_reaction_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.start_direct_goal(
        thread_id="99025",
        goal="Avoid active reaction spin",
        chat_id="parent-99025",
    )
    target = dwb.thread_status_targets()[0]
    assert target.get("reaction_state", target["state"]) == "active"
    assert target["terminal_reaction_sync_pending"] is False

    adapter = ReactionSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))
    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert [item["board"] for item in adapter.synced] == [board.slug]


def test_discord_kanban_typing_watcher_syncs_non_terminal_blocked_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99008",
        goal="Sync blocked Discord thread",
        chat_id="parent-997",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        assert kb.block_task(conn, task.id, reason="waiting for input") is True
    finally:
        conn.close()

    target = dwb.thread_status_targets()[0]
    assert target["board"] == board.slug
    assert target["state"] == "blocked"
    assert target.get("reaction_state", target["state"]) == "blocked"

    adapter = DiscordStatusSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 2
    assert adapter.synced[0]["board"] == board.slug
    assert adapter.synced[0]["state"] == "blocked"
    assert adapter.synced[0].get("reaction_state", adapter.synced[0]["state"]) == "blocked"
    assert adapter.synced[1]["board"] == board.slug
    assert adapter.synced[1]["state"] == "blocked"
    assert adapter.synced[1].get("reaction_state", adapter.synced[1]["state"]) == "blocked"


def test_discord_kanban_typing_watcher_syncs_spawn_failure_as_blocked_status(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99009",
        goal="Sync failed worker as blocked Discord thread",
        chat_id="parent-998",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        claimed = kb.claim_task(conn, task.id)
        assert claimed is not None
        blocked = kb._record_spawn_failure(
            conn,
            task.id,
            "worker failed before start",
            failure_limit=1,
        )
        assert blocked is True
    finally:
        conn.close()

    assert dwb.board_thread_state(board.slug) == "blocked"
    assert dwb.board_thread_reaction_state(board.slug) == "blocked"
    target = dwb.thread_status_targets()[0]
    assert target["board"] == board.slug
    assert target["state"] == "blocked"
    assert target.get("reaction_state", target["state"]) == "blocked"

    adapter = DiscordStatusSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 2
    assert adapter.synced[0]["board"] == board.slug
    assert adapter.synced[0]["state"] == "blocked"
    assert adapter.synced[0].get("reaction_state", adapter.synced[0]["state"]) == "blocked"
    assert adapter.synced[1]["board"] == board.slug
    assert adapter.synced[1]["state"] == "blocked"
    assert adapter.synced[1].get("reaction_state", adapter.synced[1]["state"]) == "blocked"


def test_discord_worker_status_sync_is_inference_free(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from agent import auxiliary_client
    from hermes_cli import discord_worker_boards as dwb

    call_llm = Mock(side_effect=AssertionError("status sync must not call LLM"))
    async_call_llm = AsyncMock(side_effect=AssertionError("status sync must not call async LLM"))
    monkeypatch.setattr(auxiliary_client, "call_llm", call_llm)
    monkeypatch.setattr(auxiliary_client, "async_call_llm", async_call_llm)

    board = dwb.set_goal(
        thread_id="99010",
        goal="Check active board without inference",
        chat_id="parent-999",
    )

    targets = dwb.thread_status_targets()
    assert [target["board"] for target in targets] == [board.slug]

    adapter = DiscordStatusSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 2
    call_llm.assert_not_called()
    async_call_llm.assert_not_called()


def test_discord_kanban_typing_watcher_resyncs_stale_reaction_state(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setattr("gateway.run.time.monotonic", lambda: 100.0)
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99004",
        goal="Repair stale Discord reaction",
        chat_id="parent-993",
    )

    adapter = ReactionSyncAdapter()
    runner = _make_discord_runner(adapter)
    runner._discord_kanban_reaction_states = {
        board.slug: {"state": "active", "synced_at": 10.0},
    }

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 1
    assert adapter.synced[0]["board"] == board.slug
    assert adapter.synced[0]["thread_id"] == "99004"
    cache_key = runner._discord_kanban_target_cache_key(adapter.synced[0])
    assert runner._discord_kanban_reaction_states[cache_key] == {
        "state": "active",
        "synced_at": 100.0,
    }
    assert board.slug not in runner._discord_kanban_reaction_states


def test_discord_kanban_typing_watcher_upgrades_state_only_reaction_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setattr("gateway.run.time.monotonic", lambda: 200.0)
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99006",
        goal="Repair same-state Discord reaction drift",
        chat_id="parent-995",
    )

    adapter = ReactionSyncAdapter()
    runner = _make_discord_runner(adapter)
    runner._discord_kanban_reaction_states = {board.slug: "active"}

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 1
    cache_key = runner._discord_kanban_target_cache_key(adapter.synced[0])
    assert runner._discord_kanban_reaction_states[cache_key] == {
        "state": "active",
        "synced_at": 200.0,
    }
    assert board.slug not in runner._discord_kanban_reaction_states


def test_discord_kanban_typing_watcher_keeps_recent_reaction_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setattr("gateway.run.time.monotonic", lambda: 100.0)
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99005",
        goal="Avoid excessive Discord reaction sync",
        chat_id="parent-994",
    )

    adapter = ReactionSyncAdapter()
    runner = _make_discord_runner(adapter)
    runner._discord_kanban_reaction_states = {
        board.slug: {"state": "active", "synced_at": 90.0},
    }

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.synced == []
    cache_key = runner._discord_kanban_target_cache_key(
        {
            "board": board.slug,
            "thread_id": "99005",
            "chat_id": "parent-994",
            "state": "active",
        }
    )
    assert runner._discord_kanban_reaction_states[cache_key] == {
        "state": "active",
        "synced_at": 90.0,
    }
    assert board.slug not in runner._discord_kanban_reaction_states


def test_discord_kanban_typing_watcher_ignores_legacy_cache_for_message_target(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.setattr("gateway.run.time.monotonic", lambda: 100.0)
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99007",
        goal="Repair stale Discord reaction on the actual OP",
        chat_id="parent-996",
    )
    dwb.set_feature_summary_handle(board.slug, message_id="222", source_message_id="111")

    adapter = ReactionSyncAdapter()
    runner = _make_discord_runner(adapter)
    runner._discord_kanban_reaction_states = {
        board.slug: {"state": "active", "synced_at": 90.0},
    }

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 1
    assert adapter.synced[0]["source_message_id"] == "111"
    assert adapter.synced[0]["message_id"] == "222"
    cache_key = runner._discord_kanban_target_cache_key(adapter.synced[0])
    assert runner._discord_kanban_reaction_states[cache_key] == {
        "state": "active",
        "synced_at": 100.0,
    }
    assert board.slug not in runner._discord_kanban_reaction_states


def test_discord_kanban_reaction_cache_key_includes_message_identity():
    runner = GatewayRunner.__new__(GatewayRunner)

    source_key = runner._discord_kanban_target_cache_key(
        {
            "board": "discord-1",
            "thread_id": "1",
            "source_message_id": "10",
            "message_id": "20",
        }
    )
    other_source_key = runner._discord_kanban_target_cache_key(
        {
            "board": "discord-1",
            "thread_id": "1",
            "source_message_id": "11",
            "message_id": "20",
        }
    )
    other_summary_key = runner._discord_kanban_target_cache_key(
        {
            "board": "discord-1",
            "thread_id": "1",
            "source_message_id": "10",
            "message_id": "21",
        }
    )

    assert source_key != other_source_key
    assert source_key != other_summary_key


def test_kanban_notifier_claim_prevents_second_watcher_send(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    kb.init_db()
    tid = _create_completed_subscription()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = DisconnectedAdapters({Platform.TELEGRAM: RecordingAdapter()})
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_kanban_db_path_is_test_isolated_from_real_home():
    hermes_home = Path(kb.kanban_home())
    production_db = Path.home() / ".hermes" / "kanban.db"
    assert kb.kanban_db_path().resolve() != production_db.resolve()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="x", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
    finally:
        conn.close()

    assert kb.kanban_db_path().resolve().is_relative_to(hermes_home.resolve())
    assert kb.kanban_db_path().resolve() != production_db.resolve()


class FailingAdapter:
    """Adapter whose send() always raises, simulating a transient send error."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        raise RuntimeError("simulated send failure")


def test_kanban_notifier_rewinds_claim_on_send_exception(tmp_path, monkeypatch):
    """A raising adapter rewinds the claim so the next tick can retry.

    This is the second rewind path (distinct from the adapter-disconnect path
    in test_kanban_notifier_rewinds_claim_if_adapter_disconnects). Here the
    adapter is connected and the send call actually fires; the claim must
    still rewind so the event isn't lost when send() raises mid-tick.
    """
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    kb.init_db()
    tid = _create_completed_subscription()

    adapter = FailingAdapter()
    runner = _make_runner(adapter)

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # Send was attempted (so we exercised the failure path, not just the
    # disconnect path) and the claim was rewound — the unseen-events query
    # still returns the event for retry on the next tick.
    assert adapter.attempts >= 1, "send should have been attempted at least once"
    assert [ev.kind for ev in _unseen_terminal_events(tid)] == ["completed"]


def test_notifier_redelivers_same_kind_on_dispatch_cycle(tmp_path, monkeypatch):
    """A retry cycle (crashed → reclaimed → crashed) notifies the user twice.

    Before #21398 the notifier auto-unsubscribed on any terminal event kind
    (gave_up / crashed / timed_out), so the second crash in a respawn cycle
    silently dropped — the subscription was already gone. This test pins the
    new contract: subscription survives non-final terminal events; the
    cursor handles dedup.

    Two crashes ten seconds apart on the same task — both should land on
    the adapter.
    """
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="cycle test", assignee="worker")
        kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat-1")
        # First crash — fired by the dispatcher when the worker PID dies.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    adapter = RecordingAdapter()
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # First crash delivered.
    assert len(adapter.sent) == 1
    assert "crashed" in adapter.sent[0]["text"].lower()

    # Subscription survives — the cursor advanced past event #1, but the
    # row is still there.
    conn = kb.connect()
    try:
        subs = kb.list_notify_subs(conn, tid)
        assert len(subs) == 1, (
            "Subscription must survive a crashed event so a respawn-cycle "
            "second crash also notifies the user (issue #21398)."
        )

        # Second crash — same task, same dispatcher (or a respawn). Append
        # another event to simulate the dispatcher firing crashed a second
        # time during retry.
        kb._append_event(conn, tid, kind="crashed")
    finally:
        conn.close()

    # New tick: the second event has a fresh id past the cursor advance,
    # so it gets claimed and delivered.
    runner = _make_runner(adapter)
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(adapter.sent) == 2, (
        f"Second crashed event should also notify; got {len(adapter.sent)} "
        f"deliveries (texts: {[d['text'] for d in adapter.sent]})"
    )
    assert "crashed" in adapter.sent[1]["text"].lower()


def test_notifier_owning_profile_adapter_no_default_fallback(tmp_path, monkeypatch):
    """A subscription owned by a secondary profile whose profile-adapter
    registry entry EXISTS but lacks this platform must NOT fall back to the
    default profile's same-platform adapter — the notifier must route through
    the shared ``_authorization_adapter`` chokepoint, which forbids that
    fallback (gateway/authz_mixin.py). Delivering via the default profile's bot
    is the exact cross-profile mis-delivery this whole change exists to fix
    (`[230002] Bot can NOT be out of the chat`).

    Mutation check: reverting kanban_watchers.py's adapter selection to the old
    inline ``if adapter is None: adapter = self.adapters.get(plat)`` fallback
    makes this test FAIL (the default adapter receives the delivery).
    """
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by beta", assignee="worker")
        # Subscription is owned by profile "beta".
        kb.add_notify_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat-beta",
            notifier_profile="beta",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    default_adapter = RecordingAdapter()
    other_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    # Default profile has a telegram adapter …
    runner.adapters = {Platform.TELEGRAM: default_adapter}
    # … and profile "beta" HAS a non-empty registry entry (so it passes the
    # notifier's upstream skip-filter, which only skips owning profiles with NO
    # adapter at all), but that entry does NOT contain a telegram adapter — beta
    # connected a different platform (discord). The telegram sub owned by beta
    # must therefore resolve to NO adapter, not silently borrow the default
    # profile's telegram bot.
    runner._profile_adapters = {"beta": {Platform.DISCORD: other_adapter}}
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    # The default profile's adapter must never receive beta's notification.
    assert default_adapter.sent == [], (
        "Owning-profile subscription must not fall back to the default "
        f"profile's adapter; got {default_adapter.sent!r}"
    )
    assert other_adapter.sent == [], (
        f"beta's discord adapter must not receive a telegram sub; got {other_adapter.sent!r}"
    )
    # The claim is rewound (adapter resolved to None → treated as disconnected),
    # so the event is still unseen and will deliver once beta's adapter connects.
    assert [ev.kind for ev in _unseen_terminal_events_for(tid, "chat-beta")] == ["completed"]


def test_notifier_delivers_via_secondary_profile_adapter(tmp_path, monkeypatch):
    """A secondary-owned subscription must positively deliver via that bot.

    This covers the multiplex case where the primary profile has no adapter for
    the subscription's platform at all.  Merely preventing fallback is not
    enough: the notifier must discover and use the connected secondary adapter.
    """
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by beta", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-beta",
            notifier_profile="beta",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    secondary_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {}
    runner._profile_adapters = {
        "beta": {Platform.TELEGRAM: secondary_adapter},
    }
    runner._kanban_notifier_profile = "default"
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert len(secondary_adapter.sent) == 1
    assert secondary_adapter.sent[0]["chat_id"] == "chat-beta"
    assert f"Kanban {tid} done" in secondary_adapter.sent[0]["text"]
    assert _unseen_terminal_events_for(tid, "chat-beta") == []


def test_notifier_default_owner_does_not_use_secondary_gateway_adapter(
    tmp_path, monkeypatch
):
    """An explicit default-owned subscription cannot leak through a
    secondary profile gateway's ``self.adapters`` registry.

    ``_authorization_adapter(..., profile="default")`` normally resolves to
    ``self.adapters``. On a standalone secondary gateway, however, that map
    belongs to the secondary profile, so treating the default stamp as a
    normal adapter lookup sends through the wrong bot.
    """
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="owned by default", assignee="worker")
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id="chat-default",
            notifier_profile="default",
        )
        kb.complete_task(conn, tid, summary="done")
    finally:
        conn.close()

    secondary_adapter = RecordingAdapter()
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = {Platform.TELEGRAM: secondary_adapter}
    runner._profile_adapters = {}
    runner._kanban_notifier_profile = "beta"
    runner._kanban_sub_fail_counts = {}

    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert secondary_adapter.sent == []
    assert [
        ev.kind for ev in _unseen_terminal_events_for(tid, "chat-default")
    ] == ["completed"]


def _unseen_terminal_events_for(tid, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform="telegram",
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()
