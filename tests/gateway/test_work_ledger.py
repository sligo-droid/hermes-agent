import asyncio
import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
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


def test_ledger_deduplicates_discord_message_ids(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="m1")
    event.goal_thread_context = "[Goal thread context]\n[Alice] prior detail"
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


def test_ledger_records_discord_board_final_response_provenance(tmp_path, monkeypatch):
    import gateway.work_ledger as work_ledger

    calls = []
    monkeypatch.setattr(
        work_ledger,
        "_record_discord_board_final_response",
        lambda item, result_message_id=None: calls.append(
            (item.get("final_response"), result_message_id)
        ),
    )
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="m1")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(item["id"], final_response="normal final answer")
    ledger.mark_response_delivered(item["id"], result_message_id="result-1")

    assert calls == [
        ("normal final answer", None),
        ("normal final answer", "result-1"),
    ]


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
    data = runner.work_ledger._read()
    data["items"][item["id"]]["claim_pid"] = os.getpid() + 10_000_000
    runner.work_ledger._write(data)

    scheduled = runner._schedule_incomplete_discord_work_items()
    await asyncio.sleep(0)

    assert scheduled == 1
    runner.adapters[Platform.DISCORD].handle_message.assert_awaited_once()
    replay = runner.adapters[Platform.DISCORD].handle_message.await_args.args[0]
    assert replay.work_replay is True
    assert replay.work_item_id == item["id"]
    assert replay.text == "do the work"
    assert replay.goal_thread_context == event.goal_thread_context


@pytest.mark.asyncio
async def test_startup_auto_resume_reuses_original_discord_work_item(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner._background_tasks = set()
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner.adapters = {Platform.DISCORD: adapter}

    event = _discord_event(message_id="m1")
    event.feature_summary = {
        "message_id": "summary-1",
        "initial_request": "do the work",
    }
    event.goal_thread_context = "[Goal thread context]\n[Alice] details"
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response="",
        session_id="session-1",
        summary_status="Interrupted",
        feature_summary=event.feature_summary,
    )
    entry = SimpleNamespace(
        session_key=session_key,
        origin=event.source,
        resume_pending=True,
        suspended=False,
        resume_reason="restart_timeout",
        last_resume_marked_at=datetime.now(),
        updated_at=datetime.now(),
    )
    runner.session_store = MagicMock()
    runner.session_store._entries = {session_key: entry}

    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    assert scheduled == 1
    adapter.handle_message.assert_awaited_once()
    resumed = adapter.handle_message.await_args.args[0]
    assert resumed.internal is True
    assert resumed.work_item_id == item["id"]
    assert resumed.work_replay is True
    assert resumed.feature_summary == event.feature_summary
    assert resumed.goal_thread_context == event.goal_thread_context
    assert resumed.message_id == "m1"


@pytest.mark.asyncio
async def test_startup_defers_interrupted_discord_work_for_resume_pending_session(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner._background_tasks = set()
    adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        _send_with_retry=AsyncMock(),
        update_feature_summary=AsyncMock(return_value=True),
    )
    runner.adapters = {Platform.DISCORD: adapter}

    event = _discord_event(message_id="m1")
    event.feature_summary = {
        "message_id": "summary-1",
        "initial_request": "do the work",
    }
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response="",
        session_id="session-1",
        summary_status="Interrupted",
        feature_summary=event.feature_summary,
    )
    runner.session_store = SimpleNamespace(
        _entries={
            session_key: SimpleNamespace(
                resume_pending=True,
                suspended=False,
                resume_reason="restart_timeout",
                last_resume_marked_at=datetime.now(),
                updated_at=datetime.now(),
            )
        }
    )

    scheduled = runner._schedule_incomplete_discord_work_items()

    assert scheduled == 0
    adapter.handle_message.assert_not_called()
    adapter._send_with_retry.assert_not_awaited()
    adapter.update_feature_summary.assert_not_awaited()
    assert runner.work_ledger.get(item["id"])["status"] == "agent_done"


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
async def test_startup_updates_agent_done_summary_without_final_response(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner._background_tasks = set()
    runner._session_db = None
    adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        _send_with_retry=AsyncMock(),
        update_feature_summary=AsyncMock(return_value=True),
    )
    runner.adapters = {Platform.DISCORD: adapter}

    event = _discord_event(message_id="m1")
    event.feature_summary = {
        "message_id": "summary-1",
        "initial_request": "do the work",
    }
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response="",
        session_id="session-1",
        summary_status="Interrupted",
        feature_summary=event.feature_summary,
    )

    scheduled = runner._schedule_incomplete_discord_work_items()
    if runner._background_tasks:
        await asyncio.gather(*runner._background_tasks)

    assert scheduled == 1
    adapter.handle_message.assert_not_called()
    adapter._send_with_retry.assert_not_awaited()
    adapter.update_feature_summary.assert_awaited_once()
    assert adapter.update_feature_summary.await_args.kwargs["status"] == "Interrupted"
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
