"""Regression tests for Discord tracked-thread missed-message replay."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
import sys

import pytest

from gateway.config import PlatformConfig


def _ensure_discord_mock():
    """Install a lightweight discord module when discord.py is unavailable."""
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.Client = MagicMock
    discord_mod.File = MagicMock
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Object = lambda *, id: SimpleNamespace(id=id)
    discord_mod.MessageType = SimpleNamespace(default="default", reply="reply")
    discord_mod.ui = SimpleNamespace(View=object, button=lambda *a, **k: (lambda fn: fn), Button=object)
    discord_mod.ButtonStyle = SimpleNamespace(success=1, primary=2, secondary=5, danger=3, green=1, grey=2, blurple=2, red=3)
    discord_mod.Color = SimpleNamespace(orange=lambda: 1, green=lambda: 2, blue=lambda: 3, red=lambda: 4, purple=lambda: 5)
    discord_mod.Interaction = object
    discord_mod.Embed = MagicMock
    discord_mod.app_commands = SimpleNamespace(
        describe=lambda **kwargs: (lambda fn: fn),
        choices=lambda **kwargs: (lambda fn: fn),
        Choice=lambda **kwargs: SimpleNamespace(**kwargs),
    )

    ext_mod = MagicMock()
    commands_mod = MagicMock()
    commands_mod.Bot = MagicMock
    ext_mod.commands = commands_mod

    sys.modules.setdefault("discord", discord_mod)
    sys.modules.setdefault("discord.ext", ext_mod)
    sys.modules.setdefault("discord.ext.commands", commands_mod)


_ensure_discord_mock()

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


BOT_USER = SimpleNamespace(id=999, display_name="Hermes", name="Hermes", bot=True)
HUMAN_USER = SimpleNamespace(id=42, display_name="Alice", name="Alice", bot=False)
OTHER_BOT = SimpleNamespace(id=1234, display_name="OtherBot", name="OtherBot", bot=True)


class FakeThread:
    def __init__(self, thread_id: int = 456, messages=None):
        self.id = thread_id
        self.name = "project-thread"
        self.guild = SimpleNamespace(id=1, name="Hermes Guild")
        self.parent = SimpleNamespace(id=123, name="pid", guild=self.guild)
        self.parent_id = self.parent.id
        self.topic = None
        self._messages = list(messages or [])
        self.join_count = 0

    async def join(self):
        self.join_count += 1

    def history(self, *, limit, oldest_first=False, after=None):
        after_id = int(getattr(after, "id", after)) if after is not None else None
        messages = [
            message for message in self._messages
            if after_id is None or int(message.id) > after_id
        ]
        messages.sort(key=lambda message: int(message.id), reverse=not oldest_first)

        async def _iter():
            for message in messages[:limit]:
                yield message

        return _iter()

    async def fetch_message(self, message_id):
        for message in self._messages:
            if int(message.id) == int(message_id):
                return message
        raise KeyError(message_id)


def make_message(*, thread, message_id: int, content: str, author=HUMAN_USER, mentions=None, reference=None, msg_type=None):
    return SimpleNamespace(
        id=message_id,
        content=content,
        clean_content=content,
        mentions=list(mentions or []),
        attachments=[],
        message_snapshots=[],
        reference=reference,
        created_at=datetime.now(timezone.utc),
        channel=thread,
        author=author,
        guild=thread.guild,
        type=msg_type if msg_type is not None else discord_platform.discord.MessageType.default,
    )


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("DISCORD_MISSED_THREAD_BACKFILL", raising=False)
    monkeypatch.delenv("DISCORD_MISSED_THREAD_BACKFILL_LIMIT", raising=False)
    monkeypatch.delenv("DISCORD_MISSED_THREAD_BACKFILL_MAX_AGE_SECONDS", raising=False)
    monkeypatch.delenv("DISCORD_ALLOWED_USERS", raising=False)
    monkeypatch.delenv("DISCORD_ALLOWED_ROLES", raising=False)
    monkeypatch.setattr(discord_platform.discord, "Thread", FakeThread, raising=False)

    config = PlatformConfig(
        enabled=True,
        token="test-token",
        extra={
            "missed_thread_backfill": True,
            "missed_thread_backfill_limit": 20,
            "missed_thread_backfill_max_age_seconds": 24 * 60 * 60,
        },
    )
    adapter = DiscordAdapter(config)
    adapter._text_batch_delay_seconds = 0
    adapter._client = SimpleNamespace(user=BOT_USER)
    adapter._handle_message = AsyncMock()
    return adapter


def attach_thread(adapter, thread):
    adapter._client = SimpleNamespace(
        user=BOT_USER,
        get_channel=lambda channel_id: thread if int(channel_id) == int(thread.id) else None,
        fetch_channel=AsyncMock(return_value=thread),
    )
    adapter._threads.mark(str(thread.id))


@pytest.mark.asyncio
async def test_tracked_thread_backfill_replays_missed_mention_after_last_bot_response(adapter):
    thread = FakeThread()
    original = make_message(thread=thread, message_id=100, content="<@999> start", mentions=[BOT_USER])
    bot_response = make_message(thread=thread, message_id=110, content="working", author=BOT_USER)
    missed = make_message(thread=thread, message_id=120, content="<@999> follow up", mentions=[])
    thread._messages = [original, bot_response, missed]
    attach_thread(adapter, thread)

    await adapter._backfill_missed_tracked_thread_messages()

    adapter._handle_message.assert_awaited_once_with(missed)
    assert thread.join_count == 0
    assert adapter._last_self_message_id[str(thread.id)] == "110"
    assert BOT_USER in missed.mentions


@pytest.mark.asyncio
async def test_tracked_thread_backfill_ignores_duplicate_message_ids(adapter):
    thread = FakeThread()
    bot_response = make_message(thread=thread, message_id=110, content="working", author=BOT_USER)
    missed = make_message(thread=thread, message_id=120, content="<@999> follow up", mentions=[BOT_USER])
    thread._messages = [bot_response, missed]
    attach_thread(adapter, thread)
    adapter._dedup.is_duplicate(str(missed.id))

    await adapter._backfill_missed_tracked_thread_messages()

    adapter._handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_tracked_thread_backfill_skips_non_mentions_and_bot_messages(adapter):
    thread = FakeThread()
    bot_response = make_message(thread=thread, message_id=110, content="working", author=BOT_USER)
    human_noise = make_message(thread=thread, message_id=120, content="not for you")
    bot_noise = make_message(thread=thread, message_id=130, content="<@999> bot chatter", author=OTHER_BOT, mentions=[BOT_USER])
    thread._messages = [bot_response, human_noise, bot_noise]
    attach_thread(adapter, thread)

    await adapter._backfill_missed_tracked_thread_messages()

    adapter._handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_tracked_thread_backfill_replays_reply_to_bot(adapter):
    thread = FakeThread()
    bot_response = make_message(thread=thread, message_id=110, content="working", author=BOT_USER)
    reply_ref = SimpleNamespace(message_id=110, channel_id=thread.id, resolved=None)
    missed_reply = make_message(
        thread=thread,
        message_id=120,
        content="please continue",
        reference=reply_ref,
        msg_type=discord_platform.discord.MessageType.reply,
    )
    thread._messages = [bot_response, missed_reply]
    attach_thread(adapter, thread)

    await adapter._backfill_missed_tracked_thread_messages()

    adapter._handle_message.assert_awaited_once_with(missed_reply)
    assert reply_ref.resolved is bot_response


@pytest.mark.asyncio
async def test_tracked_thread_backfill_default_limit_covers_older_tracked_threads(adapter):
    target = FakeThread(thread_id=1000)
    bot_response = make_message(thread=target, message_id=110, content="working", author=BOT_USER)
    missed = make_message(thread=target, message_id=120, content="<@999> follow up", mentions=[])
    target._messages = [bot_response, missed]
    adapter._client = SimpleNamespace(
        user=BOT_USER,
        get_channel=lambda channel_id: target if int(channel_id) == int(target.id) else None,
        fetch_channel=AsyncMock(return_value=None),
    )
    adapter._threads.mark(str(target.id))
    for thread_id in range(1001, 1061):
        adapter._threads.mark(str(thread_id))

    await adapter._backfill_missed_tracked_thread_messages()

    adapter._handle_message.assert_awaited_once_with(missed)
