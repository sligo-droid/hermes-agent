from __future__ import annotations

import json
import logging
from types import SimpleNamespace

from agent import conversation_loop
from agent import tool_executor


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
        _turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0),
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
    assert payload["top_tools"][0]["name"] == "terminal"
