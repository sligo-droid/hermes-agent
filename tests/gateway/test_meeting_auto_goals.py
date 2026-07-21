from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class FakeGoalManager:
    def __init__(self, *, has_goal: bool = False):
        self.goal = None
        self.subgoals = []
        self._has_goal = has_goal
        self.state = SimpleNamespace(goal="Existing goal") if has_goal else None

    def has_goal(self):
        return self._has_goal

    def set(self, goal):
        self.goal = goal
        self._has_goal = True
        self.state = SimpleNamespace(goal=goal)
        return self.state

    def add_subgoal(self, text):
        self.subgoals.append(text)
        return text

    def next_continuation_prompt(self):
        return "continue meeting follow-up"


class FakeDiscordAdapter:
    def __init__(self):
        self._active_sessions = {}
        self._post_delivery_callbacks = {}
        self.callback = None
        self.callback_session_key = None
        self.initialized = []

    def register_post_delivery_callback(self, session_key, callback, *, generation=None):
        self.callback_session_key = session_key
        BasePlatformAdapter.register_post_delivery_callback(
            self,
            session_key,
            callback,
            generation=generation,
        )
        entry = self._post_delivery_callbacks.get(session_key)
        self.callback = entry[1] if isinstance(entry, tuple) else entry

    def pop_post_delivery_callback(self, session_key, *, generation=None):
        return BasePlatformAdapter.pop_post_delivery_callback(
            self,
            session_key,
            generation=generation,
        )

    async def initialize_goal_feature_summary_for_source(
        self,
        source,
        *,
        initial_request,
        project_context=None,
    ):
        self.initialized.append(
            {
                "source": source,
                "initial_request": initial_request,
                "project_context": project_context,
            }
        )
        return {"message_id": "summary-1"}


def _meeting_event(*, platform: Platform = Platform.DISCORD) -> MessageEvent:
    return MessageEvent(
        text="/meeting",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=platform,
            chat_id="thread-1" if platform == Platform.DISCORD else "channel-1",
            chat_type="thread" if platform == Platform.DISCORD else "dm",
            user_id="user-1",
            thread_id="thread-1" if platform == Platform.DISCORD else None,
            guild_id="guild-1" if platform == Platform.DISCORD else None,
            parent_chat_id="parent-1" if platform == Platform.DISCORD else None,
            project_name="Pid" if platform == Platform.DISCORD else None,
            project_path="/tmp/PID" if platform == Platform.DISCORD else None,
            project_github_url="https://github.com/sligo-labs/PID" if platform == Platform.DISCORD else None,
            project_channel_id="parent-1" if platform == Platform.DISCORD else None,
            project_mapping_source="test" if platform == Platform.DISCORD else None,
            project_mapping_resolved=True if platform == Platform.DISCORD else None,
        ),
        invoked_skill_name="meeting",
        invoked_skill_command="meeting",
    )


def test_extract_meeting_todos_from_next_todos_section():
    response = """
Meeting summary
- We discussed launch prep.

Next todos
1. Alice drafts the launch checklist by Friday.
2. Bob verifies the demo login.

Open questions / risks
- Waiting on client assets.
"""

    todos = GatewayRunner._extract_meeting_todos_from_response(response)

    assert todos == [
        "Alice drafts the launch checklist by Friday.",
        "Bob verifies the demo login.",
    ]


def test_extract_meeting_todos_recovers_printed_subgoal_commands():
    response = "/goal Follow up on the meeting /subgoal Draft checklist /subgoal Verify demo login"

    todos = GatewayRunner._extract_meeting_todos_from_response(response)

    assert todos == ["Draft checklist", "Verify demo login"]


@pytest.mark.asyncio
async def test_meeting_auto_goal_creates_discord_kanban_goal_and_subgoal_tickets(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = FakeDiscordAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    calls = {"start_direct_goal": None, "subgoals": []}

    from hermes_cli import discord_worker_boards as _dwb

    def fake_start_direct_goal(**kwargs):
        calls["start_direct_goal"] = kwargs
        return SimpleNamespace(
            slug="discord-thread-1",
            public_url="https://kanban.example/thread-1",
            worker={"project_context": kwargs["project_context"]},
        )

    def fake_add_subgoal(board, text):
        calls["subgoals"].append((board, text))
        return len(calls["subgoals"]), text

    monkeypatch.setattr(
        runner,
        "_get_goal_manager_for_event",
        lambda event: pytest.fail("Discord meeting todos should use Kanban before legacy goals"),
    )
    monkeypatch.setattr(_dwb, "start_direct_goal", fake_start_direct_goal)
    monkeypatch.setattr(_dwb, "add_subgoal", fake_add_subgoal)
    monkeypatch.setattr(runner, "_session_key_for_source", lambda source: "session-key")

    status = await runner._apply_meeting_auto_goal_from_response(
        _meeting_event(),
        """
Next todos
- Draft the client summary.
- Create the follow-up issue list.
""",
    )

    assert calls["start_direct_goal"] == {
        "thread_id": "thread-1",
        "goal": "Follow up on the todos from this meeting.",
        "chat_id": "thread-1",
        "guild_id": "guild-1",
        "parent_channel_id": "parent-1",
        "project_context": {
            "project_name": "Pid",
            "project_path": "/tmp/PID",
            "project_github_url": "https://github.com/sligo-labs/PID",
            "project_channel_id": "parent-1",
            "project_mapping_source": "test",
            "project_mapping_resolved": True,
        },
    }
    assert calls["subgoals"] == [
        ("discord-thread-1", "Draft the client summary."),
        ("discord-thread-1", "Create the follow-up issue list."),
    ]
    assert status == ""
    assert adapter.callback_session_key == "session-key"
    assert adapter.initialized == []

    assert adapter.callback is not None
    assert await adapter.callback() is True
    assert len(adapter.initialized) == 1
    assert adapter.initialized[0]["source"].thread_id == "thread-1"
    assert adapter.initialized[0]["initial_request"] == "/goal Follow up on the todos from this meeting."


@pytest.mark.asyncio
async def test_meeting_auto_goal_chains_with_existing_post_delivery_callback(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    adapter = FakeDiscordAdapter()
    runner.adapters = {Platform.DISCORD: adapter}
    calls = {"subgoals": []}
    preexisting_fired = []

    from hermes_cli import discord_worker_boards as _dwb

    monkeypatch.setattr(
        _dwb,
        "start_direct_goal",
        lambda **kwargs: SimpleNamespace(
            slug="discord-thread-1",
            public_url="https://kanban.example/thread-1",
            worker={"project_context": kwargs["project_context"]},
        ),
    )
    monkeypatch.setattr(
        _dwb,
        "add_subgoal",
        lambda board, text: calls["subgoals"].append((board, text))
        or (len(calls["subgoals"]), text),
    )
    monkeypatch.setattr(runner, "_session_key_for_source", lambda source: "session-key")
    adapter.register_post_delivery_callback(
        "session-key",
        lambda: preexisting_fired.append("response delivered"),
    )

    status = await runner._apply_meeting_auto_goal_from_response(
        _meeting_event(),
        """
Next todos
- Draft the client summary.
""",
    )

    assert status == ""
    assert adapter.initialized == []

    callback = adapter.pop_post_delivery_callback("session-key")
    assert callback is not None
    result = callback()
    if hasattr(result, "__await__"):
        await result

    assert preexisting_fired == ["response delivered"]
    assert len(adapter.initialized) == 1
    assert adapter.initialized[0]["initial_request"] == "/goal Follow up on the todos from this meeting."


@pytest.mark.asyncio
async def test_meeting_auto_goal_directly_sets_legacy_goal_and_subgoals_for_non_discord(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    mgr = FakeGoalManager(has_goal=False)
    queued = []

    monkeypatch.setattr(
        runner,
        "_get_goal_manager_for_event",
        AsyncMock(return_value=(mgr, SimpleNamespace(session_id="sid-1"))),
    )
    monkeypatch.setattr(
        runner,
        "_enqueue_goal_work",
        lambda event, prompt: queued.append((event, prompt)),
    )

    status = await runner._apply_meeting_auto_goal_from_response(
        _meeting_event(platform=Platform.LOCAL),
        """
Next todos
- Draft the client summary.
- Create the follow-up issue list.
""",
    )

    assert mgr.goal == "Follow up on the todos from this meeting."
    assert mgr.subgoals == [
        "Draft the client summary.",
        "Create the follow-up issue list.",
    ]
    assert queued and queued[0][1] == "continue meeting follow-up"
    assert status == "Goal tracking started with 2 subgoals."


@pytest.mark.asyncio
async def test_meeting_auto_goal_falls_back_when_discord_kanban_unavailable(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    mgr = FakeGoalManager(has_goal=False)
    queued = []

    from hermes_cli import discord_worker_boards as _dwb

    monkeypatch.setattr(_dwb, "start_direct_goal", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("db unavailable")))
    monkeypatch.setattr(
        runner,
        "_get_goal_manager_for_event",
        AsyncMock(return_value=(mgr, SimpleNamespace(session_id="sid-1"))),
    )
    monkeypatch.setattr(
        runner,
        "_enqueue_goal_work",
        lambda event, prompt: queued.append((event, prompt)),
    )

    status = await runner._apply_meeting_auto_goal_from_response(
        _meeting_event(),
        """
Next todos
- Verify demo login.
""",
    )

    assert mgr.goal == "Follow up on the todos from this meeting."
    assert mgr.subgoals == ["Verify demo login."]
    assert queued and queued[0][1] == "continue meeting follow-up"
    assert status == "Goal tracking started with 1 subgoal."


@pytest.mark.asyncio
async def test_meeting_auto_goal_adds_to_existing_goal_without_overwriting(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    mgr = FakeGoalManager(has_goal=True)

    monkeypatch.setattr(
        runner,
        "_get_goal_manager_for_event",
        AsyncMock(return_value=(mgr, SimpleNamespace(session_id="sid-1"))),
    )
    monkeypatch.setattr(runner, "_enqueue_goal_work", lambda event, prompt: None)

    status = await runner._apply_meeting_auto_goal_from_response(
        _meeting_event(platform=Platform.LOCAL),
        """
Next todos
1. Check the staging deploy.
""",
    )

    assert mgr.goal is None
    assert mgr.subgoals == ["Check the staging deploy."]
    assert status == "Added 1 meeting subgoal to the active goal."
