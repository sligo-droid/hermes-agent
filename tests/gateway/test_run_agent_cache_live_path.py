"""Live ``GatewayRunner._run_agent`` cache-coherence regressions."""

import importlib
import sys
import threading
import types
from collections import OrderedDict
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource, SessionStore
from hermes_state import AsyncSessionDB, SessionDB


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None) -> SendResult:
        return SendResult(success=True, message_id="sent-1")

    async def send_typing(self, chat_id, metadata=None) -> None:
        return None

    async def stop_typing(self, chat_id) -> None:
        return None

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


class _Agent:
    instances = []
    calls = []
    effects = []
    rotations = {}

    @classmethod
    def reset(cls):
        cls.instances = []
        cls.calls = []
        cls.effects = []
        cls.rotations = {}

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

    def release_clients(self):
        return None

    def run_conversation(self, message, conversation_history=None, task_id=None):
        if isinstance(message, str) and message in type(self).rotations:
            self.session_id = type(self).rotations[message]
        type(self).calls.append((self, task_id, message))
        type(self).effects.append((self.session_id, task_id, message))
        return {
            "final_response": f"done-{len(type(self).calls)}",
            "messages": [
                *(conversation_history or []),
                {"role": "user", "content": message},
                {"role": "assistant", "content": "done"},
            ],
            "api_calls": 1,
        }


class _SessionStore:
    def __init__(self, db):
        self._db = db
        self._entries = {}

    def _is_session_ended_in_db(self, session_id):
        row = self._db.get_session(session_id)
        return bool(row is not None and row.get("end_reason") is not None)


def _source():
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="user-1",
        chat_type="dm",
    )


def _make_runner(monkeypatch, tmp_path, db, *, session_store=None):
    _Agent.reset()

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _Agent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)

    gateway_run = importlib.import_module("gateway.run")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"api_key": "***"},
    )
    monkeypatch.setattr("agent.title_generator.maybe_auto_title", lambda *_a, **_k: None)
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")

    adapter = _Adapter()
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._session_run_generation = {}
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    runner._session_db = AsyncSessionDB(db)
    runner.session_store = session_store or _SessionStore(db)
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(
        thread_sessions_per_user=False,
        group_sessions_per_user=False,
        multiplex_profiles=False,
        stt_enabled=False,
    )
    runner._model = "openai/gpt-4.1-mini"
    runner._base_url = None
    runner._decide_image_input_mode = lambda **_kw: "native"
    return runner, adapter


async def _run_turn(runner, *, session_id, session_key, message):
    return await runner._run_agent(
        message=message,
        context_prompt="",
        history=[],
        source=_source(),
        session_id=session_id,
        session_key=session_key,
    )


@pytest.mark.asyncio
async def test_live_cache_rebuilds_when_cached_agent_owns_old_session(
    monkeypatch,
    tmp_path,
):
    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session("session-b", source="telegram")
    store = SessionStore(
        sessions_dir=tmp_path / "sessions",
        config=GatewayConfig(),
    )
    store._db = db
    store._loaded = True
    source = _source()
    old_entry = store.get_or_create_session(source)
    runner, _adapter = _make_runner(
        monkeypatch,
        tmp_path,
        db,
        session_store=store,
    )
    session_key = runner._session_key_for_source(source)
    assert old_entry.session_key == session_key

    await _run_turn(
        runner,
        session_id=old_entry.session_id,
        session_key=session_key,
        message="from a",
    )

    def _fail_outgoing_end(*_args, **_kwargs):
        raise RuntimeError("outgoing DB end failed")

    monkeypatch.setattr(db, "promote_to_session_reset", _fail_outgoing_end)
    switched = store.switch_session(session_key, "session-b")
    assert switched is not None
    assert switched.session_id == "session-b"
    assert db.get_session(old_entry.session_id)["end_reason"] is None

    await _run_turn(
        runner,
        session_id="session-b",
        session_key=session_key,
        message="from b",
    )

    assert len(_Agent.instances) == 2
    assert _Agent.effects == [
        (old_entry.session_id, old_entry.session_id, "from a"),
        ("session-b", "session-b", "from b"),
    ]
    with runner._agent_cache_lock:
        cached = runner._agent_cache[session_key]
    assert cached[0] is _Agent.instances[1]
    assert cached[3] == "session-b"


@pytest.mark.asyncio
async def test_live_cache_reuses_agent_already_rotated_to_incoming_session(
    monkeypatch,
    tmp_path,
):
    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session("session-a", source="telegram")
    db.create_session("session-b", source="telegram")
    runner, _adapter = _make_runner(monkeypatch, tmp_path, db)
    session_key = runner._session_key_for_source(_source())
    _Agent.rotations = {"rotate to b": "session-b"}

    first = await _run_turn(
        runner,
        session_id="session-a",
        session_key=session_key,
        message="rotate to b",
    )
    rotated_agent = _Agent.instances[0]
    assert first["session_id"] == "session-b"

    await _run_turn(
        runner,
        session_id="session-b",
        session_key=session_key,
        message="continue in b",
    )

    assert len(_Agent.instances) == 1
    assert [call[0] for call in _Agent.calls] == [rotated_agent, rotated_agent]
    assert _Agent.effects == [
        ("session-b", "session-a", "rotate to b"),
        ("session-b", "session-b", "continue in b"),
    ]
    with runner._agent_cache_lock:
        cached = runner._agent_cache[session_key]
    assert cached[0] is rotated_agent
    assert cached[2] == 0
    assert cached[3] == "session-b"


@pytest.mark.asyncio
async def test_live_rotated_agent_queued_followup_uses_effective_session(
    monkeypatch,
    tmp_path,
):
    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session("session-a", source="telegram")
    db.create_session("session-b", source="telegram")
    runner, adapter = _make_runner(monkeypatch, tmp_path, db)
    source = _source()
    session_key = runner._session_key_for_source(source)
    _Agent.rotations = {"rotate before queue": "session-b"}
    adapter._pending_messages[session_key] = MessageEvent(
        text="queued in b",
        message_type=MessageType.TEXT,
        source=source,
        message_id="queued-rotation-1",
    )

    result = await _run_turn(
        runner,
        session_id="session-a",
        session_key=session_key,
        message="rotate before queue",
    )

    assert result["session_id"] == "session-b"
    assert len(_Agent.instances) == 1
    assert _Agent.effects == [
        ("session-b", "session-a", "rotate before queue"),
        ("session-b", "session-b", "queued in b"),
    ]
    with runner._agent_cache_lock:
        cached = runner._agent_cache[session_key]
    assert cached[0] is _Agent.instances[0]
    assert cached[2] == 0
    assert cached[3] == "session-b"


@pytest.mark.asyncio
async def test_live_cache_evicts_ended_session_after_routing_self_heal(
    monkeypatch,
    tmp_path,
):
    db = SessionDB(db_path=tmp_path / "sessions.db")
    store = SessionStore(
        sessions_dir=tmp_path / "sessions",
        config=GatewayConfig(),
    )
    store._db = db
    store._loaded = True
    source = _source()
    dead_entry = store.get_or_create_session(source)
    runner, _adapter = _make_runner(
        monkeypatch,
        tmp_path,
        db,
        session_store=store,
    )
    session_key = runner._session_key_for_source(source)

    await _run_turn(
        runner,
        session_id=dead_entry.session_id,
        session_key=session_key,
        message="before self-heal",
    )
    dead_agent = _Agent.instances[0]
    db.end_session(dead_entry.session_id, "user_requested")
    fresh_entry = store.get_or_create_session(source)
    assert fresh_entry.session_id != dead_entry.session_id

    await _run_turn(
        runner,
        session_id=fresh_entry.session_id,
        session_key=session_key,
        message="after self-heal",
    )

    assert len(_Agent.instances) == 2
    with runner._agent_cache_lock:
        cached = runner._agent_cache[session_key]
    assert cached[0] is not dead_agent
    assert cached[3] == fresh_entry.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entry_size", "expect_invalidation"),
    [(2, False), (3, True), (4, True)],
    ids=["legacy-two", "legacy-three", "counted-four"],
)
async def test_live_cache_entry_shapes_and_external_invalidation_cleanup(
    monkeypatch,
    tmp_path,
    entry_size,
    expect_invalidation,
):
    db = SessionDB(db_path=tmp_path / "sessions.db")
    db.create_session("session-1", source="telegram")
    runner, _adapter = _make_runner(monkeypatch, tmp_path, db)
    session_key = runner._session_key_for_source(_source())

    await _run_turn(
        runner,
        session_id="session-1",
        session_key=session_key,
        message="build cache",
    )
    original_agent = _Agent.instances[0]
    with runner._agent_cache_lock:
        counted = runner._agent_cache[session_key]
        assert len(counted) == 4
        runner._agent_cache[session_key] = counted[:entry_size]

    # Current code only creates four-element entries. Two- and three-element
    # entries are transition compatibility states, so seed their documented
    # shapes at the cache boundary and execute the real lookup/invalidation path.
    db.append_message("session-1", role="user", content="external process write")
    cleanup_event = threading.Event()
    cleanup_observations = []
    caller_thread = threading.get_ident()

    def _release_evicted(agent):
        acquired = runner._agent_cache_lock.acquire(blocking=False)
        if acquired:
            runner._agent_cache_lock.release()
        cleanup_observations.append(
            (agent, acquired, threading.get_ident() != caller_thread)
        )
        cleanup_event.set()

    runner._release_evicted_agent_soft = _release_evicted
    await _run_turn(
        runner,
        session_id="session-1",
        session_key=session_key,
        message="after external write",
    )

    if expect_invalidation:
        assert len(_Agent.instances) == 2
        assert cleanup_event.wait(2), "soft cleanup thread did not run"
        assert cleanup_observations == [(original_agent, True, True)]
        with runner._agent_cache_lock:
            rebuilt = runner._agent_cache[session_key]
        assert len(rebuilt) == 4
        assert rebuilt[0] is not original_agent
        assert rebuilt[2] == 1
        assert rebuilt[3] == "session-1"
    else:
        assert len(_Agent.instances) == 1
        assert not cleanup_event.is_set()
        with runner._agent_cache_lock:
            cached = runner._agent_cache[session_key]
        assert len(cached) == 2
        assert cached[0] is original_agent
