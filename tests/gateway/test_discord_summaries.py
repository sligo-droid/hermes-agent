"""Tests for Discord project and feature summary surfaces."""

import inspect
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
import gateway.run as gateway_run


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return
    discord_mod = types.ModuleType("discord")
    discord_mod.Intents = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.MessageType = SimpleNamespace(default=0, reply=1)
    discord_mod.Color = SimpleNamespace(
        blue=lambda: "blue",
        green=lambda: "green",
        red=lambda: "red",
    )
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.ui = SimpleNamespace(
        View=object,
        button=lambda *args, **kwargs: (lambda fn: fn),
        Button=object,
    )
    discord_mod.ButtonStyle = SimpleNamespace(
        success=1,
        primary=2,
        secondary=2,
        danger=3,
        green=1,
        grey=2,
        blurple=2,
        red=3,
    )
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = types.ModuleType("discord.ext")
    commands_mod = types.ModuleType("discord.ext.commands")
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

import gateway.platforms.discord as discord_platform  # noqa: E402
from gateway.platforms.discord import DiscordAdapter  # noqa: E402


class FakeEmbed:
    def __init__(self, **kwargs):
        self.title = kwargs.get("title")
        self.description = kwargs.get("description")
        self.color = kwargs.get("color")
        self.fields = []

    def add_field(self, *, name, value, inline=False):
        self.fields.append(SimpleNamespace(name=name, value=value, inline=inline))


class FakeDMChannel:
    pass


class FakeTextChannel:
    def __init__(self, channel_id=100, *, topic=None):
        self.id = channel_id
        self.name = "general"
        self.guild = SimpleNamespace(id=5, name="Hermes Server")
        self.topic = topic
        self.edit = AsyncMock(side_effect=self._edit)

    async def _edit(self, **kwargs):
        if "topic" in kwargs:
            self.topic = kwargs["topic"]


class FakeThread:
    def __init__(self, channel_id=200, *, parent=None):
        self.id = channel_id
        self.name = "feature-thread"
        self.parent = parent
        self.parent_id = getattr(parent, "id", None)
        self.guild = getattr(parent, "guild", None)
        self.sent = []

    async def send(self, **kwargs):
        msg = SimpleNamespace(id=300, edit=AsyncMock())
        self.sent.append((kwargs, msg))
        return msg


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(discord_platform.discord, "DMChannel", FakeDMChannel, raising=False)
    monkeypatch.setattr(discord_platform.discord, "Thread", FakeThread, raising=False)
    monkeypatch.setattr(discord_platform.discord, "Embed", FakeEmbed, raising=False)
    monkeypatch.setattr(
        discord_platform.discord,
        "Color",
        SimpleNamespace(blue=lambda: "blue", green=lambda: "green", red=lambda: "red"),
        raising=False,
    )
    for var in (
        "DISCORD_REQUIRE_MENTION",
        "DISCORD_AUTO_THREAD",
        "DISCORD_FREE_RESPONSE_CHANNELS",
        "DISCORD_NO_THREAD_CHANNELS",
        "DISCORD_ALLOWED_CHANNELS",
        "DISCORD_IGNORED_CHANNELS",
    ):
        monkeypatch.delenv(var, raising=False)

    instance = DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))
    instance._client = SimpleNamespace(user=SimpleNamespace(id=999))
    instance._text_batch_delay_seconds = 0
    instance.handle_message = AsyncMock()
    instance._collect_discord_project_metadata = MagicMock(
        return_value={
            "project_name": "Hermes Project",
            "repo_url": "https://github.com/acme/hermes-project",
            "production_url": None,
            "branch": "feature/summary",
            "branch_url": "https://github.com/acme/hermes-project/tree/feature/summary",
            "pr_url": None,
        }
    )
    state = {}
    instance._read_project_summary_state = MagicMock(side_effect=lambda: dict(state))
    instance._write_project_summary_state = MagicMock(side_effect=lambda value: state.clear() or state.update(value))
    return instance


def _make_message(adapter, *, channel, content):
    return SimpleNamespace(
        id=123,
        content=content,
        mentions=[adapter._client.user],
        attachments=[],
        reference=None,
        created_at=datetime.now(timezone.utc),
        channel=channel,
        author=SimpleNamespace(id=42, display_name="Jezza", name="Jezza"),
        type=discord_platform.discord.MessageType.default,
    )


@pytest.mark.asyncio
async def test_tagged_parent_message_initializes_project_and_feature_summaries(adapter, monkeypatch):
    monkeypatch.setenv("DISCORD_REQUIRE_MENTION", "true")
    parent = FakeTextChannel(channel_id=100, topic="Existing channel note")
    thread = FakeThread(channel_id=200, parent=parent)
    adapter._auto_create_thread = AsyncMock(return_value=thread)

    await adapter._handle_message(
        _make_message(adapter, channel=parent, content="<@999> Build a deploy dashboard")
    )

    parent.edit.assert_awaited_once()
    assert parent.topic.startswith("Project Summary: Project: Hermes Project")
    assert "Existing channel note" in parent.topic
    assert len(thread.sent) == 1
    sent_embed = thread.sent[0][0]["embed"]
    assert sent_embed.title == "Feature Summary"
    assert sent_embed.description == "Build a deploy dashboard"

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.project_summary["channel_id"] == "100"
    assert event.feature_summary["thread_id"] == "200"
    assert event.text == "Build a deploy dashboard"


def test_project_summary_topic_replaces_managed_line(adapter):
    topic = adapter._merge_project_summary_topic(
        "Project Summary: Project: Old | Repo: pending | Prod: pending\n\nKeep this note",
        "Project Summary: Project: New | Repo: repo | Prod: prod",
    )

    assert topic.startswith("Project Summary: Project: New")
    assert "Old" not in topic
    assert "Keep this note" in topic


@pytest.mark.asyncio
async def test_project_summary_retries_after_failed_attempt(adapter):
    parent = FakeTextChannel(channel_id=100, topic=None)
    key = adapter._project_summary_state_key(parent)
    state = {
        key: {
            "channel_id": "100",
            "guild_id": "5",
            "attempted_at": 1,
            "success": False,
        }
    }
    adapter._read_project_summary_state = MagicMock(side_effect=lambda: dict(state))
    adapter._write_project_summary_state = MagicMock(side_effect=lambda value: state.clear() or state.update(value))

    handle = await adapter.initialize_project_summary(parent)

    parent.edit.assert_awaited_once()
    assert handle is not None
    assert handle["channel_id"] == "100"
    assert state[key]["success"] is True


@pytest.mark.asyncio
async def test_feature_summary_update_edits_initial_message(adapter):
    parent = FakeTextChannel(channel_id=100)
    thread = FakeThread(channel_id=200, parent=parent)
    handle = await adapter.initialize_feature_summary(
        thread,
        parent_channel=parent,
        initial_request="Ship project links",
    )

    assert handle is not None
    assert await adapter.update_feature_summary(
        handle,
        final_response="Done. The repo and production links are visible.",
        status="Complete",
        title="Project Links",
    )

    message = handle["_message_obj"]
    message.edit.assert_awaited_once()
    edited_embed = message.edit.await_args.kwargs["embed"]
    fields = {field.name: field.value for field in edited_embed.fields}
    assert fields["Generated Title"] == "Project Links"
    assert fields["Concise Outcome"].startswith("Done.")
    assert fields["Branch"] == "feature/summary"


@pytest.mark.asyncio
async def test_runner_registers_discord_summary_post_delivery_callback():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner._session_db = None
    adapter = SimpleNamespace(
        callbacks=[],
        update_project_summary=AsyncMock(),
        update_feature_summary=AsyncMock(),
    )

    def register_post_delivery_callback(session_key, callback, generation=None):
        adapter.callbacks.append((session_key, callback, generation))

    adapter.register_post_delivery_callback = register_post_delivery_callback
    runner.adapters = {Platform.DISCORD: adapter}
    source = SessionSource(platform=Platform.DISCORD, chat_id="200", chat_type="thread")
    event = MessageEvent(
        text="Build it",
        source=source,
        feature_summary={"message_id": "300"},
        project_summary={"channel_id": "100"},
    )

    runner._register_discord_summary_post_delivery(
        event=event,
        source=source,
        session_key="discord:200",
        run_generation=7,
        session_id="session-1",
        final_response="Final answer",
        agent_result={"completed": True},
    )

    assert len(adapter.callbacks) == 1
    session_key, callback, generation = adapter.callbacks[0]
    assert session_key == "discord:200"
    assert generation == 7
    result = callback()
    if inspect.isawaitable(result):
        await result

    adapter.update_project_summary.assert_awaited_once_with({"channel_id": "100"})
    adapter.update_feature_summary.assert_awaited_once_with(
        {"message_id": "300"},
        final_response="Final answer",
        status="Complete",
        title=None,
    )
