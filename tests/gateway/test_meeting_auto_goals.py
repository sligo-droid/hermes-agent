from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
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


def _meeting_event() -> MessageEvent:
    return MessageEvent(
        text="/meeting",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="channel-1",
            user_id="user-1",
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
async def test_meeting_auto_goal_directly_sets_goal_and_subgoals(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    mgr = FakeGoalManager(has_goal=False)
    queued = []

    monkeypatch.setattr(
        runner,
        "_get_goal_manager_for_event",
        lambda event: (mgr, SimpleNamespace(session_id="sid-1")),
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
async def test_meeting_auto_goal_adds_to_existing_goal_without_overwriting(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    mgr = FakeGoalManager(has_goal=True)

    monkeypatch.setattr(
        runner,
        "_get_goal_manager_for_event",
        lambda event: (mgr, SimpleNamespace(session_id="sid-1")),
    )
    monkeypatch.setattr(runner, "_enqueue_goal_work", lambda event, prompt: None)

    status = await runner._apply_meeting_auto_goal_from_response(
        _meeting_event(),
        """
Next todos
1. Check the staging deploy.
""",
    )

    assert mgr.goal is None
    assert mgr.subgoals == ["Check the staging deploy."]
    assert status == "Added 1 meeting subgoal to the active goal."
