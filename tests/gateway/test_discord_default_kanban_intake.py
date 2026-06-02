import asyncio
import json
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner, _assign_default_board_intake_ready_tasks
from gateway.session import SessionEntry, SessionSource, build_session_key
from hermes_cli import kanban_db


class _RecordingAdapter:
    def __init__(self):
        self.sent = []

    async def send(self, chat_id, text, metadata=None):
        self.sent.append({
            "chat_id": chat_id,
            "text": text,
            "metadata": metadata or {},
        })


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _runner(channel_id: str) -> Any:
    runner: Any = object.__new__(GatewayRunner)
    runner.config = {
        "kanban": {
            "discord_intake": {
                "default_board_channels": [channel_id],
            },
        },
    }
    return runner


def _message_runner(channel_id: str) -> Any:
    runner: Any = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="***")}
    )
    runner.adapters = {}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(),
        emit_collect=AsyncMock(return_value=[]),
        loaded_hooks=False,
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.side_effect = lambda source, force_new=False: SessionEntry(
        session_key=build_session_key(source),
        session_id="sess-discord-default-intake",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.DISCORD,
        chat_type=getattr(source, "chat_type", "thread"),
        origin=source,
    )
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._busy_ack_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._queued_events = {}
    runner._session_model_overrides = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._draining = False
    runner._busy_input_mode = "interrupt"
    runner._update_prompt_pending = {}
    runner._is_user_authorized = lambda _source: True
    runner._session_key_for_source = lambda source: build_session_key(source)
    runner._accept_discord_work_item = lambda event, key: None
    runner._is_telegram_topic_root_lobby = lambda source: False
    runner._begin_session_run_generation = MagicMock(return_value=1)
    runner._is_session_run_current = MagicMock(return_value=True)
    runner._release_running_agent_state = lambda key: runner._running_agents.pop(key, None)
    runner._log_gateway_flow_telemetry = MagicMock()
    runner._handle_message_with_agent = AsyncMock(return_value={"final_response": ""})
    runner._runtime_config_dict = lambda: {
        "kanban": {
            "discord_intake": {
                "default_board_channels": [channel_id],
            },
        },
    }
    return runner


def _event(
    text: str,
    *,
    channel_id: str = "dev-channel",
    thread_id: str = "thread-1",
    message_id: str = "message-1",
    is_bot: bool = False,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id=thread_id,
            chat_type="thread",
            thread_id=thread_id,
            parent_chat_id=channel_id,
            guild_id="guild-1",
            user_id="user-1",
            user_name="Sligo",
            is_bot=is_bot,
            project_path="/home/droid/hermes",
            project_github_url="https://github.com/sligo-droid/hermes-agent",
        ),
        message_id=message_id,
    )


def test_discord_default_kanban_intake_creates_blocked_default_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    runner = _runner("dev-channel")
    runner._kanban_notifier_profile = "discord-profile"

    response = runner._maybe_route_discord_default_kanban_intake(
        _event("<@123> make #dev feed the top board")
    )

    assert response is not None
    assert "Queued on top-level Kanban" in response

    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=True)
        subs = kanban_db.list_notify_subs(conn)
    finally:
        conn.close()

    assert len(tasks) == 1
    task = tasks[0]
    assert task.status == "blocked"
    assert task.assignee == "default"
    assert task.title == "#dev intake: make #dev feed the top board"
    assert task.created_by == "discord-default-intake"
    assert task.tenant == "discord-default-intake"
    assert task.workspace_kind == "dir"
    assert task.workspace_path == "/home/droid/hermes"
    assert task.idempotency_key == "discord-default-intake:guild-1:thread-1:message-1"
    assert "https://discord.com/channels/guild-1/thread-1/message-1" in (task.body or "")
    assert "GitHub: https://github.com/sligo-droid/hermes-agent" in (task.body or "")
    assert len(subs) == 1
    sub = subs[0]
    assert sub["task_id"] == task.id
    assert sub["platform"] == "discord"
    assert sub["chat_id"] == "thread-1"
    assert sub["thread_id"] == "thread-1"
    assert sub["user_id"] == "user-1"
    assert sub["notifier_profile"] == "discord-profile"


@pytest.mark.asyncio
async def test_default_board_channel_message_reaches_agent_before_intake(monkeypatch):
    runner = _message_runner("dev-channel")
    runner._maybe_route_discord_default_kanban_intake = MagicMock(
        side_effect=AssertionError("default-board intake should not bypass the agent")
    )

    result = await runner._handle_message(_event("can you answer a direct question?"))

    assert result == {"final_response": ""}
    runner._maybe_route_discord_default_kanban_intake.assert_not_called()
    runner._handle_message_with_agent.assert_awaited_once()


def test_default_intake_context_marks_source_for_agent():
    runner = _runner("dev-channel")
    runner._kanban_notifier_profile = "discord-profile"
    event = _event("please ship this as work")

    marked = runner._mark_discord_default_kanban_intake_context(
        event,
        event.source,
        runner.config,
    )

    assert marked is True
    assert getattr(event.source, "default_kanban_intake") is True
    assert event.source.message_id == "message-1"
    assert getattr(event.source, "default_kanban_intake_assignee") == "default"
    assert getattr(event.source, "kanban_notifier_profile") == "discord-profile"
    prompt = runner._discord_default_kanban_intake_prompt(event.source)
    assert "Answer ordinary questions" in prompt
    assert "kanban_create" in prompt


def test_default_intake_kanban_create_subscribes_originating_discord_thread(tmp_path, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    from gateway.session_context import clear_session_vars, set_session_vars
    from model_tools import _clear_tool_defs_cache, get_tool_definitions
    from tools import kanban_tools as kt
    from tools.registry import invalidate_check_fn_cache

    tokens = set_session_vars(
        platform="discord",
        chat_id="thread-1",
        thread_id="thread-1",
        user_id="user-1",
        user_name="Sligo",
        session_key="discord:thread-1",
        project_path="/home/droid/hermes",
        project_github_url="https://github.com/sligo-droid/hermes-agent",
        guild_id="guild-1",
        parent_chat_id="dev-channel",
        kanban_default_intake="1",
        kanban_default_intake_assignee="default",
        kanban_notify_profile="discord-profile",
        message_id="message-1",
    )
    try:
        invalidate_check_fn_cache()
        _clear_tool_defs_cache()
        names = {
            tool["function"]["name"]
            for tool in get_tool_definitions(enabled_toolsets=["kanban"], quiet_mode=True)
        }
        assert "kanban_create" in names

        result = json.loads(kt._handle_create({
            "title": "ship default-board intake follow-up",
            "body": "Please implement the Discord intake fix.",
        }))
    finally:
        clear_session_vars(tokens)
        invalidate_check_fn_cache()

    assert result["ok"] is True
    assert result["status"] == "ready"
    assert result["notify_subscribed"] is True

    names_after_clear = {
        tool["function"]["name"]
        for tool in get_tool_definitions(enabled_toolsets=["kanban"], quiet_mode=True)
    }
    assert "kanban_create" not in names_after_clear
    _clear_tool_defs_cache()

    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=True)
        subs = kanban_db.list_notify_subs(conn)
    finally:
        conn.close()

    assert len(tasks) == 1
    task = tasks[0]
    assert task.assignee == "default"
    assert task.created_by == "discord-default-intake"
    assert task.tenant == "discord-default-intake"
    assert task.workspace_kind == "dir"
    assert task.workspace_path == "/home/droid/hermes"
    assert task.idempotency_key == "discord-default-intake:guild-1:thread-1:message-1"
    assert "https://discord.com/channels/guild-1/thread-1/message-1" in (task.body or "")
    assert "Task request:\nPlease implement the Discord intake fix." in (task.body or "")
    assert len(subs) == 1
    sub = subs[0]
    assert sub["task_id"] == task.id
    assert sub["platform"] == "discord"
    assert sub["chat_id"] == "thread-1"
    assert sub["thread_id"] == "thread-1"
    assert sub["user_id"] == "user-1"
    assert sub["notifier_profile"] == "discord-profile"


def test_completed_discord_default_intake_is_picked_up_by_notifier(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    intake_runner = _runner("dev-channel")
    intake_runner._kanban_notifier_profile = "discord-profile"

    response = intake_runner._maybe_route_discord_default_kanban_intake(
        _event("ship default board notifications")
    )

    assert response is not None
    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        task = kanban_db.list_tasks(conn, include_archived=True)[0]
        assert kanban_db.complete_task(
            conn, task.id, summary="worker finished intake",
        ) is True
    finally:
        conn.close()

    adapter = _RecordingAdapter()
    notifier_runner = GatewayRunner.__new__(GatewayRunner)
    notifier_runner._running = True
    notifier_runner.adapters = {Platform.DISCORD: adapter}
    notifier_runner._kanban_sub_fail_counts = {}
    notifier_runner._kanban_notifier_profile = "discord-profile"

    asyncio.run(_run_one_notifier_tick(monkeypatch, notifier_runner))

    assert len(adapter.sent) == 1
    sent = adapter.sent[0]
    assert sent["chat_id"] == "thread-1"
    assert sent["metadata"] == {"thread_id": "thread-1"}
    assert task.id in sent["text"]
    assert "done" in sent["text"]
    assert "worker finished intake" in sent["text"]

    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        subs = kanban_db.list_notify_subs(conn, task.id)
    finally:
        conn.close()
    assert subs == []


def test_discord_default_kanban_intake_is_idempotent_by_message_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    runner = _runner("dev-channel")
    event = _event("same message", message_id="message-dupe")

    first = runner._maybe_route_discord_default_kanban_intake(event)
    second = runner._maybe_route_discord_default_kanban_intake(event)

    assert first == second
    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=True)
    finally:
        conn.close()
    assert len(tasks) == 1


def test_discord_default_kanban_intake_skips_unconfigured_channels_and_bots(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    runner = _runner("dev-channel")

    assert runner._maybe_route_discord_default_kanban_intake(_event("hello", channel_id="pid-channel")) is None
    assert runner._maybe_route_discord_default_kanban_intake(_event("hello", is_bot=True)) is None
    assert runner._maybe_route_discord_default_kanban_intake(_event("/status")) is None

    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=True)
    finally:
        conn.close()
    assert tasks == []


def test_legacy_ready_unassigned_default_intake_gets_assigned(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="legacy intake",
            created_by="discord-default-intake",
            tenant="discord-default-intake",
        )

        assigned = _assign_default_board_intake_ready_tasks(conn, "default")
        res = kanban_db.dispatch_once(conn, spawn_fn=lambda task, workspace: 123)
        task = kanban_db.get_task(conn, task_id)
    finally:
        conn.close()

    assert assigned == [task_id]
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == task_id
    assert res.spawned[0][1] == "default"
    assert task is not None
    assert task.status == "running"
    assert task.assignee == "default"


def test_default_intake_assignment_ignores_generic_unassigned_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        task_id = kanban_db.create_task(conn, title="generic floater")

        assigned = _assign_default_board_intake_ready_tasks(conn, "default")
        task = kanban_db.get_task(conn, task_id)
    finally:
        conn.close()

    assert assigned == []
    assert task is not None
    assert task.status == "ready"
    assert task.assignee is None
