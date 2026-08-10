from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


def _event():
    return MessageEvent(
        text="approve PR merge",
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.DISCORD,
            chat_id="thread-1",
            chat_type="thread",
            thread_id="thread-1",
            user_id="dev-1",
            user_name="Dev User",
            message_id="final-1",
        ),
        message_id="final-1",
        discord_runtime_reason="dev_merge_reaction",
        participates_in_work_lifecycle=False,
    )


@pytest.mark.asyncio
async def test_gateway_intercepts_dev_merge_before_work_item_acceptance(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.plugins.invoke_hook_async",
        AsyncMock(return_value=[]),
    )
    runner = object.__new__(GatewayRunner)
    runner._consume_promoted_replay_fence = AsyncMock(return_value=True)
    runner._startup_restore_in_progress = False
    runner._is_user_authorized = MagicMock(return_value=True)
    runner._handle_dev_merge_reaction = AsyncMock(return_value="Merged: PR")
    runner._accept_discord_work_item = MagicMock()

    result = await runner._handle_message(_event())

    assert result == "Merged: PR"
    runner._handle_dev_merge_reaction.assert_awaited_once()
    runner._accept_discord_work_item.assert_not_called()


@pytest.mark.asyncio
async def test_gateway_executes_and_persists_claimed_dev_merge(monkeypatch):
    closeout = {"pr": {"url": "https://github.com/acme/example/pull/7"}}
    ledger = SimpleNamespace(
        claim_dev_merge_for_message=MagicMock(
            return_value={
                "id": "work-1",
                "closeout": closeout,
                "_dev_merge_claim": "claimed",
                "_dev_merge_attempt_id": "dev-merge-1",
            }
        ),
        finish_dev_merge=MagicMock(return_value=True),
    )
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    monkeypatch.setattr(
        "hermes_cli.dev_pr_merge.merge_published_pr",
        lambda state: SimpleNamespace(
            outcome="merged",
            pr_url=state["pr"]["url"],
            message=f"Merged: {state['pr']['url']}",
        ),
    )

    result = await runner._handle_dev_merge_reaction(_event())

    assert result == "Merged: https://github.com/acme/example/pull/7"
    ledger.claim_dev_merge_for_message.assert_called_once_with(
        chat_id="thread-1",
        message_id="final-1",
        actor_id="dev-1",
    )
    ledger.finish_dev_merge.assert_called_once_with(
        "work-1",
        attempt_id="dev-merge-1",
        outcome="merged",
        message="Merged: https://github.com/acme/example/pull/7",
        pr_url="https://github.com/acme/example/pull/7",
    )


@pytest.mark.asyncio
async def test_gateway_ignores_reaction_without_terminal_ledger_match():
    ledger = SimpleNamespace(
        claim_dev_merge_for_message=MagicMock(return_value=None),
    )
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger

    assert await runner._handle_dev_merge_reaction(_event()) is None
