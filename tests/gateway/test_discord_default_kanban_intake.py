import asyncio
from typing import Any

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner, _assign_default_board_intake_ready_tasks
from gateway.session import SessionSource
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
