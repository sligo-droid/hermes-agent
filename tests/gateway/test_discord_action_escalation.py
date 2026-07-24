from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource
from gateway.work_ledger import GatewayWorkLedger
from plugins.platforms.discord.adapter import DiscordAdapter


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
        discord_runtime_mode="read_only",
        discord_action_escalation_allowed=True,
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
    runner._session_run_generation = {"discord:thread-1": 1}

    url = await runner._promote_discord_action_escalation(
        event=event,
        source=source,
        session_key="discord:thread-1",
        run_generation=1,
        agent_result=_result(),
    )

    assert url.endswith("/thread-1")
    runner._prepend_fifo.assert_called_once_with(
        "discord:thread-1", promoted, adapter
    )
    adapter.handle_message.assert_not_awaited()
    runner._evict_cached_agent.assert_called_once_with("discord:thread-1")


def test_busy_target_promotion_stays_ahead_of_racing_followup():
    runner = object.__new__(gateway_run.GatewayRunner)
    source = _source()
    promoted = MessageEvent(text="Build it", source=source, discord_runtime_mode="action")
    promoted._discord_promotion_origin_session_key = "discord:thread-1"
    promoted._discord_promotion_origin_generation = 1
    followup = MessageEvent(text="one more detail", source=source)
    adapter = SimpleNamespace(_pending_messages={"discord:thread-1": followup})
    runner._queued_events = {}

    assert runner._defer_promoted_replay_to_fresh_turn(
        event=promoted,
        session_key="discord:thread-1",
        adapter=adapter,
    ) is True

    assert adapter._pending_messages["discord:thread-1"] is promoted
    assert runner._queued_events["discord:thread-1"] == [followup]


def test_read_only_turn_leaves_promoted_action_for_fresh_adapter_entry():
    source = _source()
    promoted = MessageEvent(text="Build it", source=source, discord_runtime_mode="action")
    promoted._discord_promotion_origin_session_key = "discord:thread-1"
    promoted._discord_promotion_origin_generation = 1
    pending = {"discord:thread-1": promoted}

    class Adapter:
        def get_pending_message(self, session_key):
            return pending.pop(session_key, None)

    replay = gateway_run._dequeue_pending_event_for_turn(
        Adapter(),
        "discord:thread-1",
        "read_only",
    )

    assert replay is None
    assert pending["discord:thread-1"] is promoted


def test_promotion_queued_during_restart_is_durable_action_work(tmp_path):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    source = _source()
    promoted = MessageEvent(
        text="Build it",
        source=source,
        message_id="message-1",
        discord_runtime_mode="action",
        discord_runtime_reason="promoted_action_replay",
        participates_in_work_lifecycle=True,
    )
    promoted._discord_promotion_origin_session_key = "discord:thread-1"
    promoted._discord_promotion_origin_generation = 1

    item = runner._record_discord_work_for_drain(promoted, "discord:thread-1")

    assert item is not None
    replay = runner.work_ledger.event_from_item(
        runner.work_ledger.get(item["id"])
    )
    assert replay.discord_runtime_mode == "action"
    assert replay.discord_runtime_reason == "promoted_action_replay"
    assert replay.participates_in_work_lifecycle is True
    assert not hasattr(replay, "_discord_promotion_origin_generation")


@pytest.mark.asyncio
async def test_restart_recovered_read_only_escalation_restores_base_channel_prompt(
    tmp_path,
):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    source = _source()
    event = MessageEvent(
        text="Please implement the recommended change",
        source=source,
        message_id="message-1",
        discord_runtime_mode="read_only",
        discord_runtime_reason="classified_read_only",
        discord_action_escalation_allowed=True,
        channel_prompt="Project instructions\n\nRead-only runtime overlay",
        discord_action_request_base_channel_prompt="Project instructions",
        feature_summary={
            "thread_id": "thread-1",
            "message_id": "feature-summary",
            "initial_request": "Earlier task",
        },
    )
    item = ledger.accept_event(
        event,
        session_key="discord:thread-1",
        freshness_seconds=60,
        drain_recovery=True,
    )
    replay = ledger.event_from_item(ledger.get(item["id"]))

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    thread = SimpleNamespace(
        id="thread-1",
        parent=SimpleNamespace(id="parent-1"),
        guild=SimpleNamespace(id="guild-1"),
    )
    adapter._resolve_channel_by_id = AsyncMock(return_value=thread)
    adapter._load_feature_summary_handle_for_request = MagicMock(
        return_value=replay.feature_summary
    )
    adapter._resolve_project_context_for_channel = MagicMock(return_value=None)
    adapter._threads.mark = MagicMock()
    adapter._mark_discord_thread_participation = MagicMock()

    promoted, _url = await adapter.promote_event_to_action_request(
        replay,
        initial_request=replay.text,
    )

    assert replay.channel_prompt.endswith("Read-only runtime overlay")
    assert replay.discord_action_request_base_channel_prompt == "Project instructions"
    assert promoted is not None
    assert promoted.discord_runtime_mode == "action"
    assert promoted.channel_prompt == "Project instructions"


@pytest.mark.asyncio
async def test_new_promotion_thread_preseeds_starter_dedup():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    parent = SimpleNamespace(id=123, name="planning")
    thread = SimpleNamespace(
        id=456,
        name="Build it",
        parent=parent,
        guild=SimpleNamespace(id=789),
    )
    raw_message = SimpleNamespace(channel=parent, thread=None)
    event = MessageEvent(
        text="Build it",
        source=_source(chat_id="parent-1", chat_type="group", thread_id=None),
        raw_message=raw_message,
        message_id="message-1",
        discord_runtime_mode="read_only",
        discord_action_escalation_allowed=True,
        project_summary={"message_id": "project-summary"},
    )
    adapter._auto_create_thread = AsyncMock(return_value=thread)
    adapter._resolve_project_context_for_channel = MagicMock(return_value=None)
    adapter._load_feature_summary_handle_for_request = MagicMock(
        return_value={
            "thread_id": "456",
            "message_id": "feature-summary",
            "initial_request": "Build it",
        }
    )
    adapter._threads.mark = MagicMock()
    adapter._mark_discord_thread_participation = MagicMock()

    promoted, _url = await adapter.promote_event_to_action_request(
        event,
        initial_request="Build it",
    )

    assert promoted is not None
    assert promoted.discord_runtime_mode == "action"
    assert adapter._dedup.is_duplicate("456") is True


@pytest.mark.asyncio
async def test_cross_session_action_escalation_dispatches_new_thread():
    runner = object.__new__(gateway_run.GatewayRunner)
    source = _source(chat_id="parent-1", chat_type="group", thread_id=None)
    promoted_source = _source()
    event = MessageEvent(
        text="Please take care of the mixed request",
        source=source,
        discord_action_request_intent=False,
        discord_runtime_mode="read_only",
        discord_action_escalation_allowed=True,
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
    runner._session_run_generation = {"discord:parent-1": 1}

    assert await runner._promote_discord_action_escalation(
        event=event,
        source=source,
        session_key="discord:parent-1",
        run_generation=1,
        agent_result=_result(),
    ) == "thread-url"

    adapter.handle_message.assert_awaited_once_with(promoted)
    runner._prepend_fifo.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_no_action_event_cannot_promote_even_with_model_payload():
    runner = object.__new__(gateway_run.GatewayRunner)
    source = _source()
    event = MessageEvent(
        text="Do not implement; plan only.",
        source=source,
        discord_runtime_mode="read_only",
        discord_action_escalation_allowed=False,
        discord_runtime_reason="explicit_no_implementation",
    )
    adapter = SimpleNamespace(
        promote_event_to_action_request=AsyncMock(),
        rollback_promoted_action_request=AsyncMock(),
    )
    runner._adapter_for_source = lambda _source: adapter
    runner._session_run_generation = {"discord:thread-1": 1}

    assert await runner._promote_discord_action_escalation(
        event=event,
        source=source,
        session_key="discord:thread-1",
        run_generation=1,
        agent_result=_result(),
    ) is None
    adapter.promote_event_to_action_request.assert_not_awaited()


@pytest.mark.asyncio
async def test_generation_change_during_promotion_rolls_back_and_never_dispatches():
    runner = object.__new__(gateway_run.GatewayRunner)
    source = _source()
    event = MessageEvent(
        text="Could you make the parser handle this?",
        source=source,
        discord_runtime_mode="read_only",
        discord_action_escalation_allowed=True,
    )
    promoted = MessageEvent(text=event.text, source=source, discord_runtime_mode="action")
    runner._session_run_generation = {"discord:thread-1": 1}

    async def promote(*_args, **_kwargs):
        runner._session_run_generation["discord:thread-1"] = 2
        return promoted, "thread-url"

    adapter = SimpleNamespace(
        promote_event_to_action_request=AsyncMock(side_effect=promote),
        rollback_promoted_action_request=AsyncMock(),
        handle_message=AsyncMock(),
    )
    runner._adapter_for_source = lambda _source: adapter
    runner._session_key_for_source = lambda _source: "discord:thread-1"
    runner._prepend_fifo = MagicMock()
    runner._evict_cached_agent = MagicMock()

    assert await runner._promote_discord_action_escalation(
        event=event,
        source=source,
        session_key="discord:thread-1",
        run_generation=1,
        agent_result=_result(),
    ) is None
    adapter.rollback_promoted_action_request.assert_awaited_once_with(promoted)
    runner._prepend_fifo.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_generation_barrier_rechecks_immediately_before_enqueue():
    runner = object.__new__(gateway_run.GatewayRunner)
    source = _source()
    event = MessageEvent(
        text="Could you make the parser handle this?",
        source=source,
        discord_runtime_mode="read_only",
        discord_action_escalation_allowed=True,
    )
    promoted = MessageEvent(text=event.text, source=source, discord_runtime_mode="action")
    adapter = SimpleNamespace(
        promote_event_to_action_request=AsyncMock(return_value=(promoted, "thread-url")),
        rollback_promoted_action_request=AsyncMock(),
        handle_message=AsyncMock(),
    )
    runner._adapter_for_source = lambda _source: adapter
    runner._session_key_for_source = lambda _source: "discord:thread-1"
    runner._prepend_fifo = MagicMock()
    runner._evict_cached_agent = MagicMock()
    runner._is_session_run_current = MagicMock(side_effect=[True, True, False])

    assert await runner._promote_discord_action_escalation(
        event=event,
        source=source,
        session_key="discord:thread-1",
        run_generation=1,
        agent_result=_result(),
    ) is None

    adapter.rollback_promoted_action_request.assert_awaited_once_with(promoted)
    runner._prepend_fifo.assert_not_called()
    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_promoted_replay_is_rejected_when_consumed():
    runner = object.__new__(gateway_run.GatewayRunner)
    source = _source()
    event = MessageEvent(text="Build it", source=source, discord_runtime_mode="action")
    event._discord_promotion_origin_session_key = "discord:thread-1"
    event._discord_promotion_origin_generation = 1
    runner._session_run_generation = {"discord:thread-1": 2}
    adapter = SimpleNamespace(rollback_promoted_action_request=AsyncMock())
    runner._adapter_for_source = lambda _source: adapter

    assert await runner._consume_promoted_replay_fence(event) is False
    adapter.rollback_promoted_action_request.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_promoted_replay_fence_survives_dequeue_and_rechecks_at_dispatch():
    runner = object.__new__(gateway_run.GatewayRunner)
    source = _source()
    event = MessageEvent(text="Build it", source=source, discord_runtime_mode="action")
    event._discord_promotion_origin_session_key = "discord:thread-1"
    event._discord_promotion_origin_generation = 1
    runner._session_run_generation = {"discord:thread-1": 1}
    adapter = SimpleNamespace(rollback_promoted_action_request=AsyncMock())
    runner._adapter_for_source = lambda _source: adapter

    assert await runner._consume_promoted_replay_fence(event, consume=False) is True
    assert event._discord_promotion_origin_generation == 1

    runner._session_run_generation["discord:thread-1"] = 2
    assert await runner._consume_promoted_replay_fence(event) is False
    adapter.rollback_promoted_action_request.assert_awaited_once_with(event)
