from __future__ import annotations

from agent.runtime_breakdown import build_turn_runtime_breakdown, merge_runtime_breakdowns, render_runtime_breakdown_text


def test_render_turn_runtime_breakdown_compact_top_tools():
    breakdown = build_turn_runtime_breakdown(
        {
            "api_calls": 2,
            "api_duration_s": 26,
            "tool_calls": 3,
            "tool_duration_s": 11,
            "tools": {
                "terminal": {"count": 1, "duration_s": 8},
                "read_file": {"count": 2, "duration_s": 2},
            },
        },
        total_elapsed_s=42,
    )

    text = render_runtime_breakdown_text(breakdown)

    assert text.splitlines()[0] == "42s wall · model 26s · tools 11s · overhead 5s"
    assert "Top: terminal 8s · read_file 2s" in text
    assert len(text) <= 1024


def test_runtime_merge_accumulates_without_raw_details():
    merged = merge_runtime_breakdowns(
        [
            build_turn_runtime_breakdown({"api_calls": 1, "api_duration_s": 2}, total_elapsed_s=3),
            build_turn_runtime_breakdown({"tool_calls": 1, "tool_duration_s": 4}, total_elapsed_s=5),
        ],
        scope="goal",
    )

    assert merged["wall_s"] == 8
    assert merged["model_s"] == 2
    assert merged["tools_s"] == 4
    assert merged["api_calls"] == 1
    assert merged["tool_calls"] == 1
