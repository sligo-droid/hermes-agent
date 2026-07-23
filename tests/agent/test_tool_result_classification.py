"""Tests for shared tool result classification helpers."""

import json

from agent.tool_result_classification import (
    file_mutation_result_landed,
)
from agent.display import _detect_tool_failure
from agent.tool_executor import _process_closeout_receipt


def test_write_file_with_nested_lint_error_counts_as_landed():
    result = json.dumps({
        "bytes_written": 12,
        "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
    })

    assert file_mutation_result_landed("write_file", result) is True


def test_patch_with_nested_lsp_diagnostics_counts_as_landed():
    result = json.dumps({
        "success": True,
        "diff": "--- a/tmp.py\n+++ b/tmp.py\n",
        "lsp_diagnostics": "<diagnostics>ERROR [1:1] type mismatch</diagnostics>",
    })

    assert file_mutation_result_landed("patch", result) is True


def test_top_level_file_mutation_error_does_not_count_as_landed():
    result = json.dumps({"success": True, "error": "post-write verification failed"})

    assert file_mutation_result_landed("patch", result) is False


def test_side_effect_classification_keeps_session_mutations():
    from agent.tool_result_classification import tool_may_have_side_effect

    assert tool_may_have_side_effect("todo") is True
    assert tool_may_have_side_effect("memory") is True
    assert tool_may_have_side_effect("write_file") is True
    assert tool_may_have_side_effect("mcp_unknown") is True
    assert tool_may_have_side_effect("read_file") is False
    assert tool_may_have_side_effect("web_search") is False


def test_terminal_semantic_failure_counts_as_failure_with_exit_zero():
    result = json.dumps({
        "output": "test output",
        "exit_code": 0,
        "error": None,
        "classification": {
            "kind": "test_failure",
            "semantic_failure": True,
            "dependency_installation_indicated": False,
            "summary": "Command output indicates a test failure.",
        },
    })

    assert _detect_tool_failure("terminal", result) == (True, " [test failure]")


class _CloseoutAgent:
    _origin_work_item_id = ""
    visual_qa_config = {"mode": "shadow"}
    visual_qa_requirement = {"level": "none"}
    _visual_qa_last_edit_order = 0
    _turn_runtime_stats = {}
    _budget_grace_call = False


def test_closeout_receipt_records_only_sanitized_turn_state(monkeypatch):
    agent = _CloseoutAgent()
    monkeypatch.setattr(
        "agent.terminal_outcomes.inspect_repo_closeout_receipt",
        lambda **kwargs: {
            "schema_version": 1,
            "status": "passed",
            "head_sha": "a" * 40,
            "script": "scripts/closeout.sh",
        },
    )
    result, accepted = _process_closeout_receipt(
        agent,
        "terminal",
        {"command": "./scripts/closeout.sh"},
        json.dumps({
            "exit_code": 0,
            "closeout_receipt": {
                "status": "passed",
                "head_sha": "a" * 40,
                "script": "scripts/closeout.sh",
                "secret": "drop-me",
            },
        }),
    )

    assert accepted is True
    payload = json.loads(result)
    assert payload["closeout_receipt"] == {
        "schema_version": 1,
        "status": "passed",
        "head_sha": "a" * 40,
        "script": "scripts/closeout.sh",
    }
    assert "drop-me" not in result
    assert agent._accepted_closeout_receipt == payload["closeout_receipt"]
    assert agent._turn_runtime_stats["closeout_receipt"] == payload["closeout_receipt"]
    assert agent._budget_grace_call is True


def test_closeout_receipt_rejects_unmet_visual_gate(monkeypatch):
    agent = _CloseoutAgent()
    agent.visual_qa_config = {"mode": "enforce_explicit"}
    agent.visual_qa_requirement = {
        "level": "surface",
        "target": "dashboard",
        "assertions": ["sidebar remains visible"],
    }
    monkeypatch.setattr(
        "agent.terminal_outcomes.inspect_repo_closeout_receipt",
        lambda **kwargs: {
            "schema_version": 1,
            "status": "passed",
            "head_sha": "b" * 40,
            "script": "closeout",
        },
    )
    result, accepted = _process_closeout_receipt(
        agent,
        "terminal",
        {"command": "./closeout"},
        json.dumps({
            "exit_code": 0,
            "closeout_receipt": {
                "status": "passed",
                "head_sha": "b" * 40,
                "script": "closeout",
            },
        }),
    )

    assert accepted is False
    assert json.loads(result)["closeout_receipt_rejected"] == {
        "reason": "visual_qa_pending"
    }
    assert getattr(agent, "_accepted_closeout_receipt", None) is None


def test_closeout_receipt_rejects_pending_required_async_gate(monkeypatch):
    agent = _CloseoutAgent()
    agent._origin_work_item_id = "work-1"
    monkeypatch.setattr(
        "agent.terminal_outcomes.inspect_repo_closeout_receipt",
        lambda **kwargs: {
            "schema_version": 1,
            "status": "passed",
            "head_sha": "e" * 40,
            "script": "closeout",
        },
    )

    class _Ledger:
        def required_async_completion_state(self, work_id):
            assert work_id == "work-1"
            return {
                "has_required": True,
                "failed": False,
                "required_pending_count": 1,
                "sealed": False,
            }

    monkeypatch.setattr("gateway.work_ledger.GatewayWorkLedger", _Ledger)
    result, accepted = _process_closeout_receipt(
        agent,
        "terminal",
        {"command": "./closeout"},
        json.dumps({
            "output": json.dumps({"status": "passed", "head_sha": "e" * 40}),
            "exit_code": 0,
            "classification": {"kind": "unknown", "semantic_failure": False},
            "closeout_receipt": {
                "status": "passed",
                "head_sha": "e" * 40,
                "script": "closeout",
            },
        }),
    )

    assert accepted is False
    assert json.loads(result)["closeout_receipt_rejected"] == {
        "reason": "required_async_pending"
    }
