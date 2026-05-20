import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import goals


class _FakeSessionEntry:
    session_id = "sid-gateway-goal-config"


class _FakeSessionStore:
    def __init__(self):
        self.entry = _FakeSessionEntry()

    def get_or_create_session(self, source):
        return self.entry

    def _generate_session_key(self, source):
        return "agent:main:discord:channel:goal-config"


class _FakeAdapter:
    def __init__(self):
        self._pending_messages = {}


@pytest.mark.asyncio
async def test_gateway_goal_uses_goals_max_turns_from_full_config(tmp_path, monkeypatch):
    """Gateway /goal should honor top-level goals.max_turns from config.yaml."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("goals:\n  max_turns: 7\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()

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
        state = goals.GoalManager("sid-gateway-goal-config").state
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
        state = goals.GoalManager("sid-gateway-goal-config").state
        assert state is not None
        assert state.goal == "Then implement the smallest fix"
        assert state.subgoals == ["inspect the PID logs"]
        queued = adapter._pending_messages["agent:main:discord:channel:goal-config"]
        assert not queued.text.startswith("/")
        assert "Goal: Then implement the smallest fix" in queued.text
        assert "Additional criteria" in queued.text
        assert "1. inspect the PID logs" in queued.text
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
@pytest.mark.parametrize("pause_first", [False, True])
async def test_gateway_goal_resume_queues_continuation_turn(tmp_path, monkeypatch, pause_first):
    """Regression: /goal resume must wake the gateway loop, not just report state."""
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

    mgr = goals.GoalManager("sid-gateway-goal-config")
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
        queued = adapter._pending_messages["agent:main:discord:channel:goal-config"]
        assert not queued.text.startswith("/")
        resumed_mgr = goals.GoalManager("sid-gateway-goal-config")
        assert queued.text == resumed_mgr.next_continuation_prompt()
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
    mgr = goals.GoalManager("sid-gateway-goal-config")
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
    mgr = goals.GoalManager("sid-gateway-goal-config")
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
        cleared_mgr = goals.GoalManager("sid-gateway-goal-config")
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
