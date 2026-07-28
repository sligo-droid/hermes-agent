import base64
import importlib
import sys
import threading
import types
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult
from gateway.session import SessionSource


_ONE_BY_ONE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO6L2ioAAAAASUVORK5CYII="
)


class CaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []
        self.typing = []

    async def connect(self) -> bool:
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
        return SendResult(success=True, message_id="sent-1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        self.typing.append({"chat_id": chat_id, "metadata": metadata})

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class CaptureQueuedNativeImageAgent:
    calls = []

    def __init__(self, **kwargs):
        self.tools = []
        self.tool_progress_callback = kwargs.get("tool_progress_callback")

    def run_conversation(self, message, conversation_history=None, task_id=None):
        type(self).calls.append(message)
        return {
            "final_response": f"done-{len(type(self).calls)}",
            "messages": [],
            "api_calls": 1,
        }


class CaptureQueuedCacheRebaselineAgent:
    runner = None
    db = None
    session_key = ""
    calls = []
    instances = []
    snapshots = []

    def __init__(self, **kwargs):
        type(self).instances.append(self)
        self.tools = []
        self.session_id = kwargs["session_id"]
        self.model = kwargs["model"]
        self.provider = kwargs.get("provider")
        self.iteration_budget = SimpleNamespace(max_total=0)
        self.max_iterations = 0
        self._last_activity_ts = 0.0
        self._api_call_count = 0
        self._last_activity_desc = ""
        self._last_flushed_db_idx = 0

    def interrupt(self, *_args, **_kwargs):
        return None

    def run_conversation(self, message, conversation_history=None, task_id=None):
        cls = type(self)
        cls.calls.append(message)
        runner = cls.runner
        db = cls.db
        assert runner is not None
        assert db is not None

        if len(cls.calls) == 1:
            row = db.get_session(self.session_id)
            build_count = row.get("message_count", 0) if row else 0
            db.append_message(self.session_id, role="user", content="first")
            db.append_message(self.session_id, role="assistant", content="done-1")
            # The parent implementation creates legacy two-element entries.
            # Normalize that live entry after the first turn so the second turn
            # observes the intended stale count instead of failing on tuple shape.
            with runner._agent_cache_lock:
                cached = runner._agent_cache[cls.session_key]
                if len(cached) < 3:
                    runner._agent_cache[cls.session_key] = (
                        cached[0],
                        cached[1],
                        build_count,
                        self.session_id,
                    )
        else:
            row = db.get_session(self.session_id)
            live_count = row.get("message_count", 0) if row else 0
            with runner._agent_cache_lock:
                cached = runner._agent_cache[cls.session_key]
                cached_count = cached[2] if len(cached) > 2 else None
            cls.snapshots.append((cached_count, live_count))

        return {
            "final_response": f"done-{len(cls.calls)}",
            "messages": [
                *(conversation_history or []),
                {"role": "user", "content": message},
                {"role": "assistant", "content": f"done-{len(cls.calls)}"},
            ],
            "api_calls": 1,
        }


def _make_runner(adapter):
    gateway_run = importlib.import_module("gateway.run")
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {adapter.platform: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._session_db = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        stt_enabled=False,
    )
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    runner._decide_image_input_mode = lambda **_kw: "native"
    return runner


@pytest.mark.asyncio
async def test_queued_followup_uses_pending_event_session_key_for_native_images(monkeypatch, tmp_path):
    CaptureQueuedNativeImageAgent.calls = []

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CaptureQueuedNativeImageAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "***"})

    adapter = CaptureAdapter()
    runner = _make_runner(adapter)

    image_path = tmp_path / "queued-image.png"
    image_path.write_bytes(_ONE_BY_ONE_PNG)

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
    )
    pending_source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
        thread_id="17585",
    )

    adapter._pending_messages["agent:main:telegram:group:-1001"] = MessageEvent(
        text="describe this",
        message_type=MessageType.PHOTO,
        source=pending_source,
        media_urls=[str(image_path)],
        media_types=["image/png"],
        message_id="queued-1",
    )

    result = await runner._run_agent(
        message="hello",
        context_prompt="",
        history=[],
        source=source,
        session_id="sess-native-image-followup",
        session_key="agent:main:telegram:group:-1001",
    )

    assert result["final_response"] == "done-2"
    assert len(CaptureQueuedNativeImageAgent.calls) == 2
    queued_message = CaptureQueuedNativeImageAgent.calls[1]
    assert isinstance(queued_message, list)
    assert queued_message[0]["type"] == "text"
    assert queued_message[0]["text"].startswith("describe this")
    assert any(part.get("type") == "image_url" for part in queued_message)


@pytest.mark.asyncio
async def test_queued_followup_rebaselines_live_cached_agent_before_recursing(
    monkeypatch,
    tmp_path,
):
    from hermes_state import AsyncSessionDB, SessionDB

    CaptureQueuedCacheRebaselineAgent.calls = []
    CaptureQueuedCacheRebaselineAgent.instances = []
    CaptureQueuedCacheRebaselineAgent.snapshots = []

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CaptureQueuedCacheRebaselineAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "***"},
    )
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *_a, **_k: None)

    adapter = CaptureAdapter()
    runner = _make_runner(adapter)
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()

    session_id = "sess-cache-followup"
    session_key = "agent:main:telegram:group:-1001"
    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session(session_id, source="telegram")
    runner._session_db = AsyncSessionDB(db)

    CaptureQueuedCacheRebaselineAgent.runner = runner
    CaptureQueuedCacheRebaselineAgent.db = db
    CaptureQueuedCacheRebaselineAgent.session_key = session_key

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="-1001",
        chat_type="group",
    )
    adapter._pending_messages[session_key] = MessageEvent(
        text="follow up",
        message_type=MessageType.TEXT,
        source=source,
        message_id="queued-cache-1",
    )

    result = await runner._run_agent(
        message="first",
        context_prompt="",
        history=[],
        source=source,
        session_id=session_id,
        session_key=session_key,
    )

    assert result["final_response"] == "done-2"
    assert CaptureQueuedCacheRebaselineAgent.calls == ["first", "follow up"]
    assert len(CaptureQueuedCacheRebaselineAgent.instances) == 1
    assert CaptureQueuedCacheRebaselineAgent.snapshots == [(2, 2)]
    with runner._agent_cache_lock:
        assert session_key in runner._agent_cache
