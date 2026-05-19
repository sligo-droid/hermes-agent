from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageType
from gateway.platforms.discord import DiscordAdapter


@pytest.fixture
def adapter():
    config = PlatformConfig(enabled=True, token="***")
    a = DiscordAdapter(config)
    a._client = SimpleNamespace(user=SimpleNamespace(id=99999, name="HermesBot"), guilds=[])
    return a


def _make_interaction():
    channel = SimpleNamespace(id=12345, name="general")
    return SimpleNamespace(
        user=SimpleNamespace(id=100200300, name="tbrent", display_name="tbrent"),
        guild=SimpleNamespace(owner_id=999, id=42, get_member=lambda uid: None),
        guild_id=42,
        channel_id=12345,
        channel=channel,
        response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(),
        delete_original_response=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_native_meeting_slash_builds_command_event_with_audio(adapter, monkeypatch):
    captured = []

    async def fake_cache(att, ext):
        assert att.filename == "meeting.ogg"
        assert ext == ".ogg"
        return "/tmp/meeting.ogg"

    async def fake_handle(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "_cache_discord_audio", fake_cache)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)

    interaction = _make_interaction()
    attachment = SimpleNamespace(
        filename="meeting.ogg",
        content_type="audio/ogg",
        url="https://cdn.discordapp.example/meeting.ogg",
    )

    await adapter._handle_meeting_slash(interaction, attachment, "client kickoff")

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.delete_original_response.assert_awaited_once()
    assert len(captured) == 1
    event = captured[0]
    assert event.text == "/meeting client kickoff"
    assert event.message_type == MessageType.COMMAND
    assert event.media_urls == ["/tmp/meeting.ogg"]
    assert event.media_types == ["audio/ogg"]
    assert event.message_id is None
    assert event.reply_to_message_id is None


@pytest.mark.asyncio
async def test_bare_text_meeting_command_bypasses_discord_mention_gate(adapter, monkeypatch):
    import discord
    from datetime import datetime

    captured = []

    async def fake_cache(att, ext):
        return "/tmp/uploaded-meeting.ogg"

    async def fake_handle(event):
        captured.append(event)

    monkeypatch.setattr(adapter, "_cache_discord_audio", fake_cache)
    monkeypatch.setattr(adapter, "handle_message", fake_handle)
    classifier = AsyncMock(return_value=False)
    monkeypatch.setattr(adapter, "_classify_discord_feature_request", classifier)

    channel = SimpleNamespace(id=12345, name="general", guild=SimpleNamespace(id=42, name="Guild"))
    attachment = SimpleNamespace(
        filename="meeting.ogg",
        content_type="audio/ogg",
        url="https://cdn.discordapp.example/meeting.ogg",
        size=123,
    )
    message = SimpleNamespace(
        id=555,
        content="/meeting",
        clean_content="/meeting",
        type=discord.MessageType.default,
        channel=channel,
        author=SimpleNamespace(id=100200300, display_name="tbrent", name="tbrent", bot=False),
        mentions=[],
        attachments=[attachment],
        message_snapshots=[],
        flags=SimpleNamespace(value=0, voice=False),
        guild=channel.guild,
        created_at=datetime.now(),
        reference=None,
    )

    await adapter._handle_message(message)

    assert len(captured) == 1
    event = captured[0]
    assert event.text == "/meeting"
    assert event.message_type == MessageType.COMMAND
    assert event.media_urls == ["/tmp/uploaded-meeting.ogg"]
    assert event.media_types == ["audio/ogg"]
    classifier.assert_not_awaited()


def test_register_slash_commands_includes_native_meeting(adapter, monkeypatch):
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

    assert "meeting" in tree.registered
    assert "meeting recording" in tree.registered["meeting"].description
