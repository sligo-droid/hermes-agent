from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _source(*, chat_id="thread-1", chat_type="thread", thread_id="thread-1"):
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        chat_type=chat_type,
        thread_id=thread_id,
        parent_chat_id="parent-1" if thread_id else None,
        guild_id="guild-1",
        message_id="message-1",
    )


def _result():
    return {
        "action_escalation_requested": {
            "success": True,
            "action_escalation_requested": True,
        }
    }


@pytest.mark.asyncio
async def test_same_session_action_escalation_is_prepended_before_followups():
    runner = object.__new__(gateway_run.GatewayRunner)
    source = _source()
    event = MessageEvent(
        text="Could you make the parser handle this?",
        source=source,
        discord_action_request_intent=False,
    )
    promoted = MessageEvent(
        text=event.text,
        source=source,
        discord_action_request_intent=True,
        internal=True,
    )
    adapter = SimpleNamespace(
        promote_event_to_action_request=AsyncMock(
            return_value=(promoted, "https://discord.com/channels/guild-1/thread-1")
        ),
        handle_message=AsyncMock(),
    )
    runner._adapter_for_source = lambda _source: adapter
    runner._session_key_for_source = lambda _source: "discord:thread-1"
    runner._prepend_fifo = MagicMock()
    runner._evict_cached_agent = MagicMock()

    url = await runner._promote_discord_action_escalation(
        event=event,
        source=source,
        session_key="discord:thread-1",
        agent_result=_result(),
    )

    assert url.endswith("/thread-1")
    runner._prepend_fifo.assert_called_once_with(
        "discord:thread-1", promoted, adapter
    )
    adapter.handle_message.assert_not_awaited()
    runner._evict_cached_agent.assert_called_once_with("discord:thread-1")


@pytest.mark.asyncio
async def test_cross_session_action_escalation_dispatches_new_thread():
    runner = object.__new__(gateway_run.GatewayRunner)
    source = _source(chat_id="parent-1", chat_type="group", thread_id=None)
    promoted_source = _source()
    event = MessageEvent(
        text="Please take care of the mixed request",
        source=source,
        discord_action_request_intent=False,
    )
    promoted = MessageEvent(
        text=event.text,
        source=promoted_source,
        discord_action_request_intent=True,
        internal=True,
    )
    adapter = SimpleNamespace(
        promote_event_to_action_request=AsyncMock(return_value=(promoted, "thread-url")),
        handle_message=AsyncMock(),
    )
    runner._adapter_for_source = lambda _source: adapter
    runner._session_key_for_source = lambda candidate: (
        "discord:thread-1" if candidate.chat_type == "thread" else "discord:parent-1"
    )
    runner._prepend_fifo = MagicMock()
    runner._evict_cached_agent = MagicMock()

    assert await runner._promote_discord_action_escalation(
        event=event,
        source=source,
        session_key="discord:parent-1",
        agent_result=_result(),
    ) == "thread-url"

    adapter.handle_message.assert_awaited_once_with(promoted)
    runner._prepend_fifo.assert_not_called()
