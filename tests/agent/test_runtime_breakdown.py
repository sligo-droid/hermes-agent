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
    assert "Visual QA:" not in text
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


def test_runtime_breakdown_carries_one_sanitized_visual_receipt():
    receipt = {
        "level": "artifact",
        "target": "national-map-export",
        "assertions": ["attribution remains inside image bounds"],
        "check": "rendered-artifact-inspection",
        "status": "passed",
        "evidence_ref": "representative export inspected",
    }

    breakdown = build_turn_runtime_breakdown({
        "visual_qa_level": "artifact",
        "visual_qa_receipts": [receipt, {**receipt, "status": "failed"}],
        "visual_qa_check_duration_s": 2.5,
        "visual_qa_followup_count": 1,
    })

    assert breakdown["visual_qa_receipts"] == [receipt]
    assert breakdown["visual_qa"] == {
        "level": "artifact",
        "receipt_status": "passed",
        "followup_count": 1,
        "check_duration_s": 2.5,
    }
    assert "Visual QA: artifact" in render_runtime_breakdown_text(breakdown)
    assert "check 2s" in render_runtime_breakdown_text(breakdown)
    assert "1 follow-up" in render_runtime_breakdown_text(breakdown)


def test_runtime_breakdown_reports_missing_explicit_visual_receipt_without_duration():
    breakdown = build_turn_runtime_breakdown(
        {
            "visual_qa_level": "surface",
            "visual_qa_check_duration_s": 20,
            "visual_qa_followup_count": 3,
        }
    )

    assert breakdown["visual_qa"] == {
        "level": "surface",
        "receipt_status": "missing",
        "followup_count": 1,
        "check_duration_s": 0.0,
    }
