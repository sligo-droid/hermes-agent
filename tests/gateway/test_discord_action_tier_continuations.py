"""Regression coverage for Discord action-tier continuations and footers."""

from __future__ import annotations

import importlib
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionSource


class _CaptureAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []
        self.events: list[object] = []

    async def send(self, chat_id: str, content: str, metadata=None) -> None:
        self.sent.append((chat_id, content, metadata))

    async def handle_message(self, event) -> None:
        self.events.append(event)

    @staticmethod
    def extract_media(content: str):
        lines = content.splitlines()
        media = []
        cleaned = []
        for line in lines:
            if line.startswith("MEDIA:"):
                media.append((line.removeprefix("MEDIA:"), False))
            else:
                cleaned.append(line)
        return media, "\n".join(cleaned).rstrip()

    @staticmethod
    def extract_images(content: str):
        return [], content

    @staticmethod
    def extract_local_files(content: str):
        lines = content.splitlines()
        files = []
        cleaned = []
        for line in lines:
            if line.startswith("/tmp/"):
                files.append(line)
            else:
                cleaned.append(line)
        return files, "\n".join(cleaned).rstrip()


def _discord_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        thread_id="thread-1",
        user_id="user-1",
    )


def test_direct_question_leaves_pending_followup_for_fresh_event_processing() -> None:
    session_key = "agent:main:discord:thread:thread-1"
    followup = MessageEvent(
        text="Okay, let's build this.",
        source=_discord_source(),
        message_id="followup-message",
        discord_action_request_intent=True,
    )
    pending = {session_key: followup}
    adapter = SimpleNamespace(
        get_pending_message=lambda key: pending.pop(key, None),
    )

    recursive = gateway_run._dequeue_pending_event_for_turn(
        adapter,
        session_key,
        False,
    )

    assert recursive is None
    assert pending[session_key] is followup
    fresh_event = adapter.get_pending_message(session_key)
    assert fresh_event is followup
    assert fresh_event.discord_action_request_intent is True


@pytest.mark.asyncio
async def test_queued_response_footer_uses_its_completed_turn_model() -> None:
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._deliver_media_from_response = AsyncMock()
    adapter = _CaptureAdapter()

    await runner._deliver_queued_turn_response(
        adapter=adapter,
        source=_discord_source(),
        response="Terra completed the action.",
        agent_result={
            "model": "gpt-5.6-terra",
            "last_prompt_tokens": 100,
            "context_length": 200,
            "reasoning_effort": "xhigh",
        },
        user_config={
            "display": {"runtime_footer": {"enabled": True, "fields": ["model", "reasoning"]}}
        },
        cwd="/tmp/project",
        metadata={"thread_id": "thread-1"},
        already_delivered=False,
    )

    expected = "Terra completed the action." + chr(10) * 2 + "gpt-5.6-terra · xhigh"
    assert adapter.sent == [("thread-1", expected, {"thread_id": "thread-1"})]
    runner._deliver_media_from_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_queued_response_delivers_media_without_leaking_local_paths() -> None:
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._deliver_media_from_response = AsyncMock()
    adapter = _CaptureAdapter()

    await runner._deliver_queued_turn_response(
        adapter=adapter,
        source=_discord_source(),
        response=(
            "Completed.\n\n"
            "What changed:\n- Added the Confidence tab.\n\n"
            "Verification:\n- Visual QA passed.\n"
            "MEDIA:/tmp/qa/desktop.png\n"
            "MEDIA:/tmp/qa/mobile.png"
        ),
        agent_result={"model": "gpt-5.6-terra", "reasoning_effort": "none"},
        user_config={"display": {"runtime_footer": {"enabled": False}}},
        cwd="/tmp/project",
        metadata={"thread_id": "thread-1"},
        already_delivered=False,
    )

    assert adapter.sent == [
        (
            "thread-1",
            "Completed.\n\nWhat changed:\n- Added the Confidence tab.\n\n"
            "Verification:\n- Visual QA passed.",
            {"thread_id": "thread-1"},
        )
    ]
    delivered_response = runner._deliver_media_from_response.await_args.args[0]
    assert delivered_response.endswith("MEDIA:/tmp/qa/mobile.png")


@pytest.mark.asyncio
async def test_queued_response_delivers_bare_local_file_without_empty_text() -> None:
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._deliver_media_from_response = AsyncMock()
    adapter = _CaptureAdapter()

    await runner._deliver_queued_turn_response(
        adapter=adapter,
        source=_discord_source(),
        response="/tmp/qa/desktop.png",
        agent_result={"model": "gpt-5.6-terra", "reasoning_effort": "none"},
        user_config={"display": {"runtime_footer": {"enabled": False}}},
        cwd="/tmp/project",
        metadata={"thread_id": "thread-1"},
        already_delivered=False,
    )

    assert adapter.sent == []
    runner._deliver_media_from_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_media_only_queued_response_sends_runtime_footer() -> None:
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._deliver_media_from_response = AsyncMock()
    adapter = _CaptureAdapter()

    await runner._deliver_queued_turn_response(
        adapter=adapter,
        source=_discord_source(),
        response="MEDIA:/tmp/qa/desktop.png",
        agent_result={"model": "gpt-5.6-terra", "reasoning_effort": "xhigh"},
        user_config={
            "display": {"runtime_footer": {"enabled": True, "fields": ["model", "reasoning"]}}
        },
        cwd="/tmp/project",
        metadata={"thread_id": "thread-1"},
        already_delivered=False,
    )

    assert adapter.sent == [
        ("thread-1", "gpt-5.6-terra · xhigh", {"thread_id": "thread-1"})
    ]
    runner._deliver_media_from_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_queued_image_delivery_preserves_visible_url() -> None:
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._deliver_media_from_response = AsyncMock(
        return_value=SendResult(
            success=False,
            error="rate limited",
            retryable=True,
            retry_safe=True,
        )
    )
    adapter = _CaptureAdapter()
    adapter.extract_images = BasePlatformAdapter.extract_images

    await runner._deliver_queued_turn_response(
        adapter=adapter,
        source=_discord_source(),
        response="Chart: ![confidence](https://example.com/confidence.png)",
        agent_result={"model": "gpt-5.6-terra", "reasoning_effort": "none"},
        user_config={"display": {"runtime_footer": {"enabled": False}}},
        cwd="/tmp/project",
        metadata={"thread_id": "thread-1"},
        already_delivered=False,
    )

    assert adapter.sent == [
        (
            "thread-1",
            "Chart: ![confidence](https://example.com/confidence.png)",
            {"thread_id": "thread-1"},
        )
    ]


@pytest.mark.asyncio
async def test_streamed_queued_response_sends_its_own_trailing_footer() -> None:
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._deliver_media_from_response = AsyncMock()
    adapter = _CaptureAdapter()

    await runner._deliver_queued_turn_response(
        adapter=adapter,
        source=_discord_source(),
        response="Terra completed the action.",
        agent_result={"model": "gpt-5.6-terra", "reasoning_effort": "none"},
        user_config={
            "display": {"runtime_footer": {"enabled": True, "fields": ["model", "reasoning"]}}
        },
        cwd="/tmp/project",
        metadata={"thread_id": "thread-1"},
        already_delivered=True,
    )

    assert adapter.sent == [("thread-1", "gpt-5.6-terra · none", {"thread_id": "thread-1"})]
    runner._deliver_media_from_response.assert_awaited_once()


@pytest.mark.parametrize(
    ("reasoning_config", "expected"),
    [
        ({"enabled": True, "effort": "high"}, "high"),
        ({"enabled": False}, "none"),
        (None, ""),
    ],
)
def test_reasoning_effort_footer_label(reasoning_config, expected) -> None:
    assert gateway_run._reasoning_effort_footer_label(reasoning_config) == expected


class _ProcessRegistry:
    def __init__(self, session) -> None:
        self.session = session

    def get(self, _session_id: str):
        return self.session

    def is_completion_consumed(self, _session_id: str) -> bool:
        return False


class _Ledger:
    def __init__(self, item: dict) -> None:
        self.item = item

    def id_for_event(self, event, session_key: str) -> str:
        assert event.message_id == "root-message"
        assert session_key == "agent:main:discord:thread:thread-1"
        return "work-1"

    def get(self, work_id: str):
        assert work_id == "work-1"
        return self.item


class _SessionFallbackLedger:
    def __init__(self, item: dict) -> None:
        self.item = item

    def id_for_event(self, _event, _session_key: str):
        return None

    def get(self, _work_id: str):
        return None

    def incomplete_items(self):
        return [self.item]


def test_queued_continuation_recovers_structural_metadata_with_legacy_intent() -> None:
    feature_summary = {
        "initial_request": "Deploy the dashboard",
        "kanban_board": None,
    }
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._ledger = lambda: _SessionFallbackLedger({
        "id": "work-1",
        "session_key": "agent:main:discord:thread:thread-1",
        "platform": "discord",
        "feature_summary": feature_summary,
        "project_summary": {"title": "Dashboard"},
        "channel_prompt": "Project instructions",
        "status": "agent_running",
    })
    event = MessageEvent(
        text="Ship the completed build.",
        source=_discord_source(),
        message_id="queued-message",
    )

    runner._hydrate_discord_continuation_event_from_work_item(
        event,
        "agent:main:discord:thread:thread-1",
        allow_session_fallback=True,
    )

    assert event.feature_summary == feature_summary
    assert event.project_summary == {"title": "Dashboard"}
    assert event.work_item_id == "work-1"
    assert event.message_id == "queued-message"
    assert event.channel_prompt == "Project instructions"
    assert event.discord_runtime_mode == "action"
    assert event.discord_action_request_intent is None
    assert event.discord_action_request_base_channel_prompt is None
    assert gateway_run._is_standard_discord_action_request(
        event.source,
        event.feature_summary,
    )


@pytest.mark.asyncio
async def test_process_completion_inherits_discord_action_metadata(monkeypatch) -> None:
    source = _discord_source()
    adapter = _CaptureAdapter()
    feature_summary = {
        "initial_request": "Deploy the dashboard",
        "kanban_board": None,
    }
    ledger = _Ledger({
        "id": "work-1",
        "message_id": "root-message",
        "feature_summary": feature_summary,
        "project_summary": {"title": "Dashboard"},
        "channel_prompt": "Project instructions",
    })
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._build_process_event_source = lambda _evt: source
    runner._ledger = lambda: ledger
    runner._load_background_notifications_mode = lambda: "result"
    runner._completion_delivery_lock = threading.Lock()
    runner._completion_deliveries_inflight = set()
    runner._completion_deliveries_delivered = set()

    process_registry_module = importlib.import_module("tools.process_registry")
    monkeypatch.setattr(
        process_registry_module,
        "process_registry",
        _ProcessRegistry(
            SimpleNamespace(
                exited=True,
                exit_code=0,
                command="pnpm install",
                output_buffer="done",
            )
        ),
    )

    async def _no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(gateway_run.asyncio, "sleep", _no_sleep)

    await runner._run_process_watcher({
        "session_id": "proc-1",
        "session_key": "agent:main:discord:thread:thread-1",
        "check_interval": 0,
        "platform": "discord",
        "chat_id": "thread-1",
        "thread_id": "thread-1",
        "user_id": "user-1",
        "user_name": "Tester",
        "message_id": "root-message",
        "notify_on_complete": True,
    })

    assert len(adapter.events) == 1
    event = adapter.events[0]
    assert event.feature_summary == feature_summary
    assert event.project_summary == {"title": "Dashboard"}
    assert event.work_item_id == "work-1"
    assert event.work_replay is True
    assert event.discord_runtime_mode == "action"
    assert event.discord_action_request_intent is None
    assert event.channel_prompt == "Project instructions"
    assert event.discord_action_request_base_channel_prompt is None


def test_internal_restart_event_does_not_copy_stored_false_intent() -> None:
    event = MessageEvent(
        text="Continue after restart.",
        source=_discord_source(),
        internal=True,
    )

    gateway_run.GatewayRunner._hydrate_discord_resume_event_from_work_item(
        event,
        {
            "id": "work-1",
            "feature_summary": {
                "initial_request": "Deploy the dashboard",
                "kanban_board": None,
            },
            "channel_prompt": "Project instructions",
        },
    )

    assert event.feature_summary["initial_request"] == "Deploy the dashboard"
    assert event.discord_runtime_mode == "action"
    assert event.discord_action_request_intent is None
    assert event.channel_prompt == "Project instructions"
    assert event.discord_action_request_base_channel_prompt is None
