from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.fixture
def adapter():
    config = PlatformConfig(enabled=True, token="***")
    a = DiscordAdapter(config)
    a._client = SimpleNamespace(user=SimpleNamespace(id=99999, name="HermesBot"), guilds=[])
    return a


@pytest.mark.asyncio
async def test_bare_text_meeting_command_creates_thread_anchored_to_audio_message(adapter, monkeypatch):
    import discord

    captured = []

    async def fake_cache(att, ext):
        assert att.filename == "meeting.ogg"
        assert ext == ".ogg"
        return "/tmp/uploaded-meeting.ogg"

    async def fake_handle(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "_cache_discord_audio", fake_cache)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    classifier = AsyncMock(return_value=False)
    monkeypatch.setattr(adapter, "_classify_discord_action_request", classifier)
    feature_summary = AsyncMock()
    feature_summary.return_value = {"thread_id": "777", "message_id": "summary-1"}
    monkeypatch.setattr(adapter, "initialize_feature_summary", feature_summary)

    guild = SimpleNamespace(id=42, name="Guild")
    channel = SimpleNamespace(id=12345, name="general", guild=guild)
    thread = SimpleNamespace(id=777, name="Meeting notes — 2026-05-19 — client kickoff", parent=channel, guild=guild)
    create_thread = AsyncMock(return_value=thread)
    attachment = SimpleNamespace(
        filename="meeting.ogg",
        content_type="audio/ogg",
        url="https://cdn.discordapp.example/meeting.ogg",
        size=123,
    )
    message = SimpleNamespace(
        id=555,
        content="/meeting client kickoff",
        clean_content="/meeting client kickoff",
        type=discord.MessageType.default,
        channel=channel,
        author=SimpleNamespace(id=100200300, display_name="tbrent", name="tbrent", bot=False),
        mentions=[],
        attachments=[attachment],
        message_snapshots=[],
        flags=SimpleNamespace(value=0, voice=False),
        guild=guild,
        created_at=datetime(2026, 5, 19, 4, 0, 0),
        reference=None,
        create_thread=create_thread,
        thread=None,
    )

    await adapter._handle_message(message)

    create_thread.assert_awaited_once()
    _, kwargs = create_thread.call_args
    assert kwargs["name"] == "Meeting notes — 2026-05-19 — client kickoff"
    assert kwargs["auto_archive_duration"] == 1440
    feature_summary.assert_not_awaited()
    classifier.assert_not_awaited()

    assert len(captured) == 1
    event = captured[0]
    assert event.text == "/meeting client kickoff"
    assert event.message_type == MessageType.COMMAND
    assert event.media_urls == ["/tmp/uploaded-meeting.ogg"]
    assert event.media_types == ["audio/ogg"]
    assert event.message_id == "555"
    assert event.source.chat_id == "777"
    assert event.source.thread_id == "777"
    assert event.source.parent_chat_id == "12345"
    assert event.feature_summary is None


@pytest.mark.asyncio
async def test_mentioned_meeting_command_with_audio_canonicalizes_after_mention_strip(adapter, monkeypatch):
    import discord

    captured = []

    async def fake_cache(att, ext):
        return "/tmp/uploaded-meeting.ogg"

    async def fake_handle(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "_cache_discord_audio", fake_cache)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    classifier = AsyncMock(return_value=False)
    monkeypatch.setattr(adapter, "_classify_discord_action_request", classifier)

    guild = SimpleNamespace(id=42, name="Guild")
    channel = SimpleNamespace(id=12345, name="general", guild=guild)
    thread = SimpleNamespace(id=777, name="Meeting notes — 2026-05-19 — client kickoff", parent=channel, guild=guild)
    create_thread = AsyncMock(return_value=thread)
    bot_user = adapter._client.user
    message = SimpleNamespace(
        id=557,
        content=f"<@{bot_user.id}> /meeting client kickoff",
        clean_content="@Sligo Labs /meeting client kickoff",
        type=discord.MessageType.default,
        channel=channel,
        author=SimpleNamespace(id=100200300, display_name="tbrent", name="tbrent", bot=False),
        mentions=[bot_user],
        attachments=[SimpleNamespace(filename="meeting.ogg", content_type="audio/ogg", url="https://cdn.discordapp.example/meeting.ogg", size=123)],
        message_snapshots=[],
        flags=SimpleNamespace(value=0, voice=False),
        guild=guild,
        created_at=datetime(2026, 5, 19, 4, 0, 0),
        reference=None,
        create_thread=create_thread,
        thread=None,
    )

    await adapter._handle_message(message)

    create_thread.assert_awaited_once()
    assert create_thread.call_args.kwargs["name"] == "Meeting notes — 2026-05-19 — client kickoff"
    assert len(captured) == 1
    event = captured[0]
    assert event.text == "/meeting client kickoff"
    assert event.message_type == MessageType.COMMAND
    assert event.media_urls == ["/tmp/uploaded-meeting.ogg"]
    assert event.source.thread_id == "777"
    classifier.assert_not_awaited()


@pytest.mark.asyncio
async def test_mentioned_audio_without_meeting_command_triggers_meeting_intake(adapter, monkeypatch):
    import discord

    captured = []

    async def fake_cache(att, ext):
        assert att.filename == "meeting.ogg"
        assert ext == ".ogg"
        return "/tmp/uploaded-meeting.ogg"

    async def fake_handle(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "_cache_discord_audio", fake_cache)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    classifier = AsyncMock(return_value=False)
    monkeypatch.setattr(adapter, "_classify_discord_action_request", classifier)
    feature_summary = AsyncMock()
    feature_summary.return_value = {"thread_id": "778", "message_id": "summary-2"}
    monkeypatch.setattr(adapter, "initialize_feature_summary", feature_summary)

    guild = SimpleNamespace(id=42, name="Guild")
    channel = SimpleNamespace(id=12345, name="general", guild=guild)
    thread = SimpleNamespace(id=778, name="Meeting notes — 2026-05-19 — client kickoff", parent=channel, guild=guild)
    create_thread = AsyncMock(return_value=thread)
    bot_user = adapter._client.user
    message = SimpleNamespace(
        id=558,
        content=f"<@{bot_user.id}> client kickoff",
        clean_content="@Sligo Labs client kickoff",
        type=discord.MessageType.default,
        channel=channel,
        author=SimpleNamespace(id=100200300, display_name="tbrent", name="tbrent", bot=False),
        mentions=[bot_user],
        attachments=[SimpleNamespace(filename="meeting.ogg", content_type="audio/ogg", url="https://cdn.discordapp.example/meeting.ogg", size=123)],
        message_snapshots=[],
        flags=SimpleNamespace(value=0, voice=False),
        guild=guild,
        created_at=datetime(2026, 5, 19, 4, 0, 0),
        reference=None,
        create_thread=create_thread,
        thread=None,
    )

    await adapter._handle_message(message)

    create_thread.assert_awaited_once()
    assert create_thread.call_args.kwargs["name"] == "Meeting notes — 2026-05-19 — client kickoff"
    feature_summary.assert_not_awaited()
    classifier.assert_not_awaited()

    assert len(captured) == 1
    event = captured[0]
    assert event.text == "/meeting client kickoff"
    assert event.message_type == MessageType.COMMAND
    assert event.media_urls == ["/tmp/uploaded-meeting.ogg"]
    assert event.media_types == ["audio/ogg"]
    assert event.source.thread_id == "778"
    assert event.feature_summary is None


@pytest.mark.asyncio
async def test_mentioned_text_without_audio_does_not_trigger_meeting_intake(adapter, monkeypatch):
    import discord

    captured = []

    async def fake_handle(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    classifier = AsyncMock(return_value=False)
    monkeypatch.setattr(adapter, "_classify_discord_action_request", classifier)
    # This test isolates the meeting-intake gate: plain mentioned text should
    # not be promoted to /meeting. Disable the separate generic auto-thread
    # path so its direct-question thread creation does not mask that assertion.
    monkeypatch.setenv("DISCORD_AUTO_THREAD", "false")
    adapter._text_batch_delay_seconds = 0

    guild = SimpleNamespace(id=42, name="Guild")
    channel = SimpleNamespace(id=12345, name="general", guild=guild)
    create_thread = AsyncMock()
    bot_user = adapter._client.user
    message = SimpleNamespace(
        id=559,
        content=f"<@{bot_user.id}> client kickoff",
        clean_content="@Sligo Labs client kickoff",
        type=discord.MessageType.default,
        channel=channel,
        author=SimpleNamespace(id=100200300, display_name="tbrent", name="tbrent", bot=False),
        mentions=[bot_user],
        attachments=[],
        message_snapshots=[],
        flags=SimpleNamespace(value=0, voice=False),
        guild=guild,
        created_at=datetime(2026, 5, 19, 4, 0, 0),
        reference=None,
        create_thread=create_thread,
        thread=None,
    )

    await adapter._handle_message(message)

    create_thread.assert_not_awaited()
    assert len(captured) == 1
    event = captured[0]
    assert event.text == "client kickoff"
    assert event.message_type == MessageType.TEXT
    assert event.media_urls == []
    classifier.assert_awaited_once_with("client kickoff")


@pytest.mark.asyncio
async def test_bare_text_meeting_command_reuses_existing_recording_thread(adapter, monkeypatch):
    import discord

    captured = []

    async def fake_cache(att, ext):
        return "/tmp/uploaded-meeting.ogg"

    async def fake_handle(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "_cache_discord_audio", fake_cache)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    monkeypatch.setattr(adapter, "_classify_discord_action_request", AsyncMock(return_value=False))

    guild = SimpleNamespace(id=42, name="Guild")
    channel = SimpleNamespace(id=12345, name="general", guild=guild)
    existing_thread = SimpleNamespace(id=888, name="Existing meeting thread", parent=channel, guild=guild)
    create_thread = AsyncMock()
    message = SimpleNamespace(
        id=556,
        content="/meeting",
        clean_content="/meeting",
        type=discord.MessageType.default,
        channel=channel,
        author=SimpleNamespace(id=100200300, display_name="tbrent", name="tbrent", bot=False),
        mentions=[],
        attachments=[SimpleNamespace(filename="meeting.ogg", content_type="audio/ogg", url="https://cdn.discordapp.example/meeting.ogg", size=123)],
        message_snapshots=[],
        flags=SimpleNamespace(value=0, voice=False),
        guild=guild,
        created_at=datetime(2026, 5, 19, 4, 0, 0),
        reference=None,
        create_thread=create_thread,
        thread=existing_thread,
    )

    await adapter._handle_message(message)

    create_thread.assert_not_awaited()
    assert len(captured) == 1
    assert captured[0].source.chat_id == "888"
    assert captured[0].source.thread_id == "888"


def test_register_slash_commands_does_not_include_native_meeting_attachment_command(adapter, monkeypatch):
    import discord

    monkeypatch.setattr(discord.app_commands, "autocomplete", lambda **_: (lambda fn: fn), raising=False)

    class Tree:
        def __init__(self):
            self.registered = {}

        def command(self, *, name, description):
            def decorator(fn):
                self.registered[name] = SimpleNamespace(
                    name=name,
                    description=description,
                    callback=fn,
                    default_permissions=None,
                    to_dict=lambda _tree, _name=name, _description=description: {
                        "type": 1,
                        "name": _name,
                        "description": _description,
                        "options": [],
                    },
                )
                return fn

            return decorator

        def add_command(self, cmd):
            self.registered[cmd.name] = cmd

        def get_commands(self):
            return list(self.registered.values())

    tree = Tree()
    adapter._client = SimpleNamespace(
        user=SimpleNamespace(id=99999, name="HermesBot"),
        guilds=[],
        tree=tree,
    )

    adapter._register_slash_commands()

    assert "meeting" not in tree.registered
