"""Tests for Discord message reactions tied to processing lifecycle hooks."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome, SendResult
from gateway.session import SessionSource, build_session_key


def _ensure_discord_mock():
    if "discord" in sys.modules and hasattr(sys.modules["discord"], "__file__"):
        return

    discord_mod = MagicMock()
    discord_mod.Intents.default.return_value = MagicMock()
    discord_mod.DMChannel = type("DMChannel", (), {})
    discord_mod.Thread = type("Thread", (), {})
    discord_mod.ForumChannel = type("ForumChannel", (), {})
    discord_mod.Interaction = object
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

from gateway.platforms.discord import DiscordAdapter  # noqa: E402
from gateway.platforms.discord import discord as discord_module  # noqa: E402


class FakeTree:
    def __init__(self):
        self.commands = {}

    def command(self, *, name, description):
        def decorator(fn):
            self.commands[name] = fn
            return fn

        return decorator


@pytest.fixture
def adapter():
    config = PlatformConfig(enabled=True, token="***")
    adapter = DiscordAdapter(config)
    adapter._client = SimpleNamespace(
        tree=FakeTree(),
        get_channel=lambda _id: None,
        fetch_channel=AsyncMock(),
        user=SimpleNamespace(id=99999, name="HermesBot"),
    )
    return adapter


def _make_event(message_id: str, raw_message) -> MessageEvent:
    return MessageEvent(
        text="hello",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="123",
            chat_type="dm",
            user_id="42",
            user_name="Jezza",
        ),
        raw_message=raw_message,
        message_id=message_id,
    )


@pytest.mark.asyncio
async def test_process_message_background_adds_and_swaps_reactions(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )

    async def handler(_event):
        await asyncio.sleep(0)
        return "ack"

    async def hold_typing(_chat_id, interval=2.0, metadata=None):
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="999"))
    adapter._keep_typing = hold_typing

    event = _make_event("1", raw_message)
    await adapter._process_message_background(event, build_session_key(event.source))

    assert raw_message.add_reaction.await_args_list[0].args == ("👀",)
    assert raw_message.remove_reaction.await_args_list[0].args == ("✅", adapter._client.user)
    assert raw_message.remove_reaction.await_args_list[1].args == ("❌", adapter._client.user)
    assert raw_message.remove_reaction.await_args_list[2].args == ("👀", adapter._client.user)
    assert raw_message.add_reaction.await_args_list[1].args == ("✅",)


@pytest.mark.asyncio
async def test_direct_question_thread_uses_normal_lifecycle_reactions(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("1", raw_message)
    event.source.chat_type = "thread"
    event.source.chat_id = "200"
    event.source.thread_id = "200"
    event.source.parent_chat_id = "100"
    event.feature_summary = None

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert [call.args for call in raw_message.remove_reaction.await_args_list] == [
        ("✅", adapter._client.user),
        ("❌", adapter._client.user),
        ("👀", adapter._client.user),
    ]
    assert [call.args for call in raw_message.add_reaction.await_args_list] == [
        ("👀",),
        ("✅",),
    ]


@pytest.mark.asyncio
async def test_reaction_completion_waits_for_queued_follow_up(adapter):
    first_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    second_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    second_started = asyncio.Event()
    release_second = asyncio.Event()

    first_event = _make_event("1", first_message)
    session_key = build_session_key(first_event.source)
    second_event = _make_event("2", second_message)

    async def handler(event):
        if event.message_id == "1":
            adapter._pending_messages[session_key] = second_event
            return "first ack"
        second_started.set()
        await release_second.wait()
        return "second ack"

    async def hold_typing(_chat_id, interval=2.0, metadata=None):
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="999"))
    adapter._keep_typing = hold_typing

    task = asyncio.create_task(
        adapter._process_message_background(first_event, session_key)
    )
    await asyncio.wait_for(second_started.wait(), timeout=1.0)

    first_message.add_reaction.assert_awaited_once_with("👀")
    assert [call.args for call in first_message.remove_reaction.await_args_list] == [
        ("✅", adapter._client.user),
        ("❌", adapter._client.user),
    ]

    release_second.set()
    await task
    for _ in range(100):
        if session_key not in adapter._active_sessions:
            break
        await asyncio.sleep(0.01)

    assert [call.args for call in first_message.remove_reaction.await_args_list] == [
        ("✅", adapter._client.user),
        ("❌", adapter._client.user),
        ("👀", adapter._client.user),
    ]
    assert first_message.add_reaction.await_args_list[1].args == ("✅",)
    assert [call.args for call in second_message.remove_reaction.await_args_list] == [
        ("✅", adapter._client.user),
        ("❌", adapter._client.user),
        ("👀", adapter._client.user),
    ]
    assert second_message.add_reaction.await_args_list[1].args == ("✅",)


@pytest.mark.asyncio
async def test_interaction_backed_events_do_not_attempt_reactions(adapter):
    interaction = SimpleNamespace(guild_id=123456789)

    async def handler(_event):
        await asyncio.sleep(0)
        return None

    async def hold_typing(_chat_id, interval=2.0, metadata=None):
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    adapter._add_reaction = AsyncMock()
    adapter._remove_reaction = AsyncMock()
    adapter._keep_typing = hold_typing

    event = MessageEvent(
        text="/status",
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="123",
            chat_type="dm",
            user_id="42",
            user_name="Jezza",
        ),
        raw_message=interaction,
        message_id="2",
    )

    await adapter._process_message_background(event, build_session_key(event.source))

    adapter._add_reaction.assert_not_awaited()
    adapter._remove_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_reaction_helper_failures_do_not_break_message_flow(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(side_effect=[RuntimeError("no perms"), RuntimeError("no perms")]),
        remove_reaction=AsyncMock(side_effect=RuntimeError("no perms")),
    )

    async def handler(_event):
        await asyncio.sleep(0)
        return "ack"

    async def hold_typing(_chat_id, interval=2.0, metadata=None):
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="999"))
    adapter._keep_typing = hold_typing

    event = _make_event("3", raw_message)
    await adapter._process_message_background(event, build_session_key(event.source))

    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_reactions_disabled_via_env(adapter, monkeypatch):
    """When DISCORD_REACTIONS=false, no reactions should be added."""
    monkeypatch.setenv("DISCORD_REACTIONS", "false")

    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )

    async def handler(_event):
        await asyncio.sleep(0)
        return "ack"

    async def hold_typing(_chat_id, interval=2.0, metadata=None):
        await asyncio.Event().wait()

    adapter.set_message_handler(handler)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="999"))
    adapter._keep_typing = hold_typing

    event = _make_event("4", raw_message)
    await adapter._process_message_background(event, build_session_key(event.source))

    raw_message.add_reaction.assert_not_awaited()
    raw_message.remove_reaction.assert_not_awaited()
    # Response should still be sent
    adapter.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_reactions_disabled_via_env_zero(adapter, monkeypatch):
    """DISCORD_REACTIONS=0 should also disable reactions."""
    monkeypatch.setenv("DISCORD_REACTIONS", "0")

    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )

    event = _make_event("5", raw_message)
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    raw_message.add_reaction.assert_not_awaited()
    raw_message.remove_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_reactions_enabled_by_default(adapter, monkeypatch):
    """When DISCORD_REACTIONS is unset, reactions should still work (default: true)."""
    monkeypatch.delenv("DISCORD_REACTIONS", raising=False)

    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )

    event = _make_event("6", raw_message)
    await adapter.on_processing_start(event)

    raw_message.add_reaction.assert_awaited_once_with("👀")


@pytest.mark.asyncio
async def test_on_processing_complete_cancelled_removes_eyes_without_terminal_reaction(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )

    event = _make_event("7", raw_message)
    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)

    raw_message.remove_reaction.assert_awaited_once_with("👀", adapter._client.user)
    raw_message.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_feature_thread_reactions_target_triggering_user_message(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    summary_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("8", raw_message)
    event.source.chat_type = "thread"
    event.feature_summary = {
        "thread_id": "123",
        "message_id": "456",
        "_message_obj": summary_message,
    }
    adapter.update_feature_summary = AsyncMock(return_value=True)

    await adapter.on_processing_start(event)

    adapter.update_feature_summary.assert_awaited_once_with(event.feature_summary, status="Running")
    summary_message.add_reaction.assert_not_awaited()
    summary_message.remove_reaction.assert_not_awaited()
    assert [call.args for call in raw_message.remove_reaction.await_args_list] == [
        ("✅", adapter._client.user),
        ("❌", adapter._client.user),
    ]
    raw_message.add_reaction.assert_awaited_once_with("👀")

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert [call.args for call in raw_message.remove_reaction.await_args_list] == [
        ("✅", adapter._client.user),
        ("❌", adapter._client.user),
        ("👀", adapter._client.user),
    ]
    assert raw_message.add_reaction.await_args_list[1].args == ("✅",)


@pytest.mark.asyncio
async def test_top_level_feature_summary_reactions_target_triggering_user_message(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    summary_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("9", raw_message)
    event.source.chat_type = "group"
    event.feature_summary = {
        "thread_id": "123",
        "message_id": "456",
        "_message_obj": summary_message,
    }
    adapter.update_feature_summary = AsyncMock(return_value=True)

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter.update_feature_summary.assert_awaited_once_with(event.feature_summary, status="Running")
    summary_message.add_reaction.assert_not_awaited()
    summary_message.remove_reaction.assert_not_awaited()
    assert [call.args for call in raw_message.remove_reaction.await_args_list] == [
        ("✅", adapter._client.user),
        ("❌", adapter._client.user),
        ("👀", adapter._client.user),
    ]
    assert [call.args for call in raw_message.add_reaction.await_args_list] == [
        ("👀",),
        ("✅",),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "emoji"),
    [
        ("active", "👀"),
        ("done", "✅"),
        ("blocked", "❓"),
        ("errored", "❌"),
    ],
)
async def test_feature_summary_reactions_follow_kanban_state(adapter, state, emoji):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("9", raw_message)
    event.feature_summary = {
        "thread_id": "123",
        "message_id": "456",
        "kanban_board": {"slug": "discord-thread-123"},
    }
    adapter._feature_kanban_reaction_state = MagicMock(return_value=state)

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert raw_message.add_reaction.await_args_list[-1].args == (emoji,)
    assert ("❓", adapter._client.user) in [call.args for call in raw_message.remove_reaction.await_args_list]


@pytest.mark.asyncio
async def test_thread_followup_reactions_target_origin_message(adapter):
    origin_message = SimpleNamespace(
        id=1000,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    followup_message = SimpleNamespace(
        id=2000,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    parent = SimpleNamespace(
        id=55,
        fetch_message=AsyncMock(return_value=origin_message),
    )
    thread = _StatusThread(thread_id=1000, name="Build dashboard")
    thread.parent = parent
    thread.parent_id = parent.id
    thread.fetch_message = AsyncMock(side_effect=LookupError("not cached in thread"))
    followup_message.channel = thread

    state = {}
    adapter._read_project_summary_state = MagicMock(side_effect=lambda: dict(state))
    adapter._write_project_summary_state = MagicMock(
        side_effect=lambda value: state.clear() or state.update(value)
    )

    event = _make_event("2000", followup_message)
    event.source.chat_type = "thread"
    event.source.thread_id = str(thread.id)
    event.source.parent_chat_id = str(parent.id)

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    parent.fetch_message.assert_awaited_with(1000)
    followup_message.add_reaction.assert_not_awaited()
    followup_message.remove_reaction.assert_not_awaited()
    assert [call.args for call in origin_message.remove_reaction.await_args_list] == [
        ("✅", adapter._client.user),
        ("❌", adapter._client.user),
        ("👀", adapter._client.user),
    ]
    assert [call.args for call in origin_message.add_reaction.await_args_list] == [
        ("👀",),
        ("✅",),
    ]


@pytest.mark.asyncio
async def test_batched_thread_followup_reactions_target_origin_message(adapter):
    origin_message = SimpleNamespace(
        id=1000,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    followup_message = SimpleNamespace(
        id=2000,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    parent = SimpleNamespace(
        id=55,
        fetch_message=AsyncMock(return_value=origin_message),
    )
    thread = _StatusThread(thread_id=1000, name="Build dashboard")
    setattr(thread, "parent", parent)
    thread.parent_id = parent.id
    setattr(thread, "fetch_message", AsyncMock(side_effect=LookupError("not cached in thread")))
    followup_message.channel = thread

    event = _make_event("2000", followup_message)
    event.source.chat_type = "thread"
    event.source.thread_id = str(thread.id)
    event.source.parent_chat_id = str(parent.id)
    setattr(event, "_batched_raw_messages", [followup_message])

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    followup_message.add_reaction.assert_not_awaited()
    followup_message.remove_reaction.assert_not_awaited()
    assert [call.args for call in origin_message.remove_reaction.await_args_list] == [
        ("✅", adapter._client.user),
        ("❌", adapter._client.user),
        ("👀", adapter._client.user),
    ]
    assert [call.args for call in origin_message.add_reaction.await_args_list] == [
        ("👀",),
        ("✅",),
    ]


@pytest.mark.asyncio
async def test_batched_text_lifecycle_reactions_target_every_user_message(adapter):
    first_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    second_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("10", first_message)
    event._batched_raw_messages = [first_message, second_message]

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.FAILURE)

    for message in (first_message, second_message):
        assert [call.args for call in message.remove_reaction.await_args_list] == [
            ("✅", adapter._client.user),
            ("❌", adapter._client.user),
            ("👀", adapter._client.user),
        ]
        assert [call.args for call in message.add_reaction.await_args_list] == [
            ("👀",),
            ("❌",),
        ]


@pytest.mark.asyncio
async def test_text_batching_preserves_all_user_reaction_messages(adapter):
    adapter._text_batch_delay_seconds = 30
    first_message = SimpleNamespace(add_reaction=AsyncMock(), remove_reaction=AsyncMock())
    second_message = SimpleNamespace(add_reaction=AsyncMock(), remove_reaction=AsyncMock())
    first = _make_event("11", first_message)
    second = _make_event("12", second_message)

    adapter._enqueue_text_event(first)
    adapter._enqueue_text_event(second)

    key = adapter._text_batch_key(first)
    pending = adapter._pending_text_batches[key]
    assert pending.raw_message is first_message
    assert pending._batched_raw_messages == [first_message, second_message]

    for task in list(adapter._pending_text_batch_tasks.values()):
        task.cancel()
    await asyncio.sleep(0)


class _StatusThread:
    def __init__(self, *, thread_id=777, name="Build thing"):
        self.id = thread_id
        self.name = name
        self.parent_id = 55
        self.guild = SimpleNamespace(id=10)
        self.edit = AsyncMock(side_effect=self._edit)

    async def _edit(self, **kwargs):
        if "name" in kwargs:
            self.name = kwargs["name"]


def _thread_status_event(message_id: str, thread: _StatusThread) -> MessageEvent:
    raw_message = SimpleNamespace(
        id=int(message_id),
        channel=thread,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event(message_id, raw_message)
    event.source.chat_type = "thread"
    event.source.thread_id = str(thread.id)
    event.source.parent_chat_id = str(thread.parent_id)
    return event


@pytest.mark.asyncio
async def test_processing_lifecycle_does_not_rename_discord_thread(adapter):
    thread = _StatusThread(name="Build dashboard")
    event = _thread_status_event("1", thread)

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert thread.name == "Build dashboard"
    thread.edit.assert_not_awaited()
    event.raw_message.add_reaction.assert_any_await("👀")
    event.raw_message.add_reaction.assert_any_await("✅")


def _ship_payload(*, emoji="👍", user_id=42, channel_id=123, message_id=999, guild_id=10):
    return SimpleNamespace(
        emoji=emoji,
        user_id=user_id,
        channel_id=channel_id,
        message_id=message_id,
        guild_id=guild_id,
    )


def _bot_message(adapter, channel, *, content="Done."):
    return SimpleNamespace(
        id=999,
        content=content,
        author=adapter._client.user,
        channel=channel,
        guild=getattr(channel, "guild", None),
    )


def _guild_channel(adapter, *, author=None):
    guild = SimpleNamespace(id=10, name="Test Guild")
    channel = SimpleNamespace(id=123, name="features", guild=guild, parent_id=None)
    message = SimpleNamespace(
        id=999,
        content="Implemented.",
        author=author or adapter._client.user,
        channel=channel,
        guild=guild,
    )
    channel.fetch_message = AsyncMock(return_value=message)
    adapter._client.get_channel = lambda _id: channel
    return channel, message


@pytest.mark.asyncio
async def test_thumbsup_on_hermes_message_dispatches_ship_it(adapter):
    channel, _message = _guild_channel(adapter)
    user = SimpleNamespace(id=42, name="jezza", display_name="Jezza", roles=[])
    payload = _ship_payload()
    payload.member = user
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "ship it"
    assert event.message_type is MessageType.TEXT
    assert event.raw_message is payload
    assert event.message_id == "999"
    assert event.reply_to_message_id == "999"
    assert event.reply_to_text == "Implemented."
    assert event.source.platform is Platform.DISCORD
    assert event.source.chat_id == "123"
    assert event.source.chat_type == "group"
    assert event.source.user_id == "42"
    assert event.source.user_name == "Jezza"
    assert event.source.guild_id == "10"


@pytest.mark.asyncio
async def test_custom_thumbsup_name_dispatches_ship_it(adapter):
    _channel, _message = _guild_channel(adapter)
    payload = _ship_payload(emoji=SimpleNamespace(name="thumbsup"))
    payload.member = SimpleNamespace(id=42, name="jezza", display_name="Jezza", roles=[])
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].text == "ship it"


@pytest.mark.asyncio
async def test_ship_reaction_routes_thread_session(adapter, monkeypatch):
    class FakeThread:
        pass

    monkeypatch.setattr(discord_module, "Thread", FakeThread)
    guild = SimpleNamespace(id=10, name="Test Guild")
    parent = SimpleNamespace(id=55, name="features", guild=guild, type=0, topic="parent topic")
    thread = FakeThread()
    thread.id = 777
    thread.name = "Implement thing"
    thread.guild = guild
    thread.parent = parent
    thread.parent_id = parent.id
    thread.fetch_message = AsyncMock(return_value=_bot_message(adapter, thread))
    adapter._client.get_channel = lambda _id: thread
    payload = _ship_payload(channel_id=777)
    payload.member = SimpleNamespace(id=42, name="jezza", display_name="Jezza", roles=[])
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "ship it"
    assert event.source.chat_id == "777"
    assert event.source.chat_type == "thread"
    assert event.source.thread_id == "777"
    assert event.source.parent_chat_id == "55"


@pytest.mark.asyncio
async def test_ship_reaction_ignores_non_thumbsup(adapter):
    _channel, _message = _guild_channel(adapter)
    payload = _ship_payload(emoji="✅")
    payload.member = SimpleNamespace(id=42, name="jezza", display_name="Jezza", roles=[])
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ship_reaction_ignores_non_hermes_message(adapter):
    other_author = SimpleNamespace(id=111, name="other", display_name="Other")
    _channel, _message = _guild_channel(adapter, author=other_author)
    payload = _ship_payload()
    payload.member = SimpleNamespace(id=42, name="jezza", display_name="Jezza", roles=[])
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ship_reaction_ignores_bot_self_reaction(adapter):
    _channel, _message = _guild_channel(adapter)
    payload = _ship_payload(user_id=adapter._client.user.id)
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ship_reaction_enforces_authorization(adapter):
    _channel, _message = _guild_channel(adapter)
    adapter._allowed_user_ids = {"999"}
    payload = _ship_payload()
    payload.member = SimpleNamespace(id=42, name="jezza", display_name="Jezza", roles=[])
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_ship_reaction_deduplicates_by_message(adapter):
    _channel, _message = _guild_channel(adapter)
    payload = _ship_payload()
    payload.member = SimpleNamespace(id=42, name="jezza", display_name="Jezza", roles=[])
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)
    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_ship_reaction_event_does_not_get_lifecycle_reactions(adapter):
    _channel, _message = _guild_channel(adapter)
    payload = _ship_payload()
    payload.member = SimpleNamespace(id=42, name="jezza", display_name="Jezza", roles=[])
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)
    event = adapter.handle_message.await_args.args[0]

    adapter._add_reaction = AsyncMock()
    adapter._remove_reaction = AsyncMock()
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter._add_reaction.assert_not_awaited()
    adapter._remove_reaction.assert_not_awaited()
