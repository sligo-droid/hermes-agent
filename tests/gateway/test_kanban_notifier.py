import asyncio
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

        dwb.mark_thread_status_synced(str(target.get("board") or ""), completion_message=True)
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
    db_path = tmp_path / "shared-kanban.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
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

    def record_incident(board, db_path_arg, reason, *, backup_path=None, fingerprint=None):
        calls["record"] += 1
        incident = {
            "pause_reason": "kanban_db_corruption",
            "db_path": str(db_path_arg),
            "fingerprint": fingerprint,
            "quarantine_path": str(backup_path) if backup_path is not None else None,
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
    assert dwb.thread_status_targets() == []


def test_discord_terminal_pr_refresh_requeues_summary_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99023",
        goal="Refresh merged PR status",
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
    assert dwb.thread_status_targets() == []

    dwb._update_worker_meta(
        board.slug,
        {
            "pr_checks_status": "passed",
            "pr_merge_state": "MERGED",
            "pr_state": "merged",
            "pr_merged_at": "2026-06-01T17:35:13Z",
            "pr_merge_commit": "abc123",
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
    assert worker["board_summary"]["pr"]["merge_state"] == "MERGED"
    assert worker["board_summary"]["pr"]["merge_commit"] == "abc123"


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


def test_discord_kanban_typing_watcher_clears_stale_completion_notice_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99019",
        goal="Suppress completed goal notice",
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
    assert "terminal_completion_message_pending" not in worker


def test_discord_kanban_typing_watcher_sends_completion_notice(tmp_path, monkeypatch):
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
    worker = kb.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_completion_message_pending" not in worker


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


def test_discord_kanban_typing_watcher_skips_blocked_status_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99008",
        goal="Do not touch blocked Discord thread",
        chat_id="parent-997",
    )
    conn = kb.connect(board=board.slug)
    try:
        task = kb.list_tasks(conn, include_archived=False)[0]
        assert kb.block_task(conn, task.id, reason="waiting for input") is True
    finally:
        conn.close()

    adapter = DiscordStatusSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.synced == []


def test_discord_kanban_typing_watcher_skips_errored_status_sync(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99009",
        goal="Do not touch errored Discord thread",
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

    adapter = DiscordStatusSyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert adapter.synced == []


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
    db_path = tmp_path / "single-owner.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    kb.init_db()

    tid = _create_completed_subscription()

    adapter1 = RecordingAdapter()
    adapter2 = RecordingAdapter()

    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter1)))
    asyncio.run(_run_one_notifier_tick(monkeypatch, _make_runner(adapter2)))

    assert len(adapter1.sent) == 1
    assert adapter2.sent == []


def test_kanban_notifier_rewinds_claim_if_adapter_disconnects(tmp_path, monkeypatch):
    db_path = tmp_path / "adapter-disconnect.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
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
    db_path = tmp_path / "send-failure.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
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
    db_path = tmp_path / "redeliver-cycle.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
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
