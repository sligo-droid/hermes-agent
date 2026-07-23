from unittest.mock import patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.fixture
def adapter():
    return DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("", False),
        ("Hello!", False),
        ("why did the scraper fail?", False),
        ("can you explain the cache?", False),
        (
            '<@1504235933598486580> Without building anything, can you give me some '
            'concrete examples of the sort of items in the "include only" list?',
            False,
        ),
        ("okay, thanks", False),
        ("action request: investigate the flaky tests", True),
        ("feature request: add dark mode", True),
        ("this needs a bug fix", True),
        ("new feature for saved searches", True),
        ("fix the login bug", True),
        ("run the entire pipeline again", True),
    ],
)
def test_heuristic_keeps_only_precise_verdicts(adapter, message, expected):
    assert adapter._heuristic_action_request_intent(message) is expected


@pytest.mark.parametrize(
    "acknowledgement",
    ["ok", "okay", "sure", "great", "cool", "thanks", "thank you"],
)
def test_heuristic_strips_leading_acknowledgements(adapter, acknowledgement):
    message = f"{acknowledgement}, fix the login bug"

    assert adapter._heuristic_action_request_intent(message) is True


def test_referential_approval_requires_existing_action_thread_context(adapter):
    message = "<@1504235933598486580> Okay, let's build this."

    assert adapter._heuristic_action_request_intent(message) is None
    assert adapter._heuristic_action_request_intent(
        message,
        actionable_thread_context=True,
    ) is True


@pytest.mark.asyncio
async def test_referential_approval_in_action_thread_skips_llm_triage(
    adapter,
):
    with patch("agent.auxiliary_client.call_llm") as call_llm:
        result = await adapter._classify_discord_action_request(
            "<@1504235933598486580> Okay, let's build this.",
            actionable_thread_context=True,
        )

    assert result is True
    call_llm.assert_not_called()


@pytest.mark.parametrize(
    "message",
    [
        "run the pipeline again",
        "the billing issue is resolved; run the entire pipeline from scratch",
        "execute the pipeline for the latest release",
        "okay the incident is resolved, execute the entire pipeline again",
    ],
)
def test_heuristic_keeps_pipeline_action_phrases_precise(adapter, message):
    assert adapter._heuristic_action_request_intent(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "fix the login bug?",
        "can you rerun the scraper?",
        "should we retry the deploy?",
        "update: the deploy finished",
        "run completed successfully",
        "make is reporting a failure",
        "support is investigating the incident",
        "change was approved yesterday",
        "how do I run the pipeline?",
        "yes do that",
        "thanks, can you update the docs?",
        "we need to fix the flaky test",
        "okay, run the entire pipeline?",
    ],
)
def test_heuristic_defers_ambiguous_messages(adapter, message):
    assert adapter._heuristic_action_request_intent(message) is None


@pytest.mark.asyncio
async def test_ambiguous_message_defaults_to_question_without_llm(
    adapter,
):
    with patch("agent.auxiliary_client.call_llm") as call_llm:
        result = await adapter._classify_discord_action_request(
            "yes do that",
            context_lines=[
                "channel: #deploys",
                "alex: Please rerun the failed deploy.",
            ],
        )

    assert result is False
    call_llm.assert_not_called()


def test_feature_triage_timeout_defaults_to_five_seconds(adapter, monkeypatch):
    monkeypatch.delenv("DISCORD_FEATURE_SUMMARY_TRIAGE_TIMEOUT", raising=False)

    assert adapter._feature_triage_timeout_seconds() == 5.0


@pytest.mark.asyncio
async def test_question_shaped_action_ask_starts_in_safe_intake(adapter):
    result = await adapter._classify_discord_action_request(
        "can you get the tests passing?"
    )

    assert result is False
