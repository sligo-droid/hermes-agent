from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from agent import conversation_loop
from agent import tool_executor
from agent.runtime_audit import set_runtime_audit_context


def test_turn_runtime_summary_logs_structured_totals(caplog):
    usage = SimpleNamespace(
        input_tokens=100,
        output_tokens=7,
        total_tokens=107,
        cache_read_tokens=80,
        cache_write_tokens=4,
        reasoning_tokens=3,
    )
    agent = SimpleNamespace(
        session_id="session-1",
        model="model-a",
        provider="provider-a",
        platform="discord",
        api_mode="codex_responses",
        reasoning_config={"enabled": True, "effort": "high"},
        service_tier=None,
        session_role="operator",
        _turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0),
    )
    set_runtime_audit_context(
        agent,
        model_tier="intermediate",
        model_tier_source="route",
        runtime_route="discord_action_request",
        runtime_role="interactive",
        runtime_pass="build",
        reasoning_source="model_tier",
        service_tier_source="gateway_disabled",
    )

    conversation_loop._record_turn_api_runtime(agent, 1.25, usage, 100)
    tool_executor._record_turn_tool_runtime(agent, "terminal", 0.5, "ok", False)
    tool_executor._record_turn_tool_runtime(agent, "read_file", 0.1, "missing", True)

    with caplog.at_level(logging.INFO):
        conversation_loop._log_turn_runtime_summary(
            agent,
            total_elapsed_s=2.0,
            exit_reason="text_response",
            interrupted=False,
            response_len=42,
        )

    record = next(item for item in caplog.records if item.message.startswith("turn_runtime_summary "))
    payload = json.loads(record.message.split("turn_runtime_summary ", 1)[1])

    assert payload["session"] == "session-1"
    assert payload["total_ms"] == 2000
    assert payload["api_ms"] == 1250
    assert payload["tool_ms"] == 600
    assert payload["overhead_ms"] == 150
    assert payload["api_calls"] == 1
    assert payload["tool_calls"] == 2
    assert payload["tool_errors"] == 1
    assert payload["input_tokens"] == 100
    assert payload["cache_read_tokens"] == 80
    assert payload["max_prompt_tokens"] == 100
    assert payload["model_tier"] == "intermediate"
    assert payload["model_tier_source"] == "route"
    assert payload["runtime_route"] == "discord_action_request"
    assert payload["runtime_role"] == "interactive"
    assert payload["runtime_pass"] == "build"
    assert payload["reasoning_effort"] == "high"
    assert payload["reasoning_mode"] == "effort"
    assert payload["reasoning_enabled"] is True
    assert payload["reasoning_source"] == "model_tier"
    assert payload["service_tier"] == "default"
    assert payload["service_tier_source"] == "gateway_disabled"
    assert payload["api_mode"] == "codex_responses"
    assert payload["top_tools"][0]["name"] == "terminal"


def test_runtime_audit_distinguishes_default_and_disabled_reasoning():
    default_agent = SimpleNamespace(
        reasoning_config=None,
        service_tier=None,
        _session_init_model_config={},
    )
    disabled_agent = SimpleNamespace(
        reasoning_config={"enabled": False}, service_tier="priority"
    )

    default = set_runtime_audit_context(
        default_agent,
        runtime_route="cli",
        api_key="must-not-be-recorded",
        prompt="must-not-be-recorded",
    )
    disabled = set_runtime_audit_context(
        disabled_agent,
        runtime_route="cli",
        reasoning_source="session_override",
        service_tier_source="session_override",
    )

    assert default["reasoning_effort"] == "default"
    assert default["reasoning_enabled"] is None
    assert "api_key" not in default_agent._runtime_audit_context
    assert "prompt" not in default_agent._runtime_audit_context
    assert default_agent._session_init_model_config["runtime_audit"] == default
    assert disabled["reasoning_effort"] == "none"
    assert disabled["reasoning_mode"] == "disabled"
    assert disabled["reasoning_enabled"] is False
    assert disabled["reasoning_source"] == "session_override"
    assert disabled["service_tier"] == "priority"
