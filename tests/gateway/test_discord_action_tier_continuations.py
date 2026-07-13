"""Regression coverage for Discord action-tier continuations and footers."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


class _CaptureAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict | None]] = []
        self.events: list[object] = []

    async def send(self, chat_id: str, content: str, metadata=None) -> None:
        self.sent.append((chat_id, content, metadata))

    async def handle_message(self, event) -> None:
        self.events.append(event)


def _discord_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        thread_id="thread-1",
        user_id="user-1",
    )


@pytest.mark.asyncio
async def test_queued_response_footer_uses_its_completed_turn_model() -> None:
    runner = object.__new__(gateway_run.GatewayRunner)
    adapter = _CaptureAdapter()

    await runner._deliver_queued_turn_response(
        adapter=adapter,
        source=_discord_source(),
        response="Terra completed the action.",
        agent_result={
            "model": "gpt-5.6-terra",
            "last_prompt_tokens": 100,
            "context_length": 200,
        },
        user_config={
            "display": {"runtime_footer": {"enabled": True, "fields": ["model"]}}
        },
        cwd="/tmp/project",
        metadata={"thread_id": "thread-1"},
        already_delivered=False,
    )

    expected = "Terra completed the action." + chr(10) * 2 + "gpt-5.6-terra"
    assert adapter.sent == [("thread-1", expected, {"thread_id": "thread-1"})]


@pytest.mark.asyncio
async def test_streamed_queued_response_sends_its_own_trailing_footer() -> None:
    runner = object.__new__(gateway_run.GatewayRunner)
    adapter = _CaptureAdapter()

    await runner._deliver_queued_turn_response(
        adapter=adapter,
        source=_discord_source(),
        response="Terra completed the action.",
        agent_result={"model": "gpt-5.6-terra"},
        user_config={
            "display": {"runtime_footer": {"enabled": True, "fields": ["model"]}}
        },
        cwd="/tmp/project",
        metadata={"thread_id": "thread-1"},
        already_delivered=True,
    )

    assert adapter.sent == [("thread-1", "gpt-5.6-terra", {"thread_id": "thread-1"})]


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


def test_queued_continuation_recovers_discord_action_metadata() -> None:
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
        "channel_prompt": "stale stored prompt",
        "status": "agent_running",
    })
    event = MessageEvent(
        text="Ship the completed build.",
        source=_discord_source(),
        message_id="queued-message",
        channel_prompt="current prompt",
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
    assert event.channel_prompt == "current prompt"
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
    })
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._build_process_event_source = lambda _evt: source
    runner._ledger = lambda: ledger
    runner._load_background_notifications_mode = lambda: "result"

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
