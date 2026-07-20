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
from gateway.run import (
    GatewayRunner,
    Platform,
    _INTERRUPT_REASON_RESET,
    _INTERRUPT_REASON_STOP,
    _format_gateway_process_notification,
)
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
        "model_tier": "advanced",
        "scope_paths": ["src"],
        "worker_run": {
            "backend": "codex",
            "model": "gpt-5.6-sol",
            "reasoning": "high",
            "model_tier": "advanced",
            "background": True,
        },
        "status": "completed",
        "result": {
            "success": True,
            "status": "completed",
            "summary": "Implemented and verified the parser fix.",
            "scope_check": {"clean": True, "out_of_scope_files": []},
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


def _analysis_batch_event(**overrides):
    event = {
        "type": "async_delegation",
        "kind": "delegation",
        "delegation_id": "deleg_batch1",
        "session_key": "agent:main:discord:thread:111:222",
        "status": "partial",
        "is_batch": True,
        "goals": ["inspect parser", "inspect tests"],
        "results": [
            {
                "task_index": 0,
                "status": "completed",
                "summary": "parser summary",
            },
            {
                "task_index": 1,
                "status": "failed",
                "error": "provider failed",
            },
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


def _registered_required_attempt(tmp_path, *, dispatch_ids=("deleg_required",)):
    ledger, work_item_id, session_key = _reaction_work_item(tmp_path)
    for delegation_id in dispatch_ids:
        assert ledger.register_required_async_dispatch(
            work_item_id,
            delegation_id=delegation_id,
            generation=7,
            attempt_id="boot-a:7",
            attempt_order=10,
            owner_pid=123,
            process_epoch="boot-a",
            scope_paths=["src"],
        )
        assert ledger.mark_required_async_dispatch_running(
            work_item_id,
            delegation_id=delegation_id,
            generation=7,
            attempt_id="boot-a:7",
            attempt_order=10,
            owner_pid=123,
            process_epoch="boot-a",
        )
    return ledger, work_item_id, session_key


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
    assert "fable_git_result" not in text
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


def test_gateway_batch_completion_uses_shared_formatter_and_partial_failure_policy():
    text = _format_gateway_process_notification(_analysis_batch_event())

    assert "ASYNC DELEGATION BATCH COMPLETE" in text
    assert "TASK 1/2: inspect parser" in text
    assert "parser summary" in text
    assert "TASK 2/2: inspect tests" in text
    assert "provider failed" in text

    runner = object.__new__(GatewayRunner)
    runner._load_background_notifications_mode = lambda: "error"
    assert runner._async_completion_notification_enabled(_analysis_batch_event()) is True
    assert runner._async_completion_notification_enabled(
        _analysis_batch_event(status="completed")
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
async def test_required_completion_never_invokes_a_model_turn():
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

    adapter.handle_message.assert_not_awaited()
    assert ledger.required_completion_records[0][0] == "work-a"
    assert ledger.mutation_attempts == []


@pytest.mark.asyncio
async def test_advisory_completion_is_persisted_without_model_or_lifecycle_replay():
    runner = object.__new__(GatewayRunner)
    adapter = SimpleNamespace(handle_message=AsyncMock())
    ledger = _OverlappingWorkLedger()
    runner._adapter_for_source = lambda source: adapter
    runner._ledger = lambda: ledger

    event = _advisory_event(origin_work_item_id="work-a", origin_run_generation=7)
    runner._enrich_async_delegation_routing(event)

    assert await runner._inject_async_delegation_completion(
        _format_gateway_process_notification(event),
        event,
    )

    adapter.handle_message.assert_not_awaited()
    assert ledger.required_completion_records[0][0] == "work-a"
    evidence = ledger.required_completion_records[0][1]["evidence"]
    assert [row["summary"] for row in evidence["advisory_results"]] == [
        "Looks good.",
        "Tests cover it.",
    ]
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
                    "model_tier": "trivial",
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
async def test_required_failure_reconciles_to_terminal_delivery_without_model(tmp_path):
    ledger, work_item_id, _session_key = _registered_required_attempt(tmp_path)
    ledger.record_required_async_completion(
        work_item_id,
        delegation_id="deleg_required",
        success=False,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="error",
        error="worker tests failed",
    )
    state = ledger.seal_required_async_attempt(
        work_item_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
    )
    assert state and state["ready_to_reconcile"]

    runner = object.__new__(GatewayRunner)
    runner._ledger = lambda: ledger
    runner._resume_finished_discord_work_item = AsyncMock()
    runner._activate_required_async_closeout = MagicMock(
        side_effect=AssertionError("failure must not activate closeout")
    )

    await runner._reconcile_required_async_item(work_item_id, state)

    stored = ledger.get(work_item_id)
    assert stored["status"] == "blocked"
    assert stored["terminal_delivery"]["source"] == "required_async_completion"
    assert stored["required_async_completions"]["reconciled_at"] is not None
    runner._resume_finished_discord_work_item.assert_awaited_once()
    runner._activate_required_async_closeout.assert_not_called()


@pytest.mark.asyncio
async def test_stale_required_attempt_never_invokes_closeout_or_model(tmp_path):
    ledger, work_item_id, _session_key = _registered_required_attempt(tmp_path)
    old = ledger.required_async_completion_state(work_item_id)
    assert ledger.begin_required_async_attempt(
        work_item_id,
        attempt_id="boot-b:8",
        attempt_order=11,
        generation=8,
    )
    runner = object.__new__(GatewayRunner)
    runner._ledger = lambda: ledger
    runner._activate_required_async_closeout = MagicMock()
    runner._resume_finished_discord_work_item = AsyncMock()

    await runner._reconcile_required_async_item(work_item_id, old)

    runner._activate_required_async_closeout.assert_not_called()
    runner._resume_finished_discord_work_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_required_attempt_advances_work_item_exactly_once(tmp_path):
    ledger, work_item_id, _session_key = _registered_required_attempt(tmp_path)
    ledger.record_required_async_completion(
        work_item_id,
        delegation_id="deleg_required",
        success=True,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="completed",
        summary="Implemented and verified the parser fix.",
    )
    state = ledger.seal_required_async_attempt(
        work_item_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
    )
    runner = object.__new__(GatewayRunner)
    runner._ledger = lambda: ledger
    runner._activate_required_async_closeout = MagicMock(
        return_value=(False, "work_item")
    )
    runner._resume_finished_discord_work_item = AsyncMock()

    await runner._reconcile_required_async_item(work_item_id, state)
    await runner._reconcile_required_async_item(work_item_id, state)

    stored = ledger.get(work_item_id)
    required = ledger.required_async_completion_state(work_item_id)
    assert stored["status"] == "agent_done"
    assert "Implemented and verified" in stored["final_response"]
    assert required["reconciled_at"] is not None
    assert required["owns_recovery"] is False
    runner._resume_finished_discord_work_item.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_required_attempt_hands_off_lifecycle_without_terminal_message(tmp_path):
    ledger, work_item_id, _session_key = _registered_required_attempt(tmp_path)
    ledger.record_required_async_completion(
        work_item_id,
        delegation_id="deleg_required",
        success=True,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="completed",
        summary="Implementation ready for closeout.",
    )
    state = ledger.seal_required_async_attempt(
        work_item_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
    )
    runner = object.__new__(GatewayRunner)
    runner._ledger = lambda: ledger
    runner._activate_required_async_closeout = MagicMock(return_value=(True, "closeout"))
    runner._resume_finished_discord_work_item = AsyncMock()

    await runner._reconcile_required_async_item(work_item_id, state)

    stored = ledger.get(work_item_id)
    required = ledger.required_async_completion_state(work_item_id)
    assert stored["status"] == "agent_running"
    assert stored.get("final_response") in {None, ""}
    assert required["reconciled_at"] is not None
    runner._activate_required_async_closeout.assert_called_once()
    runner._resume_finished_discord_work_item.assert_not_awaited()


def test_successful_required_evidence_hands_off_to_existing_closeout(tmp_path):
    ledger, work_item_id, _session_key = _registered_required_attempt(tmp_path)
    ledger.attach_closeout_workspace(
        work_item_id,
        workspace_path=str(tmp_path),
        source="direct",
        mode="enforce",
        policy={"require_local_verification": True},
    )
    item = ledger.get(work_item_id)
    state = ledger.required_async_completion_state(work_item_id)
    dispatch = state["dispatches"]["deleg_required"]
    dispatch["completed_at"] = 1
    dispatch["evidence"] = {
        "head_sha": "a" * 40,
        "scope_check": {"clean": True},
    }
    state["dispatches"]["deleg_required"] = dispatch
    runner = object.__new__(GatewayRunner)
    runner._ledger = lambda: ledger
    runner._activate_closeout_at_verified_head = MagicMock(
        return_value={"status": "pending"}
    )

    activated, route = runner._activate_required_async_closeout(
        work_item_id,
        item,
        state,
    )

    assert (activated, route) == (True, "closeout")
    runner._activate_closeout_at_verified_head.assert_called_once_with(
        work_item_id,
        repository_root=str(tmp_path),
        verified_head_sha="a" * 40,
        visual_result={},
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
        text="original parent turn",
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="111",
            chat_type="thread",
            thread_id="222",
        ),
        work_item_id=work_item_id,
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
async def test_suppressed_required_completion_is_ledger_only():
    runner = object.__new__(GatewayRunner)
    ledger = _OverlappingWorkLedger()
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner._adapter_for_source = lambda source: adapter
    runner._ledger = lambda: ledger
    event = _coding_event(origin_work_item_id="work-a")
    runner._enrich_async_delegation_routing(event)

    await runner._finalize_suppressed_async_completion(event)

    adapter.handle_message.assert_not_awaited()
    assert ledger.required_completion_records[0][0] == "work-a"
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
async def test_two_required_workers_reconcile_only_after_seal_and_all_terminal(tmp_path):
    ledger, work_item_id, _session_key = _registered_required_attempt(
        tmp_path,
        dispatch_ids=("worker-a", "worker-b"),
    )
    ledger.record_required_async_completion(
        work_item_id,
        delegation_id="worker-a",
        success=True,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="completed",
        summary="A done",
    )
    sealed = ledger.seal_required_async_attempt(
        work_item_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
    )
    assert sealed["sealed"] is True
    assert sealed["all_terminal"] is False

    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._ledger = lambda: ledger
    runner._background_tasks = set()
    runner._required_async_item_tasks = {}
    runner._reconcile_required_async_item = AsyncMock()
    assert runner._scan_required_async_reconciliation() == 0

    ledger.record_required_async_completion(
        work_item_id,
        delegation_id="worker-b",
        success=True,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="completed",
        summary="B done",
    )
    assert runner._scan_required_async_reconciliation() == 1
    await asyncio.gather(*list(runner._background_tasks))
    runner._reconcile_required_async_item.assert_awaited_once()


@pytest.mark.asyncio
async def test_mixed_attempt_emits_one_aggregate_and_late_advice_stays_silent(tmp_path):
    ledger, work_item_id, _session_key = _registered_required_attempt(
        tmp_path,
        dispatch_ids=("worker-a", "worker-b"),
    )
    for delegation_id, goal in (("advisor-a", "review code"), ("advisor-b", "review tests")):
        assert ledger.register_required_async_dispatch(
            work_item_id,
            delegation_id=delegation_id,
            generation=7,
            attempt_id="boot-a:7",
            attempt_order=10,
            owner_pid=123,
            process_epoch="boot-a",
            kind="advisory",
            required=False,
            evidence={"advisory_results": [{"goal": goal, "status": "registered"}]},
        )
    sealed = ledger.seal_required_async_attempt(
        work_item_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
    )
    assert sealed["ready_to_reconcile"] is False

    ledger.record_required_async_completion(
        work_item_id,
        delegation_id="advisor-a",
        success=True,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="completed",
        evidence={
            "advisory_results": [
                {"goal": "review code", "status": "completed", "summary": "Code review is clean."}
            ]
        },
    )
    for delegation_id, summary in (("worker-a", "Parser implemented."), ("worker-b", "Tests added.")):
        ledger.record_required_async_completion(
            work_item_id,
            delegation_id=delegation_id,
            success=True,
            generation=7,
            attempt_id="boot-a:7",
            attempt_order=10,
            status="completed",
            summary=summary,
        )
    ready = ledger.required_async_completion_state(work_item_id)
    assert ready["ready_to_reconcile"] is True
    assert ready["advisory_pending_count"] == 1

    runner = object.__new__(GatewayRunner)
    runner._ledger = lambda: ledger
    runner._activate_required_async_closeout = MagicMock(return_value=(False, "work_item"))
    runner._resume_finished_discord_work_item = AsyncMock()

    await runner._reconcile_required_async_item(work_item_id, ready)

    stored = ledger.get(work_item_id)
    assert stored["status"] == "agent_done"
    assert "Parser implemented." in stored["final_response"]
    assert "Tests added." in stored["final_response"]
    assert "Code review is clean." in stored["final_response"]
    assert "review tests" not in stored["final_response"]
    runner._resume_finished_discord_work_item.assert_awaited_once()

    late = ledger.record_required_async_completion(
        work_item_id,
        delegation_id="advisor-b",
        success=False,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="error",
        error="late advisory provider failure",
        evidence={
            "advisory_results": [
                {"goal": "review tests", "status": "error", "error": "late advisory provider failure"}
            ]
        },
    )
    assert late["reconciled_at"] is not None
    assert late["advisory_failed"] == 1
    await runner._reconcile_required_async_item(work_item_id, late)
    runner._resume_finished_discord_work_item.assert_awaited_once()


@pytest.mark.asyncio
async def test_advisory_only_failure_sends_one_deterministic_blocked_response(tmp_path):
    ledger, work_item_id, _session_key = _reaction_work_item(tmp_path)
    assert ledger.register_required_async_dispatch(
        work_item_id,
        delegation_id="advisor-a",
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        owner_pid=123,
        process_epoch="boot-a",
        kind="advisory",
        required=False,
    )
    ledger.record_required_async_completion(
        work_item_id,
        delegation_id="advisor-a",
        success=False,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="error",
        error="provider unavailable",
        evidence={
            "advisory_results": [
                {"goal": "review code", "status": "error", "error": "provider unavailable"}
            ]
        },
    )
    state = ledger.seal_required_async_attempt(
        work_item_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
    )
    assert state["ready_to_reconcile"] is True
    assert state["failed"] is False

    runner = object.__new__(GatewayRunner)
    runner._ledger = lambda: ledger
    runner._activate_required_async_closeout = MagicMock()
    runner._resume_finished_discord_work_item = AsyncMock()

    await runner._reconcile_required_async_item(work_item_id, state)

    stored = ledger.get(work_item_id)
    assert stored["status"] == "blocked"
    assert stored["blocked_reason"] == "advisory_async_completion_failed"
    assert "provider unavailable" in stored["final_response"]
    runner._activate_required_async_closeout.assert_not_called()
    runner._resume_finished_discord_work_item.assert_awaited_once()


@pytest.mark.asyncio
async def test_periodic_scan_repairs_lost_required_completion_wakeup(tmp_path):
    ledger, work_item_id, _session_key = _registered_required_attempt(tmp_path)
    ledger.record_required_async_completion(
        work_item_id,
        delegation_id="deleg_required",
        success=False,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="error",
        error="lost queue event",
    )
    ledger.seal_required_async_attempt(
        work_item_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
    )
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._ledger = lambda: ledger
    runner._background_tasks = set()
    runner._required_async_item_tasks = {}
    runner._resume_finished_discord_work_item = AsyncMock()

    assert runner._scan_required_async_reconciliation() == 1
    await asyncio.gather(*list(runner._background_tasks))

    stored = ledger.get(work_item_id)
    assert stored["status"] == "blocked"
    runner._resume_finished_discord_work_item.assert_awaited_once()


def test_required_recovery_suppresses_original_discord_replay(tmp_path):
    ledger, _work_item_id, _session_key = _registered_required_attempt(tmp_path)
    runner = object.__new__(GatewayRunner)
    runner._ledger = lambda: ledger
    runner._background_tasks = set()
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner._adapter_for_source = lambda source: adapter

    assert runner._schedule_incomplete_discord_work_items() == 0
    adapter.handle_message.assert_not_awaited()


def test_required_recovery_suppresses_resume_pending_session(tmp_path):
    ledger, _work_item_id, session_key = _registered_required_attempt(tmp_path)
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="111",
        chat_type="thread",
        thread_id="222",
    )
    entry = SimpleNamespace(
        session_key=session_key,
        resume_pending=True,
        suspended=False,
        origin=source,
        resume_reason="restart_interrupted",
        last_resume_marked_at=None,
        updated_at=None,
    )
    store = SimpleNamespace(
        _lock=threading.Lock(),
        _entries={session_key: entry},
        _ensure_loaded_locked=lambda: None,
        clear_resume_pending=MagicMock(),
    )
    runner = object.__new__(GatewayRunner)
    runner._ledger = lambda: ledger
    runner.session_store = store
    runner._adapter_for_source = lambda source: SimpleNamespace(handle_message=AsyncMock())

    assert runner._schedule_resume_pending_sessions() == 0
    store.clear_resume_pending.assert_called_once_with(session_key)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "expected_calls"),
    [
        (_INTERRUPT_REASON_RESET, 0),
        (_INTERRUPT_REASON_STOP, 1),
    ],
)
async def test_session_boundary_only_stop_cancels_required_work(reason, expected_calls):
    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._invalidate_session_run_generation = MagicMock()
    runner._adapter_for_source = lambda source: None
    runner._release_running_agent_state = MagicMock()
    runner._stop_required_async_for_session = MagicMock()
    source = SessionSource(platform=Platform.DISCORD, chat_id="111", chat_type="dm")

    await runner._interrupt_and_clear_session(
        "session-key",
        source,
        interrupt_reason=reason,
        invalidation_reason="test",
    )

    assert runner._stop_required_async_for_session.call_count == expected_calls


def test_stop_seals_required_attempt_before_scoped_interrupt(tmp_path, monkeypatch):
    ledger, work_item_id, session_key = _registered_required_attempt(tmp_path)
    calls = []

    def interrupt_session(session, **kwargs):
        calls.append((session, kwargs))
        return {"matched": 1, "durable_cancelled": 1, "interrupted": 1}

    monkeypatch.setattr("tools.async_delegation.interrupt_session", interrupt_session)
    runner = object.__new__(GatewayRunner)
    runner._ledger = lambda: ledger
    runner._background_tasks = set()

    assert runner._stop_required_async_for_session(session_key) == 1

    state = ledger.required_async_completion_state(work_item_id)
    assert state["sealed"] is True
    assert calls == [
        (
            session_key,
            {
                "kind": "coding_worker",
                "origin_work_item_id": work_item_id,
                "attempt_id": "boot-a:7",
                "reason": "session_stop",
            },
        )
    ]


@pytest.mark.asyncio
async def test_notification_mode_off_still_wakes_required_reconciliation(tmp_path):
    _reset_async_state()
    ledger, work_item_id, _session_key = _registered_required_attempt(tmp_path)
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._async_completion_seen = set()
    runner._required_async_reconcile_event = asyncio.Event()
    runner._load_background_notifications_mode = lambda: "off"
    runner._ledger = lambda: ledger
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner._adapter_for_source = lambda source: adapter
    process_registry.completion_queue.put(
        _coding_event(
            delegation_id="deleg_required",
            origin_work_item_id=work_item_id,
            origin_run_generation=7,
        )
    )

    async def stop_after_tick():
        while not runner._required_async_reconcile_event.is_set():
            await asyncio.sleep(0)
        runner._running = False

    stopper = asyncio.create_task(stop_after_tick())

    try:
        await runner._async_delegation_watcher(interval=0)
    finally:
        await stopper
        _reset_async_state()

    state = ledger.required_async_completion_state(work_item_id)
    assert state["dispatches"]["deleg_required"]["state"] == "terminal"
    assert runner._required_async_reconcile_event.is_set()
    adapter.handle_message.assert_not_awaited()
