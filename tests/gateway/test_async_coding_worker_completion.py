from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.run import GatewayRunner, Platform, _format_gateway_process_notification
from tools import async_delegation as ad
from tools.process_registry import process_registry


def _coding_event(**overrides):
    event = {
        "type": "async_delegation",
        "kind": "coding_worker",
        "delegation_id": "deleg_worker1",
        "session_key": "agent:main:discord:thread:111:222",
        "message_id": "444",
        "task": "implement the parser fix",
        "context_pack": {"approach": "keep it local"},
        "worker_cwd": "/tmp/repo",
        "worker_tier": "thorough",
        "scope_paths": ["src"],
        "worker_run": {
            "backend": "codex",
            "model": "gpt-5.6-sol",
            "reasoning": "high",
            "tier": "thorough",
            "background": True,
        },
        "status": "completed",
        "result": {
            "success": True,
            "status": "completed",
            "summary": "Implemented and verified the parser fix.",
            "scope_check": {"clean": True, "out_of_scope_files": []},
            "fable_git_result": {
                "pr_created": True,
                "pr_url": "https://github.com/sligo/example/pull/9",
            },
        },
    }
    event.update(overrides)
    return event


def _reset_async_state():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


class _OverlappingWorkLedger:
    def __init__(self, *, include_origin: bool = True):
        self.items = {
            "work-b": {
                "id": "work-b",
                "session_key": "agent:main:discord:thread:111:222",
                "platform": "discord",
                "status": "agent_running",
                "updated_at": 20,
                "feature_summary": {"initial_request": "Later work B"},
                "channel_prompt": "Later work instructions",
            }
        }
        if include_origin:
            self.items["work-a"] = {
                "id": "work-a",
                "session_key": "agent:main:discord:thread:111:222",
                "platform": "discord",
                "status": "agent_running",
                "updated_at": 10,
                "feature_summary": {"initial_request": "Original work A"},
                "channel_prompt": "Original work instructions",
            }
        self.mutation_attempts = []

    def id_for_event(self, _event, _session_key):
        return None

    def get(self, work_id):
        return self.items.get(work_id)

    def incomplete_items(self):
        return list(self.items.values())

    def mark_summary_updated(self, work_id):
        self.mutation_attempts.append(("summary", work_id))
        if work_id not in self.items:
            return False
        self.items[work_id]["status"] = "summary_updated"
        return True

    def mark_completed(self, work_id):
        self.mutation_attempts.append(("completed", work_id))
        if work_id not in self.items:
            return False
        self.items[work_id]["status"] = "completed"
        return True


def test_coding_worker_completion_message_is_self_contained():
    text = _format_gateway_process_notification(_coding_event())

    assert "ASYNC CODING WORKER COMPLETE" in text
    assert "Original task: implement the parser fix" in text
    assert "DETERMINISTIC RESULT JSON" in text
    assert '"scope_check"' in text
    assert "https://github.com/sligo/example/pull/9" in text
    assert "report the outcome to the user" in text


def test_coding_worker_completion_respects_background_notification_mode():
    runner = object.__new__(GatewayRunner)
    runner._load_background_notifications_mode = lambda: "off"
    assert runner._async_completion_notification_enabled(_coding_event()) is False
    runner._load_background_notifications_mode = lambda: "error"
    assert runner._async_completion_notification_enabled(_coding_event()) is False
    assert runner._async_completion_notification_enabled(
        _coding_event(status="partial", result={"success": False, "error": "boom"})
    ) is True


@pytest.mark.asyncio
async def test_completion_turn_targets_original_discord_thread_and_carries_worker_footer():
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner._adapter_for_source = lambda source: adapter
    runner._hydrate_discord_continuation_event_from_work_item = lambda *args, **kwargs: None
    event = _coding_event()
    runner._enrich_async_delegation_routing(event)

    accepted = await runner._inject_async_delegation_completion(
        _format_gateway_process_notification(event),
        event,
    )

    assert accepted is True
    synth_event = adapter.handle_message.await_args.args[0]
    assert synth_event.internal is True
    assert synth_event.source.platform == Platform.DISCORD
    assert synth_event.source.chat_id == "111"
    assert synth_event.source.thread_id == "222"
    assert synth_event.message_id == "444"
    assert synth_event.background_completion_id == "deleg_worker1"
    assert synth_event.completed_worker_run["background"] is True
    assert synth_event.completed_worker_run["model"] == "gpt-5.6-sol"


@pytest.mark.asyncio
async def test_visible_completion_uses_exact_origin_with_overlapping_work_items():
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(handle_message=AsyncMock())
    ledger = _OverlappingWorkLedger()
    runner._adapter_for_source = lambda source: adapter
    runner._ledger = lambda: ledger
    event = _coding_event(origin_work_item_id="work-a")
    runner._enrich_async_delegation_routing(event)

    assert await runner._inject_async_delegation_completion(
        _format_gateway_process_notification(event),
        event,
    )

    synth_event = adapter.handle_message.await_args.args[0]
    assert synth_event.work_item_id == "work-a"
    assert synth_event.feature_summary == {"initial_request": "Original work A"}
    assert synth_event.channel_prompt == "Original work instructions"
    assert synth_event.feature_summary != ledger.items["work-b"]["feature_summary"]


@pytest.mark.asyncio
async def test_coding_worker_completion_keeps_legacy_action_runtime_after_false_user_turn():
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner._adapter_for_source = lambda source: adapter
    feature_summary = {
        "initial_request": "Implement the parser fix",
        "kanban_board": None,
    }

    class _Ledger:
        def id_for_event(self, _event, _session_key):
            return None

        def get(self, _work_id):
            return None

        def incomplete_items(self):
            return [
                {
                    "id": "work-1",
                    "session_key": "agent:main:discord:thread:111:222",
                    "platform": "discord",
                    "status": "agent_running",
                    "feature_summary": feature_summary,
                    "channel_prompt": "Project instructions",
                }
            ]

    runner._ledger = lambda: _Ledger()
    event = _coding_event()
    runner._enrich_async_delegation_routing(event)

    assert await runner._inject_async_delegation_completion(
        _format_gateway_process_notification(event),
        event,
    )

    synth_event = adapter.handle_message.await_args.args[0]
    assert synth_event.internal is True
    assert synth_event.feature_summary == feature_summary
    assert synth_event.discord_action_request_intent is None
    assert synth_event.channel_prompt == "Project instructions"
    assert synth_event.discord_action_request_base_channel_prompt is None


@pytest.mark.asyncio
async def test_discord_reaction_gate_keeps_dispatch_pending_until_completion_turn():
    _reset_async_state()
    gate = threading.Event()
    session_key = "agent:main:discord:thread:111:222"
    dispatch = ad.dispatch_async_delegation(
        goal="work",
        context=None,
        toolsets=["coding_worker"],
        role="coding_worker",
        model="quick",
        session_key=session_key,
        runner=lambda: (
            gate.wait(5)
            and {
                "status": "completed",
                "result": {"success": True, "summary": "done"},
                "_async_coding_worker": {
                    "task": "work",
                    "worker_cwd": "/tmp/repo",
                    "worker_tier": "quick",
                    "scope_paths": [],
                    "worker_run": {"background": True},
                },
            }
        ),
        max_async_children=1,
        kind="coding_worker",
    )
    assert dispatch["status"] == "dispatched"

    runner = object.__new__(GatewayRunner)
    runner._session_key_for_source = lambda source: session_key
    original_start = AsyncMock()
    original_complete = AsyncMock()
    adapter = SimpleNamespace(
        platform=Platform.DISCORD,
        on_processing_start=original_start,
        on_processing_complete=original_complete,
    )
    runner._install_background_worker_reaction_gate(adapter)
    message = MessageEvent(
        text="dispatch response",
        message_type=MessageType.TEXT,
        source=SimpleNamespace(),
    )

    await adapter.on_processing_complete(message, ProcessingOutcome.SUCCESS)
    original_start.assert_awaited_once_with(message)
    original_complete.assert_not_awaited()

    gate.set()
    deadline = asyncio.get_running_loop().time() + 5
    while process_registry.completion_queue.empty():
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(0.01)
    completion = process_registry.completion_queue.get_nowait()
    completion_message = MessageEvent(
        text="completion",
        message_type=MessageType.TEXT,
        source=SimpleNamespace(),
    )
    completion_message.background_completion_id = completion["delegation_id"]

    await adapter.on_processing_complete(
        completion_message,
        ProcessingOutcome.SUCCESS,
    )
    original_complete.assert_awaited_once_with(
        completion_message,
        ProcessingOutcome.SUCCESS,
    )
    _reset_async_state()


def test_internal_completion_fifo_stays_separate_from_pending_user_turn():
    runner = object.__new__(GatewayRunner)
    runner._queued_events = {}
    adapter = SimpleNamespace(_pending_messages={})
    session_key = "agent:main:discord:thread:111:222"
    completion = MessageEvent(
        text="trusted completion",
        message_type=MessageType.TEXT,
        source=SimpleNamespace(),
        internal=True,
    )
    user_event = MessageEvent(
        text="new user request",
        message_type=MessageType.TEXT,
        source=SimpleNamespace(),
    )

    runner._enqueue_internal_completion(session_key, completion)
    adapter._pending_messages[session_key] = user_event
    pending = adapter._pending_messages.pop(session_key)
    promoted = runner._promote_queued_event(session_key, adapter, pending)

    assert promoted is user_event
    assert adapter._pending_messages[session_key] is completion
    assert completion.text == "trusted completion"
    assert user_event.text == "new user request"


@pytest.mark.asyncio
async def test_feature_summary_stays_running_while_background_worker_is_pending():
    runner = object.__new__(GatewayRunner)
    callbacks = []

    def register_callback(session_key, callback, generation=None):
        callbacks.append(callback)

    adapter = SimpleNamespace(register_post_delivery_callback=register_callback)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._session_has_pending_background_workers = lambda *args, **kwargs: True
    runner._discord_work_item_id_for_event = lambda *args, **kwargs: None
    runner._update_discord_summaries = AsyncMock(return_value=True)
    source = SimpleNamespace(platform=Platform.DISCORD)
    event = SimpleNamespace(feature_summary={"thread_id": "222"}, project_summary=None)

    runner._register_discord_summary_post_delivery(
        event=event,
        source=source,
        session_key="agent:main:discord:thread:111:222",
        run_generation=7,
        session_id="session-1",
        final_response="I dispatched the worker.",
        agent_result={"failed": False},
    )
    assert len(callbacks) == 1
    await callbacks[0]()

    kwargs = runner._update_discord_summaries.await_args.kwargs
    assert kwargs["status"] == "Running"
    assert "workers are still running" in kwargs["final_response"]


@pytest.mark.asyncio
async def test_suppressed_completion_missing_origin_cannot_mutate_later_work_item():
    runner = object.__new__(GatewayRunner)
    ledger = _OverlappingWorkLedger(include_origin=False)
    adapter = SimpleNamespace(on_processing_complete=AsyncMock())
    runner._adapter_for_source = lambda source: adapter
    runner._ledger = lambda: ledger
    runner._session_has_pending_background_workers = lambda *args, **kwargs: False
    runner._update_discord_summaries = AsyncMock(return_value=True)
    event = _coding_event(origin_work_item_id="work-a")
    runner._enrich_async_delegation_routing(event)

    await runner._finalize_suppressed_async_completion(event)

    completion_event = adapter.on_processing_complete.await_args.args[0]
    assert completion_event.work_item_id == "work-a"
    assert completion_event.feature_summary is None
    assert completion_event.channel_prompt is None
    assert ledger.items["work-b"]["status"] == "agent_running"
    assert ledger.mutation_attempts == [
        ("summary", "work-a"),
        ("completed", "work-a"),
    ]


@pytest.mark.asyncio
async def test_suppressed_completion_keeps_summary_running_for_sibling_worker():
    runner = object.__new__(GatewayRunner)
    runner._enrich_async_delegation_routing = lambda event: None
    runner._build_process_event_source = lambda event: SimpleNamespace(
        platform=Platform.DISCORD,
        chat_id="111",
        thread_id="222",
    )
    adapter = SimpleNamespace(on_processing_complete=AsyncMock())
    runner._adapter_for_source = lambda source: adapter
    runner._hydrate_discord_continuation_event_from_work_item = (
        lambda *args, **kwargs: None
    )
    runner._session_has_pending_background_workers = lambda *args, **kwargs: True
    runner._update_discord_summaries = AsyncMock(return_value=True)

    await runner._finalize_suppressed_async_completion(_coding_event())

    kwargs = runner._update_discord_summaries.await_args.kwargs
    assert kwargs["status"] == "Running"
    assert "workers are still running" in kwargs["final_response"]


@pytest.mark.asyncio
async def test_last_suppressed_completion_finalizes_summary_and_ledger():
    runner = object.__new__(GatewayRunner)
    runner._build_process_event_source = lambda event: SimpleNamespace(
        platform=Platform.DISCORD,
        chat_id="111",
        thread_id="222",
    )
    adapter = SimpleNamespace(on_processing_complete=AsyncMock())
    runner._adapter_for_source = lambda source: adapter

    def hydrate(event, *args, **kwargs):
        event.work_item_id = "work-1"
        event.session_id = "session-1"
        event.feature_summary = {"thread_id": "222"}

    runner._hydrate_discord_continuation_event_from_work_item = hydrate
    runner._session_has_pending_background_workers = lambda *args, **kwargs: False
    runner._discord_ledger_summary_status = lambda work_id, status: status
    runner._update_discord_summaries = AsyncMock(return_value=True)
    ledger = SimpleNamespace(
        mark_summary_updated=MagicMock(),
        mark_completed=MagicMock(),
    )
    runner._ledger = lambda: ledger

    await runner._finalize_suppressed_async_completion(_coding_event())

    summary_kwargs = runner._update_discord_summaries.await_args.kwargs
    assert summary_kwargs["status"] == "Complete"
    assert summary_kwargs["session_id"] == "session-1"
    ledger.mark_summary_updated.assert_called_once_with("work-1")
    ledger.mark_completed.assert_called_once_with("work-1")
