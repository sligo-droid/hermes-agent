from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


@pytest.fixture
def adapter():
    return DiscordAdapter(PlatformConfig(enabled=True, token="fake-token"))


@pytest.fixture
def run_llm_inline(monkeypatch):
    async def _run_inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(
        "plugins.platforms.discord.adapter.asyncio.to_thread",
        _run_inline,
    )


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("", False),
        ("Hello!", False),
        ("why did the scraper fail?", False),
        ("can you explain the cache?", False),
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
    ],
)
def test_heuristic_defers_ambiguous_messages(adapter, message):
    assert adapter._heuristic_action_request_intent(message) is None


def _response(verdict):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=verdict))]
    )


@pytest.mark.asyncio
async def test_llm_prompt_includes_few_shot_examples_and_context(
    adapter,
    run_llm_inline,
):
    with patch(
        "agent.auxiliary_client.call_llm",
        return_value=_response("question"),
    ) as call_llm:
        result = await adapter._classify_discord_action_request(
            "yes do that",
            context_lines=[
                "channel: #deploys",
                "alex: Please rerun the failed deploy.",
            ],
        )

    assert result is False
    call_llm.assert_called_once()
    kwargs = call_llm.call_args.kwargs
    prompt = kwargs["messages"][1]["content"]
    assert "Few-shot examples:" in prompt
    assert "Message: update: the deploy finished\nVerdict: question" in prompt
    assert "Message: can you get the tests passing?\nVerdict: action" in prompt
    assert "Message: should we retry the deploy?\nVerdict: unsure" in prompt
    assert "Message: yes do that\nVerdict: unsure" in prompt
    assert "- channel: #deploys" in prompt
    assert "- alex: Please rerun the failed deploy." in prompt
    assert kwargs["max_tokens"] == 12
    assert kwargs["temperature"] == 0
    assert kwargs["timeout"] == adapter._feature_triage_timeout_seconds()


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("action", True),
        ("question", False),
        ("unsure", False),
        ("garbage", False),
    ],
)
@pytest.mark.asyncio
async def test_llm_verdict_mapping(adapter, run_llm_inline, verdict, expected):
    with patch(
        "agent.auxiliary_client.call_llm",
        return_value=_response(verdict),
    ):
        result = await adapter._classify_discord_action_request(
            "can you get the tests passing?"
        )

    assert result is expected


@pytest.mark.asyncio
async def test_llm_exception_fails_safe_to_question(adapter, run_llm_inline):
    with patch(
        "agent.auxiliary_client.call_llm",
        side_effect=RuntimeError("provider unavailable"),
    ):
        result = await adapter._classify_discord_action_request("yes do that")

    assert result is False
