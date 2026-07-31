from __future__ import annotations

from agent.runtime_breakdown import build_turn_runtime_breakdown, merge_runtime_breakdowns, render_runtime_breakdown_text


_VISUAL_RECEIPT = {
    "requirement_id": "vrq_" + ("1" * 24),
    "contract_id": "vac_" + ("2" * 24),
    "assertion_ids": ["layout-check"],
    "status": "passed",
    "attempts": 1,
    "vision_calls": 0,
    "duration_ms": 25,
    "diagnostic_codes": ["viewport_contained_satisfied"],
}


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
    receipt = {**_VISUAL_RECEIPT, "order": 1}
    latest = {**receipt, "status": "failed", "order": 2}

    breakdown = build_turn_runtime_breakdown({
        "visual_qa_level": "artifact",
        "visual_qa_receipts": [receipt, latest],
        "visual_qa_check_duration_s": 2.5,
        "visual_qa_followup_count": 1,
    })

    assert breakdown["visual_qa_receipts"] == [latest]
    assert breakdown["visual_qa"] == {
        "level": "artifact",
        "receipt_status": "failed",
        "followup_count": 1,
        "check_duration_s": 2.5,
    }
    assert "Visual QA: artifact" in render_runtime_breakdown_text(breakdown)
    assert "check 2s" in render_runtime_breakdown_text(breakdown)
    assert "1 follow-up" in render_runtime_breakdown_text(breakdown)


def test_runtime_breakdown_preserves_orchestrator_coverage_for_ledger_validation():
    receipt = {
        **_VISUAL_RECEIPT,
        "assertion_ids": ["vassert_" + ("3" * 24)],
        "coverage_ids": ["vassert_" + ("4" * 24)],
        "order": 7,
    }

    breakdown = build_turn_runtime_breakdown(
        {
            "visual_qa_level": "surface",
            "visual_qa_receipts": [receipt],
        }
    )

    assert breakdown["visual_qa_receipts"] == [receipt]


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


def test_runtime_breakdown_v2_uses_trusted_span_unions_and_keeps_v1_fields():
    breakdown = build_turn_runtime_breakdown(
        {
            "api_duration_s": 9,
            "tool_duration_s": 11,
            "phase_spans": [
                {
                    "id": "span-deadbeef-0001",
                    "name": "model_attempt",
                    "phase": "model",
                    "started_at": 100.0,
                    "ended_at": 105.0,
                    "duration_s": 5.0,
                    "status": "ok",
                    "metadata": {"model": "gpt-5", "secret": "drop"},
                },
                {
                    "id": "span-deadbeef-0002",
                    "name": "browser_snapshot",
                    "phase": "browser",
                    "started_at": 102.0,
                    "ended_at": 108.0,
                    "duration_s": 6.0,
                    "status": "ok",
                },
            ],
        },
        total_elapsed_s=10,
    )

    assert breakdown["schema_version"] == 2
    assert breakdown["model_s"] == 9
    assert breakdown["tools_s"] == 11
    assert breakdown["active_s"] == 8.0
    assert breakdown["summed_active_s"] == 11.0
    assert breakdown["overlap_s"] == 3.0
    assert breakdown["max_concurrency"] == 2
    assert breakdown["span_window_s"] == 8.0
    assert breakdown["phases"] == [
        {
            "name": "browser",
            "duration_s": 6.0,
            "summed_s": 6.0,
            "overlap_s": 0.0,
            "count": 1,
        },
        {
            "name": "model",
            "duration_s": 5.0,
            "summed_s": 5.0,
            "overlap_s": 0.0,
            "count": 1,
        },
    ]
    assert breakdown["phase_spans"][0]["metadata"]["model"].startswith("meta_")
    assert "gpt-5" not in repr(breakdown["phase_spans"][0])
    assert "secret" not in repr(breakdown)


def test_runtime_breakdown_preserves_uncertain_visual_status():
    breakdown = build_turn_runtime_breakdown(
        {
            "visual_qa_level": "surface",
            "visual_qa_receipts": [{**_VISUAL_RECEIPT, "status": "uncertain"}],
        }
    )

    assert breakdown["visual_qa"]["receipt_status"] == "uncertain"


def test_runtime_breakdown_carries_only_sanitized_closeout_receipt():
    breakdown = build_turn_runtime_breakdown({
        "closeout_receipt": {
            "status": "completed",
            "head_sha": "c" * 40,
            "script": "scripts/closeout.sh",
            "raw_output": "do-not-store",
        },
    })

    assert breakdown["closeout_receipt"] == {
        "schema_version": 1,
        "status": "completed",
        "head_sha": "c" * 40,
        "script": "scripts/closeout.sh",
    }
    assert "do-not-store" not in repr(breakdown)
