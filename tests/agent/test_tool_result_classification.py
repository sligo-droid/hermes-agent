"""Tests for shared tool result classification helpers."""

import json
from types import SimpleNamespace

from agent.tool_result_classification import (
    coding_worker_mutation_paths,
    file_mutation_result_landed,
)
from agent.display import _detect_tool_failure
from agent.tool_executor import (
    _attach_closeout_log_reference,
    _promote_pending_closeout_receipt,
    _process_closeout_receipt,
    _record_coding_worker_mutation_paths,
    _record_visual_qa_edit_order,
)
from agent.visual_qa import classify_visual_requirement


def test_write_file_with_nested_lint_error_counts_as_landed():
    result = json.dumps({
        "bytes_written": 12,
        "lint": {"status": "error", "output": "SyntaxError: invalid syntax"},
    })

    assert file_mutation_result_landed("write_file", result) is True






def test_successful_execute_code_direct_write_counts_as_landed_mutation():
    result = json.dumps({"status": "success", "exit_code": 0})

    assert file_mutation_result_landed(
        "execute_code",
        result,
        {"code": "from pathlib import Path\nPath('/tmp/theme.css').write_text('fixed')"},
    ) is True


def test_read_only_execute_code_does_not_count_as_landed_mutation():
    result = json.dumps({"status": "success", "exit_code": 0})

    assert file_mutation_result_landed(
        "execute_code",
        result,
        {"code": "from pathlib import Path\nprint(Path('/tmp/theme.css').read_text())"},
    ) is False


def test_execute_code_open_write_counts_without_comparison_false_positive():
    result = json.dumps({"status": "success", "exit_code": 0})

    assert file_mutation_result_landed(
        "execute_code",
        result,
        {"code": "with open('/tmp/theme.css', 'w') as handle: handle.write('fixed')"},
    ) is True
    assert file_mutation_result_landed(
        "execute_code",
        result,
        {"code": "print(label.replace('old', 'new'), current_width > minimum_width)"},
    ) is False


def test_coding_worker_host_scope_evidence_counts_as_landed_mutation():
    result = json.dumps(
        {
            "success": True,
            "scope_check": {
                "scope_paths": ["dashboard"],
                "changed_files": ["dashboard/src/App.svelte"],
                "out_of_scope_files": [],
                "clean": True,
            },
        }
    )

    assert coding_worker_mutation_paths(result) == ["dashboard/src/App.svelte"]
    assert file_mutation_result_landed("delegate_coding_task", result) is True


def test_coding_worker_mutation_promotes_parent_visual_gate_and_order():
    result = json.dumps(
        {
            "success": True,
            "scope_check": {
                "scope_paths": ["dashboard"],
                "changed_files": ["dashboard/src/App.svelte"],
                "out_of_scope_files": [],
                "clean": True,
            },
        }
    )
    agent = SimpleNamespace(
        _turn_file_mutation_paths=set(),
        _turn_runtime_stats={"tool_calls": 3},
        _turn_mutation_generation=0,
        _turn_mutation_boundary=0,
        _visual_qa_tool_calls=3,
        _visual_qa_total_calls=4,
        _visual_qa_followup_turns=1,
        _runtime_mode="action",
        _current_task_id="worker-visual-mutation",
        visual_qa_config={"mode": "enforce_explicit"},
        visual_qa_requirement=classify_visual_requirement(
            "Implement a responsive dashboard visual.",
            worker_route="action",
        ),
    )

    _record_coding_worker_mutation_paths(agent, "delegate_coding_task", result)
    _record_visual_qa_edit_order(
        agent,
        "delegate_coding_task",
        result,
        task_id="worker-visual-mutation",
    )

    assert agent._turn_file_mutation_paths == {"dashboard/src/App.svelte"}
    assert agent.visual_qa_requirement["level"] == "surface"
    assert agent._turn_mutation_generation == 1
    assert agent._turn_mutation_boundary == 4
    assert agent._visual_qa_tool_calls == 0
    assert agent._visual_qa_total_calls == 0
    assert agent._visual_qa_followup_turns == 0


def test_execute_code_edit_resets_visual_generation_budget():
    agent = SimpleNamespace(
        _turn_runtime_stats={"tool_calls": 8},
        _turn_mutation_generation=1,
        _turn_mutation_boundary=4,
        _visual_qa_last_edit_order=4,
        _visual_qa_tool_calls=3,
        _visual_qa_total_calls=3,
        _visual_qa_followup_turns=1,
        _current_task_id="preview-variants",
    )

    _record_visual_qa_edit_order(
        agent,
        "execute_code",
        json.dumps({"status": "success", "exit_code": 0}),
        function_args={
            "code": (
                "from pathlib import Path\n"
                "path = Path('/tmp/app.css')\n"
                "path.write_text(path.read_text() + '/* divider */')"
            )
        },
        task_id="preview-variants",
    )

    assert agent._visual_qa_last_edit_order == 9
    assert agent._visual_qa_tool_calls == 0
    assert agent._visual_qa_total_calls == 0
    assert agent._visual_qa_followup_turns == 0


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
    _budget_grace_call = False

    def __init__(self):
        self._turn_runtime_stats = {}


def test_closeout_receipt_records_only_sanitized_turn_state(monkeypatch):
    agent = _CloseoutAgent()
    monkeypatch.setattr(
        "agent.terminal_outcomes.inspect_repo_closeout_receipt",
        lambda **kwargs: {
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
            "output": "build log\n" * 5000,
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
    assert "build log" not in result
    assert "output" not in payload
    assert agent._accepted_closeout_receipt == payload["closeout_receipt"]
    assert agent._turn_runtime_stats["closeout_receipt"] == payload["closeout_receipt"]
    assert agent._budget_grace_call is True


def test_closeout_receipt_waits_for_unmet_visual_gate(monkeypatch):
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
    payload = json.loads(result)
    assert payload["closeout_receipt_pending"] == {
        "reason": "visual_qa_pending",
        "required_tool": "visual_qa",
    }
    assert payload["closeout_receipt"]["head_sha"] == "b" * 40
    assert "output" not in payload
    assert agent._pending_closeout_receipt == payload["closeout_receipt"]
    assert agent._pending_closeout_cwd
    assert agent._budget_grace_call is True
    assert getattr(agent, "_accepted_closeout_receipt", None) is None


def test_pending_closeout_promotes_only_after_visual_gate_passes(monkeypatch):
    agent = _CloseoutAgent()
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "head_sha": "c" * 40,
        "script": "scripts/closeout.sh",
    }
    agent._pending_closeout_receipt = receipt
    agent._pending_closeout_cwd = "/repo"

    monkeypatch.setattr(
        "agent.tool_executor._closeout_receipt_gate_reason",
        lambda _agent: "visual_qa_pending",
    )
    unchanged, promoted = _promote_pending_closeout_receipt(
        agent,
        "visual_qa",
        json.dumps({"status": "failed"}),
    )
    assert promoted is False
    assert json.loads(unchanged) == {"status": "failed"}
    assert agent._pending_closeout_receipt == receipt
    assert getattr(agent, "_accepted_closeout_receipt", None) is None

    monkeypatch.setattr(
        "agent.tool_executor._closeout_receipt_gate_reason",
        lambda _agent: "",
    )
    monkeypatch.setattr(
        "agent.terminal_outcomes.closeout_receipt_matches_repo_state",
        lambda _receipt, _cwd: True,
    )
    result, promoted = _promote_pending_closeout_receipt(
        agent,
        "visual_qa",
        json.dumps({"status": "passed"}),
    )
    assert promoted is True
    payload = json.loads(result)
    assert payload["closeout_receipt"] == receipt
    assert "finalization_required" in payload
    assert agent._pending_closeout_receipt is None
    assert agent._accepted_closeout_receipt == receipt


def test_pending_closeout_rejects_promotion_after_repo_change(monkeypatch):
    agent = _CloseoutAgent()
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "head_sha": "d" * 40,
        "script": "scripts/closeout.sh",
    }
    agent._pending_closeout_receipt = receipt
    agent._pending_closeout_cwd = "/repo"
    monkeypatch.setattr(
        "agent.tool_executor._closeout_receipt_gate_reason",
        lambda _agent: "",
    )
    monkeypatch.setattr(
        "agent.terminal_outcomes.closeout_receipt_matches_repo_state",
        lambda _receipt, _cwd: False,
    )

    result, promoted = _promote_pending_closeout_receipt(
        agent,
        "visual_qa",
        json.dumps({"status": "passed"}),
    )

    assert promoted is False
    assert json.loads(result)["closeout_receipt_rejected"] == {
        "reason": "repository_changed_after_closeout"
    }
    assert agent._pending_closeout_receipt is None
    assert agent._pending_closeout_cwd is None
    assert getattr(agent, "_accepted_closeout_receipt", None) is None


def test_closeout_log_persistence_failure_keeps_compact_control_result(monkeypatch):
    compact = json.dumps({
        "closeout_receipt": {
            "schema_version": 1,
            "status": "passed",
            "head_sha": "f" * 40,
            "script": "scripts/closeout.sh",
        },
        "finalization_required": "finish",
    })
    monkeypatch.setattr(
        "agent.tool_executor.maybe_persist_tool_result",
        lambda **_kwargs: "[Truncated: full output could not be saved to sandbox.]",
    )

    result = _attach_closeout_log_reference(
        compact,
        json.dumps({"output": "large log"}),
        tool_call_id="call-1",
        effective_task_id="task-1",
    )

    payload = json.loads(result)
    assert payload["finalization_required"] == "finish"
    assert payload["closeout_log"].startswith("[Truncated:")
    assert "output" not in payload


def test_closeout_receipt_rejects_pending_required_async_gate(monkeypatch):
    agent = _CloseoutAgent()
    agent._origin_work_item_id = "work-1"
    monkeypatch.setattr(
        "agent.terminal_outcomes.inspect_repo_closeout_receipt",
        lambda **kwargs: {
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
    assert "output" in json.loads(result)
    assert getattr(agent, "_pending_closeout_receipt", None) is None
