from __future__ import annotations

import logging
import time

from run_agent import AIAgent


def _bare_agent() -> AIAgent:
    agent = object.__new__(AIAgent)
    agent.session_id = "sess-123"
    agent._gateway_session_key = "discord:channel:thread"
    agent.provider = "openai-codex"
    agent.model = "gpt-5.5"
    agent._provider_no_progress_timeout = 10.0
    agent._provider_no_progress_retry_count = 0
    agent._provider_no_progress_events = []
    agent._last_provider_no_progress_event = None
    return agent


def test_provider_no_progress_trips_after_useful_progress(caplog):
    agent = _bare_agent()
    agent._provider_no_progress_start_turn()

    agent._provider_no_progress_mark_progress("successful_tool_call", phase="tool_execution")
    agent._provider_no_progress_last_progress_at = time.time() - 11.0

    with caplog.at_level(logging.WARNING):
        agent._record_provider_no_progress_retry(
            phase="provider_api_error",
            failure_class="timeout",
        )
        assert agent._provider_no_progress_should_trip("provider_api_error") is True
        event = agent._provider_no_progress_event(
            phase="provider_api_error",
            failure_class="timeout",
            action="degraded_partial",
        )

    assert event["session_id"] == "sess-123"
    assert event["gateway_session_key"] == "discord:channel:thread"
    assert event["provider"] == "openai-codex"
    assert event["model"] == "gpt-5.5"
    assert event["phase"] == "provider_api_error"
    assert event["failure_class"] == "timeout"
    assert event["action"] == "degraded_partial"
    assert event["retry_count"] == 1
    assert event["delay_class"] == "under_1m"
    assert "provider_no_progress_event" in caplog.text
    assert "sk-" not in caplog.text


def test_provider_no_progress_requires_prior_useful_progress():
    agent = _bare_agent()
    agent._provider_no_progress_start_turn()
    agent._provider_no_progress_last_progress_at = time.time() - 60.0

    assert agent._provider_no_progress_should_trip("provider_api_error") is False


def test_provider_no_progress_resets_on_continuing_progress():
    agent = _bare_agent()
    agent._provider_no_progress_start_turn()
    agent._provider_no_progress_mark_progress("assistant_content", phase="assistant_response")
    agent._provider_no_progress_last_progress_at = time.time() - 9.0

    assert agent._provider_no_progress_should_trip("provider_api_error") is False

    agent._provider_no_progress_mark_progress("successful_tool_call", phase="tool_execution")
    assert agent._provider_no_progress_should_trip("provider_api_error") is False
    assert agent._provider_no_progress_retry_count == 0


def test_provider_no_progress_disabled_outside_gateway():
    agent = _bare_agent()
    agent._gateway_session_key = None
    agent._provider_no_progress_start_turn()
    agent._provider_no_progress_mark_progress("assistant_content", phase="assistant_response")
    agent._provider_no_progress_last_progress_at = time.time() - 60.0

    assert agent._provider_no_progress_should_trip("provider_api_error") is False
