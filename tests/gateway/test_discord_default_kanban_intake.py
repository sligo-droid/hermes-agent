from typing import Any

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from hermes_cli import kanban_db


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

    response = runner._maybe_route_discord_default_kanban_intake(
        _event("<@123> make #dev feed the top board")
    )

    assert response is not None
    assert "Queued on top-level Kanban" in response

    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=True)
    finally:
        conn.close()

    assert len(tasks) == 1
    task = tasks[0]
    assert task.status == "blocked"
    assert task.title == "#dev intake: make #dev feed the top board"
    assert task.created_by == "discord-default-intake"
    assert task.tenant == "discord-default-intake"
    assert task.workspace_kind == "dir"
    assert task.workspace_path == "/home/droid/hermes"
    assert task.idempotency_key == "discord-default-intake:guild-1:thread-1:message-1"
    assert "https://discord.com/channels/guild-1/thread-1/message-1" in (task.body or "")
    assert "GitHub: https://github.com/sligo-droid/hermes-agent" in (task.body or "")


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
