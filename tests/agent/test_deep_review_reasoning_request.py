"""Tests for root-turn authorization of elevated review reasoning."""

import pytest

from agent.conversation_loop import _human_requested_elevated_review_reasoning


@pytest.mark.parametrize(
    "message",
    [
        "Please do a deep review.",
        "Use xhigh for this.",
        "Use higher reasoning for this review.",
        "Give this more reasoning effort.",
        "Think harder about the failure path.",
        "Reason more deeply before answering.",
    ],
)
def test_explicit_elevated_reasoning_requests_authorize_deep_review(message):
    assert _human_requested_elevated_review_reasoning(message)


@pytest.mark.parametrize(
    "message",
    [
        "Review this change.",
        "Use high reasoning for this.",
        "Explain the reasoning in the review.",
    ],
)
def test_ordinary_review_language_does_not_authorize_deep_review(message):
    assert not _human_requested_elevated_review_reasoning(message)
