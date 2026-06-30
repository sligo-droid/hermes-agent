from __future__ import annotations

import logging
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.provider_progress import clear_provider_progress_signal, record_provider_progress_signal
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


def test_provider_no_progress_resets_on_committed_worker_artifact_signal(monkeypatch):
    agent = _bare_agent()
    clock = {"now": 1_000.0}
    monkeypatch.setattr("run_agent.time.time", lambda: clock["now"])
    monkeypatch.setattr("agent.provider_progress.time.time", lambda: clock["now"])
    clear_provider_progress_signal(agent._gateway_session_key)

    agent._provider_no_progress_start_turn()
    agent._provider_no_progress_mark_progress("assistant_content", phase="assistant_response")
    clock["now"] += 11.0
    agent._record_provider_no_progress_retry(phase="provider_api_error", failure_class="timeout")
    assert agent._provider_no_progress_should_trip("provider_api_error") is True

    record_provider_progress_signal(
        agent._gateway_session_key,
        "committed_worker_artifacts",
        phase="worker_artifact_delivery",
        source="kanban_notifier",
        metadata={"task_id": "t-1"},
    )

    assert agent._provider_no_progress_should_trip("provider_api_error") is False
    assert agent._provider_no_progress_retry_count == 0
    assert agent._provider_no_progress_last_progress_reason == "kanban_notifier:committed_worker_artifacts"


def test_provider_no_progress_resets_on_ledger_and_finalizer_signals(monkeypatch):
    agent = _bare_agent()
    clock = {"now": 2_000.0}
    monkeypatch.setattr("run_agent.time.time", lambda: clock["now"])
    monkeypatch.setattr("agent.provider_progress.time.time", lambda: clock["now"])
    clear_provider_progress_signal(agent._gateway_session_key)

    agent._provider_no_progress_start_turn()
    agent._provider_no_progress_mark_progress("successful_tool_call", phase="tool_execution")
    clock["now"] += 11.0
    assert agent._provider_no_progress_should_trip("provider_api_error") is True

    record_provider_progress_signal(
        agent._gateway_session_key,
        "ledger_status_agent_running",
        phase="work_ledger",
        source="work_ledger",
    )
    assert agent._provider_no_progress_should_trip("provider_api_error") is False
    assert agent._provider_no_progress_last_progress_reason == "work_ledger:ledger_status_agent_running"

    clock["now"] += 11.0
    assert agent._provider_no_progress_should_trip("provider_api_error") is True

    record_provider_progress_signal(
        agent._gateway_session_key,
        "ledger_status_summary_updated",
        phase="work_ledger",
        source="work_ledger",
    )
    assert agent._provider_no_progress_should_trip("provider_api_error") is False
    assert agent._provider_no_progress_last_progress_reason == "work_ledger:ledger_status_summary_updated"


def test_provider_retries_without_external_progress_still_trip(monkeypatch):
    agent = _bare_agent()
    clock = {"now": 3_000.0}
    monkeypatch.setattr("run_agent.time.time", lambda: clock["now"])
    clear_provider_progress_signal(agent._gateway_session_key)

    agent._provider_no_progress_start_turn()
    agent._provider_no_progress_mark_progress("assistant_content", phase="assistant_response")
    clock["now"] += 11.0

    agent._record_provider_no_progress_retry(phase="provider_api_error", failure_class="timeout")
    agent._record_provider_no_progress_retry(phase="provider_api_error", failure_class="timeout")

    assert agent._provider_no_progress_should_trip("provider_api_error") is True
    assert agent._provider_no_progress_retry_count == 2


def test_provider_no_progress_disabled_outside_gateway():
    agent = _bare_agent()
    agent._gateway_session_key = None
    agent._provider_no_progress_start_turn()
    agent._provider_no_progress_mark_progress("assistant_content", phase="assistant_response")
    agent._provider_no_progress_last_progress_at = time.time() - 60.0

    assert agent._provider_no_progress_should_trip("provider_api_error") is False


def test_gateway_turn_preserves_partial_work_after_tool_progress_then_provider_stall(monkeypatch):
    responses = [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=[
                            SimpleNamespace(
                                id="call-1",
                                type="function",
                                function=SimpleNamespace(name="web_search", arguments='{"query":"x"}'),
                            )
                        ],
                    ),
                    finish_reason="tool_calls",
                )
            ],
            model="gpt-5.5",
            usage=None,
        ),
        SimpleNamespace(choices=[], model="gpt-5.5", usage=None),
        SimpleNamespace(choices=[], model="gpt-5.5", usage=None),
    ]

    with (
        patch("run_agent.get_tool_definitions", return_value=[{
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "search",
                "parameters": {"type": "object", "properties": {}},
            },
        }]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={"agent": {"provider_no_progress_timeout": 10}}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            provider="openai-codex",
            model="gpt-5.5",
            api_mode="chat_completions",
            max_iterations=3,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            gateway_session_key="agent:main:discord:thread:thread-1",
        )

    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent._api_max_retries = 3
    clock = {"now": 1_000.0}

    def fake_api_call(_api_kwargs):
        if len(responses) == 2:
            clock["now"] += 11.0
        return responses.pop(0)

    def fake_handle(*_args, **_kwargs):
        return '{"ok": true, "artifact": "committed worker result"}'

    monkeypatch.setattr("run_agent.time.time", lambda: clock["now"])
    monkeypatch.setattr(agent, "_interruptible_api_call", fake_api_call)
    monkeypatch.setattr(agent, "_interruptible_streaming_api_call", lambda api_kwargs, **_kw: fake_api_call(api_kwargs))
    monkeypatch.setattr("agent.conversation_loop.jittered_backoff", lambda *args, **kwargs: 0.0)

    with patch("run_agent.handle_function_call", side_effect=fake_handle):
        result = agent.run_conversation("do useful work")

    event = result["provider_no_progress"]
    assert result["failed"] is True
    assert result["recoverable"] is True
    assert result["partial"] is True
    assert result["completed"] is False
    assert "partial transcript and tool results were saved" in result["final_response"]
    assert event["session_id"] == agent.session_id
    assert event["gateway_session_key"] == "agent:main:discord:thread:thread-1"
    assert event["provider"] == "openai-codex"
    assert event["model"] == "gpt-5.5"
    assert event["phase"] == "provider_invalid_response"
    assert event["failure_class"] == "invalid_response"
    assert event["action"] == "degraded_partial"
    assert event["last_progress_reason"] == "successful_tool_call"
    assert event["retry_count"] == 1
    assert any(msg.get("role") == "tool" and "committed worker result" in msg.get("content", "") for msg in result["messages"])
