"""Tests for root-turn authorization of elevated review reasoning."""

import pytest

from agent.conversation_loop import _human_requested_elevated_review_reasoning


@pytest.mark.parametrize(
    "message",
    [
        "Please do a deep review.",
        "Use xhigh for this.",
        "Use high reasoning for this.",
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
        "Explain the high-level reasoning in this review.",
        "Explain the reasoning in the review.",
        "Explain how high reasoning differs from medium.",
        "Do not use high reasoning for this.",
    ],
)
def test_ordinary_review_language_does_not_authorize_deep_review(message):
    assert not _human_requested_elevated_review_reasoning(message)
