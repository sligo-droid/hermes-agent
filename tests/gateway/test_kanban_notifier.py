import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_discord_kanban_typing_watcher_syncs_feature_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.set_goal(
        thread_id="99002",
        goal="Sync the feature summary card",
        chat_id="parent-991",
    )

    adapter = FeatureSummarySyncAdapter()
    runner = _make_discord_runner(adapter)

    asyncio.run(_run_one_discord_typing_tick(monkeypatch, runner))

    assert len(adapter.synced) == 1
    target = adapter.synced[0]
    assert target["board"] == board.slug
    assert target["thread_id"] == "99002"
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
