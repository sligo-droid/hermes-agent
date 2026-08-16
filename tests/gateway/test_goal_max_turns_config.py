import json
import os
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals


def _session_id() -> str:
    raw = os.environ.get("PYTEST_CURRENT_TEST", "default")
    safe = "".join(ch if ch.isalnum() else "-" for ch in raw)
    return f"sid-gateway-goal-config-{safe[:160]}"


class _FakeSessionEntry:
    @property
    def session_id(self) -> str:
        return _session_id()

    @property
    def session_key(self) -> str:
        return "agent:main:discord:channel:goal-config"


class _FakeSessionStore:
    def __init__(self):
        self.entry = _FakeSessionEntry()

    def get_or_create_session(self, source, **_kwargs):
        return self.entry

    def _generate_session_key(self, source):
        return "agent:main:discord:channel:goal-config"


class _FakeAdapter:
    def __init__(self):
        self._pending_messages = {}


def _make_discord_thread_event(text: str, *, thread_id: str = "thread-100") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="parent-channel",
            chat_type="thread",
            thread_id=thread_id,
            parent_chat_id="parent-channel",
            user_id="user-goal-config",
        ),
        message_id=f"msg-{thread_id}",
    )


def _make_discord_parent_event(text: str, *, channel_id: str = "parent-channel") -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id=channel_id,
            chat_type="group",
            user_id="user-goal-config",
        ),
        message_id=f"msg-{channel_id}",
    )


@pytest.mark.asyncio
async def test_gateway_goal_uses_goals_max_turns_from_full_config(tmp_path, monkeypatch):
    """Gateway /goal should honor top-level goals.max_turns from config.yaml."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("goals:\n  max_turns: 7\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    goals.GoalManager(_session_id()).clear()

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}

    event = MessageEvent(
        text="/goal ship the benchmark",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-goal-config",
            chat_type="channel",
            user_id="user-goal-config",
        ),
        message_id="msg-goal-config",
    )

    response = await GatewayRunner._handle_goal_command(runner, event)

    try:
        assert "⊙ Goal set (7-turn budget): ship the benchmark" in response
        state = goals.GoalManager(_session_id()).state
        assert state is not None
        assert state.max_turns == 7
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_gateway_goal_kickoff_wraps_nested_slash_body(tmp_path, monkeypatch):
    """A /goal body starting with /subgoal must queue a normal agent turn."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "hermes_cli.discord_worker_boards.board_for_gateway_event",
        lambda *args, **kwargs: None,
    )
    goals._DB_CACHE.clear()

    adapter = _FakeAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._queued_events = {}

    goal_body = "/subgoal inspect the PID logs\nThen implement the smallest fix"
    event = MessageEvent(
        text=f"/goal {goal_body}",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-goal-config",
            chat_type="channel",
            user_id="user-goal-config",
        ),
        message_id="msg-goal-config",
    )

    response = await GatewayRunner._handle_goal_command(runner, event)

    try:
        assert "⊙ Goal set (20-turn budget): Then implement the smallest fix" in response
        state = goals.GoalManager(_session_id()).state
        assert state is not None
        assert state.goal == "Then implement the smallest fix"
        assert state.subgoals == ["inspect the PID logs"]
        assert len(adapter._pending_messages) == 1
        queued = next(iter(adapter._pending_messages.values()))
        assert not queued.text.startswith("/")
        assert "Goal: Then implement the smallest fix" in queued.text
        assert "Additional criteria" in queued.text
        assert "1. inspect the PID logs" in queued.text
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_native_goal_kickoff_includes_thread_context_without_changing_goal(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "hermes_cli.discord_worker_boards.board_for_gateway_event",
        lambda *args, **kwargs: None,
    )
    goals._DB_CACHE.clear()

    adapter = _FakeAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._queued_events = {}

    event = MessageEvent(
        text="/goal Ship the dashboard",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-goal-config",
            chat_type="channel",
            user_id="user-goal-config",
        ),
        message_id="msg-goal-config",
    )
    event.native_slash_command = True
    event.goal_thread_context = "[Expanded Discord thread plan]\n## Plan\nUse the linked plan."

    response = await GatewayRunner._handle_goal_command(runner, event)

    try:
        assert "⊙ Goal set" in response
        state = goals.GoalManager(_session_id()).state
        assert state is not None
        assert state.goal == "Ship the dashboard"
        queued = next(iter(adapter._pending_messages.values()))
        assert "[Discord goal thread context]" in queued.text
        assert "Use the linked plan" in queued.text
        assert "Use the linked plan" not in state.goal
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_discord_subgoal_without_board_creates_dev_ticket(tmp_path, monkeypatch):
    kanban_home = tmp_path / "kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )

    event = MessageEvent(
        text="/subgoal Add regression tests",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="parent-channel",
            chat_type="thread",
            thread_id="thread-100",
            parent_chat_id="parent-channel",
            user_id="user-goal-config",
        ),
        message_id="msg-subgoal",
    )

    response = await GatewayRunner._handle_subgoal_command(runner, event)

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.board_slug_for_discord_thread("thread-100")
    meta = kanban_db.read_board_metadata(board)
    worker = meta["discord_worker"]
    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()

    assert response == "Added subgoal 1: Add regression tests"
    assert worker["execution_mode"] == "kanban_pipeline"
    assert worker["goal_status"] == "active"
    assert len(tasks) == 1
    assert tasks[0].assignee == "dev"


@pytest.mark.asyncio
async def test_discord_feature_summary_goal_set_suppresses_board_ack(tmp_path, monkeypatch):
    kanban_home = tmp_path / "kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://kanban.example")

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )

    event = MessageEvent(
        text="/goal Ship the dashboard",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="1506523962190860318",
            chat_type="thread",
            thread_id="1506523962190860318",
            parent_chat_id="parent-channel",
            user_id="user-goal-config",
        ),
        message_id="msg-goal",
        feature_summary={
            "thread_id": "1506523962190860318",
            "message_id": "summary-message",
            "source_message_id": "msg-goal",
            "kanban_board": {
                "slug": "discord-1506523962190860318-m-msg-goal",
                "public_url": "https://kanban.example/workers/discord-1506523962190860318-m-msg-goal",
            },
        },
    )

    response = await GatewayRunner._handle_goal_command(runner, event)

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.board_slug_for_discord_request("1506523962190860318", "msg-goal")
    meta = kanban_db.read_board_metadata(board)

    assert response is None
    assert meta["discord_worker"]["root_goal"] == "Ship the dashboard"


@pytest.mark.asyncio
async def test_discord_feature_summary_goal_set_syncs_existing_summary(tmp_path, monkeypatch):
    kanban_home = tmp_path / "kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://kanban.example")
    from hermes_cli import discord_worker_boards as dwb

    stale = dwb.set_goal(
        thread_id="1507755696501030933",
        goal="The previous thread goal",
        guild_id="guild-1",
        parent_channel_id="parent-channel",
    )
    dwb.set_feature_summary_title(stale.slug, "Stale generated title")

    adapter = _FakeAdapter()
    adapter.sync_kanban_feature_summary = AsyncMock(return_value="sync-1")

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.adapters = {Platform.DISCORD: adapter}

    event = MessageEvent(
        text="/goal Ship the dashboard",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="parent-channel",
            chat_type="thread",
            thread_id="1507755696501030933",
            parent_chat_id="parent-channel",
            guild_id="guild-1",
            user_id="user-goal-config",
        ),
        message_id="msg-goal",
        feature_summary={
            "thread_id": "1507755696501030933",
            "message_id": "summary-message",
            "source_message_id": "msg-goal",
            "initial_request": "/goal Ship the dashboard",
            "kanban_board": None,
        },
    )

    response = await GatewayRunner._handle_goal_command(runner, event)

    assert response is None
    adapter.sync_kanban_feature_summary.assert_awaited_once()
    target = adapter.sync_kanban_feature_summary.await_args.args[0]
    assert target["board"] == "discord-1507755696501030933-m-msg-goal"
    assert target["thread_id"] == "1507755696501030933"
    assert target["guild_id"] == "guild-1"
    assert target["parent_channel_id"] == "parent-channel"
    assert target["message_id"] == "summary-message"
    assert target["source_message_id"] == "msg-goal"
    assert target["title"] == "Ship the dashboard"
    assert target["public_url"] == "https://kanban.example/workers/discord-1507755696501030933-m-msg-goal"


@pytest.mark.asyncio
async def test_discord_goal_set_passes_thread_context_to_planner(tmp_path, monkeypatch):
    kanban_home = tmp_path / "kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner._signal_kanban_dispatcher_dirty = lambda: True
    runner._log_gateway_flow_telemetry = lambda **kwargs: None

    event = _make_discord_thread_event("/goal Ship the dashboard", thread_id="thread-context")
    event.goal_thread_context = "[Goal thread context]\n[Alice] important prior detail"

    response = await GatewayRunner._handle_goal_command(runner, event)

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.board_slug_for_discord_request("thread-context", "msg-thread-context")
    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()

    assert response is not None
    payload = json.loads(tasks[0].body or "{}")
    assert payload["discord_thread_context"] == event.goal_thread_context


@pytest.mark.asyncio
async def test_discord_goal_set_signals_dirty_dispatch(tmp_path, monkeypatch):
    kanban_home = tmp_path / "kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))

    signals = []

    def fake_signal(self):
        signals.append(True)
        return True

    monkeypatch.setattr(GatewayRunner, "_signal_kanban_dispatcher_dirty", fake_signal)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )

    response = await GatewayRunner._handle_goal_command(
        runner,
        _make_discord_thread_event("/goal Ship the dashboard", thread_id="thread-set"),
    )

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.board_slug_for_discord_request("thread-set", "msg-thread-set")
    meta = kanban_db.read_board_metadata(board)

    assert response is not None
    assert response.startswith("Kanban goal set. Board: ")
    assert response.endswith("/workers/discord-thread-set-m-msg-thread-set")
    assert meta["discord_worker"]["root_goal"] == "Ship the dashboard"
    assert signals == [True]


@pytest.mark.asyncio
async def test_native_discord_goal_slash_uses_hermes_loop_not_kanban(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    kanban_home = tmp_path / "kanban-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    goals._DB_CACHE.clear()
    goals.GoalManager(_session_id()).clear()

    board_lookup_calls = []

    def fake_board_for_gateway_event(*args, **kwargs):
        board_lookup_calls.append((args, kwargs))
        return None

    monkeypatch.setattr(
        "hermes_cli.discord_worker_boards.board_for_gateway_event",
        fake_board_for_gateway_event,
    )

    adapter = _FakeAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()  # type: ignore[assignment]
    runner.adapters = {Platform.DISCORD: adapter}  # type: ignore[assignment]
    runner._queued_events = {}
    runner._log_gateway_flow_telemetry = lambda **kwargs: None

    event = _make_discord_thread_event("/goal Ship the dashboard", thread_id="thread-native")
    event.native_slash_command = True

    try:
        response = await GatewayRunner._handle_goal_command(runner, event)

        from hermes_cli import discord_worker_boards as dwb
        from hermes_cli import kanban_db

        board = dwb.board_slug_for_discord_request("thread-native", "msg-thread-native")
        session_key = runner._session_key_for_source(event.source)

        assert response is not None
        assert board_lookup_calls == []
        assert "⊙ Goal set" in response
        assert not kanban_db.board_exists(board)
        assert adapter._pending_messages[session_key].text.startswith(
            "[Starting work toward your standing goal]\nGoal:\nShip the dashboard"
        )
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_discord_goal_resume_signals_dirty_dispatch(tmp_path, monkeypatch):
    kanban_home = tmp_path / "kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))

    from hermes_cli import discord_worker_boards as dwb

    dwb.set_goal(thread_id="thread-resume", goal="Ship the dashboard", chat_id="parent-channel")
    dwb.pause_board(dwb.board_slug_for_discord_thread("thread-resume"))

    signals = []

    def fake_signal(self):
        signals.append(True)
        return True

    monkeypatch.setattr(GatewayRunner, "_signal_kanban_dispatcher_dirty", fake_signal)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )

    response = await GatewayRunner._handle_goal_command(
        runner,
        _make_discord_thread_event("/goal resume", thread_id="thread-resume"),
    )

    assert response == "Kanban goal resumed."
    assert signals == [True]


@pytest.mark.asyncio
async def test_stop_cancels_request_scoped_discord_worker_board(tmp_path, monkeypatch):
    kanban_home = tmp_path / "kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.set_goal(
        thread_id="thread-stop",
        goal="Ship the dashboard",
        chat_id="parent-channel",
        request_id="source-message",
    )
    assert board.slug == "discord-thread-stop-m-source-message"

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}

    response = await GatewayRunner._handle_stop_command(
        runner,
        _make_discord_thread_event("/stop", thread_id="thread-stop"),
    )

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert "Stopped" in str(response)
    assert meta["goal_status"] == "cancelled"
    assert meta["phase"] == "cancelled"
    assert meta["cancelled"] is True
    assert meta["paused"] is True


@pytest.mark.asyncio
async def test_stop_cancels_parent_channel_discord_worker_board(tmp_path, monkeypatch):
    kanban_home = tmp_path / "kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))

    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.set_goal(
        thread_id="thread-stop-parent",
        goal="Ship the dashboard",
        chat_id="thread-stop-parent",
        parent_channel_id="parent-channel",
        request_id="source-message",
    )

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {}
    runner._queued_events = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}

    response = await GatewayRunner._handle_stop_command(
        runner,
        _make_discord_parent_event("/stop", channel_id="parent-channel"),
    )

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert "Stopped" in str(response)
    assert meta["goal_status"] == "cancelled"
    assert meta["phase"] == "cancelled"
    assert meta["cancelled"] is True
    assert meta["paused"] is True


@pytest.mark.asyncio
async def test_discord_subgoal_add_signals_dirty_dispatch(tmp_path, monkeypatch):
    kanban_home = tmp_path / "kanban-home"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))

    signals = []

    def fake_signal(self):
        signals.append(True)
        return True

    monkeypatch.setattr(GatewayRunner, "_signal_kanban_dispatcher_dirty", fake_signal)

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )

    response = await GatewayRunner._handle_subgoal_command(
        runner,
        _make_discord_thread_event("/subgoal Add regression tests", thread_id="thread-subgoal"),
    )

    assert response == "Added subgoal 1: Add regression tests"
    assert signals == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_first", [False, True])
async def test_gateway_goal_resume_queues_continuation_turn(tmp_path, monkeypatch, pause_first):
    """Regression: /goal resume must wake the gateway loop, not just report state."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(
        "hermes_cli.discord_worker_boards.board_for_gateway_event",
        lambda *args, **kwargs: None,
    )
    goals._DB_CACHE.clear()

    adapter = _FakeAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._queued_events = {}

    mgr = goals.GoalManager(_session_id())
    mgr.set("finish the queued work")
    if pause_first:
        mgr.pause()

    event = MessageEvent(
        text="/goal resume",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-goal-config",
            chat_type="channel",
            user_id="user-goal-config",
        ),
        message_id="msg-goal-resume",
    )

    response = await GatewayRunner._handle_goal_command(runner, event)

    try:
        assert "Goal resumed" in response
        assert len(adapter._pending_messages) == 1
        queued = next(iter(adapter._pending_messages.values()))
        assert not queued.text.startswith("/")
        resumed_mgr = goals.GoalManager(_session_id())
        assert resumed_mgr.state is not None
        assert "finish the queued work" in queued.text
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_gateway_goal_control_commands_do_not_enqueue_work(tmp_path, monkeypatch):
    """Status, pause, and clear should be control-only gateway commands."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()

    adapter = _FakeAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._queued_events = {}

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chat-goal-config",
        chat_type="channel",
        user_id="user-goal-config",
    )
    mgr = goals.GoalManager(_session_id())
    mgr.set("only report control state")

    try:
        for text in ("/goal status", "/goal pause", "/goal clear"):
            event = MessageEvent(
                text=text,
                message_type=MessageType.TEXT,
                source=source,
                message_id=f"msg-{text.rsplit(' ', 1)[-1]}",
            )
            await GatewayRunner._handle_goal_command(runner, event)
            assert adapter._pending_messages == {}
            assert runner._queued_events == {}
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/goal clear", "/goal stop", "/goal done"])
async def test_gateway_goal_clear_aliases_are_silent_control_commands(tmp_path, monkeypatch, text):
    """Clearing a gateway goal should mutate state without sending chat text."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()

    adapter = _FakeAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._queued_events = {}

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chat-goal-config",
        chat_type="channel",
        user_id="user-goal-config",
    )
    mgr = goals.GoalManager(_session_id())
    mgr.set("clear silently")

    try:
        event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=f"msg-{text.rsplit(' ', 1)[-1]}",
        )

        response = await GatewayRunner._handle_goal_command(runner, event)

        assert response is None
        cleared_mgr = goals.GoalManager(_session_id())
        assert not cleared_mgr.is_active()
        assert not cleared_mgr.has_goal()
        assert adapter._pending_messages == {}
        assert runner._queued_events == {}
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
async def test_gateway_goal_clear_is_silent_when_no_goal_exists(tmp_path, monkeypatch):
    """Repeated /goal clear should not answer with a noisy 'No active goal'."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()

    adapter = _FakeAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._queued_events = {}

    event = MessageEvent(
        text="/goal clear",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="chat-goal-config",
            chat_type="channel",
            user_id="user-goal-config",
        ),
        message_id="msg-clear-empty",
    )

    try:
        response = await GatewayRunner._handle_goal_command(runner, event)

        assert response is None
        assert adapter._pending_messages == {}
        assert runner._queued_events == {}
    finally:
        goals._DB_CACHE.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["/goal pause", "/goal clear"])
async def test_gateway_goal_pause_and_clear_remove_pending_goal_work(tmp_path, monkeypatch, text):
    """Pause/clear must stop a kickoff that was queued but not yet drained."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()

    adapter = _FakeAdapter()
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="token")}
    )
    runner.session_store = _FakeSessionStore()
    runner.adapters = {Platform.DISCORD: adapter}
    runner._queued_events = {}

    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="chat-goal-config",
        chat_type="channel",
        user_id="user-goal-config",
    )

    try:
        set_event = MessageEvent(
            text="/goal /subgoal queued content must not run after cancellation",
            message_type=MessageType.TEXT,
            source=source,
            message_id="msg-set",
        )
        await GatewayRunner._handle_goal_command(runner, set_event)
        assert adapter._pending_messages

        control_event = MessageEvent(
            text=text,
            message_type=MessageType.TEXT,
            source=source,
            message_id=f"msg-{text.rsplit(' ', 1)[-1]}",
        )
        await GatewayRunner._handle_goal_command(runner, control_event)

        assert adapter._pending_messages == {}
        assert runner._queued_events == {}
    finally:
        goals._DB_CACHE.clear()
