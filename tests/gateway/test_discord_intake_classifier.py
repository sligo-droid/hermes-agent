from unittest.mock import patch

import pytest

from agent.runtime_capabilities import RuntimeMode
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
        ("review the authentication changes", False),
        ("conduct a security audit of the API", False),
        ("plan the database migration", False),
        ("produce a list of recommendations", False),
        ("can you investigate the flaky tests?", False),
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


def test_narrative_prefixed_observational_task_is_read_only(adapter):
    message = (
        "we tried to implement some stuff yesterday via discord but it was a bit "
        "of a disaster. look through the site the way a human would and try to "
        "identify things you may have broken. also look through the commits for "
        "areas you changed and focus on those. produce a list of recommendations"
    )

    assert adapter._heuristic_action_request_intent(message) is False


def test_narrative_prefixed_plan_request_is_read_only(adapter):
    message = (
        "The search bar at the top of the page is jarring because it breaks up "
        "the flow of the layout. Please create a plan for making this improvement."
    )

    assert adapter._heuristic_action_request_intent(message) is False


@pytest.mark.parametrize(
    "message",
    [
        "audit the permissions model and report the findings",
        "please review this pull request",
        "could you verify the release against the acceptance criteria?",
        "research the available migration approaches",
        "write a plan for improving the caching layer",
        "provide recommendations for the next iteration",
    ],
)
@pytest.mark.asyncio
async def test_read_only_task_requests_use_read_only_runtime(adapter, message):
    assert adapter._heuristic_action_request_intent(message) is False
    assert await adapter._classify_discord_runtime_mode(message) is RuntimeMode.READ_ONLY


@pytest.mark.parametrize(
    "message",
    [
        "why did the scraper fail?",
        "can you explain how the cache works?",
        "what is the status of the security audit?",
        "what do you recommend?",
        "did the review finish?",
        "give me an explanation of the change",
    ],
)
def test_explanation_status_and_short_factual_questions_remain_intake(adapter, message):
    assert adapter._heuristic_action_request_intent(message) is not True


@pytest.mark.parametrize(
    "message",
    [
        "update: the deploy finished",
        "run completed successfully",
        "make is reporting a failure",
        "support is investigating the incident",
        "change was approved yesterday",
        "yes do that",
        (
            "sorry for the thrash. Let’s say we don’t go with one API. "
            "What were the limitations with the fifty individual states approach?"
        ),
    ],
)
def test_heuristic_defers_ambiguous_messages(adapter, message):
    assert adapter._heuristic_action_request_intent(message) is None


@pytest.mark.parametrize(
    "message",
    [
        "fix the login bug?",
        "can you rerun the scraper?",
        "thanks, can you update the docs?",
        "we need to fix the flaky test",
        "okay, run the entire pipeline?",
        "can you get the tests passing?",
        "gimme a test no-op change to a comment file in the UI",
        "give me a test no-op change to a comment file in the UI",
    ],
)
def test_heuristic_aggressively_routes_mutation_requests(adapter, message):
    assert adapter._heuristic_action_request_intent(message) is True


@pytest.mark.parametrize(
    "message",
    [
        "should we retry the deploy?",
        "how do I run the pipeline?",
        "Could this parser be improved?",
    ],
)
def test_informational_action_questions_remain_read_only(adapter, message):
    assert adapter._heuristic_action_request_intent(message) is False


@pytest.mark.asyncio
async def test_ambiguous_message_defaults_to_read_only_without_llm(
    adapter,
):
    with patch("agent.auxiliary_client.call_llm") as call_llm:
        result = await adapter._classify_discord_runtime_mode(
            (
                "sorry for the thrash. Let’s say we don’t go with one API. "
                "What were the limitations with the fifty individual states approach?"
            ),
            context_lines=[
                "channel: #deploys",
                "alex: Please rerun the failed deploy.",
            ],
        )

    assert result is RuntimeMode.READ_ONLY
    call_llm.assert_not_called()


def test_feature_triage_timeout_defaults_to_five_seconds(adapter, monkeypatch):
    monkeypatch.delenv("DISCORD_FEATURE_SUMMARY_TRIAGE_TIMEOUT", raising=False)

    assert adapter._feature_triage_timeout_seconds() == 5.0


@pytest.mark.asyncio
async def test_question_shaped_action_ask_routes_directly_to_action(adapter):
    result = await adapter._classify_discord_action_request(
        "can you get the tests passing?"
    )

    assert result is True
