"""Tests for Discord message reactions tied to processing lifecycle hooks."""

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.visual_qa import visual_requirement_id
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome, SendResult
from gateway.session import SessionSource, build_session_key
from gateway.work_ledger import GatewayWorkLedger


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

from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402
import plugins.platforms.discord.adapter as discord_platform  # noqa: E402


STATUS_REACTION_EMOJIS = ("✅", "❌", "👀", "❓", "⏳", "🔨")


def _status_remove_calls(adapter, *, except_emoji=None):
    return [
        (emoji, adapter._client.user)
        for emoji in STATUS_REACTION_EMOJIS
        if emoji != except_emoji
    ]


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

    assert [call.args for call in raw_message.remove_reaction.await_args_list] == [
        ("⏳", adapter._client.user),
    ]
    assert [call.args for call in raw_message.add_reaction.await_args_list] == [
        ("⏳",),
        ("✅",),
    ]


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
        ("⏳", adapter._client.user),
    ]
    assert [call.args for call in raw_message.add_reaction.await_args_list] == [
        ("⏳",),
        ("✅",),
    ]


@pytest.mark.asyncio
async def test_thread_reactions_update_triggering_message_and_opener(adapter):
    origin_message = SimpleNamespace(
        id=100,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    thread = SimpleNamespace(id=200, parent_id=50, starter_message=origin_message)
    raw_message = SimpleNamespace(
        id=300,
        channel=thread,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("300", raw_message)
    event.source.chat_type = "thread"
    event.source.chat_id = "200"
    event.source.thread_id = "200"

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    for message in (raw_message, origin_message):
        assert [call.args for call in message.add_reaction.await_args_list] == [
            ("⏳",),
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

    first_message.add_reaction.assert_awaited_once_with("⏳")
    first_message.remove_reaction.assert_not_awaited()

    release_second.set()
    await task
    for _ in range(100):
        if session_key not in adapter._active_sessions:
            break
        await asyncio.sleep(0.01)

    assert [call.args for call in first_message.remove_reaction.await_args_list] == [
        ("⏳", adapter._client.user),
    ]
    assert first_message.add_reaction.await_args_list[1].args == ("✅",)
    assert [call.args for call in second_message.remove_reaction.await_args_list] == [
        ("⏳", adapter._client.user),
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

    raw_message.add_reaction.assert_awaited_once_with("⏳")


@pytest.mark.asyncio
async def test_on_processing_complete_cancelled_removes_eyes_without_terminal_reaction(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )

    event = _make_event("7", raw_message)
    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)

    raw_message.remove_reaction.assert_not_awaited()
    raw_message.add_reaction.assert_not_awaited()
    assert adapter._message_identity(raw_message) not in adapter._hermes_reaction_states


@pytest.mark.asyncio
async def test_cancelled_transition_records_known_absent_after_known_running(adapter):
    raw_message = SimpleNamespace(
        id=71,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("71", raw_message)

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.CANCELLED)

    raw_message.remove_reaction.assert_awaited_once_with("⏳", adapter._client.user)
    assert adapter._hermes_reaction_states[adapter._message_identity(raw_message)] is None


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

    adapter.update_feature_summary.assert_awaited_once()
    assert adapter.update_feature_summary.await_args.args == (event.feature_summary,)
    assert adapter.update_feature_summary.await_args.kwargs["status"] == "Running"
    summary_message.add_reaction.assert_not_awaited()
    summary_message.remove_reaction.assert_not_awaited()
    raw_message.remove_reaction.assert_not_awaited()
    raw_message.add_reaction.assert_awaited_once_with("⏳")

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert [call.args for call in raw_message.remove_reaction.await_args_list] == [
        ("⏳", adapter._client.user),
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

    adapter.update_feature_summary.assert_awaited_once()
    assert adapter.update_feature_summary.await_args.args == (event.feature_summary,)
    assert adapter.update_feature_summary.await_args.kwargs["status"] == "Running"
    summary_message.add_reaction.assert_not_awaited()
    summary_message.remove_reaction.assert_not_awaited()
    assert [call.args for call in raw_message.remove_reaction.await_args_list] == [
        ("⏳", adapter._client.user),
    ]
    assert [call.args for call in raw_message.add_reaction.await_args_list] == [
        ("⏳",),
        ("✅",),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "emoji"),
    [
        ("active", "⏳"),
        ("running", "⏳"),
        ("done", "✅"),
        ("blocked", "❓"),
        ("errored", "❌"),
        ("foreman", "🔨"),
    ],
)
async def test_feature_summary_reactions_follow_kanban_state(adapter, monkeypatch, state, emoji):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("9", raw_message)
    event.feature_summary = {
        "thread_id": "123",
        "message_id": "456",
        "kanban_board": {"slug": "discord-123"},
    }
    adapter._feature_kanban_reaction_state = MagicMock(return_value=state)
    monkeypatch.setattr(
        "hermes_cli.discord_worker_boards.thread_status_targets",
        lambda: [{"thread_id": "123", "reaction_state": state}],
    )

    await adapter.on_processing_start(event)
    removed_before_complete = len(raw_message.remove_reaction.await_args_list)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert raw_message.add_reaction.await_args_list[-1].args == (emoji,)
    completion_removes = [
        call.args
        for call in raw_message.remove_reaction.await_args_list[removed_before_complete:]
    ]
    assert (emoji, adapter._client.user) not in completion_removes
    assert completion_removes == (
        [] if emoji == "⏳" else [("⏳", adapter._client.user)]
    )


def test_enforced_visual_qa_gate_keeps_prose_and_reaction_state_synchronized(
    adapter,
    tmp_path,
):
    blocked_ledger = GatewayWorkLedger(tmp_path / "blocked-ledger.json")
    blocked_event = MessageEvent(
        text="Build a responsive dashboard with a mobile sidebar.",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="blocked-thread",
            chat_type="thread",
            thread_id="blocked-thread",
            guild_id="77",
            message_id="blocked-message",
        ),
        message_id="blocked-message",
    )
    blocked = blocked_ledger.accept_event(
        blocked_event,
        session_key=build_session_key(blocked_event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert blocked is not None
    assert blocked_ledger.mark_agent_done(
        blocked["id"],
        final_response="Fresh verification passed.",
        visual_qa_receipts=[],
        visual_qa_code_mutation_observed=True,
        visual_qa_min_receipt_order=2,
    )
    blocked_stored = blocked_ledger.get(blocked["id"])
    assert blocked_stored["final_response"].startswith("⚠️ **Completion blocked.**")
    assert "Fresh verification passed." in blocked_stored["final_response"]
    assert blocked_ledger.mark_completed(blocked["id"])
    blocked_state = blocked_ledger.discord_thread_reaction_state(blocked)
    assert blocked_state == "blocked"
    assert adapter._feature_kanban_reaction_emoji(blocked_state) == "❓"

    successful_ledger = GatewayWorkLedger(tmp_path / "successful-ledger.json")
    successful_event = MessageEvent(
        text="Build a responsive dashboard with a mobile sidebar.",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="successful-thread",
            chat_type="thread",
            thread_id="successful-thread",
            guild_id="77",
            message_id="successful-message",
        ),
        message_id="successful-message",
    )
    successful = successful_ledger.accept_event(
        successful_event,
        session_key=build_session_key(successful_event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert successful is not None
    requirement = successful["visual_qa_requirement"]
    coverage_ids = [item["id"] for item in requirement["assertions"]]
    assert successful_ledger.mark_agent_done(
        successful["id"],
        final_response=(
            "Implemented the responsive dashboard and mobile sidebar. "
            "Fresh verification and Visual QA passed. PR #42 is merged."
        ),
        visual_qa_receipts=[
            {
                "requirement_id": visual_requirement_id(requirement),
                "contract_id": "vac_" + ("a" * 24),
                "coverage_ids": coverage_ids,
                "assertion_ids": ["vassert_" + ("c" * 24)],
                "status": "passed",
                "attempts": 1,
                "vision_calls": 0,
                "duration_ms": 25,
                "diagnostic_codes": [],
                "order": 2,
            }
        ],
        visual_qa_code_mutation_observed=True,
        visual_qa_min_receipt_order=2,
    )
    successful_stored = successful_ledger.get(successful["id"])
    assert successful_stored["final_response"] == (
        "Implemented the responsive dashboard and mobile sidebar. "
        "Fresh verification and Visual QA passed. PR #42 is merged."
    )
    assert successful_ledger.mark_completed(successful["id"])
    successful_state = successful_ledger.discord_thread_reaction_state(successful)
    assert successful_state == "done"
    assert adapter._feature_kanban_reaction_emoji(successful_state) == "✅"


@pytest.mark.asyncio
async def test_processing_start_uses_running_reaction_for_kanban_backed_action_request(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("1524608039204950268", raw_message)
    event.source.chat_type = "thread"
    event.source.chat_id = "1524513305559961600"
    event.source.thread_id = "1524513305559961600"
    event.feature_summary = {
        "thread_id": "1524513305559961600",
        "message_id": "1524608039204950268",
        "kanban_board": {"slug": "discord-rollout-action"},
    }

    await adapter.on_processing_start(event)

    raw_message.remove_reaction.assert_not_awaited()
    raw_message.add_reaction.assert_awaited_once_with("⏳")


@pytest.mark.asyncio
async def test_fable_feature_summary_reactions_ignore_kanban_state(adapter, monkeypatch):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("10", raw_message)
    event.invoked_skill_command = "fable"
    event.feature_summary = {
        "thread_id": "123",
        "message_id": "456",
        "initial_request": "/fable plan reporting",
        "kanban_board": {"slug": "discord-123"},
    }
    monkeypatch.setattr(
        "hermes_cli.discord_worker_boards.board_thread_reaction_state",
        lambda _slug: "active",
    )

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert [call.args for call in raw_message.add_reaction.await_args_list] == [
        ("⏳",),
        ("✅",),
    ]


@pytest.mark.asyncio
async def test_processing_complete_success_resolves_stale_kanban_running_to_done(
    adapter,
    monkeypatch,
):
    op_message = SimpleNamespace(
        id=1512478961647616142,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )
    followup_message = SimpleNamespace(
        id=999,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    thread = SimpleNamespace(
        id=1512478961647616142,
        starter_message=op_message,
        parent_id=1505275259006484570,
    )
    followup_message.channel = thread
    event = _make_event(str(followup_message.id), followup_message)
    event.source.chat_type = "thread"
    event.source.chat_id = str(thread.id)
    event.source.thread_id = str(thread.id)
    event.feature_summary = {
        "thread_id": str(thread.id),
        "message_id": "456",
        "kanban_board": {"slug": "discord-123"},
    }
    adapter._feature_kanban_reaction_state = MagicMock(return_value="running")
    monkeypatch.setattr("hermes_cli.discord_worker_boards.thread_status_targets", lambda: [])

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    followup_message.remove_reaction.assert_not_awaited()
    followup_message.add_reaction.assert_awaited_once_with("✅")
    op_message.remove_reaction.assert_not_awaited()
    op_message.add_reaction.assert_awaited_once_with("✅")


def test_feature_kanban_reaction_state_uses_reaction_specific_board_state(adapter, monkeypatch):
    from hermes_cli import discord_worker_boards as dwb

    monkeypatch.setattr(dwb, "board_thread_reaction_state", lambda slug: "running")

    assert adapter._feature_kanban_reaction_state({"kanban_board": {"slug": "discord-123"}}) == "running"


@pytest.mark.asyncio
async def test_kanban_thread_reaction_prefers_explicit_reaction_state(adapter):
    message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="👀", me=True)],
    )
    thread = SimpleNamespace(id=123, starter_message=message)
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": "discord-123",
            "thread_id": "123",
            "state": "done",
            "reaction_state": "foreman",
        }
    )

    assert synced == "foreman"
    assert [call.args for call in message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="🔨",
    )
    message.add_reaction.assert_awaited_once_with("🔨")


@pytest.mark.asyncio
async def test_kanban_thread_reaction_explicit_foreman_wins_over_stale_source_done(
    adapter,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        source_task = kanban_db.create_task(conn, title="Default intake", assignee="default")
        claimed = kanban_db.claim_task(conn, source_task)
        assert claimed is not None
        kanban_db.complete_task(conn, source_task, summary="done", expected_run_id=claimed.current_run_id)
    finally:
        conn.close()

    message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="✅", me=True)],
    )
    thread = SimpleNamespace(id=123, starter_message=message)
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": "discord-123",
            "thread_id": "123",
            "state": "done",
            "reaction_state": "foreman",
            "source_board": kanban_db.DEFAULT_BOARD,
            "source_task_id": source_task,
        }
    )

    assert synced == "foreman"
    assert [call.args for call in message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="🔨",
    )
    message.add_reaction.assert_awaited_once_with("🔨")


@pytest.mark.asyncio
async def test_kanban_thread_reaction_repairs_summary_embed_and_source_op(adapter):
    op_message = SimpleNamespace(
        id=111,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="✅", me=True)],
    )
    summary_message = SimpleNamespace(
        id=222,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[],
    )

    async def fetch_parent_message(message_id):
        if int(message_id) == op_message.id:
            return op_message
        if int(message_id) == summary_message.id:
            return summary_message
        raise LookupError(message_id)

    parent = SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch_parent_message))
    thread = SimpleNamespace(
        id=333,
        parent=parent,
        fetch_message=AsyncMock(side_effect=LookupError("not cached in thread")),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": "discord-333",
            "thread_id": "333",
            "state": "active",
            "reaction_state": "running",
            "message_id": str(summary_message.id),
            "source_message_id": str(op_message.id),
        }
    )

    assert synced == "running"
    assert [call.args for call in op_message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="⏳",
    )
    op_message.add_reaction.assert_awaited_once_with("⏳")
    assert [call.args for call in summary_message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="⏳",
    )
    summary_message.add_reaction.assert_awaited_once_with("⏳")


@pytest.mark.asyncio
async def test_kanban_thread_reaction_updates_origin_when_source_is_later_followup(adapter, monkeypatch):
    op_message = SimpleNamespace(
        id=333,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )
    source_message = SimpleNamespace(
        id=444,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )
    summary_message = SimpleNamespace(
        id=555,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )

    async def fetch_message(message_id):
        messages = {
            op_message.id: op_message,
            source_message.id: source_message,
            summary_message.id: summary_message,
        }
        return messages[int(message_id)]

    thread = SimpleNamespace(
        id=op_message.id,
        starter_message=op_message,
        fetch_message=AsyncMock(side_effect=fetch_message),
        parent=SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch_message)),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)
    monkeypatch.setattr(
        "hermes_cli.discord_worker_boards.thread_status_targets",
        lambda: [
            {
                "thread_id": str(thread.id),
                "state": "done",
                "reaction_state": "done",
            }
        ],
    )

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": "discord-333",
            "thread_id": str(thread.id),
            "state": "done",
            "reaction_state": "done",
            "message_id": str(summary_message.id),
            "source_message_id": str(source_message.id),
        }
    )

    assert synced == "done"
    for message in (op_message, source_message, summary_message):
        assert [call.args for call in message.remove_reaction.await_args_list] == _status_remove_calls(
            adapter,
            except_emoji="✅",
        )
        message.add_reaction.assert_awaited_once_with("✅")


@pytest.mark.asyncio
async def test_kanban_thread_reaction_keeps_origin_active_when_other_thread_work_remains(adapter, monkeypatch):
    op_message = SimpleNamespace(
        id=333,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )
    source_message = SimpleNamespace(
        id=444,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )
    summary_message = SimpleNamespace(
        id=555,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )

    async def fetch_message(message_id):
        messages = {
            op_message.id: op_message,
            source_message.id: source_message,
            summary_message.id: summary_message,
        }
        return messages[int(message_id)]

    thread = SimpleNamespace(
        id=op_message.id,
        starter_message=op_message,
        fetch_message=AsyncMock(side_effect=fetch_message),
        parent=SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch_message)),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)
    monkeypatch.setattr(
        "hermes_cli.discord_worker_boards.thread_status_targets",
        lambda: [
            {
                "thread_id": str(thread.id),
                "state": "done",
                "reaction_state": "done",
            },
            {
                "thread_id": str(thread.id),
                "state": "running",
                "reaction_state": "running",
            },
        ],
    )

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": "discord-333",
            "thread_id": str(thread.id),
            "state": "done",
            "reaction_state": "done",
            "message_id": str(summary_message.id),
            "source_message_id": str(source_message.id),
        }
    )

    assert synced == "done"
    assert [call.args for call in op_message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="⏳",
    )
    op_message.add_reaction.assert_awaited_once_with("⏳")
    for message in (source_message, summary_message):
        assert [call.args for call in message.remove_reaction.await_args_list] == _status_remove_calls(
            adapter,
            except_emoji="✅",
        )
        message.add_reaction.assert_awaited_once_with("✅")


@pytest.mark.asyncio
async def test_kanban_thread_reaction_origin_uses_latest_feature_summary(adapter, monkeypatch):
    op_message = SimpleNamespace(
        id=333,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="❓", me=True)],
    )
    old_source_message = SimpleNamespace(
        id=444,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="✅", me=True)],
    )
    old_summary_message = SimpleNamespace(
        id=555,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="✅", me=True)],
    )

    async def fetch_message(message_id):
        messages = {
            op_message.id: op_message,
            old_source_message.id: old_source_message,
            old_summary_message.id: old_summary_message,
        }
        return messages[int(message_id)]

    thread = SimpleNamespace(
        id=op_message.id,
        starter_message=op_message,
        fetch_message=AsyncMock(side_effect=fetch_message),
        parent=SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch_message)),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)
    monkeypatch.setattr(
        "hermes_cli.discord_worker_boards.thread_status_targets",
        lambda: [
            {
                "thread_id": str(thread.id),
                "state": "blocked",
                "reaction_state": "blocked",
                "message_id": str(old_summary_message.id),
                "source_message_id": str(old_source_message.id),
            },
            {
                "thread_id": str(thread.id),
                "state": "done",
                "reaction_state": "done",
                "message_id": "777",
                "source_message_id": "666",
            },
        ],
    )

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": "discord-333-old",
            "thread_id": str(thread.id),
            "state": "blocked",
            "reaction_state": "blocked",
            "message_id": str(old_summary_message.id),
            "source_message_id": str(old_source_message.id),
        }
    )

    assert synced == "blocked"
    for message in (old_source_message, old_summary_message):
        assert [call.args for call in message.remove_reaction.await_args_list] == _status_remove_calls(
            adapter,
            except_emoji="❓",
        )
        message.add_reaction.assert_awaited_once_with("❓")
    assert [call.args for call in op_message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="✅",
    )
    op_message.add_reaction.assert_awaited_once_with("✅")


@pytest.mark.asyncio
async def test_kanban_thread_reaction_origin_uses_latest_thread_embed_when_status_targets_are_stale(
    adapter,
    monkeypatch,
):
    op_message = SimpleNamespace(
        id=333,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="❓", me=True)],
    )
    old_source_message = SimpleNamespace(
        id=444,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="❓", me=True)],
    )
    old_summary_message = SimpleNamespace(
        id=555,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        embeds=[SimpleNamespace(title="old blocked board")],
        reactions=[SimpleNamespace(emoji="❓", me=True)],
        author=SimpleNamespace(bot=True),
    )
    latest_summary_message = SimpleNamespace(
        id=777,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        embeds=[SimpleNamespace(title="newer completed feature")],
        reactions=[SimpleNamespace(emoji="✅", me=True)],
        author=SimpleNamespace(bot=True),
    )

    async def fetch_message(message_id):
        messages = {
            op_message.id: op_message,
            old_source_message.id: old_source_message,
            old_summary_message.id: old_summary_message,
            latest_summary_message.id: latest_summary_message,
        }
        return messages[int(message_id)]

    async def history(**_kwargs):
        for message in (latest_summary_message, old_summary_message):
            yield message

    thread = SimpleNamespace(
        id=op_message.id,
        starter_message=op_message,
        fetch_message=AsyncMock(side_effect=fetch_message),
        history=history,
        parent=SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch_message)),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)
    monkeypatch.setattr(
        "hermes_cli.discord_worker_boards.thread_status_targets",
        lambda: [
            {
                "thread_id": str(thread.id),
                "state": "blocked",
                "reaction_state": "blocked",
                "message_id": str(old_summary_message.id),
                "source_message_id": str(old_source_message.id),
            },
        ],
    )

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": "discord-333-old",
            "thread_id": str(thread.id),
            "state": "blocked",
            "reaction_state": "blocked",
            "message_id": str(old_summary_message.id),
            "source_message_id": str(old_source_message.id),
        }
    )

    assert synced == "blocked"
    for message in (old_source_message, old_summary_message):
        assert [call.args for call in message.remove_reaction.await_args_list] == _status_remove_calls(
            adapter,
            except_emoji="❓",
        )
        message.add_reaction.assert_awaited_once_with("❓")
    assert [call.args for call in op_message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="✅",
    )
    op_message.add_reaction.assert_awaited_once_with("✅")


@pytest.mark.asyncio
async def test_kanban_thread_reaction_keeps_terminal_origin_when_latest_summary_target_is_stale(
    adapter,
):
    op_message = SimpleNamespace(
        id=333,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )
    summary_message = SimpleNamespace(
        id=777,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        embeds=[SimpleNamespace(title="completed feature")],
        reactions=[SimpleNamespace(emoji="✅", me=True)],
        author=SimpleNamespace(bot=True),
    )

    async def fetch_message(message_id):
        return {op_message.id: op_message, summary_message.id: summary_message}[int(message_id)]

    async def history(**_kwargs):
        yield summary_message

    thread = SimpleNamespace(
        id=op_message.id,
        starter_message=op_message,
        fetch_message=AsyncMock(side_effect=fetch_message),
        history=history,
        parent=SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch_message)),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": "discord-333",
            "thread_id": str(thread.id),
            "state": "running",
            "reaction_state": "running",
            "message_id": str(summary_message.id),
        }
    )

    assert synced == "running"
    op_message.add_reaction.assert_awaited_once_with("✅")
    summary_message.add_reaction.assert_awaited_once_with("⏳")


@pytest.mark.asyncio
async def test_kanban_thread_reaction_uses_source_task_state_over_done_target(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        source_task = kanban_db.create_task(conn, title="Default intake", assignee="default")
        claimed = kanban_db.claim_task(conn, source_task)
        assert claimed is not None
    finally:
        conn.close()

    op_message = SimpleNamespace(
        id=111,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="✅", me=True)],
    )
    summary_message = SimpleNamespace(
        id=222,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="✅", me=True)],
    )

    async def fetch_parent_message(message_id):
        if int(message_id) == op_message.id:
            return op_message
        if int(message_id) == summary_message.id:
            return summary_message
        raise LookupError(message_id)

    parent = SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch_parent_message))
    thread = SimpleNamespace(
        id=333,
        parent=parent,
        fetch_message=AsyncMock(side_effect=LookupError("not cached in thread")),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": "foreman-333",
            "thread_id": "333",
            "state": "active",
            "reaction_state": "done",
            "message_id": str(summary_message.id),
            "source_message_id": str(op_message.id),
            "source_board": kanban_db.DEFAULT_BOARD,
            "source_task_id": source_task,
        }
    )

    assert synced == "running"
    assert [call.args for call in op_message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="⏳",
    )
    op_message.add_reaction.assert_awaited_once_with("⏳")
    assert [call.args for call in summary_message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="⏳",
    )
    summary_message.add_reaction.assert_awaited_once_with("⏳")


@pytest.mark.asyncio
async def test_kanban_thread_reaction_uses_terminal_done_over_stale_source_task(
    adapter,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=kanban_db.DEFAULT_BOARD)
    try:
        source_task = kanban_db.create_task(conn, title="Default intake", assignee="default")
        claimed = kanban_db.claim_task(conn, source_task)
        assert claimed is not None
    finally:
        conn.close()

    source_message = SimpleNamespace(
        id=555,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )
    origin_message = SimpleNamespace(
        id=333,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )
    summary_message = SimpleNamespace(
        id=444,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )

    async def fetch_message(message_id):
        messages = {
            source_message.id: source_message,
            origin_message.id: origin_message,
            summary_message.id: summary_message,
        }
        return messages[int(message_id)]

    thread = SimpleNamespace(
        id=origin_message.id,
        starter_message=origin_message,
        fetch_message=AsyncMock(side_effect=fetch_message),
        parent=SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch_message)),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)
    target = {
        "board": "discord-1512532369897160735",
        "thread_id": str(thread.id),
        "state": "done",
        "message_id": str(summary_message.id),
        "source_message_id": str(source_message.id),
        "source_board": kanban_db.DEFAULT_BOARD,
        "source_task_id": source_task,
    }
    monkeypatch.setattr(
        "hermes_cli.discord_worker_boards.thread_status_targets",
        lambda: [target],
    )

    synced = await adapter.sync_kanban_thread_reaction(target)

    assert synced == "done"
    for message in (source_message, origin_message, summary_message):
        assert [call.args for call in message.remove_reaction.await_args_list] == _status_remove_calls(
            adapter,
            except_emoji="✅",
        )
        message.add_reaction.assert_awaited_once_with("✅")


@pytest.mark.asyncio
async def test_kanban_thread_reaction_clears_flag_after_syncing_reachable_target(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(thread_id="334", goal="Ship it")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
        },
    )
    summary_message = SimpleNamespace(
        id=222,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="❌", me=True)],
    )

    async def fetch_parent_message(message_id):
        if int(message_id) == summary_message.id:
            return summary_message
        raise RuntimeError("404 not found: unknown message")

    parent = SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch_parent_message))
    thread = SimpleNamespace(
        id=334,
        parent=parent,
        fetch_message=AsyncMock(side_effect=RuntimeError("404 not found: unknown message")),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": board.slug,
            "thread_id": "334",
            "state": "done",
            "message_id": str(summary_message.id),
            "source_message_id": "111",
            "terminal_reaction_sync_pending": True,
        }
    )

    assert synced == "done"
    assert [call.args for call in summary_message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="✅",
    )
    summary_message.add_reaction.assert_awaited_once_with("✅")
    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_reaction_sync_pending" not in worker


@pytest.mark.asyncio
async def test_kanban_thread_reaction_syncs_github_pr_amend_done_reaction(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.start_direct_goal(thread_id="336", goal="Ship PR amend")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
        },
    )
    summary_message = SimpleNamespace(
        id=222,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[],
    )
    parent = SimpleNamespace(fetch_message=AsyncMock(return_value=summary_message))
    thread = SimpleNamespace(
        id=336,
        parent=parent,
        fetch_message=AsyncMock(side_effect=RuntimeError("404 not found: unknown message")),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)
    github_sync = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "gateway.platforms.webhook.WebhookAdapter.sync_github_pr_amend_terminal_reaction",
        github_sync,
    )
    github_pr_amend = {
        "repo": "reserve-protocol/reserve-index-dtf",
        "pr_number": "182",
        "source_kind": "issue_comment",
        "source_id": "4700001",
    }

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": board.slug,
            "thread_id": "336",
            "state": "done",
            "message_id": str(summary_message.id),
            "source_message_id": "111",
            "terminal_reaction_sync_pending": True,
            "github_pr_amend": github_pr_amend,
        }
    )

    assert synced == "done"
    github_sync.assert_awaited_once()
    assert github_sync.await_args.args[0] == github_pr_amend
    assert github_sync.await_args.args[1] == "done"


@pytest.mark.asyncio
async def test_kanban_thread_reaction_swallows_github_pr_amend_sync_failure(adapter, monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb

    board = dwb.start_direct_goal(thread_id="337", goal="Ship PR amend")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "blocked",
            "phase": "blocked",
            "terminal_reaction_sync_pending": True,
        },
    )
    summary_message = SimpleNamespace(
        id=222,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[],
    )
    parent = SimpleNamespace(fetch_message=AsyncMock(return_value=summary_message))
    thread = SimpleNamespace(
        id=337,
        parent=parent,
        fetch_message=AsyncMock(side_effect=RuntimeError("404 not found: unknown message")),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)
    github_sync = AsyncMock(side_effect=RuntimeError("gh unavailable"))
    monkeypatch.setattr(
        "gateway.platforms.webhook.WebhookAdapter.sync_github_pr_amend_terminal_reaction",
        github_sync,
    )

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": board.slug,
            "thread_id": "337",
            "state": "blocked",
            "message_id": str(summary_message.id),
            "source_message_id": "111",
            "terminal_reaction_sync_pending": True,
            "github_pr_amend": {
                "repo": "reserve-protocol/reserve-index-dtf",
                "pr_number": "182",
                "source_kind": "review_comment",
                "source_id": "4800001",
            },
        }
    )

    assert synced == "blocked"
    summary_message.add_reaction.assert_awaited_once_with("❓")
    github_sync.assert_awaited_once()
    assert github_sync.await_args.args[1] == "blocked"


@pytest.mark.asyncio
async def test_kanban_thread_reaction_uses_origin_fallback_when_source_fetch_is_transient(
    adapter,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(thread_id="335", goal="Ship it")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
        },
    )
    summary_message = SimpleNamespace(
        id=222,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="❌", me=True)],
    )
    origin_message = SimpleNamespace(
        id=333,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="❌", me=True)],
    )

    async def fetch_parent_message(message_id):
        if int(message_id) == summary_message.id:
            return summary_message
        raise RuntimeError("temporary Discord fetch outage")

    parent = SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch_parent_message))
    thread = SimpleNamespace(
        id=335,
        parent=parent,
        starter_message=origin_message,
        fetch_message=AsyncMock(side_effect=RuntimeError("temporary Discord fetch outage")),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": board.slug,
            "thread_id": "335",
            "state": "done",
            "message_id": str(summary_message.id),
            "source_message_id": "111",
            "terminal_reaction_sync_pending": True,
        }
    )

    assert synced is None
    assert [call.args for call in summary_message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="✅",
    )
    summary_message.add_reaction.assert_awaited_once_with("✅")
    assert [call.args for call in origin_message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="✅",
    )
    origin_message.add_reaction.assert_awaited_once_with("✅")
    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert worker["terminal_reaction_sync_pending"] is True


@pytest.mark.asyncio
async def test_kanban_thread_reaction_uses_summary_and_origin_when_source_missing(adapter):
    origin_message = SimpleNamespace(
        id=333,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="✅", me=True)],
    )
    summary_message = SimpleNamespace(
        id=222,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[],
    )

    async def fetch_parent_message(message_id):
        if int(message_id) == origin_message.id:
            return origin_message
        if int(message_id) == summary_message.id:
            return summary_message
        raise LookupError(message_id)

    parent = SimpleNamespace(fetch_message=AsyncMock(side_effect=fetch_parent_message))
    thread = SimpleNamespace(
        id=origin_message.id,
        parent=parent,
        fetch_message=AsyncMock(side_effect=LookupError("not cached in thread")),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": "discord-333",
            "thread_id": "333",
            "state": "active",
            "reaction_state": "running",
            "message_id": str(summary_message.id),
        }
    )

    assert synced == "running"
    assert [call.args for call in origin_message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="⏳",
    )
    origin_message.add_reaction.assert_awaited_once_with("⏳")
    assert [call.args for call in summary_message.remove_reaction.await_args_list] == _status_remove_calls(
        adapter,
        except_emoji="⏳",
    )
    summary_message.add_reaction.assert_awaited_once_with("⏳")


@pytest.mark.asyncio
async def test_kanban_thread_reaction_clears_terminal_flag_when_message_missing(
    adapter,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban-home"))
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(thread_id="123", goal="Ship it")
    dwb._update_worker_meta(
        board.slug,
        {
            "goal_status": "done",
            "phase": "complete",
            "terminal_reaction_sync_pending": True,
        },
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=SimpleNamespace(id=123))

    synced = await adapter.sync_kanban_thread_reaction(
        {
            "board": board.slug,
            "thread_id": "123",
            "state": "done",
            "terminal_reaction_sync_pending": True,
        }
    )

    assert synced == "done"
    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert "terminal_reaction_sync_pending" not in worker


@pytest.mark.asyncio
async def test_status_reaction_state_does_not_treat_cache_as_authoritative(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="👀", me=True)],
    )

    await adapter._set_message_reaction_state(raw_message, "👀")

    raw_message.remove_reaction.assert_not_awaited()
    raw_message.add_reaction.assert_awaited_once_with("👀")


@pytest.mark.asyncio
async def test_status_reaction_state_leaves_unknown_stale_reactions_for_reconciliation(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="✅", me=True)],
    )

    await adapter._set_message_reaction_state(raw_message, "✅")

    raw_message.remove_reaction.assert_not_awaited()
    raw_message.add_reaction.assert_awaited_once_with("✅")


@pytest.mark.asyncio
async def test_status_reaction_state_replaces_different_target(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="👀", me=True), SimpleNamespace(emoji="⏳", me=True)],
    )

    adapter._hermes_reaction_states[adapter._message_identity(raw_message)] = "⏳"
    await adapter._set_message_reaction_state(raw_message, "❓")

    raw_message.remove_reaction.assert_awaited_once_with("⏳", adapter._client.user)
    raw_message.add_reaction.assert_awaited_once_with("❓")


@pytest.mark.asyncio
async def test_status_reaction_state_reports_failure_to_remove_a_known_stale_state(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(side_effect=RuntimeError("Discord unavailable")),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )

    adapter._hermes_reaction_states[adapter._message_identity(raw_message)] = "⏳"
    assert await adapter._set_message_reaction_state(raw_message, "✅") is False
    raw_message.add_reaction.assert_awaited_once_with("✅")


@pytest.mark.asyncio
async def test_delayed_running_generation_cannot_overwrite_terminal_state(adapter):
    message = SimpleNamespace(
        id=901,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    running_generation = adapter._next_reaction_generation()
    terminal_generation = adapter._next_reaction_generation()

    assert await adapter._set_message_reaction_state(
        message,
        "✅",
        generation=terminal_generation,
        transition="processing_complete",
    )
    assert not await adapter._set_message_reaction_state(
        message,
        "⏳",
        generation=running_generation,
        transition="processing_start",
    )

    assert [call.args for call in message.add_reaction.await_args_list] == [("✅",)]
    message.remove_reaction.assert_not_awaited()
    assert adapter._hermes_reaction_states[adapter._message_identity(message)] == "✅"


@pytest.mark.asyncio
async def test_terminal_transition_corrects_running_mutation_already_in_flight(adapter):
    running_started = asyncio.Event()
    release_running = asyncio.Event()

    async def add_reaction(emoji):
        if emoji == "⏳":
            running_started.set()
            await release_running.wait()

    message = SimpleNamespace(
        id=904,
        add_reaction=AsyncMock(side_effect=add_reaction),
        remove_reaction=AsyncMock(),
    )
    running_generation = adapter._next_reaction_generation()
    running_task = asyncio.create_task(
        adapter._set_message_reaction_state(
            message,
            "⏳",
            generation=running_generation,
            transition="processing_start",
        )
    )
    await running_started.wait()

    terminal_generation = adapter._next_reaction_generation()
    terminal_task = asyncio.create_task(
        adapter._set_message_reaction_state(
            message,
            "✅",
            generation=terminal_generation,
            transition="processing_complete",
        )
    )
    release_running.set()
    await asyncio.gather(running_task, terminal_task)

    assert [call.args for call in message.add_reaction.await_args_list] == [
        ("⏳",),
        ("✅",),
    ]
    message.remove_reaction.assert_awaited_once_with("⏳", adapter._client.user)
    assert adapter._hermes_reaction_states[adapter._message_identity(message)] == "✅"


@pytest.mark.asyncio
async def test_processing_transition_deduplicates_distinct_discord_message_ids(adapter):
    first = SimpleNamespace(
        id=902,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    duplicate = SimpleNamespace(
        id=902,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("902", first)
    event._batched_raw_messages = [first, duplicate, first]

    await adapter.on_processing_start(event)

    first.add_reaction.assert_awaited_once_with("⏳")
    duplicate.add_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_processing_start_telemetry_reports_substeps_and_rest_counts(
    adapter,
    caplog,
):
    message = SimpleNamespace(
        id=903,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    event = _make_event("903", message)

    with caplog.at_level("INFO", logger="plugins.platforms.discord.adapter"):
        await adapter.on_processing_start(event)

    line = next(
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("discord_processing_start_timing")
    )
    assert "summary_ms=" in line
    assert "reaction_resolve_ms=" in line
    assert "reaction_sync_ms=" in line
    assert "recovery_record_ms=" in line
    assert "reaction_targets=1" in line
    assert "rest_add_attempts=1" in line
    assert "rest_remove_attempts=0" in line


@pytest.mark.asyncio
async def test_thread_followup_reactions_target_message_and_origin(adapter):
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
    for message in (followup_message, origin_message):
        assert [call.args for call in message.remove_reaction.await_args_list] == [
            ("⏳", adapter._client.user),
        ]
        assert [call.args for call in message.add_reaction.await_args_list] == [
            ("⏳",),
            ("✅",),
        ]


@pytest.mark.asyncio
async def test_expired_work_reconciliation_fetches_thread_origin_without_raw_message(
    adapter,
    tmp_path,
):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = MessageEvent(
        text="implement it",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="55",
            chat_type="thread",
            thread_id="1000",
            parent_chat_id="55",
            guild_id="77",
            message_id="1000",
        ),
        message_id="1000",
    )
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None
    assert ledger.mark_expired(item["id"])

    origin_message = SimpleNamespace(
        id=1000,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )
    parent = SimpleNamespace(id=55, fetch_message=AsyncMock(return_value=origin_message))
    thread = _StatusThread(thread_id=1000, name="Build dashboard")
    thread.parent = parent
    thread.parent_id = 55
    thread.fetch_message = AsyncMock(side_effect=LookupError("not cached"))
    adapter._client.get_channel = lambda channel_id: thread if channel_id == 1000 else None
    adapter.gateway_runner = SimpleNamespace(work_ledger=ledger)

    state = await adapter.reconcile_work_ledger_thread_reaction(item)

    assert state == "errored"
    origin_message.add_reaction.assert_awaited_once_with("❌")
    assert ledger.pending_terminal_reaction_items() == []


@pytest.mark.asyncio
async def test_terminal_reconciliation_repairs_op_even_when_summary_is_unavailable(
    adapter,
    tmp_path,
):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1000",
        chat_type="thread",
        thread_id="1000",
        parent_chat_id="55",
        guild_id="77",
        message_id="1000",
    )
    event = MessageEvent(
        text="Implement the dashboard repair",
        message_type=MessageType.TEXT,
        source=source,
        message_id="1000",
        feature_summary={
            "thread_id": "1000",
            "message_id": "2000",
            "source_message_id": "1000",
        },
        discord_runtime_mode="action",
    )
    item = ledger.accept_event(
        event,
        session_key=build_session_key(source),
        freshness_seconds=60,
    )
    assert item is not None
    assert ledger.mark_agent_done(item["id"], final_response="Done.")
    assert ledger.mark_completed(item["id"])

    origin_message = SimpleNamespace(
        id=1000,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )
    parent = SimpleNamespace(id=55, fetch_message=AsyncMock(return_value=origin_message))
    thread = _StatusThread(thread_id=1000, name="Build dashboard")
    thread.parent = parent
    thread.parent_id = 55
    thread.fetch_message = AsyncMock(side_effect=LookupError("summary unavailable"))
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)
    adapter.gateway_runner = SimpleNamespace(work_ledger=ledger)

    state = await adapter.reconcile_work_ledger_thread_reaction(item)

    assert state is None
    origin_message.add_reaction.assert_awaited_once_with("✅")
    assert ledger.get(item["id"])["terminal_reaction_sync_pending"] is True


@pytest.mark.asyncio
async def test_terminal_reconciliation_repairs_summary_embed_and_source_post(
    adapter,
    tmp_path,
):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1000",
        chat_type="thread",
        thread_id="1000",
        parent_chat_id="55",
        guild_id="77",
        message_id="1000",
    )
    summary_handle = {
        "thread_id": "1000",
        "message_id": "2000",
        "source_message_id": "1000",
        "parent_channel_id": "55",
        "initial_request": "Implement the dashboard repair",
        "kanban_board": None,
    }
    event = MessageEvent(
        text="Implement the dashboard repair",
        message_type=MessageType.TEXT,
        source=source,
        message_id="1000",
        feature_summary=summary_handle,
        discord_runtime_mode="action",
    )
    item = ledger.accept_event(
        event,
        session_key=build_session_key(source),
        freshness_seconds=60,
    )
    assert item is not None
    assert ledger.mark_agent_done(item["id"], final_response="Dashboard repair completed.")
    assert ledger.mark_response_delivered(item["id"], result_message_id="3000")
    assert ledger.mark_summary_updated(item["id"])
    assert ledger.mark_completed(item["id"])
    event.work_item_id = item["id"]

    origin_message = SimpleNamespace(
        id=1000,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )
    summary_message = SimpleNamespace(
        id=2000,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
        embeds=[
            SimpleNamespace(
                fields=[SimpleNamespace(name="Status", value="⏳ Running")]
            )
        ],
    )

    async def fetch_message(message_id):
        return {1000: origin_message, 2000: summary_message}[int(message_id)]

    parent = SimpleNamespace(id=55, fetch_message=AsyncMock(side_effect=fetch_message))
    thread = SimpleNamespace(
        id=1000,
        parent=parent,
        parent_id=55,
        fetch_message=AsyncMock(side_effect=fetch_message),
    )
    adapter._resolve_summary_channel = AsyncMock(return_value=thread)
    adapter.gateway_runner = SimpleNamespace(work_ledger=ledger)
    adapter.update_feature_summary = AsyncMock(return_value=True)

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter.update_feature_summary.assert_awaited_once_with(
        summary_handle,
        final_response="Dashboard repair completed.",
        status="Complete",
        title=None,
        runtime_breakdown=None,
    )
    summary_message.add_reaction.assert_awaited_once_with("✅")
    origin_message.add_reaction.assert_awaited_once_with("✅")
    assert ledger.pending_terminal_reaction_items() == []


@pytest.mark.asyncio
async def test_replaced_adapter_uses_current_connection_for_terminal_reconciliation(
    adapter,
    tmp_path,
):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1000",
        chat_type="thread",
        thread_id="1000",
        parent_chat_id="55",
        guild_id="77",
        message_id="1000",
        profile="reviewer",
    )
    event = MessageEvent(
        text="Implement the dashboard repair",
        message_type=MessageType.TEXT,
        source=source,
        message_id="1000",
        discord_runtime_mode="action",
    )
    item = ledger.accept_event(
        event,
        session_key=build_session_key(source),
        freshness_seconds=60,
    )
    assert item is not None
    assert ledger.mark_completed(item["id"])
    event.work_item_id = item["id"]

    primary_adapter = SimpleNamespace(
        reconcile_work_ledger_thread_reaction=AsyncMock(
            side_effect=AssertionError("primary adapter must not reconcile")
        )
    )
    async def reconcile_current(persisted_item, _state):
        ledger.mark_discord_thread_reaction_synced(persisted_item)
        return "done"

    current_adapter = SimpleNamespace(
        reconcile_work_ledger_thread_reaction=AsyncMock(
            side_effect=reconcile_current
        )
    )
    runner = SimpleNamespace(
        work_ledger=ledger,
        adapters={Platform.DISCORD: primary_adapter},
        _profile_adapters={"reviewer": {Platform.DISCORD: current_adapter}},
    )
    async def reconcile_via_runner(persisted_item, state):
        selected = runner._profile_adapters[
            persisted_item["source"]["profile"]
        ][Platform.DISCORD]
        return await selected.reconcile_work_ledger_thread_reaction(
            persisted_item, state
        )

    runner._reconcile_discord_terminal_reaction = AsyncMock(
        side_effect=reconcile_via_runner
    )
    adapter.gateway_runner = runner
    adapter.reconcile_work_ledger_thread_reaction = AsyncMock(
        side_effect=AssertionError("disconnected adapter must not reconcile")
    )

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    runner._reconcile_discord_terminal_reaction.assert_awaited_once()
    persisted_item, state = runner._reconcile_discord_terminal_reaction.await_args.args
    assert persisted_item["id"] == item["id"]
    assert state == "done"
    current_adapter.reconcile_work_ledger_thread_reaction.assert_awaited_once()
    primary_adapter.reconcile_work_ledger_thread_reaction.assert_not_awaited()
    assert ledger.pending_terminal_reaction_items() == []


@pytest.mark.asyncio
async def test_terminal_reconciliation_keeps_retry_marker_when_summary_sync_fails(
    adapter,
    tmp_path,
):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="1000",
        chat_type="thread",
        thread_id="1000",
        parent_chat_id="55",
        guild_id="77",
        message_id="1000",
    )
    summary_handle = {
        "thread_id": "1000",
        "message_id": "2000",
        "source_message_id": "1000",
        "initial_request": "Implement the dashboard repair",
        "kanban_board": None,
    }
    event = MessageEvent(
        text="Implement the dashboard repair",
        message_type=MessageType.TEXT,
        source=source,
        message_id="1000",
        feature_summary=summary_handle,
        discord_runtime_mode="action",
    )
    item = ledger.accept_event(
        event,
        session_key=build_session_key(source),
        freshness_seconds=60,
    )
    assert item is not None
    assert ledger.mark_agent_done(item["id"], final_response="Dashboard repair completed.")
    assert ledger.mark_response_delivered(item["id"], result_message_id="3000")
    assert ledger.mark_summary_updated(item["id"])
    assert ledger.mark_completed(item["id"])
    event.work_item_id = item["id"]

    adapter.gateway_runner = SimpleNamespace(work_ledger=ledger)
    adapter.update_feature_summary = AsyncMock(return_value=False)

    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert [pending["id"] for pending in ledger.pending_terminal_reaction_items()] == [
        item["id"]
    ]


@pytest.mark.asyncio
async def test_later_generic_success_repairs_expired_thread_origin_to_done(adapter, tmp_path):
    now = [100.0]
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: now[0])
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="55",
        chat_type="thread",
        thread_id="1000",
        parent_chat_id="55",
        guild_id="77",
        message_id="1000",
    )
    expired_event = MessageEvent(
        text="implement it",
        message_type=MessageType.TEXT,
        source=source,
        message_id="1000",
    )
    expired = ledger.accept_event(
        expired_event,
        session_key=build_session_key(source),
        freshness_seconds=60,
    )
    assert expired is not None
    ledger.mark_expired(expired["id"])

    now[0] = 200.0
    status_event = MessageEvent(
        text="what is the status?",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="55",
            chat_type="thread",
            thread_id="1000",
            parent_chat_id="55",
            guild_id="77",
            message_id="2000",
        ),
        message_id="2000",
    )
    status_item = ledger.accept_event(
        status_event,
        session_key=build_session_key(status_event.source),
        freshness_seconds=60,
    )
    assert status_item is not None
    assert ledger.mark_completed(status_item["id"])
    status_event.work_item_id = status_item["id"]
    status_event.discord_action_request_intent = False

    origin_message = SimpleNamespace(
        id=1000,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="⏳", me=True)],
    )
    parent = SimpleNamespace(id=55, fetch_message=AsyncMock(return_value=origin_message))
    thread = _StatusThread(thread_id=1000, name="Build dashboard")
    thread.parent = parent
    thread.parent_id = 55
    thread.fetch_message = AsyncMock(side_effect=LookupError("not cached"))
    adapter._client.get_channel = lambda channel_id: thread if channel_id == 1000 else None
    adapter.gateway_runner = SimpleNamespace(work_ledger=ledger)

    await adapter.on_processing_complete(status_event, ProcessingOutcome.SUCCESS)

    origin_message.add_reaction.assert_awaited_once_with("✅")
    assert ledger.discord_thread_reaction_state(status_item) == "done"
    assert ledger.pending_terminal_reaction_items() == []


@pytest.mark.asyncio
async def test_thread_origin_message_fetches_missing_parent_from_parent_id(adapter):
    origin_message = SimpleNamespace(
        id=1000,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    parent = SimpleNamespace(
        id=55,
        fetch_message=AsyncMock(return_value=origin_message),
    )
    thread = _StatusThread(thread_id=1000, name="Build dashboard")
    thread.parent = None
    thread.parent_id = parent.id
    thread.fetch_message = AsyncMock(side_effect=LookupError("thread fetch unavailable"))
    adapter._client.get_channel = lambda _id: None
    adapter._client.fetch_channel = AsyncMock(return_value=parent)

    resolved = await adapter._thread_origin_message(thread)

    assert resolved is origin_message
    adapter._client.fetch_channel.assert_awaited_once_with(parent.id)
    parent.fetch_message.assert_awaited_once_with(thread.id)


@pytest.mark.asyncio
async def test_batched_thread_followup_reactions_target_message_and_origin(adapter):
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

    for message in (followup_message, origin_message):
        assert [call.args for call in message.remove_reaction.await_args_list] == [
            ("⏳", adapter._client.user),
        ]
        assert [call.args for call in message.add_reaction.await_args_list] == [
            ("⏳",),
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
            ("⏳", adapter._client.user),
        ]
        assert [call.args for call in message.add_reaction.await_args_list] == [
            ("⏳",),
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


def test_feature_summary_loader_falls_back_to_source_scoped_thread_handle(adapter):
    thread = _StatusThread(thread_id=1000, name="Build dashboard")
    state = {
        discord_platform._DISCORD_FEATURE_SUMMARY_STATE_BUCKET: {
            "10:1000:1000": {
                "thread_id": "1000",
                "message_id": "2000",
                "source_message_id": "1000",
                "guild_id": "10",
                "parent_channel_id": "55",
                "kanban_board": {"slug": "discord-1000"},
                "updated_at": 123.0,
            }
        }
    }
    adapter._read_project_summary_state = MagicMock(return_value=state)
    adapter._write_project_summary_state = MagicMock()

    handle = adapter._load_feature_summary_handle_for_thread(thread)

    assert handle is not None
    assert handle["message_id"] == "2000"
    assert handle["source_message_id"] == "1000"
    assert handle["kanban_board"] == {"slug": "discord-1000"}
    assert handle["_thread_obj"] is thread


@pytest.mark.asyncio
async def test_processing_start_fable_turn_uses_progress_emoji_in_kanban_thread(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="👀", me=True)],
    )
    event = _make_event("9", raw_message)
    event.invoked_skill_command = "fable"
    event.feature_summary = {"kanban_board": {"slug": "discord-1000"}}

    await adapter.on_processing_start(event)

    assert [call.args for call in raw_message.add_reaction.await_args_list] == [("⏳",)]


@pytest.mark.asyncio
async def test_processing_complete_fable_turn_uses_outcome_rather_than_kanban(adapter):
    raw_message = SimpleNamespace(
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
        reactions=[SimpleNamespace(emoji="👀", me=True)],
    )
    event = _make_event("9", raw_message)
    event.invoked_skill_command = "fable"
    event.feature_summary = {
        "thread_id": "1000",
        "kanban_board": {"slug": "discord-1000"},
    }
    adapter._feature_kanban_reaction_state = MagicMock(return_value="active")

    await adapter.on_processing_start(event)
    adapter._feature_kanban_reaction_state.reset_mock()
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert [call.args for call in raw_message.add_reaction.await_args_list] == [
        ("⏳",),
        ("✅",),
    ]
    adapter._feature_kanban_reaction_state.assert_not_called()


@pytest.mark.asyncio
async def test_processing_lifecycle_does_not_rename_discord_thread(adapter):
    thread = _StatusThread(name="Build dashboard")
    event = _thread_status_event("1", thread)
    adapter._client.fetch_channel = AsyncMock(side_effect=LookupError("parent unavailable"))

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    assert thread.name == "Build dashboard"
    thread.edit.assert_not_awaited()
    event.raw_message.add_reaction.assert_any_await("⏳")
    event.raw_message.add_reaction.assert_any_await("✅")


@pytest.mark.asyncio
async def test_goal_thread_followup_uses_loaded_kanban_reaction(adapter, monkeypatch):
    origin_message = SimpleNamespace(
        id=1000,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    followup_message = SimpleNamespace(
        id=3000,
        add_reaction=AsyncMock(),
        remove_reaction=AsyncMock(),
    )
    parent = SimpleNamespace(id=55, fetch_message=AsyncMock(return_value=origin_message))
    thread = _StatusThread(thread_id=1000, name="Build dashboard")
    thread.parent = parent
    thread.parent_id = parent.id
    thread.fetch_message = AsyncMock(side_effect=LookupError("not cached in thread"))
    followup_message.channel = thread
    state = {
        discord_platform._DISCORD_FEATURE_SUMMARY_STATE_BUCKET: {
            "10:1000:1000": {
                "thread_id": "1000",
                "message_id": "2000",
                "source_message_id": "1000",
                "guild_id": "10",
                "parent_channel_id": "55",
                "kanban_board": {"slug": "discord-1000"},
                "updated_at": 123.0,
            }
        }
    }
    adapter._read_project_summary_state = MagicMock(return_value=state)
    adapter._write_project_summary_state = MagicMock()
    adapter._feature_kanban_reaction_state = MagicMock(return_value="active")
    monkeypatch.setattr(
        "hermes_cli.discord_worker_boards.thread_status_targets",
        lambda: [{"thread_id": "1000", "reaction_state": "active"}],
    )

    event = _make_event("3000", followup_message)
    event.source.chat_type = "thread"
    event.source.thread_id = str(thread.id)
    event.source.parent_chat_id = str(parent.id)
    event.feature_summary = adapter._load_feature_summary_handle_for_thread(thread)

    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    followup_message.add_reaction.assert_awaited_once_with("⏳")
    origin_message.add_reaction.assert_awaited_once_with("⏳")


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


def _dev_member(*, user_id=42, role_name="Dev"):
    return SimpleNamespace(
        id=user_id,
        name="jezza",
        display_name="Jezza",
        roles=[SimpleNamespace(id=7, name=role_name)],
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
async def test_thumbsup_from_dev_on_hermes_message_dispatches_merge_approval(adapter):
    channel, _message = _guild_channel(adapter)
    user = _dev_member()
    payload = _ship_payload()
    payload.member = user
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "approve PR merge"
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
    assert event.discord_runtime_mode == "action"
    assert event.discord_runtime_reason == "dev_merge_reaction"
    assert event.participates_in_work_lifecycle is False


@pytest.mark.asyncio
async def test_custom_thumbsup_name_dispatches_ship_it(adapter):
    _channel, _message = _guild_channel(adapter)
    payload = _ship_payload(emoji=SimpleNamespace(name="thumbsup"))
    payload.member = _dev_member()
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.await_args.args[0].text == "approve PR merge"


@pytest.mark.asyncio
async def test_dev_merge_reaction_routes_thread_session(adapter, monkeypatch):
    class FakeThread:
        pass

    monkeypatch.setattr(discord_platform.discord, "Thread", FakeThread)
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
    payload.member = _dev_member()
    adapter.handle_message = AsyncMock()
    project_context = {
        "project_channel_id": "55",
        "project_name": "Hermes",
        "project_path": "/home/droid/hermes",
        "project_mapping_source": "configured_channel_cwd",
        "project_mapping_resolved": True,
    }
    monkeypatch.setattr(
        discord_platform,
        "resolve_discord_project_context",
        lambda channel: SimpleNamespace(to_dict=lambda: dict(project_context)),
    )

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert event.text == "approve PR merge"
    assert event.source.chat_id == "777"
    assert event.source.chat_type == "thread"
    assert event.source.thread_id == "777"
    assert event.source.parent_chat_id == "55"
    assert event.source.project_path == "/home/droid/hermes"
    assert event.source.project_channel_id == "55"


@pytest.mark.asyncio
async def test_dev_merge_reaction_ignores_non_thumbsup(adapter):
    _channel, _message = _guild_channel(adapter)
    payload = _ship_payload(emoji="✅")
    payload.member = _dev_member()
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dev_merge_reaction_ignores_non_hermes_message(adapter):
    other_author = SimpleNamespace(id=111, name="other", display_name="Other")
    _channel, _message = _guild_channel(adapter, author=other_author)
    payload = _ship_payload()
    payload.member = _dev_member()
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dev_merge_reaction_ignores_bot_self_reaction(adapter):
    _channel, _message = _guild_channel(adapter)
    payload = _ship_payload(user_id=adapter._client.user.id)
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dev_merge_reaction_enforces_authorization(adapter):
    _channel, _message = _guild_channel(adapter)
    adapter._allowed_user_ids = {"999"}
    payload = _ship_payload()
    payload.member = _dev_member()
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dev_merge_reaction_ignores_non_dev_member(adapter):
    _channel, _message = _guild_channel(adapter)
    payload = _ship_payload()
    payload.member = _dev_member(role_name="Developer")
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_dev_merge_reaction_event_does_not_get_lifecycle_reactions(adapter):
    _channel, _message = _guild_channel(adapter)
    payload = _ship_payload()
    payload.member = _dev_member()
    adapter.handle_message = AsyncMock()

    await adapter._handle_raw_reaction_add(payload)
    event = adapter.handle_message.await_args.args[0]

    adapter._add_reaction = AsyncMock()
    adapter._remove_reaction = AsyncMock()
    await adapter.on_processing_start(event)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    adapter._add_reaction.assert_not_awaited()
    adapter._remove_reaction.assert_not_awaited()


@pytest.mark.asyncio
async def test_dev_merge_reaction_dispatches_inline_without_typing(adapter):
    event = _make_event("999", SimpleNamespace())
    event.discord_runtime_reason = "dev_merge_reaction"
    event.participates_in_work_lifecycle = False
    adapter.set_message_handler(AsyncMock(return_value="Merged: PR"))
    adapter._dispatch_command_without_typing = AsyncMock()

    await adapter.handle_message(event)

    adapter._dispatch_command_without_typing.assert_awaited_once_with(
        event,
        "dev-merge",
    )
