import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from gateway.work_ledger import GatewayWorkLedger


def _discord_event(message_id="m1", text="do the work"):
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        user_id="user-1",
        thread_id="thread-1",
        guild_id="guild-1",
        parent_chat_id="channel-1",
        message_id=message_id,
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id=message_id,
    )


def _make_busy_runner(ledger_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(ledger_path)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner._busy_input_mode = "queue"
    runner._is_user_authorized = lambda _source: True
    return runner


class _LedgerDrainAdapter(BasePlatformAdapter):
    async def connect(self):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content=None, **kwargs):
        return SendResult(success=True, message_id=f"sent-{chat_id}")

    async def get_chat_info(self, chat_id):
        return {}


def _mark_claim_stale(ledger, work_id):
    data = ledger._read()
    data["items"][work_id]["claim_pid"] = os.getpid() + 10_000_000
    ledger._write(data)


def test_ledger_deduplicates_discord_message_ids(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)

    first = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    second = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)

    assert first is not None
    assert second is not None
    assert first["id"] == second["id"]
    assert first["_existing"] is False
    assert second["_existing"] is True
    assert len(ledger.incomplete_items()) == 1


def test_ledger_strips_transient_summary_objects(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="m1")
    event.feature_summary = {
        "thread_id": "thread-1",
        "message_id": "summary-1",
        "kanban_board": {"slug": "discord-thread-1", "public_url": "https://example.test/board"},
        "_thread_obj": object(),
        "_message_obj": object(),
    }
    event.project_summary = {
        "channel_id": "channel-1",
        "_channel_obj": object(),
    }
    session_key = build_session_key(event.source)

    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)

    assert item is not None
    stored = ledger.get(item["id"])
    assert stored["feature_summary"] == {
        "thread_id": "thread-1",
        "message_id": "summary-1",
        "kanban_board": {"slug": "discord-thread-1", "public_url": "https://example.test/board"},
    }
    assert stored["project_summary"] == {"channel_id": "channel-1"}


def test_ledger_skips_completed_and_expires_stale_items(tmp_path):
    now = 1000.0
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: now)
    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=1)

    assert item is not None
    now = 1002.0
    assert ledger.incomplete_items() == []
    assert ledger.get(item["id"])["status"] == "expired"

    fresh = ledger.accept_event(_discord_event(message_id="m2"), session_key=session_key, freshness_seconds=60)
    assert fresh is not None
    ledger.mark_completed(fresh["id"], result_message_id="result-1")
    assert ledger.incomplete_items() == []
    assert ledger.get(fresh["id"])["result_message_id"] == "result-1"


def test_ledger_keeps_finished_delivery_phases_incomplete_until_completed(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)

    assert item is not None
    ledger.claim(item["id"])
    ledger.mark_agent_running(item["id"], session_id="session-1")
    ledger.mark_agent_done(
        item["id"],
        final_response="normal final answer",
        session_id="session-1",
        summary_status="Complete",
        feature_summary={"message_id": "summary-1", "_message_obj": object()},
    )
    assert ledger.get(item["id"])["status"] == "agent_done"
    assert ledger.get(item["id"])["feature_summary"] == {"message_id": "summary-1"}
    assert ledger.incomplete_items()[0]["final_response"] == "normal final answer"

    ledger.mark_response_delivered(item["id"], result_message_id="result-1")
    assert ledger.get(item["id"])["status"] == "response_delivered"
    assert ledger.incomplete_items()[0]["result_message_id"] == "result-1"

    ledger.mark_summary_updated(item["id"])
    assert ledger.get(item["id"])["status"] == "summary_updated"
    assert ledger.incomplete_items()[0]["status"] == "summary_updated"

    ledger.mark_completed(item["id"])
    assert ledger.incomplete_items() == []


def _set_ledger_status(ledger, work_id, status):
    data = ledger._read()
    data["items"][work_id]["status"] = status
    ledger._write(data)


@pytest.mark.parametrize(
    "status",
    [
        "agent_done",
        "response_delivered",
        "summary_updated",
        "completed",
        "failed",
        "blocked",
        "cancelled",
        "expired",
    ],
)
def test_duplicate_normal_accept_preserves_finished_and_terminal_statuses(tmp_path, status):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")

    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    _set_ledger_status(runner.work_ledger, item["id"], status)

    duplicate = runner._accept_discord_work_item(event, session_key)

    assert duplicate is not None
    assert runner.work_ledger.get(item["id"])["status"] == status


@pytest.mark.parametrize(
    "status",
    ["agent_done", "response_delivered", "summary_updated", "failed"],
)
def test_duplicate_drain_accept_preserves_finished_and_terminal_statuses(tmp_path, status):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")

    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    _set_ledger_status(runner.work_ledger, item["id"], status)

    duplicate = runner._record_discord_work_for_drain(event, session_key)

    assert duplicate is not None
    assert event.defer_work_completion is True
    assert runner.work_ledger.get(item["id"])["status"] == status


@pytest.mark.asyncio
async def test_startup_replays_only_incomplete_discord_work(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner.adapters = {Platform.DISCORD: type("Adapter", (), {})()}
    runner.adapters[Platform.DISCORD].handle_message = AsyncMock()
    runner._background_tasks = set()

    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    runner.work_ledger.claim(item["id"])
    # Simulate a previous gateway process.  An alive current PID would not be
    # replayed, because that would duplicate active work.
    _mark_claim_stale(runner.work_ledger, item["id"])

    scheduled = runner._schedule_incomplete_discord_work_items()
    await asyncio.sleep(0)

    assert scheduled == 1
    runner.adapters[Platform.DISCORD].handle_message.assert_awaited_once()
    replay = runner.adapters[Platform.DISCORD].handle_message.await_args.args[0]
    assert replay.work_replay is True
    assert replay.work_item_id == item["id"]
    assert replay.text == "do the work"


@pytest.mark.asyncio
async def test_startup_replays_busy_followup_accepted_before_drain(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    ledger_path = tmp_path / "work_ledger.json"
    runner = _make_busy_runner(ledger_path)
    adapter = SimpleNamespace(_pending_messages={}, _send_with_retry=AsyncMock())

    event = _discord_event(message_id="busy-1", text="queued before drain")
    session_key = build_session_key(event.source)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._running_agents[session_key] = MagicMock()

    handled = await runner._handle_active_session_busy_message(event, session_key)

    assert handled is True
    assert adapter._pending_messages[session_key] is event
    stored = runner.work_ledger.get(event.work_item_id)
    assert stored is not None
    assert stored["text"] == "queued before drain"
    assert stored["status"] == "claimed"
    _mark_claim_stale(runner.work_ledger, event.work_item_id)

    restarted = object.__new__(GatewayRunner)
    restarted.work_ledger = GatewayWorkLedger(ledger_path)
    replay_adapter = SimpleNamespace(handle_message=AsyncMock())
    restarted.adapters = {Platform.DISCORD: replay_adapter}
    restarted._background_tasks = set()

    scheduled = restarted._schedule_incomplete_discord_work_items()
    await asyncio.sleep(0)

    assert scheduled == 1
    replay_adapter.handle_message.assert_awaited_once()
    replay = replay_adapter.handle_message.await_args.args[0]
    assert replay.work_replay is True
    assert replay.work_item_id == event.work_item_id
    assert replay.text == "queued before drain"


@pytest.mark.asyncio
async def test_same_process_busy_followup_completion_is_not_replayed_after_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    ledger_path = tmp_path / "work_ledger.json"
    runner = _make_busy_runner(ledger_path)
    adapter = _LedgerDrainAdapter(PlatformConfig(enabled=True, token="token"), Platform.DISCORD)
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = SimpleNamespace(
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
        platforms={},
        quick_commands={},
    )
    runner.session_store = MagicMock()
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)

    initial_event = _discord_event(message_id="initial", text="first turn")
    busy_event = _discord_event(message_id="busy-1", text="queued while first turn is active")
    session_key = build_session_key(initial_event.source)
    processed_message_ids = []
    followup_finished = asyncio.Event()

    async def fake_handle_message_with_agent(event, source, _quick_key, _run_generation):
        processed_message_ids.append(event.message_id)
        if event.message_id == "initial":
            await adapter.handle_message(busy_event)

        work_id = getattr(event, "work_item_id", None)
        assert work_id
        runner.work_ledger.mark_agent_running(work_id, session_id="session-1")
        runner.work_ledger.mark_agent_done(
            work_id,
            final_response="follow-up done",
            session_id="session-1",
            summary_status="Complete",
        )
        runner.work_ledger.mark_response_delivered(work_id, result_message_id="result-1")
        runner.work_ledger.mark_summary_updated(work_id)
        runner.work_ledger.mark_completed(work_id)
        if event.message_id == "busy-1":
            followup_finished.set()
        return ""

    runner._handle_message_with_agent = fake_handle_message_with_agent
    adapter.set_message_handler(runner._handle_message)

    await adapter.handle_message(initial_event)
    await asyncio.wait_for(followup_finished.wait(), timeout=2)
    for _ in range(200):
        if session_key not in adapter._active_sessions:
            break
        await asyncio.sleep(0.01)

    assert processed_message_ids == ["initial", "busy-1"]
    assert session_key not in adapter._active_sessions
    stored = runner.work_ledger.get(busy_event.work_item_id)
    assert stored is not None
    assert stored["status"] == "completed"
    assert runner.work_ledger.incomplete_items() == []

    restarted = object.__new__(GatewayRunner)
    restarted.work_ledger = GatewayWorkLedger(ledger_path)
    replay_adapter = SimpleNamespace(handle_message=AsyncMock())
    restarted.adapters = {Platform.DISCORD: replay_adapter}
    restarted._background_tasks = set()

    scheduled = restarted._schedule_incomplete_discord_work_items()
    await asyncio.sleep(0)

    assert scheduled == 0
    replay_adapter.handle_message.assert_not_called()
    await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_duplicate_redelivery_during_startup_replay_is_not_requeued(tmp_path, monkeypatch):
    from gateway.run import _AGENT_PENDING_SENTINEL

    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    ledger_path = tmp_path / "work_ledger.json"
    ledger = GatewayWorkLedger(ledger_path)
    event = _discord_event(message_id="busy-1", text="queued before restart")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    ledger.claim(item["id"])
    _mark_claim_stale(ledger, item["id"])

    runner = _make_busy_runner(ledger_path)
    adapter = SimpleNamespace(
        _pending_messages={},
        _send_with_retry=AsyncMock(),
        handle_message=AsyncMock(),
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._background_tasks = set()

    scheduled = runner._schedule_incomplete_discord_work_items()
    runner._running_agents[session_key] = _AGENT_PENDING_SENTINEL
    duplicate = _discord_event(message_id="busy-1", text="queued before restart")
    handled = await runner._handle_active_session_busy_message(duplicate, session_key)
    await asyncio.sleep(0)

    assert scheduled == 1
    assert handled is True
    adapter.handle_message.assert_awaited_once()
    assert session_key not in adapter._pending_messages
    assert runner.work_ledger.get(item["id"])["status"] == "claimed"


@pytest.mark.asyncio
async def test_startup_replays_same_session_discord_work_in_fifo_order(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_BUSY_ACK_ENABLED", "false")
    ledger_path = tmp_path / "work_ledger.json"
    runner = _make_busy_runner(ledger_path)
    adapter = _LedgerDrainAdapter(PlatformConfig(enabled=True, token="token"), Platform.DISCORD)
    runner.adapters = {Platform.DISCORD: adapter}
    runner.config = SimpleNamespace(
        group_sessions_per_user=True,
        thread_sessions_per_user=False,
        platforms={},
        quick_commands={},
    )
    runner.session_store = MagicMock()
    runner._background_tasks = set()
    adapter.set_busy_session_handler(runner._handle_active_session_busy_message)
    adapter.set_message_handler(runner._handle_message)

    first_event = _discord_event(message_id="replay-1", text="first replay")
    second_event = _discord_event(message_id="replay-2", text="second replay")
    session_key = build_session_key(first_event.source)
    first_item = runner.work_ledger.accept_event(
        first_event,
        session_key=session_key,
        freshness_seconds=60,
    )
    second_item = runner.work_ledger.accept_event(
        second_event,
        session_key=session_key,
        freshness_seconds=60,
    )
    assert first_item is not None
    assert second_item is not None
    runner.work_ledger.claim(first_item["id"])
    runner.work_ledger.claim(second_item["id"])
    _mark_claim_stale(runner.work_ledger, first_item["id"])
    _mark_claim_stale(runner.work_ledger, second_item["id"])

    processed_message_ids = []
    first_started = asyncio.Event()
    allow_first_to_finish = asyncio.Event()
    both_processed = asyncio.Event()

    async def fake_handle_message_with_agent(event, source, _quick_key, _run_generation):
        processed_message_ids.append(event.message_id)
        if event.message_id == "replay-1":
            first_started.set()
            await asyncio.wait_for(allow_first_to_finish.wait(), timeout=2)

        work_id = getattr(event, "work_item_id", None)
        assert work_id
        assert getattr(event, "defer_work_completion", False) is False
        runner.work_ledger.mark_agent_running(work_id, session_id="session-1")
        runner.work_ledger.mark_agent_done(
            work_id,
            final_response=f"done {event.message_id}",
            session_id="session-1",
            summary_status="Complete",
        )
        runner.work_ledger.mark_response_delivered(
            work_id,
            result_message_id=f"result-{event.message_id}",
        )
        runner.work_ledger.mark_summary_updated(work_id)
        runner.work_ledger.mark_completed(work_id)
        if len(processed_message_ids) == 2:
            both_processed.set()
        return ""

    runner._handle_message_with_agent = fake_handle_message_with_agent

    scheduled = runner._schedule_incomplete_discord_work_items()
    await asyncio.wait_for(first_started.wait(), timeout=2)
    await asyncio.sleep(0)
    allow_first_to_finish.set()
    await asyncio.wait_for(both_processed.wait(), timeout=2)

    assert scheduled == 2
    assert processed_message_ids == ["replay-1", "replay-2"]
    assert runner.work_ledger.get(first_item["id"])["status"] == "completed"
    assert runner.work_ledger.get(second_item["id"])["status"] == "completed"
    await adapter.cancel_background_tasks()


@pytest.mark.asyncio
async def test_startup_delivers_agent_done_work_without_rerunning_agent(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner._background_tasks = set()
    runner._session_db = None
    adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        _send_with_retry=AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="result-1")
        ),
        update_feature_summary=AsyncMock(return_value=True),
    )
    runner.adapters = {Platform.DISCORD: adapter}

    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response="normal final answer",
        session_id="session-1",
        summary_status="Complete",
        feature_summary={"message_id": "summary-1"},
    )

    scheduled = runner._schedule_incomplete_discord_work_items()
    if runner._background_tasks:
        await asyncio.gather(*runner._background_tasks)

    assert scheduled == 1
    adapter.handle_message.assert_not_called()
    adapter._send_with_retry.assert_awaited_once()
    assert adapter._send_with_retry.await_args.kwargs["content"] == "normal final answer"
    adapter.update_feature_summary.assert_awaited_once()
    assert runner.work_ledger.get(item["id"])["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,duplicate_mode",
    [
        ("agent_done", "normal"),
        ("response_delivered", "normal"),
        ("summary_updated", "normal"),
        ("agent_done", "drain"),
        ("response_delivered", "drain"),
        ("summary_updated", "drain"),
    ],
)
async def test_startup_resumes_duplicate_finished_work_without_rerunning_agent(
    tmp_path,
    status,
    duplicate_mode,
):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner._background_tasks = set()
    runner._session_db = None
    adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        _send_with_retry=AsyncMock(return_value=SimpleNamespace(success=True, message_id="result-1")),
        update_feature_summary=AsyncMock(return_value=True),
    )
    runner.adapters = {Platform.DISCORD: adapter}

    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response="normal final answer",
        session_id="session-1",
        summary_status="Complete",
        feature_summary={"message_id": "summary-1"},
    )
    if status in {"response_delivered", "summary_updated"}:
        runner.work_ledger.mark_response_delivered(item["id"], result_message_id="result-1")
    if status == "summary_updated":
        runner.work_ledger.mark_summary_updated(item["id"])

    if duplicate_mode == "normal":
        runner._accept_discord_work_item(event, session_key)
    else:
        runner._record_discord_work_for_drain(event, session_key)

    assert runner.work_ledger.get(item["id"])["status"] == status

    scheduled = runner._schedule_incomplete_discord_work_items()
    if runner._background_tasks:
        await asyncio.gather(*runner._background_tasks)

    assert scheduled == 1
    adapter.handle_message.assert_not_called()
    if status == "agent_done":
        adapter._send_with_retry.assert_awaited_once()
    else:
        adapter._send_with_retry.assert_not_called()
    adapter.update_feature_summary.assert_awaited_once()
    assert runner.work_ledger.get(item["id"])["status"] == "completed"


def test_discord_worker_reference_context_resolves_bare_message_ids(monkeypatch):
    from hermes_cli import discord_worker_boards as dwb

    def fake_fetch(channel_id, message_id):
        if channel_id == "parent-1" and message_id == "1507176047022575776":
            return {
                "id": message_id,
                "content": "reported bug details",
                "author": {"username": "alice"},
            }
        return None

    monkeypatch.setattr(dwb, "_fetch_discord_message_reference", fake_fetch)

    refs = dwb._discord_reference_context(
        "fix the bug reported in message 1507176047022575776",
        {"chat_id": "thread-1", "parent_channel_id": "parent-1"},
    )

    assert len(refs) == 1
    assert refs[0]["id"] == "1507176047022575776"
    assert refs[0]["channel_id"] == "parent-1"
    assert refs[0]["content"] == "reported bug details"
