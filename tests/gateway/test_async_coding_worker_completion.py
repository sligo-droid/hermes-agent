from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    ProcessingOutcome,
    SendResult,
)
from gateway.run import GatewayRunner, Platform, _format_gateway_process_notification
from gateway.session import SessionSource, build_session_key
from gateway.work_ledger import GatewayWorkLedger
from tools import async_delegation as ad
from tools.process_registry import process_registry


def _coding_event(**overrides):
    event = {
        "type": "async_delegation",
        "kind": "coding_worker",
        "delegation_id": "deleg_worker1",
        "session_key": "agent:main:discord:thread:111:222",
        "message_id": "444",
        "origin_run_generation": 7,
        "origin_attempt_id": "boot-a:7",
        "origin_attempt_order": 10,
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


def _advisory_event(**overrides):
    event = {
        "type": "async_delegation",
        "delegation_id": "deleg_advisors",
        "session_key": "agent:main:discord:thread:111:222",
        "message_id": "444",
        "origin_run_generation": 7,
        "origin_attempt_id": "boot-a:7",
        "origin_attempt_order": 10,
        "goal": "review the parser fix",
        "goals": ["review correctness", "review tests"],
        "status": "completed",
        "is_batch": True,
        "results": [
            {"success": True, "status": "completed", "summary": "Looks good."},
            {"success": True, "status": "completed", "summary": "Tests cover it."},
        ],
    }
    event.update(overrides)
    return event


def _reset_async_state():
    ad._reset_for_tests()
    while not process_registry.completion_queue.empty():
        process_registry.completion_queue.get_nowait()


async def _injected_completion_event(runner, adapter, event):
    runner._enrich_async_delegation_routing(event)
    assert await runner._inject_async_delegation_completion(
        _format_gateway_process_notification(event),
        event,
    )
    return adapter.handle_message.await_args.args[0]


class _SilentCompletionAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)
        self.sent = []
        self.typing = []
        self.processing_hooks = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, reply_to, metadata))
        return SendResult(success=True, message_id="sent-1")

    async def send_typing(self, chat_id, metadata=None):
        self.typing.append((chat_id, metadata))

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def on_processing_start(self, event):
        self.processing_hooks.append(("start", event.background_completion_id))

    async def on_processing_complete(self, event, outcome):
        self.processing_hooks.append(
            ("complete", event.background_completion_id, outcome)
        )


def _reaction_work_item(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "reaction_ledger.json")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="111",
        chat_type="thread",
        thread_id="222",
        user_id="user-1",
        message_id="444",
    )
    session_key = build_session_key(source)
    event = MessageEvent(
        text="implement the parser fix",
        source=source,
        message_id="444",
    )
    item = ledger.accept_event(
        event,
        session_key=session_key,
        freshness_seconds=60,
    )
    assert item is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_id="session-1",
        session_key=session_key,
        run_generation=7,
        process_epoch="boot-a",
    )
    assert ledger.begin_required_async_attempt(
        item["id"],
        attempt_id="boot-a:7",
        attempt_order=10,
        generation=7,
    ) is not None
    return ledger, item["id"], session_key


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
        self.required_completion_records = []

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

    def record_required_async_completion(self, work_id, **kwargs):
        self.required_completion_records.append((work_id, kwargs))
        return {
            "generation": kwargs.get("generation"),
            "outcomes": {
                kwargs.get("delegation_id"): {"success": kwargs.get("success")}
            },
            "failed": kwargs.get("success") is False,
            "succeeded": int(kwargs.get("success") is True),
        }


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
@pytest.mark.parametrize(
    ("handler_result", "expected_outcome"),
    [
        (
            "internal closeout complete\nMEDIA:/tmp/result.png\n[[audio_as_voice]]",
            ProcessingOutcome.SUCCESS,
        ),
        (RuntimeError("internal failure"), ProcessingOutcome.FAILURE),
    ],
)
async def test_suppressed_completion_runs_hooks_and_closeout_without_output(
    handler_result,
    expected_outcome,
):
    adapter = _SilentCompletionAdapter()
    closeout = AsyncMock()

    async def handler(_event):
        if isinstance(handler_result, BaseException):
            raise handler_result
        return handler_result

    adapter.set_message_handler(handler)
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="111",
        chat_type="dm",
        thread_id="222",
    )
    event = MessageEvent(
        text="internal completion",
        source=source,
        internal=True,
        background_completion_id="deleg_worker1",
        suppress_user_output=True,
    )
    session_key = build_session_key(source)
    adapter.register_post_delivery_callback(session_key, closeout)

    await adapter._process_message_background(event, session_key)

    assert adapter.sent == []
    assert adapter.typing == []
    assert adapter.processing_hooks == [
        ("start", "deleg_worker1"),
        ("complete", "deleg_worker1", expected_outcome),
    ]
    closeout.assert_awaited_once()


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
async def test_advisory_completion_cannot_claim_or_mutate_work_item():
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(handle_message=AsyncMock())
    ledger = _OverlappingWorkLedger()
    runner._adapter_for_source = lambda source: adapter
    runner._ledger = lambda: ledger

    synth_event = await _injected_completion_event(
        runner,
        adapter,
        _advisory_event(origin_work_item_id="work-a", origin_run_generation=7),
    )

    assert synth_event.background_completion_kind == "delegation"
    assert synth_event.background_completion_success is True
    assert synth_event.background_completion_generation == 7
    assert synth_event.participates_in_work_lifecycle is False
    assert synth_event.work_item_id is None
    assert synth_event.feature_summary is None
    assert synth_event.channel_prompt is None
    assert ledger.required_completion_records == []
    assert ledger.mutation_attempts == []


@pytest.mark.asyncio
async def test_missing_origin_coding_completion_does_not_guess_action_runtime():
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner._adapter_for_source = lambda source: adapter
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
                    "feature_summary": {
                        "initial_request": "Implement the parser fix",
                        "kanban_board": None,
                    },
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
    assert synth_event.work_item_id is None
    assert synth_event.feature_summary is None
    assert synth_event.discord_action_request_intent is False
    assert synth_event.channel_prompt is None
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "completion_order",
    [
        ("required_failure", "advisory_success"),
        ("advisory_success", "required_failure"),
    ],
)
async def test_required_worker_failure_cannot_be_overwritten_by_advisory_success(
    completion_order,
    tmp_path,
):
    """A successful review turn is not authoritative for required coding work."""
    ledger, work_item_id, session_key = _reaction_work_item(tmp_path)
    runner = object.__new__(GatewayRunner)
    original_start = AsyncMock()
    original_complete = AsyncMock()
    adapter = SimpleNamespace(
        platform=Platform.DISCORD,
        handle_message=AsyncMock(),
        on_processing_start=original_start,
        on_processing_complete=original_complete,
    )
    runner._adapter_for_source = lambda source: adapter
    runner._ledger = lambda: ledger
    runner._session_key_for_source = lambda source: session_key
    runner._install_background_worker_reaction_gate(adapter)

    events = {
        "required_failure": _coding_event(
            delegation_id="deleg_required",
            origin_work_item_id=work_item_id,
            origin_run_generation=7,
            status="error",
            result={
                "success": False,
                "status": "error",
                "error": "worker tests failed",
            },
        ),
        "advisory_success": _advisory_event(
            origin_work_item_id=work_item_id,
            origin_run_generation=7,
        ),
    }

    for name in completion_order:
        event = await _injected_completion_event(runner, adapter, events[name])
        # The synthetic agent can successfully explain a failed worker. The
        # durable worker outcome must still control the work-item reaction.
        await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    outcomes = [call.args[1] for call in original_complete.await_args_list]
    assert outcomes
    assert ProcessingOutcome.SUCCESS not in outcomes
    assert outcomes[-1] == ProcessingOutcome.FAILURE
    original_start.assert_not_awaited()
    stored = ledger.get(work_item_id)
    assert stored["completion_gate"]["reason"] == "required_async_completion_failed"
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["summary_status"] == "Failed"


@pytest.mark.asyncio
async def test_reaction_becomes_success_only_after_required_worker_closeout(tmp_path):
    ledger, work_item_id, session_key = _reaction_work_item(tmp_path)
    runner = object.__new__(GatewayRunner)
    original_start = AsyncMock()
    original_complete = AsyncMock()
    adapter = SimpleNamespace(
        platform=Platform.DISCORD,
        handle_message=AsyncMock(),
        on_processing_start=original_start,
        on_processing_complete=original_complete,
    )
    runner._adapter_for_source = lambda source: adapter
    runner._ledger = lambda: ledger
    runner._session_key_for_source = lambda source: session_key
    runner._install_background_worker_reaction_gate(adapter)

    advisory = await _injected_completion_event(
        runner,
        adapter,
        _advisory_event(
            origin_work_item_id=work_item_id,
            origin_run_generation=7,
        ),
    )
    await adapter.on_processing_complete(advisory, ProcessingOutcome.SUCCESS)
    original_start.assert_not_awaited()
    original_complete.assert_not_awaited()

    required = await _injected_completion_event(
        runner,
        adapter,
        _coding_event(
            delegation_id="deleg_required",
            origin_work_item_id=work_item_id,
            origin_run_generation=7,
        ),
    )
    await adapter.on_processing_complete(required, ProcessingOutcome.SUCCESS)
    original_start.assert_awaited_once_with(required)
    original_complete.assert_not_awaited()

    # The successful worker result is necessary but not sufficient. Only the
    # authoritative work-item closeout can move the reaction to success.
    expected_run_state = ledger.run_state_snapshot(ledger.get(work_item_id))
    assert ledger.mark_agent_done(
        work_item_id,
        final_response="Implemented and verified the parser fix.",
        session_id="session-1",
        summary_status="Complete",
        expected_run_state=expected_run_state,
    )
    assert ledger.mark_summary_updated(work_item_id)
    assert ledger.mark_completed(work_item_id)
    stored = ledger.get(work_item_id)
    assert stored["status"] == "completed"
    assert stored["completion_gate"]["allowed_to_complete"] is True

    await adapter.on_processing_complete(required, ProcessingOutcome.SUCCESS)

    original_complete.assert_awaited_once_with(
        required,
        ProcessingOutcome.SUCCESS,
    )


@pytest.mark.asyncio
async def test_completed_work_item_with_failed_summary_stays_failed(tmp_path):
    ledger, work_item_id, session_key = _reaction_work_item(tmp_path)
    runner = object.__new__(GatewayRunner)
    original_start = AsyncMock()
    original_complete = AsyncMock()
    adapter = SimpleNamespace(
        platform=Platform.DISCORD,
        on_processing_start=original_start,
        on_processing_complete=original_complete,
    )
    runner._ledger = lambda: ledger
    runner._session_key_for_source = lambda source: session_key
    runner._install_background_worker_reaction_gate(adapter)

    required = MessageEvent(
        text="completion",
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="111",
            chat_type="thread",
            thread_id="222",
        ),
        work_item_id=work_item_id,
        background_completion_id="deleg_required",
        background_completion_kind="coding_worker",
        background_completion_success=True,
        background_completion_generation=7,
        background_completion_attempt_id="boot-a:7",
    )
    ledger.record_required_async_completion(
        work_item_id,
        delegation_id="deleg_required",
        success=True,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="completed",
    )
    expected_run_state = ledger.run_state_snapshot(ledger.get(work_item_id))
    assert ledger.mark_agent_done(
        work_item_id,
        final_response="Completion review failed.",
        session_id="session-1",
        summary_status="Failed",
        expected_run_state=expected_run_state,
    )
    assert ledger.mark_summary_updated(work_item_id)
    assert ledger.mark_completed(work_item_id)

    await adapter.on_processing_complete(required, ProcessingOutcome.SUCCESS)

    original_start.assert_not_awaited()
    original_complete.assert_awaited_once_with(
        required,
        ProcessingOutcome.FAILURE,
    )


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


def test_required_async_failure_is_durable_and_generation_fenced(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="111",
        chat_type="thread",
        thread_id="222",
        user_id="user-1",
        message_id="444",
    )
    event = MessageEvent(
        text="implement the parser fix",
        message_type=MessageType.TEXT,
        source=source,
        message_id="444",
    )
    session_key = build_session_key(source)
    item = ledger.accept_event(
        event,
        session_key=session_key,
        freshness_seconds=60,
    )
    assert item is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_id="session-1",
        session_key=session_key,
        run_generation=7,
        process_epoch="boot-a",
    )
    attempt = ledger.begin_required_async_attempt(
        item["id"],
        attempt_id="boot-a:7",
        attempt_order=10,
        generation=7,
    )
    assert attempt is not None

    failed = ledger.record_required_async_completion(
        item["id"],
        delegation_id="deleg_required",
        success=False,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="error",
    )
    assert failed is not None
    assert failed["failed"] is True

    # Neither a conflicting replay nor another successful completion from the
    # same generation may repaint the required failure.
    ledger.record_required_async_completion(
        item["id"],
        delegation_id="deleg_required",
        success=True,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="completed",
    )
    ledger.record_required_async_completion(
        item["id"],
        delegation_id="deleg_other",
        success=True,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="completed",
    )
    stored = ledger.get(item["id"])
    assert stored["required_async_completions"]["generation"] == 7
    assert stored["required_async_completions"]["outcomes"]["deleg_required"]["success"] is False
    gate = stored["completion_gate"]
    assert gate["allowed_to_complete"] is False
    assert gate["summary_status"] == "Failed"
    assert gate["terminal_status"] == "blocked"
    assert gate["reason"] == "required_async_completion_failed"
    assert gate["required_async_completions"]["generation"] == 7
    assert gate["required_async_completions"]["failed"] is True
    assert gate["required_async_completions"]["succeeded"] == 1
    assert stored["summary_status"] == "Failed"

    # A later work generation can succeed, but it remains non-terminal while
    # its authoritative work item is still active. A stale generation-7 event
    # cannot mutate that newer lifecycle.
    assert ledger.mark_agent_running(
        item["id"],
        session_id="session-2",
        session_key=session_key,
        run_generation=8,
        process_epoch="boot-a",
    )
    newer_attempt = ledger.begin_required_async_attempt(
        item["id"],
        attempt_id="boot-a:8",
        attempt_order=11,
        generation=8,
    )
    assert newer_attempt is not None
    assert newer_attempt["failed"] is False
    ledger.record_required_async_completion(
        item["id"],
        delegation_id="deleg_retry",
        success=True,
        generation=8,
        attempt_id="boot-a:8",
        attempt_order=11,
        status="completed",
    )
    ledger.record_required_async_completion(
        item["id"],
        delegation_id="deleg_stale",
        success=False,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="error",
    )
    stored = ledger.get(item["id"])
    state = ledger.required_async_completion_state(item["id"])
    assert state is not None
    assert state["generation"] == 8
    assert state["attempt_id"] == "boot-a:8"
    assert state["attempt_order"] == 11
    assert state["failed"] is False
    assert set(state["outcomes"]) == {"deleg_retry"}
    assert stored["status"] == "agent_running"
    assert stored["active_run"]["generation"] == 8
    assert stored.get("completion_gate", {}).get("reason") != "required_async_completion_failed"


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
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner._adapter_for_source = lambda source: adapter
    runner._ledger = lambda: ledger
    event = _coding_event(origin_work_item_id="work-a")
    runner._enrich_async_delegation_routing(event)

    await runner._finalize_suppressed_async_completion(event)

    completion_event = adapter.handle_message.await_args.args[0]
    assert completion_event.suppress_user_output is True
    assert completion_event.work_item_id == "work-a"
    assert completion_event.feature_summary is None
    assert completion_event.channel_prompt is None
    assert ledger.items["work-b"]["status"] == "agent_running"
    assert ledger.mutation_attempts == []


@pytest.mark.asyncio
async def test_missing_origin_completion_cannot_claim_ambiguous_later_work_item():
    runner = object.__new__(GatewayRunner)
    ledger = _OverlappingWorkLedger(include_origin=False)
    ledger.items["work-c"] = {
        **ledger.items["work-b"],
        "id": "work-c",
        "updated_at": 30,
        "feature_summary": {"initial_request": "Even later work C"},
    }
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner._adapter_for_source = lambda source: adapter
    runner._ledger = lambda: ledger
    event = _coding_event(origin_work_item_id="")
    completion_event = await _injected_completion_event(runner, adapter, event)

    assert completion_event.work_item_id is None
    assert completion_event.feature_summary is None
    assert completion_event.channel_prompt is None
    assert ledger.items["work-b"]["status"] == "agent_running"
    assert ledger.items["work-c"]["status"] == "agent_running"
    assert ledger.mutation_attempts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_suppression"),
    [("off", True), ("error", True), ("result", False), ("all", False)],
)
async def test_notification_mode_changes_visibility_not_internal_closeout(
    mode,
    expected_suppression,
):
    _reset_async_state()
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._async_completion_seen = set()
    runner._load_background_notifications_mode = lambda: mode
    runner._build_process_event_source = lambda event: SimpleNamespace(
        platform=Platform.DISCORD,
        chat_id="111",
        thread_id="222",
    )
    ledger = SimpleNamespace(
        record_required_async_completion=MagicMock(
            return_value={
                "generation": 7,
                "outcomes": {"deleg_worker1": {"success": True}},
                "failed": False,
                "succeeded": 1,
            }
        ),
    )
    runner._ledger = lambda: ledger

    async def stop_after_internal_injection(event):
        runner._running = False

    adapter = SimpleNamespace(
        handle_message=AsyncMock(side_effect=stop_after_internal_injection),
    )
    runner._adapter_for_source = lambda source: adapter
    runner._hydrate_discord_continuation_event_from_work_item = (
        lambda *args, **kwargs: None
    )
    process_registry.completion_queue.put(
        _coding_event(
            origin_work_item_id="work-1",
            origin_run_generation=7,
        )
    )

    try:
        await runner._async_delegation_watcher(interval=0)
    finally:
        _reset_async_state()

    adapter.handle_message.assert_awaited_once()
    completion_event = adapter.handle_message.await_args.args[0]
    assert completion_event.internal is True
    assert completion_event.suppress_user_output is expected_suppression
    assert completion_event.participates_in_work_lifecycle is True
    assert completion_event.background_completion_kind == "coding_worker"
    assert completion_event.background_completion_success is True
    assert completion_event.background_completion_generation == 7
    assert completion_event.work_item_id == "work-1"
    ledger.record_required_async_completion.assert_called_once()
    record_args, record_kwargs = ledger.record_required_async_completion.call_args
    assert record_args == ("work-1",)
    assert record_kwargs["delegation_id"] == "deleg_worker1"
    assert record_kwargs["success"] is True
    assert record_kwargs["generation"] == 7
    assert record_kwargs["attempt_id"] == "boot-a:7"
    assert record_kwargs["attempt_order"] == 10
    assert record_kwargs["status"] == "completed"
    assert record_kwargs["completed_at"] is None
    assert record_kwargs["closeout_id"] in {None, ""}
