import asyncio
import threading
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource, build_session_key


def _make_source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
    )


def _make_event(text: str) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=_make_source(),
        message_id="m1",
        internal=True,
    )


def _session_entry() -> SessionEntry:
    return SessionEntry(
        session_key=build_session_key(_make_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
        total_tokens=0,
    )


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")}
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = _session_entry()
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._queued_events = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._update_prompt_pending = {}
    runner._busy_input_mode = "interrupt"
    runner._draining = False
    runner._session_run_generation = {}
    runner._session_sources = {}
    runner._pending_native_image_paths_by_session = {}
    runner._background_tasks = {}
    runner._background_task_counter = 0
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._service_tier = None
    runner._fast_mode_by_session = {}
    runner._goal_state_by_session = {}
    runner._goal_runs_in_progress = set()
    runner._goal_queued_by_session = set()
    runner._is_telegram_topic_root_lobby = lambda _source: False
    runner._should_send_telegram_lobby_reminder = lambda _source: False
    runner._check_slash_access = lambda _source, _command: None
    runner._begin_session_run_generation = lambda _key: 1
    runner._release_running_agent_state = (
        lambda key, **_kwargs: runner._running_agents.pop(key, None)
    )
    return runner, adapter


@pytest.mark.asyncio
async def test_work_item_acceptance_does_not_block_event_loop():
    runner, _adapter = _make_runner()
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    accept_thread = None

    def slow_accept(_event, _session_key):
        nonlocal accept_thread
        accept_thread = threading.get_ident()
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=2)
        return None

    async def fake_handle_message_with_agent(event, source, key, generation):
        return {"final_response": "", "messages": []}

    runner._accept_discord_work_item = slow_accept
    runner._handle_message_with_agent = fake_handle_message_with_agent
    task = asyncio.create_task(runner._handle_message(_make_event("hello")))

    await asyncio.wait_for(started.wait(), timeout=1)
    heartbeat = asyncio.Event()
    loop.call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=0.1)
    release.set()
    await task

    assert accept_thread != loop_thread


@pytest.mark.asyncio
async def test_promoted_action_starts_worktree_before_generation_claim(
    tmp_path,
    monkeypatch,
):
    runner, adapter = _make_runner()
    runner.config.platforms = {
        Platform.DISCORD: PlatformConfig(enabled=True, token="***")
    }
    runner.adapters = {Platform.DISCORD: adapter}
    project = tmp_path / "project"
    project.mkdir()
    source = SessionSource(
        platform=Platform.DISCORD,
        user_id="u1",
        chat_id="thread-1",
        user_name="tester",
        chat_type="thread",
        thread_id="thread-1",
        parent_chat_id="parent-1",
        guild_id="guild-1",
        project_path=str(project),
    )
    event = MessageEvent(
        text="build it",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
        discord_runtime_mode="action",
        participates_in_work_lifecycle=True,
        feature_summary={"initial_request": "build it"},
    )
    event._discord_promotion_origin_session_key = build_session_key(source)
    event._discord_promotion_origin_generation = 1
    runner._consume_promoted_replay_fence = AsyncMock(return_value=True)
    runner._hydrate_discord_feature_summary_from_adapter = lambda _event: None
    runner._claim_active_session_slot = lambda *_args: (None, None)
    runner._begin_session_run_generation = lambda _key: 1
    runner._open_start_user_followups = lambda *_args: None
    runner._refresh_active_agent_runtime_status = lambda: None
    runner._release_turn_lease = lambda *_args: None
    captured = {}

    async def handle_with_prefetch(
        _event,
        _source,
        _session_key,
        _generation,
        *,
        action_worktree_task,
    ):
        captured["worktree"] = await action_worktree_task
        return {"final_response": ""}

    runner._handle_message_with_agent = AsyncMock(side_effect=handle_with_prefetch)

    def accept_item(accepted_event, _session_key):
        accepted_event.work_item_id = "work-1"
        accepted_event.discord_pr_generation = 1
        accepted_event._discord_work_item_gateway_config = {}
        return {"id": "work-1", "status": "claimed"}

    runner._accept_discord_work_item = accept_item
    worker_started = threading.Event()
    resolver_release = threading.Event()

    def resolve_worktree(*_args, **_kwargs):
        worker_started.set()
        resolver_release.wait(timeout=2)
        return str(project), None, str(project)

    monkeypatch.setattr(gateway_run, "_resolve_gateway_turn_cwd", resolve_worktree)

    class Ledger:
        def normalize_discord_pr_generation(self, value):
            return int(value)

        def discord_pr_generation(self, _session_key):
            return 1

        def claim(self, *_args, **_kwargs):
            assert worker_started.is_set()
            resolver_release.set()

    runner._ledger = lambda: Ledger()

    await runner._handle_message(event)

    assert runner._handle_message_with_agent.await_count == 1
    worktree_task = runner._handle_message_with_agent.await_args.kwargs[
        "action_worktree_task"
    ]
    assert worktree_task.done()
    assert captured["worktree"] == (str(project), None, str(project))


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_stage", ["worker_start", "generation_claim"])
async def test_promoted_action_startup_cancellation_releases_scoped_state(
    tmp_path,
    monkeypatch,
    cancel_stage,
):
    runner, adapter = _make_runner()
    runner.config.platforms = {
        Platform.DISCORD: PlatformConfig(enabled=True, token="***")
    }
    runner.adapters = {Platform.DISCORD: adapter}
    project = tmp_path / "project"
    project.mkdir()
    source = SessionSource(
        platform=Platform.DISCORD,
        user_id="u1",
        chat_id="thread-1",
        user_name="tester",
        chat_type="thread",
        thread_id="thread-1",
        parent_chat_id="parent-1",
        guild_id="guild-1",
        project_path=str(project),
    )
    session_key = build_session_key(source)
    event = MessageEvent(
        text="build it",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
        discord_runtime_mode="action",
        participates_in_work_lifecycle=True,
        feature_summary={"initial_request": "build it"},
    )
    event._discord_promotion_origin_session_key = session_key
    event._discord_promotion_origin_generation = 1
    runner._consume_promoted_replay_fence = AsyncMock(return_value=True)
    runner._hydrate_discord_feature_summary_from_adapter = lambda _event: None
    runner._claim_active_session_slot = lambda *_args: (MagicMock(), None)
    runner._begin_session_run_generation = lambda _key: 7
    runner._open_start_user_followups = lambda *_args: None
    runner._refresh_active_agent_runtime_status = lambda: None
    runner._release_turn_lease = MagicMock()
    runner._release_running_agent_state = MagicMock(return_value=True)
    runner._handle_message_with_agent = AsyncMock()

    def accept_item(accepted_event, _session_key):
        accepted_event.work_item_id = "work-1"
        accepted_event.discord_pr_generation = 1
        accepted_event._discord_work_item_gateway_config = {}
        return {"id": "work-1", "status": "claimed"}

    runner._accept_discord_work_item = accept_item
    stage_entered = asyncio.Event()
    claim_release = threading.Event()

    def resolve_worktree(*_args, **_kwargs):
        return str(project), None, str(project)

    monkeypatch.setattr(gateway_run, "_resolve_gateway_turn_cwd", resolve_worktree)
    if cancel_stage == "worker_start":
        async def wait_for_worker(_started):
            stage_entered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            gateway_run,
            "_wait_for_action_worktree_worker",
            wait_for_worker,
        )
    else:
        original_to_thread = asyncio.to_thread
        to_thread_calls = 0

        async def controlled_to_thread(func, /, *args, **kwargs):
            nonlocal to_thread_calls
            to_thread_calls += 1
            if to_thread_calls == 3:
                stage_entered.set()
                await asyncio.Event().wait()
            return await original_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(gateway_run.asyncio, "to_thread", controlled_to_thread)

    class Ledger:
        def normalize_discord_pr_generation(self, value):
            return int(value)

        def discord_pr_generation(self, _session_key):
            return 1

        def claim(self, *_args, **_kwargs):
            claim_release.wait(timeout=2)

    ledger = Ledger()
    runner._ledger = lambda: ledger
    task = asyncio.create_task(runner._handle_message(event))
    await asyncio.wait_for(stage_entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    claim_release.set()

    runner._handle_message_with_agent.assert_not_awaited()
    runner._release_running_agent_state.assert_called_once_with(
        session_key,
        run_generation=7,
    )
    runner._release_turn_lease.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("command_text", ["/queue do this next", "/q do this next"])
async def test_idle_queue_sends_payload_as_next_turn(command_text):
    runner, _adapter = _make_runner()
    captured = {}

    async def fake_handle_message_with_agent(event, source, key, generation):
        captured["text"] = event.text
        captured["command"] = event.get_command()
        captured["source"] = source
        captured["key"] = key
        captured["generation"] = generation
        return {"final_response": "", "messages": []}

    runner._handle_message_with_agent = fake_handle_message_with_agent

    result = await runner._handle_message(_make_event(command_text))

    assert result == {"final_response": "", "messages": []}
    assert captured["text"] == "do this next"
    assert captured["command"] is None
    assert captured["source"] == _make_source()
    assert captured["key"] == build_session_key(_make_source())
    assert captured["generation"] == 1
    assert runner._running_agents == {}


@pytest.mark.asyncio
async def test_priority_busy_path_reports_closed_turn_as_delivery_finalization():
    runner, adapter = _make_runner()
    runner._busy_input_mode = "steer"

    event = _make_event("start the manual run")
    event.internal = False
    sk = build_session_key(event.source)
    running_agent = MagicMock()
    running_agent.steer.return_value = False
    running_agent.steer_state.return_value = "closed"
    runner._running_agents[sk] = running_agent

    result = await runner._handle_message(event)

    assert result is not None
    assert "Finishing delivery of the previous response" in result
    assert "starting this as the next turn" in result
    assert adapter._pending_messages[sk] is event


@pytest.mark.asyncio
async def test_idle_queue_without_payload_returns_usage():
    runner, _adapter = _make_runner()
    called = False

    async def fake_handle_message_with_agent(event, source, key, generation):
        nonlocal called
        called = True
        return {"final_response": "", "messages": []}

    runner._handle_message_with_agent = fake_handle_message_with_agent

    result = await runner._handle_message(_make_event("/queue"))

    assert result == "Usage: /queue <prompt>"
    assert called is False
    assert runner._running_agents == {}
