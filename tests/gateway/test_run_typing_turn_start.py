"""Gateway turn-start typing lifecycle tests."""

import sys
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig, StreamingConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.session import SessionEntry, SessionSource


SESSION_KEY = "agent:main:discord:thread:parent-channel:thread-123"


class _TypingCaptureAdapter(BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = True

    def __init__(self, events):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.DISCORD)
        self.events = events
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        self.sent.append(
            {
                "chat_id": chat_id,
                "content": content,
                "reply_to": reply_to,
                "metadata": metadata,
            }
        )
        return SendResult(success=True, message_id="message-1")

    async def edit_message(self, chat_id, message_id, content) -> SendResult:
        return SendResult(success=True, message_id=message_id)

    async def send_typing(self, chat_id, metadata=None) -> None:
        self.events.append(("typing-start", chat_id, metadata))

    async def stop_typing(self, chat_id) -> None:
        self.events.append(("typing-stop", chat_id))

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _agent_class(events, *, fail: bool):
    class _ToollessAgent:
        def __init__(self, **kwargs):
            self.tools = []
            self.session_id = kwargs["session_id"]
            self.model = kwargs["model"]
            self.provider = kwargs.get("provider")
            self.context_compressor = SimpleNamespace(
                last_prompt_tokens=0,
                context_length=200_000,
            )
            self.session_prompt_tokens = 0
            self.session_completion_tokens = 0

        def run_conversation(
            self,
            message,
            conversation_history=None,
            task_id=None,
            **_kwargs,
        ):
            events.append(("agent-call",))
            if fail:
                raise RuntimeError("simulated turn failure")
            return {
                "final_response": "done",
                "messages": [
                    *(conversation_history or []),
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": "done"},
                ],
                "api_calls": 1,
            }

        def interrupt(self, *_args, **_kwargs) -> None:
            return None

    return _ToollessAgent


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="parent-channel",
        chat_type="thread",
        thread_id="thread-123",
        user_id="user-1",
    )


def _event() -> MessageEvent:
    return MessageEvent(
        text="continue",
        source=_source(),
        message_id="inbound-1",
    )


def _runner(monkeypatch, tmp_path, adapter, agent_cls):
    (tmp_path / "config.yaml").write_text(
        "display:\n"
        "  tool_progress: off\n"
        "  interim_assistant_messages: false\n"
        "  streaming: false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")
    monkeypatch.setenv("DISCORD_HOME_CHANNEL", "parent-channel")

    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(gateway_run.asyncio, "to_thread", _run_inline)

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "fake"},
    )

    config = GatewayConfig(
        sessions_dir=tmp_path / "sessions",
        streaming=StreamingConfig(enabled=False, transport="off"),
    )
    runner = gateway_run.GatewayRunner(config)
    runner.adapters = {Platform.DISCORD: adapter}
    runner.delivery_router.adapters = runner.adapters
    runner._session_db = None
    runner._recover_telegram_topic_thread_id = lambda _source: None
    runner._cache_session_source = lambda _key, _source: None
    runner._is_session_run_current = lambda _key, _generation: True
    runner._reply_anchor_for_event = lambda event: event.message_id
    runner._get_guild_id = lambda _event: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._get_proxy_url = lambda: None
    runner.hooks = SimpleNamespace(loaded_hooks=False, emit=AsyncMock())
    runner._session_run_generation[SESSION_KEY] = 1

    session_entry = SessionEntry(
        session_key=SESSION_KEY,
        session_id="session-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type="thread",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = [
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "earlier response"},
    ]
    runner.session_store.has_platform_message_id.return_value = False
    runner.session_store._entries = {SESSION_KEY: session_entry}
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True], ids=["success", "error"])
async def test_turn_start_typing_precedes_agent_and_stops_on_exit(
    monkeypatch,
    tmp_path,
    fail,
):
    events = []
    adapter = _TypingCaptureAdapter(events)
    runner = _runner(
        monkeypatch,
        tmp_path,
        adapter,
        _agent_class(events, fail=fail),
    )

    response = await runner._handle_message_with_agent(
        _event(),
        _source(),
        SESSION_KEY,
        1,
    )

    assert events == [
        ("typing-start", "parent-channel", {"thread_id": "thread-123"}),
        ("agent-call",),
        ("typing-stop", "parent-channel"),
    ]
    assert adapter.sent == []
    if fail:
        assert "simulated turn failure" in response
    else:
        assert response == "done"
