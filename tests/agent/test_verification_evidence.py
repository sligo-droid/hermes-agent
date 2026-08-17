from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from agent import conversation_loop, tool_executor
from agent.visual_assertions import visual_assertion_contract_id
from agent.visual_qa import (
    classify_visual_requirement,
    normalize_visual_requirement,
    visual_requirement_id,
)
from agent.verification_evidence import (
    claim_constraints_for_text,
    classify_tool_verification_evidence,
    classify_tool_visual_receipt,
    classify_verification_command,
    downgrade_final_response_for_evidence,
    latest_evidence_by_surface,
    record_terminal_result,
)


def test_terminal_production_browser_timeout_blocks_matching_shipped_claim():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps({"output": "", "exit_code": 124, "error": "Command timed out after 30 seconds"})

    tool_executor._record_turn_tool_runtime(agent, "terminal", 30.0, result, True)
    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {"command": "python -m hermes_cli.worker_frontend_smoke --url https://app.example --route /modal --browser chromium"},
        result,
        True,
    )

    evidence = agent._turn_runtime_stats["verification_evidence"]
    constraints = claim_constraints_for_text(
        "Shipped and verified in production; browser modal is visible.",
        evidence,
    )

    assert constraints["allowed"] is False
    blocked = {item["surface"]: item for item in constraints["blocked_surfaces"]}
    assert blocked["production_browser"]["status"] == "timeout"
    assert "worker_frontend_smoke" in blocked["production_browser"]["check_name"]


def test_successful_http_probe_timeout_setting_is_not_timeout_evidence():
    command = """python3 - <<'PY'
import urllib.request
request = urllib.request.Request(
    'https://pid-git-public-beta-sligo-labs.vercel.app/',
    headers={'User-Agent': 'PID-deployment-verifier/1.0'},
)
with urllib.request.urlopen(request, timeout=30) as response:
    print('status=', response.status)
PY"""
    result = json.dumps(
        {
            "output": (
                "https://pid-git-public-beta-sligo-labs.vercel.app/ "
                "status= 200 content_type= text/html"
            ),
            "exit_code": 0,
            "error": None,
        }
    )

    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": command},
        result,
        False,
        order=1,
    )

    latest = latest_evidence_by_surface(evidence)
    assert latest["deployment"]["status"] == "success"
    assert claim_constraints_for_text(
        "Deployed the public beta successfully.", evidence
    )["allowed"] is True


def test_terminal_timeout_uses_structured_error_when_partial_output_exists():
    result = json.dumps(
        {
            "output": "probe started and produced partial output",
            "exit_code": 124,
            "error": "Command timed out after 30 seconds",
        }
    )

    evidence = classify_tool_verification_evidence(
        "terminal",
        {
            "command": (
                "python -m hermes_cli.worker_frontend_smoke "
                "--url https://app.example --browser chromium"
            )
        },
        result,
        True,
        order=2,
    )

    assert latest_evidence_by_surface(evidence)["production_browser"]["status"] == "timeout"


def test_failed_test_output_timeout_setting_is_failure_not_timeout():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "scripts/run_tests.sh tests/test_timeout_config.py"},
        json.dumps(
            {
                "output": "E assert configured timeout == 60\n1 failed",
                "exit_code": 1,
                "error": None,
            }
        ),
        True,
        order=3,
    )

    assert latest_evidence_by_surface(evidence)["ci"]["status"] == "failure"


def test_unstructured_timed_out_outcome_remains_timeout():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "scripts/run_tests.sh tests/test_slow_probe.py"},
        json.dumps(
            {
                "output": "[Command timed out after 60s]",
                "exit_code": 1,
                "error": None,
            }
        ),
        True,
        order=4,
    )

    assert latest_evidence_by_surface(evidence)["ci"]["status"] == "timeout"


def test_structured_browser_failure_can_report_timeout_without_wrapper_error():
    evidence = classify_tool_verification_evidence(
        "browser_navigate",
        {"url": "https://app.example/dashboard"},
        json.dumps(
            {
                "success": False,
                "error": "Navigation timed out after 30 seconds",
                "url": "https://app.example/dashboard",
            }
        ),
        False,
        order=3,
    )

    assert latest_evidence_by_surface(evidence)["production_browser"]["status"] == "timeout"


def test_successful_browser_page_timeout_text_is_not_timeout_evidence():
    evidence = classify_tool_verification_evidence(
        "browser_navigate",
        {"url": "https://app.example/settings"},
        json.dumps(
            {
                "success": True,
                "url": "https://app.example/settings",
                "title": "Timeout settings",
                "snapshot": "Request timeout is configured to 30 seconds.",
            }
        ),
        False,
        order=4,
    )

    assert latest_evidence_by_surface(evidence)["production_browser"]["status"] == "success"


def test_successful_terminal_evidence_binds_host_snapshot_to_mutation_boundary():
    head_sha = "a" * 40
    agent = SimpleNamespace(
        _turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0),
        _turn_mutation_generation=3,
        _turn_mutation_boundary=7,
    )
    result = json.dumps(
        {
            "output": "passed",
            "exit_code": 0,
            "error": None,
            "verification_evidence": {
                "status": "passed",
                "kind": "test",
                "scope": "targeted",
                "canonical_command": "scripts/run_tests.sh",
                "repository_root": "/repo/worktree",
                "verified_head_sha": head_sha,
            },
        }
    )

    tool_executor._record_turn_tool_runtime(agent, "terminal", 1.0, result, False)
    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {"command": "scripts/run_tests.sh tests/gateway/test_work_ledger.py"},
        result,
        False,
    )

    evidence = latest_evidence_by_surface(
        agent._turn_runtime_stats["verification_evidence"]
    )["ci"]
    assert evidence["repository_root"] == "/repo/worktree"
    assert evidence["canonical_command"] == "scripts/run_tests.sh"
    assert evidence["scope"] == "targeted"
    assert evidence["mutation_generation"] == 3
    assert evidence["mutation_boundary"] == 7
    assert evidence["verified_head_sha"] == head_sha


def test_read_only_verification_evidence_binds_host_snapshot_to_mutation_boundary():
    head_sha = "b" * 40
    agent = SimpleNamespace(
        _turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0),
        _turn_mutation_generation=4,
        _turn_mutation_boundary=9,
    )
    result = json.dumps(
        {
            "success": True,
            "command": ["scripts/run_tests.sh", "tests/gateway/test_work_ledger.py"],
            "exit_code": 0,
            "output": "passed",
            "error": None,
            "verification_evidence": {
                "status": "passed",
                "scope": "targeted",
                "canonical_command": "scripts/run_tests.sh",
                "repository_root": "/repo/worktree",
                "verified_head_sha": head_sha,
            },
        }
    )

    tool_executor._record_turn_tool_runtime(
        agent,
        "read_only_verify",
        1.0,
        result,
        False,
    )
    tool_executor._record_turn_verification_evidence(
        agent,
        "read_only_verify",
        {"command": "scripts/run_tests.sh tests/gateway/test_work_ledger.py"},
        result,
        False,
    )

    evidence = latest_evidence_by_surface(
        agent._turn_runtime_stats["verification_evidence"]
    )["verification"]
    assert evidence["repository_root"] == "/repo/worktree"
    assert evidence["canonical_command"] == "scripts/run_tests.sh"
    assert evidence["scope"] == "targeted"
    assert evidence["mutation_generation"] == 4
    assert evidence["mutation_boundary"] == 9
    assert evidence["verified_head_sha"] == head_sha


def test_verification_command_allows_narrow_activation_but_rejects_mutation(
    tmp_path,
    monkeypatch,
):
    from agent import coding_context

    facts = {
        "root": str(tmp_path),
        "verifyCommands": ["scripts/run_tests.sh"],
    }
    monkeypatch.setattr(coding_context, "project_facts_for", lambda _cwd: facts)

    accepted = classify_verification_command(
        "source .venv/bin/activate && scripts/run_tests.sh tests/agent/test_x.py",
        cwd=tmp_path,
        exit_code=0,
    )

    assert accepted is not None
    assert accepted.canonical_command == "scripts/run_tests.sh"
    assert accepted.scope == "targeted"
    assert classify_verification_command(
        "scripts/run_tests.sh && mutate && git commit -am unsafe",
        cwd=tmp_path,
        exit_code=0,
    ) is None
    assert classify_verification_command(
        "scripts/run_tests.sh & git commit -am unsafe",
        cwd=tmp_path,
        exit_code=0,
    ) is None


def test_nested_monorepo_verification_binds_exact_git_toplevel_and_head(
    tmp_path,
    monkeypatch,
):
    repository = tmp_path / "repo"
    package = repository / "packages" / "widget"
    (package / "scripts").mkdir(parents=True)
    (package / "pyproject.toml").write_text("[project]\nname = 'widget'\n", encoding="utf-8")
    (package / "scripts" / "run_tests.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=repository,
            capture_output=True,
            text=True,
            check=True,
        )

    git("init")
    git("config", "user.email", "tests@example.invalid")
    git("config", "user.name", "Tests")
    git("add", ".")
    git("commit", "-m", "initial")
    head = git("rev-parse", "HEAD").stdout.strip()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))

    evidence = record_terminal_result(
        command="scripts/run_tests.sh",
        cwd=package,
        session_id="nested-package",
        exit_code=0,
        output="passed",
    )

    assert evidence is not None
    assert evidence["root"] == str(package.resolve())
    assert evidence["repository_root"] == str(repository.resolve())
    assert evidence["verified_head_sha"] == head


def test_non_git_project_root_never_gains_closeout_snapshot(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'standalone'\n", encoding="utf-8")
    (project / "scripts" / "run_tests.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))

    evidence = record_terminal_result(
        command="scripts/run_tests.sh",
        cwd=project,
        session_id="non-git-project",
        exit_code=0,
        output="passed",
    )

    assert evidence is not None
    assert evidence["root"] == str(project.resolve())
    assert "repository_root" not in evidence
    assert "verified_head_sha" not in evidence


def test_untagged_successful_browser_navigation_is_not_visual_qa_receipt():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps({"success": True, "output": "page loaded"})

    tool_executor._record_turn_tool_runtime(agent, "browser_navigate", 0.1, result, False)
    tool_executor._record_turn_verification_evidence(
        agent, "browser_navigate", {"url": "http://127.0.0.1:3000"}, result, False
    )

    assert agent._turn_runtime_stats.get("visual_qa_receipts", []) == []


def test_dedicated_visual_tool_records_distinct_safe_receipt():
    requirement = normalize_visual_requirement(
        {
            "level": "surface",
            "target": "mobile-toolbar",
            "assertions": ["toolbar has no horizontal overflow"],
        }
    )
    assertion_id = requirement["assertions"][0]["id"]
    agent = SimpleNamespace(
        _turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0),
        visual_qa_requirement=requirement,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assertions = [
        {
            "id": assertion_id,
            "kind": "no_horizontal_overflow",
            "locator": {"by": "test_id", "value": "mobile-toolbar"},
        }
    ]
    receipt = {
        "requirement_id": visual_requirement_id(requirement),
        "contract_id": visual_assertion_contract_id(assertions),
        "assertion_ids": [assertion_id],
        "status": "passed",
        "attempts": 1,
        "vision_calls": 0,
        "duration_ms": 100,
        "diagnostic_codes": ["no_horizontal_overflow_satisfied"],
    }
    result = json.dumps({"status": "passed", "visual_qa_receipt": receipt})
    args = {"assertions": assertions}

    tool_executor._record_turn_tool_runtime(agent, "visual_qa", 0.1, result, False)
    tool_executor._record_turn_verification_evidence(agent, "visual_qa", args, result, False, 2.5)

    receipts = agent._turn_runtime_stats["visual_qa_receipts"]
    assert receipts == [{**receipt, "order": 1}]
    assert agent._turn_runtime_stats["visual_qa_check_duration_s"] == 2.5


def test_visual_receipt_uses_execution_args_after_storage_redaction():
    requirement = normalize_visual_requirement(
        {
            "level": "surface",
            "target": "dashboard",
            "assertions": ["dashboard matches the requested appearance"],
        }
    )
    assertion_id = requirement["assertions"][0]["id"]
    assertions = [
        {
            "id": assertion_id,
            "kind": "screenshot_appearance",
            "expectation": "dashboard matches the requested appearance",
        }
    ]
    receipt = {
        "requirement_id": visual_requirement_id(requirement),
        "contract_id": visual_assertion_contract_id(assertions),
        "assertion_ids": [assertion_id],
        "status": "blocked",
        "attempts": 1,
        "vision_calls": 0,
        "duration_ms": 10,
        "diagnostic_codes": ["screenshot_unavailable"],
    }
    result = json.dumps({"status": "blocked", "visual_qa_receipt": receipt})
    execution_args = {"assertions": assertions}
    storage_args = tool_executor._storage_safe_tool_args("visual_qa", execution_args)
    agent = SimpleNamespace(
        _turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0),
        visual_qa_requirement=requirement,
        visual_qa_config={"mode": "enforce_explicit"},
    )

    assert storage_args == {
        "assertions": [{"id": assertion_id, "kind": "screenshot_appearance"}]
    }
    tool_executor._record_turn_tool_runtime(agent, "visual_qa", 0.1, result, False)
    tool_executor._record_turn_verification_evidence(
        agent,
        "visual_qa",
        storage_args,
        result,
        False,
        visual_assertion_args=execution_args,
    )

    assert agent._turn_runtime_stats["visual_qa_receipts"] == [
        {**receipt, "order": 1}
    ]


def test_orchestrated_visual_contract_is_opaque_in_durable_tool_calls():
    requirement = classify_visual_requirement(
        "Fix the Issue Attention graph on the State Brief page.",
        worker_route="action",
    )
    execution_args = {
        "target": {
            "description": "Issue Attention graph region",
            "locator": {"by": "test_id", "value": "issue-attention-graph"},
        },
        "page": {"state": "prepared", "description": "State Brief page"},
        "viewport": {"description": "current desktop viewport"},
        "state": ["chart data loaded"],
        "assertions": [
            {
                "kind": "screenshot_appearance",
                "expectation": "Rounded bars stop above the x-axis and never cross the baseline.",
            }
        ],
    }

    safe_calls = tool_executor.storage_safe_tool_calls(
        [
            {
                "function": {
                    "name": "visual_qa",
                    "arguments": json.dumps(execution_args),
                }
            }
        ]
    )

    stored_args = json.loads(safe_calls[0]["function"]["arguments"])
    assert stored_args["contract_id"].startswith("vac_")
    assert stored_args["assertions"][0]["id"].startswith("vassert_")
    assert stored_args["assertions"][0]["kind"] == "screenshot_appearance"
    serialized = repr(safe_calls)
    assert "Issue Attention" not in serialized
    assert "State Brief" not in serialized
    assert "issue-attention-graph" not in serialized
    assert "x-axis" not in serialized
    assert requirement["assertions"][0]["kind"] == "orchestrator_contract"


def test_standalone_visual_qa_receipt_is_recorded_without_active_requirement():
    from agent.visual_assertions import (
        diagnose_orchestrated_visual_contract,
        visual_execution_contract_id,
        visual_requirement_for_execution_contract,
    )

    execution_args = {
        "target": {"description": "dashboard chart"},
        "page": {"state": "already_open", "description": "dashboard page"},
        "viewport": {"description": "current desktop viewport"},
        "state": ["chart data loaded"],
        "assertions": [
            {
                "kind": "screenshot_appearance",
                "expectation": "The chart is visually balanced.",
            }
        ],
    }
    contract = diagnose_orchestrated_visual_contract(execution_args)["contract"]
    requirement = visual_requirement_for_execution_contract(contract)
    assertion_ids = [item["id"] for item in contract["assertions"]]
    receipt = {
        "requirement_id": visual_requirement_id(requirement),
        "contract_id": visual_execution_contract_id(contract),
        "coverage_ids": [requirement["assertions"][0]["id"]],
        "assertion_ids": assertion_ids,
        "status": "passed",
        "attempts": 1,
        "vision_calls": 2,
        "duration_ms": 100,
        "diagnostic_codes": ["appearance_satisfied"],
    }
    agent = SimpleNamespace(
        _turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0),
        visual_qa_requirement={"level": "none", "target": "", "assertions": []},
        visual_qa_config={"mode": "enforce_explicit"},
    )
    result = json.dumps({"status": "passed", "visual_qa_receipt": receipt})

    tool_executor._record_turn_tool_runtime(agent, "visual_qa", 0.1, result, False)
    tool_executor._record_turn_verification_evidence(
        agent,
        "visual_qa",
        execution_args,
        result,
        False,
    )

    assert agent._turn_runtime_stats["visual_qa_receipts"] == [
        {**receipt, "order": 1}
    ]


def test_later_visual_receipt_replaces_earlier_failure_after_edit():
    requirement = normalize_visual_requirement(
        {
            "level": "surface",
            "target": "mobile-toolbar",
            "assertions": ["toolbar has no horizontal overflow"],
        }
    )
    assertion_id = requirement["assertions"][0]["id"]
    assertions = [
        {
            "id": assertion_id,
            "kind": "no_horizontal_overflow",
            "locator": {"by": "test_id", "value": "mobile-toolbar"},
        }
    ]
    agent = SimpleNamespace(
        _turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0),
        visual_qa_requirement=requirement,
        visual_qa_config={"mode": "enforce_explicit"},
    )

    def result(status):
        receipt = {
            "requirement_id": visual_requirement_id(requirement),
            "contract_id": visual_assertion_contract_id(assertions),
            "assertion_ids": [assertion_id],
            "status": status,
            "attempts": 1,
            "vision_calls": 0,
            "duration_ms": 10,
            "diagnostic_codes": [
                "no_horizontal_overflow_satisfied"
                if status == "passed"
                else "no_horizontal_overflow_mismatch"
            ],
        }
        return json.dumps({"status": status, "visual_qa_receipt": receipt})

    failed = result("failed")
    tool_executor._record_turn_tool_runtime(agent, "visual_qa", 0.1, failed, False)
    tool_executor._record_turn_verification_evidence(
        agent,
        "visual_qa",
        {"assertions": assertions},
        failed,
        False,
    )
    tool_executor._record_turn_tool_runtime(agent, "patch", 0.1, "updated", False)
    agent._visual_qa_last_edit_order = 2
    passed = result("passed")
    tool_executor._record_turn_tool_runtime(agent, "visual_qa", 0.1, passed, False)
    tool_executor._record_turn_verification_evidence(
        agent,
        "visual_qa",
        {"assertions": assertions},
        passed,
        False,
    )

    receipts = agent._turn_runtime_stats["visual_qa_receipts"]
    assert len(receipts) == 1
    assert receipts[0]["status"] == "passed"
    assert receipts[0]["order"] == 3


def test_visual_receipt_rejects_contract_or_order_tampering():
    requirement = normalize_visual_requirement(
        {
            "level": "surface",
            "target": "dashboard",
            "assertions": ["layout is aligned", "copy has the requested appearance"],
        }
    )
    layout_id, copy_id = [item["id"] for item in requirement["assertions"]]
    assertions = [
        {"id": layout_id, "kind": "screenshot_appearance", "expectation": "layout is aligned"},
        {"id": copy_id, "kind": "screenshot_appearance", "expectation": "copy looks ready"},
    ]
    receipt = {
        "requirement_id": visual_requirement_id(requirement),
        "contract_id": visual_assertion_contract_id(assertions),
        "assertion_ids": [layout_id, copy_id],
        "status": "passed",
        "attempts": 1,
        "vision_calls": 1,
        "duration_ms": 10,
        "diagnostic_codes": [],
    }
    tampered_receipts = [
        {**receipt, "contract_id": "vac_" + ("a" * 24)},
        {**receipt, "assertion_ids": [layout_id]},
        {**receipt, "assertion_ids": [copy_id, layout_id]},
        {**receipt, "assertion_ids": [layout_id, "other"]},
    ]

    for tampered in tampered_receipts:
        result = {"status": "passed", "visual_qa_receipt": tampered}
        assert classify_tool_visual_receipt(
            "visual_qa",
            {"assertions": assertions},
            result,
            False,
            requirement=requirement,
        ) is None


def test_visual_receipt_rejects_invalid_assertion_or_stale_requirement():
    requirement = normalize_visual_requirement(
        {"level": "surface", "target": "dashboard", "assertions": ["layout is aligned"]}
    )
    assertion_id = requirement["assertions"][0]["id"]
    assertions = [
        {"id": assertion_id, "kind": "screenshot_appearance", "expectation": "layout is aligned"}
    ]
    receipt = {
        "requirement_id": visual_requirement_id(requirement),
        "contract_id": visual_assertion_contract_id(assertions),
        "assertion_ids": [assertion_id],
        "status": "passed",
        "attempts": 1,
        "vision_calls": 1,
        "duration_ms": 10,
        "diagnostic_codes": [],
    }
    result = {"status": "passed", "visual_qa_receipt": receipt}

    invalid_assertions = [*assertions, {"id": "bad", "kind": "arbitrary_javascript"}]
    assert classify_tool_visual_receipt(
        "visual_qa",
        {"assertions": invalid_assertions},
        result,
        False,
        requirement=requirement,
    ) is None
    stale_requirement = {"level": "surface", "target": "other", "assertions": ["layout"]}
    assert classify_tool_visual_receipt(
        "visual_qa",
        {"assertions": assertions},
        result,
        False,
        requirement=stale_requirement,
    ) is None


def test_orchestrated_uncertain_runner_receipt_is_persistable():
    from agent.visual_assertions import (
        normalize_orchestrated_visual_contract,
        visual_execution_contract_id,
    )

    requirement = classify_visual_requirement(
        "Build a responsive dashboard with a mobile sidebar.",
        worker_route="action",
    )
    args = {
        "target": {"description": "Dashboard hero", "locator": {"by": "css", "value": ".hero"}},
        "page": {"state": "already_open", "description": "Authenticated dashboard"},
        "viewport": {"description": "Desktop viewport"},
        "state": ["Updated hero is rendered."],
        "artifacts": [{"kind": "focused", "description": "Dashboard hero"}],
        "assertions": [
            {"kind": "text_present", "locator": {"by": "css", "value": ".hero"}, "text": "Updated copy"},
            {"kind": "screenshot_appearance", "expectation": "The hero is balanced and readable."},
        ],
    }
    contract = normalize_orchestrated_visual_contract(args)
    receipt = {
        "requirement_id": visual_requirement_id(requirement),
        "contract_id": visual_execution_contract_id(contract),
        "coverage_ids": [item["id"] for item in requirement["assertions"]],
        "assertion_ids": [item["id"] for item in contract["assertions"]],
        "status": "uncertain",
        "attempts": 1,
        "vision_calls": 2,
        "duration_ms": 1804,
        "diagnostic_codes": ["text_present", "vision_call_failed"],
    }

    classified = classify_tool_visual_receipt(
        "visual_qa",
        args,
        {"status": "uncertain", "visual_qa_receipt": receipt},
        False,
        order=31,
        requirement=requirement,
    )

    assert classified is not None
    assert classified["status"] == "uncertain"
    assert classified["vision_calls"] == 2
    assert classified["order"] == 31


def test_orchestrated_receipt_survives_omitted_cursorless_diagnostic():
    from agent.visual_assertions import (
        normalize_orchestrated_visual_contract,
        visual_execution_contract_id,
    )

    requirement = classify_visual_requirement(
        "Build a responsive dashboard with a mobile sidebar.",
        worker_route="action",
    )
    args = {
        "target": {"description": "Dashboard", "locator": {"by": "css", "value": "body"}},
        "page": {"state": "already_open", "description": "Dashboard is open"},
        "viewport": {"description": "Desktop viewport"},
        "state": ["Dashboard data is loaded"],
        "assertions": [
            {"kind": "no_new_diagnostics"},
            {"kind": "screenshot_appearance", "expectation": "The dashboard is readable."},
        ],
    }
    contract = normalize_orchestrated_visual_contract(args)
    receipt = {
        "requirement_id": visual_requirement_id(requirement),
        "contract_id": visual_execution_contract_id(contract),
        "coverage_ids": [item["id"] for item in requirement["assertions"]],
        "assertion_ids": [item["id"] for item in contract["assertions"]],
        "status": "passed",
        "attempts": 1,
        "vision_calls": 2,
        "duration_ms": 100,
        "diagnostic_codes": ["appearance_satisfied"],
    }

    classified = classify_tool_visual_receipt(
        "visual_qa",
        args,
        {"status": "passed", "visual_qa_receipt": receipt},
        False,
        requirement=requirement,
    )

    assert classified is not None
    assert classified["status"] == "passed"


def test_later_success_supersedes_earlier_failed_browser_check():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    fail = json.dumps({"output": "modal missing", "exit_code": 1, "error": None})
    ok = json.dumps({"output": "modal visible", "exit_code": 0, "error": None})

    tool_executor._record_turn_tool_runtime(agent, "terminal", 1.0, fail, True)
    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {"command": "npm run browser:smoke -- --prod --modal"},
        fail,
        True,
    )
    tool_executor._record_turn_tool_runtime(agent, "terminal", 1.0, ok, False)
    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {"command": "npm run browser:smoke -- --prod --modal"},
        ok,
        False,
    )

    latest = latest_evidence_by_surface(agent._turn_runtime_stats["verification_evidence"])
    constraints = claim_constraints_for_text(
        "Production browser modal verified visible after npm run browser:smoke -- --prod --modal.",
        agent._turn_runtime_stats["verification_evidence"],
    )

    assert latest["production_browser"]["status"] == "success"
    assert constraints["allowed"] is True


def test_failed_browser_check_does_not_block_independent_ci_claim():
    evidence = [
        {
            "surface": "browser",
            "check_name": "browser modal smoke",
            "status": "failure",
            "order": 1,
            "detail": "modal missing",
        },
        {
            "surface": "ci",
            "check_name": "scripts/run_tests.sh tests/unit",
            "status": "success",
            "order": 2,
            "detail": "passed",
        },
    ]

    assert claim_constraints_for_text("CI passed via scripts/run_tests.sh.", evidence)["allowed"] is True
    assert claim_constraints_for_text("CI passed and browser modal verified visible.", evidence)["allowed"] is False


def test_playwright_terminal_script_is_browser_evidence_not_ci_from_package_name():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    command = """node <<'NODE'
const { chromium } = require('@playwright/test');
const baseUrl = 'http://127.0.0.1:5184/state';
console.log(JSON.stringify({ ok: false, fail: ['desktop income header did not become active sort'] }));
NODE"""
    result = json.dumps(
        {
            "output": "{\"ok\": false, \"fail\": [\"desktop income header did not become active sort\"]}",
            "exit_code": 1,
            "error": None,
        }
    )

    tool_executor._record_turn_tool_runtime(agent, "terminal", 1.0, result, True)
    tool_executor._record_turn_verification_evidence(agent, "terminal", {"command": command}, result, True)

    latest = latest_evidence_by_surface(agent._turn_runtime_stats["verification_evidence"])

    assert "ci" not in latest
    assert latest["browser"]["status"] == "failure"
    assert "production" not in latest
    assert "production_browser" not in latest
    assert claim_constraints_for_text("PR CI passed and main CI passed.", agent._turn_runtime_stats["verification_evidence"])[
        "allowed"
    ] is True
    assert claim_constraints_for_text(
        "PR CI passed and browser QA verified header sorting.",
        agent._turn_runtime_stats["verification_evidence"],
    )["allowed"] is False


def test_compound_command_detects_verification_after_bounded_display_name():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    failed_command = "git merge --no-edit deadbeef && pnpm exec vitest run src/component.test.ts"
    failed_result = json.dumps(
        {
            "output": "src/component.svelte needs formatting",
            "exit_code": 1,
            "error": None,
        }
    )
    prefix = "prettier --write " + "src/component.svelte " * 12
    command = (
        f"{prefix} && git add src/component.svelte && git commit --amend --no-edit "
        "&& pnpm exec vitest run src/component.test.ts && pnpm check "
        "&& bash scripts/local_lifecycle/closeout.sh --source \"$PWD\""
    )
    result = json.dumps(
        {
            "output": "Tests 9 passed\nsvelte-check found 0 errors and 0 warnings",
            "exit_code": 0,
            "error": None,
        }
    )

    tool_executor._record_turn_tool_runtime(agent, "terminal", 1.0, failed_result, True)
    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {"command": failed_command},
        failed_result,
        True,
    )
    tool_executor._record_turn_tool_runtime(agent, "terminal", 1.0, result, False)
    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {"command": command},
        result,
        False,
    )

    latest = latest_evidence_by_surface(agent._turn_runtime_stats["verification_evidence"])

    assert latest["ci"]["status"] == "success"
    assert len(latest["ci"]["check_name"]) <= 160
    assert "vitest" not in latest["ci"]["check_name"]
    assert claim_constraints_for_text(
        "CI passed after the final closeout verification.",
        agent._turn_runtime_stats["verification_evidence"],
    )["allowed"] is True


def test_persisted_playwright_evidence_misclassified_as_ci_is_not_used_as_ci():
    evidence = [
        {
            "surface": "ci",
            "check_name": "set -a; source qa.env; node <<'NODE'\nconst { chromium } = require('@playwright/test');\nconst baseUrl = 'http://127.0.0.1:5184/state';",
            "status": "failure",
            "order": 50,
            "detail": "{\"ok\": false, \"fail\": [\"desktop income header did not become active sort\"]}",
        }
    ]

    latest = latest_evidence_by_surface(evidence)

    assert "ci" not in latest
    assert "browser" not in latest
    assert claim_constraints_for_text("PR CI passed and main CI passed.", evidence)["allowed"] is True


def test_final_response_downgrade_names_latest_failed_check():
    text = "Shipped and verified in production; browser modal is visible. CI passed via scripts/run_tests.sh."
    evidence = [
        {
            "surface": "production_browser",
            "check_name": "python -m hermes_cli.worker_frontend_smoke --url https://app.example --route /modal --browser chromium",
            "status": "timeout",
            "order": 1,
            "detail": "Timeout 30000ms exceeded",
        },
        {
            "surface": "ci",
            "check_name": "scripts/run_tests.sh tests/unit",
            "status": "success",
            "order": 2,
            "detail": "passed",
        },
    ]

    downgraded, constraints = downgrade_final_response_for_evidence(text, evidence)

    assert constraints["allowed"] is False
    assert "Verification downgrade:" in downgraded
    assert "production browser verification is not verified" in downgraded
    assert "worker_frontend_smoke" in downgraded
    assert "timeout" in downgraded
    assert "CI passed via scripts/run_tests.sh" in downgraded
    assert "Shipped and verified in production" not in downgraded
    assert "browser modal is visible" not in downgraded


def test_conversation_loop_final_response_guard_uses_turn_runtime_evidence():
    agent = SimpleNamespace(
        _turn_runtime_stats={
            "verification_evidence": [
                {
                    "surface": "production_browser",
                    "check_name": "browser modal smoke",
                    "status": "timeout",
                    "order": 1,
                }
            ]
        }
    )

    downgraded, transformed, constraints = conversation_loop._downgrade_final_response_for_turn_evidence(
        agent,
        "Shipped and verified in production; browser modal is visible.",
    )

    assert transformed is True
    assert constraints["allowed"] is False
    assert "Verification downgrade:" in downgraded
    assert "browser modal smoke" in downgraded
    assert "Shipped and verified in production" not in downgraded
    assert "browser modal is visible" not in downgraded


def test_final_response_downgrade_skips_later_success():
    text = "Production browser modal verified visible after npm run browser:smoke -- --prod --modal."
    evidence = [
        {"surface": "production_browser", "check_name": "npm run browser:smoke -- --prod --modal", "status": "failure", "order": 1},
        {"surface": "production_browser", "check_name": "npm run browser:smoke -- --prod --modal", "status": "success", "order": 2},
    ]

    downgraded, constraints = downgrade_final_response_for_evidence(text, evidence)

    assert constraints["allowed"] is True
    assert downgraded == text


def test_failed_build_does_not_block_precise_success_claims_for_other_checks():
    text = (
        "**Fresh verification**\n"
        "- Focused tests: 6 passed\n"
        "- Svelte check: 0 errors, 0 warnings\n"
        "- Vercel: passed\n"
    )
    evidence = [
        {
            "surface": "verification",
            "check_name": "pnpm --dir dashboard build",
            "status": "failure",
            "order": 1,
        }
    ]

    assert claim_constraints_for_text(text, evidence)["allowed"] is True


def test_failed_test_blocks_matching_focused_test_success_claim():
    evidence = [
        {
            "surface": "verification",
            "check_name": "pnpm test -- src/routes/state/page-source.test.ts",
            "status": "failure",
            "order": 1,
        }
    ]

    constraints = claim_constraints_for_text("Focused tests: 6 passed", evidence)

    assert constraints["allowed"] is False
    assert constraints["blocked_surfaces"][0]["check_name"].startswith("pnpm test")


def test_read_only_verify_accepts_success_receipt_before_loop_warning():
    receipt = {
        "success": True,
        "exit_code": 0,
        "output": "540 passed",
        "error": None,
        "verification_evidence": {
            "status": "passed",
            "canonical_command": "pnpm run test",
            "scope": "full",
            "repository_root": "/tmp/project",
            "verified_head_sha": "a" * 40,
        },
    }
    result = json.dumps(receipt) + "\n\n[Tool loop warning: repeated_exact_failure_warning]"

    evidence = classify_tool_verification_evidence(
        "read_only_verify",
        {"command": "pnpm test"},
        result,
        False,
        order=12,
    )

    assert evidence == [
        {
            "schema_version": 1,
            "surface": "verification",
            "check_name": "pnpm test",
            "status": "success",
            "order": 12,
            "detail": "540 passed",
            "repository_root": "/tmp/project",
            "canonical_command": "pnpm run test",
            "scope": "full",
            "verified_head_sha": "a" * 40,
        }
    ]


def test_pr_merge_downgrade_line_does_not_create_pr_success_claim():
    evidence = [
        {
            "surface": "pr",
            "check_name": (
                "git -C /home/droid/workspaces/PID-airflow-runtime status --short --branch && "
                "git -C /home/droid/workspaces/PID-airflow-runtime pull --ff-only origin main && git"
            ),
            "status": "failure",
            "order": 1,
        }
    ]
    final_text = (
        "Task list is fully complete: 5/5.\n"
        "Direct worker enabled and healthy/current.\n"
        "Production API direct path is live.\n"
        "Request generated via direct_worker.\n"
        "Focused route test passed 7/7.\n\n"
        "Verification downgrade: PR/merge verification is not verified: latest check "
        "`git -C /home/droid/workspaces/PID-airflow-runtime status --short --branch && "
        "git -C /home/droid/workspaces/PID-airflow-runtime pull --ff-only origin main && git` failure."
    )

    constraints = claim_constraints_for_text(final_text, evidence)

    assert constraints["allowed"] is True
    assert constraints["blocked_surfaces"] == []


def test_final_response_downgrade_keeps_independent_deployed_claim_separate():
    text = "Deployment completed and CI passed. Production browser modal verified visible."
    evidence = [
        {"surface": "production_browser", "check_name": "browser modal smoke", "status": "failure", "order": 3},
        {"surface": "deployment", "check_name": "deployment status", "status": "success", "order": 2},
        {"surface": "ci", "check_name": "gh pr checks", "status": "success", "order": 1},
    ]

    downgraded, constraints = downgrade_final_response_for_evidence(text, evidence)

    assert constraints["allowed"] is False
    assert "Deployment completed and CI passed" in downgraded
    assert "production browser verification is not verified" in downgraded
    assert "browser modal smoke" in downgraded
    assert "Production browser modal verified visible" not in downgraded


def test_protected_canonical_checkout_guardrail_is_not_failed_verification_evidence():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps(
        {
            "output": "",
            "exit_code": 1,
            "error": (
                "BLOCKED: refusing to run a non-read-only terminal command from a protected canonical checkout "
                "on main: /home/droid/.hermes/workspace/examine. Canonical project roots are inspection-only. "
                "Create/use a git worktree under /home/droid/workspace."
            ),
        }
    )

    tool_executor._record_turn_tool_runtime(agent, "terminal", 0.1, result, True)
    tool_executor._record_turn_verification_evidence(agent, "terminal", {"command": "pytest -q"}, result, True)

    assert agent._turn_runtime_stats.get("verification_evidence", []) == []


def test_missing_workflow_lookup_is_not_negative_ci_or_deployment_evidence():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps(
        {
            "output": "could not find any workflows named Deploy Local Dashboard",
            "exit_code": 1,
            "error": None,
        }
    )

    tool_executor._record_turn_tool_runtime(agent, "terminal", 0.2, result, True)
    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {
            "command": (
                "gh run list --repo sligo-labs/PID "
                "--workflow 'Deploy Local Dashboard' --limit 3 "
                "--json databaseId,headSha,status,conclusion,createdAt,url"
            )
        },
        result,
        True,
    )

    assert agent._turn_runtime_stats.get("verification_evidence", []) == []


def test_review_transcript_does_not_turn_completed_contests_and_status_into_ci_claim():
    text = (
        "Review limitation: local development was reachable but required admin authentication; "
        "external development returned Cloudflare Tunnel error 1033. "
        "Upcoming and completed contests are separated. "
        "Group candidates by meaningful status and report findings."
    )
    legacy_evidence = [
        {
            "surface": "ci",
            "check_name": (
                "gh run list --repo sligo-labs/PID "
                "--workflow 'Deploy Local Dashboard' --limit 3"
            ),
            "status": "failure",
            "order": 24,
            "detail": "could not find any workflows named Deploy Local Dashboard",
        }
    ]

    downgraded, constraints = downgrade_final_response_for_evidence(text, legacy_evidence)

    assert constraints["allowed"] is True
    assert downgraded == text


def test_browser_auth_and_error_pages_are_not_successful_verification_evidence():
    cases = [
        (
            "http://127.0.0.1:5173/races",
            '<untrusted_tool_result source="browser_navigate"> '
            '{"success":true,"url":"http://127.0.0.1:5173/races",'
            '"title":"Sign In to Races | Agora","snapshot":"Sign In Username Password"}',
            {"browser"},
        ),
        (
            "https://pid-git-evidence-sligo-labs.vercel.app/races",
            '<untrusted_tool_result source="browser_navigate"> '
            '{"success":true,"url":"https://pid-git-evidence-sligo-labs.vercel.app/races",'
            '"title":"Cloudflare Tunnel error | pid-git-evidence-sligo-labs.vercel.app | Cloudflare",'
            '"snapshot":"Error 1033"}',
            {"browser", "production", "production_browser"},
        ),
    ]

    for url, result, expected_surfaces in cases:
        agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
        tool_executor._record_turn_tool_runtime(agent, "browser_navigate", 0.2, result, False)
        tool_executor._record_turn_verification_evidence(
            agent,
            "browser_navigate",
            {"url": url},
            result,
            False,
        )

        latest = latest_evidence_by_surface(agent._turn_runtime_stats["verification_evidence"])
        assert set(latest) == expected_surfaces
        assert {item["status"] for item in latest.values()} == {"failure"}


def test_normal_browser_navigation_remains_successful_browser_evidence():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps(
        {
            "success": True,
            "url": "http://127.0.0.1:3000/races",
            "title": "Races | Agora",
            "snapshot": "Races directory. Link: Sign In. Latest evidence available.",
        }
    )

    tool_executor._record_turn_tool_runtime(agent, "browser_navigate", 0.2, result, False)
    tool_executor._record_turn_verification_evidence(
        agent,
        "browser_navigate",
        {"url": "http://127.0.0.1:3000/races"},
        result,
        False,
    )

    latest = latest_evidence_by_surface(agent._turn_runtime_stats["verification_evidence"])
    assert latest["browser"]["status"] == "success"


def test_authenticated_qa_success_supersedes_expected_login_boundary():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    login_result = json.dumps(
        {
            "success": True,
            "url": "https://pid-git-evidence-sligo-labs.vercel.app/",
            "title": "Sign In to Home | Agora",
            "snapshot": "Username Password Sign In",
        }
    )
    qa_output = """> pid-dashboard qa:auth
{
  "ok": true,
  "baseUrl": "https://pid-git-evidence-sligo-labs.vercel.app",
  "paths": ["/"],
  "routeCount": 1,
  "routes": [{"path": "/", "finalPath": "/", "screenshotPath": "artifacts/home.png"}],
  "consoleErrorCount": 0,
  "pageErrorCount": 0
}
"""
    qa_result = json.dumps(
        {"output": qa_output, "exit_code": 0, "error": None}
    )

    tool_executor._record_turn_verification_evidence(
        agent,
        "browser_navigate",
        {"url": "https://pid-git-evidence-sligo-labs.vercel.app/"},
        login_result,
        False,
    )
    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {
            "command": (
                "env PID_QA_PATH=/ PID_QA_VIEWPORT_WIDTH=390 "
                "PID_QA_VIEWPORT_HEIGHT=844 pnpm qa:auth"
            )
        },
        qa_result,
        False,
    )

    latest = latest_evidence_by_surface(agent._turn_runtime_stats["verification_evidence"])
    assert latest["browser"]["status"] == "success"
    assert latest["production"]["status"] == "success"
    assert latest["production_browser"]["status"] == "success"
    assert latest["production_browser"]["check_name"] == (
        "qa:auth https://pid-git-evidence-sligo-labs.vercel.app"
    )
    assert claim_constraints_for_text(
        "Authenticated production QA passed with no console or page errors.",
        agent._turn_runtime_stats["verification_evidence"],
    )["allowed"] is True


def test_authenticated_browser_snapshot_supersedes_login_boundary_on_production_url():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    login_result = (
        '<untrusted_tool_result source="browser_navigate">\n'
        '{"success":true,"url":"https://pid-git-evidence-sligo-labs.vercel.app/races/202",'
        '"title":"Sign In to Races | Agora","snapshot":"Username Password Sign In"}\n'
        "</untrusted_tool_result>"
    )
    large_snapshot = "Race detail " + ("content " * 200)
    snapshot_result = (
        '<untrusted_tool_result source="browser_snapshot">\n'
        + json.dumps(
            {
                "success": True,
                "snapshot": large_snapshot,
                "frame_tree": {
                    "top": {
                        "url": "https://pid-git-evidence-sligo-labs.vercel.app/races/202",
                        "origin": "https://pid-git-evidence-sligo-labs.vercel.app",
                    }
                },
            }
        )
        + "\n</untrusted_tool_result>"
    )

    tool_executor._record_turn_verification_evidence(
        agent,
        "browser_navigate",
        {"url": "https://pid-git-evidence-sligo-labs.vercel.app/races/202"},
        login_result,
        False,
    )
    tool_executor._record_turn_verification_evidence(
        agent,
        "browser_snapshot",
        {"full": False},
        snapshot_result,
        False,
    )

    latest = latest_evidence_by_surface(agent._turn_runtime_stats["verification_evidence"])
    assert latest["browser"]["status"] == "success"
    assert latest["production"]["status"] == "success"
    assert latest["production_browser"]["status"] == "success"


def test_authenticated_qa_requires_structured_success_payload():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps(
        {
            "output": (
                '> app qa:auth\n{"ok":false,"baseUrl":"https://example.com",'
                '"paths":["/"],"routeCount":1,"routes":[{"path":"/",'
                '"finalPath":"/"}],"consoleErrorCount":1,"pageErrorCount":0}'
            ),
            "exit_code": 0,
            "error": None,
        }
    )

    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {"command": "pnpm qa:auth"},
        result,
        False,
    )

    latest = latest_evidence_by_surface(agent._turn_runtime_stats["verification_evidence"])
    assert latest["production_browser"]["status"] == "failure"


def test_closed_green_pr_without_merge_supersedes_earlier_pr_query_failure():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    failed = json.dumps(
        {
            "output": (
                "Closed pull request example/repo#1092\n"
                'Unknown JSON field: "baseRefOid"'
            ),
            "exit_code": 1,
            "error": None,
        }
    )
    repaired_output = """{"base_sha":"1aa02906ca5cd01c377e67c8e404c8add905c210","closed_at":"2026-07-28T12:03:28Z","head_sha":"37e518f6fd949fc794c5299bbd90c3578d4b5a60","html_url":"https://github.com/example/repo/pull/1092","merged":false,"merged_at":null,"number":1092,"state":"closed"}
Vercel\tpass\t0\thttps://vercel.example\tDeployment has completed
Vercel Preview Comments\tpass\t0\thttps://vercel.example
CURRENT_MAIN=1aa02906ca5cd01c377e67c8e404c8add905c210"""
    repaired = json.dumps(
        {"output": repaired_output, "exit_code": 0, "error": None}
    )

    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {
            "command": (
                "set -e\ngh pr close 1092\n"
                "gh pr view 1092 --json baseRefOid,statusCheckRollup"
            )
        },
        failed,
        True,
    )
    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {
            "command": (
                "gh api repos/example/repo/pulls/1092 --jq '{state,merged}' "
                "&& gh pr checks 1092 && printf CURRENT_MAIN="
            )
        },
        repaired,
        False,
    )
    tool_executor._record_turn_verification_evidence(
        agent,
        "verify_main_parent",
        {"pr_number": 1092, "workdir": "/tmp/example"},
        json.dumps(
            {
                "success": True,
                "exit_code": 0,
                "error": None,
                "repository": "example/repo",
                "repository_root": "/tmp/example",
                "pr_number": 1092,
                "head_sha": "37e518f6fd949fc794c5299bbd90c3578d4b5a60",
                "pr_evidence": {
                    "status": "success",
                    "state": "closed",
                    "merged": False,
                    "base_ref": "main",
                    "head_sha": "37e518f6fd949fc794c5299bbd90c3578d4b5a60",
                },
                "main_branch_evidence": {
                    "status": "success",
                    "remote_main": "1aa02906ca5cd01c377e67c8e404c8add905c210",
                    "commit_parent": "1aa02906ca5cd01c377e67c8e404c8add905c210",
                },
            }
        ),
        False,
    )

    latest = latest_evidence_by_surface(agent._turn_runtime_stats["verification_evidence"])
    assert latest["pr"]["status"] == "success"
    assert latest["ci"]["status"] == "success"
    assert "closed PR verification" in latest["pr"]["check_name"]
    assert claim_constraints_for_text(
        "PR checks passed, the PR was closed without merge, and main stayed unchanged.",
        agent._turn_runtime_stats["verification_evidence"],
    )["allowed"] is True


def test_docs_diff_check_does_not_claim_ci_or_production_surfaces():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps(
        {
            "output": (
                "diff --git a/docs/discord-dev-loop-smoke.md b/docs/discord-dev-loop-smoke.md\n"
                "origin\tgit@github.com:sligo-labs/PID.git (fetch)\n"
                "origin\tgit@github.com:sligo-labs/PID.git (push)"
            ),
            "exit_code": 0,
            "error": None,
        }
    )

    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {
            "command": (
                "git diff --check && git diff -- docs/discord-dev-loop-smoke.md "
                "&& git remote -v && git config user.name"
            )
        },
        result,
        False,
    )

    latest = latest_evidence_by_surface(agent._turn_runtime_stats["verification_evidence"])
    assert set(latest) == {"verification"}
    assert latest["verification"]["status"] == "success"


def test_git_lifecycle_with_smoke_filename_is_not_verification_evidence():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps(
        {
            "output": (
                "[branch ab9c14a] Update daily dev-loop smoke touch\n"
                "To github.com:sligo-labs/PID.git\n"
                " * [new branch] HEAD -> discord-action/pid"
            ),
            "exit_code": 0,
            "error": None,
        }
    )

    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {
            "command": (
                "git add -- docs/discord-dev-loop-smoke.md && "
                "git commit -m 'Update daily dev-loop smoke touch' && git push -u origin HEAD"
            )
        },
        result,
        False,
    )

    assert agent._turn_runtime_stats.get("verification_evidence", []) == []


def test_successful_checks_and_pr_close_survive_auxiliary_query_schema_error():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps(
        {
            "output": (
                "main_before=196820e67e9e2f2420033ef62aae56c2f5f3b589\n"
                "Vercel\tpass\t0\thttps://vercel.example\tDeployment has completed\n"
                "Vercel Preview Comments\tpass\t0\thttps://vercel.example\n"
                "✓ Closed pull request sligo-labs/PID#1093 (Daily smoke)\n"
                "main_after=196820e67e9e2f2420033ef62aae56c2f5f3b589\n"
                'Unknown JSON field: "baseRefOid"'
            ),
            "exit_code": 1,
            "error": None,
        }
    )
    command = (
        "set -e\n"
        "BASE_BEFORE=$(gh api repos/sligo-labs/PID/git/ref/heads/main --jq .object.sha)\n"
        "gh pr checks 1093 --repo sligo-labs/PID\n"
        "gh pr close 1093 --repo sligo-labs/PID\n"
        "BASE_AFTER=$(gh api repos/sligo-labs/PID/git/ref/heads/main --jq .object.sha)\n"
        "gh pr view 1093 --json baseRefOid,statusCheckRollup\n"
        'test "$BASE_BEFORE" = "$BASE_AFTER"'
    )

    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {"command": command},
        result,
        True,
    )

    latest = latest_evidence_by_surface(agent._turn_runtime_stats["verification_evidence"])
    assert latest["ci"]["status"] == "success"
    assert latest["pr"]["status"] == "success"
    assert "deployment" not in latest
    assert "production" not in latest


def test_closed_pr_json_and_named_check_runs_repair_closeout_evidence():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps(
        {
            "output": (
                '{"checks":[{"conclusion":null,"name":null,"status":null},'
                '{"conclusion":"SUCCESS","name":"Vercel Preview Comments",'
                '"status":"COMPLETED"}],"closed":true,'
                '"closedAt":"2026-07-29T12:03:51Z",'
                '"headRefOid":"ab9c14ac44cbfa49fdf598feea2694bf0d713a40",'
                '"mergeCommit":null,"mergedAt":null,"number":1093,'
                '"state":"CLOSED","url":"https://github.com/sligo-labs/PID/pull/1093"}'
            ),
            "exit_code": 0,
            "error": None,
        }
    )

    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {
            "command": (
                "MAIN_NOW=$(gh api repos/sligo-labs/PID/git/ref/heads/main --jq .object.sha); "
                "test \"$MAIN_NOW\" = \"196820e67e9e2f2420033ef62aae56c2f5f3b589\"; "
                "gh pr view 1093 --repo sligo-labs/PID --json state,closed,mergedAt,statusCheckRollup"
            )
        },
        result,
        False,
    )

    latest = latest_evidence_by_surface(agent._turn_runtime_stats["verification_evidence"])
    assert latest["ci"]["status"] == "success"
    assert latest["pr"]["status"] == "success"


def test_daily_smoke_closeout_response_is_not_falsely_downgraded():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    bad_query = json.dumps(
        {
            "output": (
                "MAIN_BEFORE=196820e67e9e2f2420033ef62aae56c2f5f3b589\n"
                "Vercel\tpass\t0\thttps://vercel.example\tDeployment has completed\n"
                "Vercel Preview Comments\tpass\t0\thttps://vercel.example\n"
                "✓ Closed pull request sligo-labs/PID#1093 (Daily smoke)\n"
                "MAIN_AFTER=196820e67e9e2f2420033ef62aae56c2f5f3b589\n"
                'Unknown JSON field: "baseRefOid"'
            ),
            "exit_code": 1,
            "error": None,
        }
    )
    repaired = json.dumps(
        {
            "output": (
                '{"checks":[{"completed_at":"2026-07-29T12:03:34Z",'
                '"conclusion":"success","name":"Vercel Preview Comments",'
                '"status":"completed"}],"total_count":1}\n'
                '{"base_sha":"196820e67e9e2f2420033ef62aae56c2f5f3b589",'
                '"closed_at":"2026-07-29T12:03:51Z",'
                '"head_sha":"ab9c14ac44cbfa49fdf598feea2694bf0d713a40",'
                '"html_url":"https://github.com/sligo-labs/PID/pull/1093",'
                '"merged":false,"merged_at":null,"state":"closed"}'
            ),
            "exit_code": 0,
            "error": None,
        }
    )
    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {"command": "gh pr checks 1093 && gh pr close 1093 && gh pr view 1093 --json baseRefOid"},
        bad_query,
        True,
    )
    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {
            "command": (
                "gh api repos/sligo-labs/PID/commits/ab9c14a/check-runs && "
                "gh api repos/sligo-labs/PID/pulls/1093"
            )
        },
        repaired,
        False,
    )
    sha = "196820e67e9e2f2420033ef62aae56c2f5f3b589"
    tool_executor._record_turn_verification_evidence(
        agent,
        "verify_main_parent",
        {"pr_number": 1093, "workdir": "/tmp/pid"},
        json.dumps(
            {
                "success": True,
                "exit_code": 0,
                "error": None,
                "repository": "sligo-labs/pid",
                "repository_root": "/tmp/pid",
                "pr_number": 1093,
                "head_sha": "ab9c14ac44cbfa49fdf598feea2694bf0d713a40",
                "pr_evidence": {
                    "status": "success",
                    "state": "closed",
                    "merged": False,
                    "base_ref": "main",
                    "head_sha": "ab9c14ac44cbfa49fdf598feea2694bf0d713a40",
                },
                "main_branch_evidence": {
                    "status": "success",
                    "remote_main": sha,
                    "commit_parent": sha,
                },
            }
        ),
        False,
    )
    final = (
        "Commit `ab9c14a` was pushed and PR #1093 checks passed. "
        "The PR was closed without merge, and main remained unchanged."
    )

    downgraded, constraints = downgrade_final_response_for_evidence(
        final,
        agent._turn_runtime_stats["verification_evidence"],
    )

    assert constraints["allowed"] is True
    assert downgraded == final


def test_typed_main_parent_receipt_proves_main_unchanged():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    output = json.dumps(
        {
            "success": True,
            "exit_code": 0,
            "error": None,
            "repository": "example/repo",
            "repository_root": "/tmp/example",
            "pr_number": 7,
            "head_sha": "7" * 40,
            "pr_evidence": {
                "status": "success",
                "state": "closed",
                "merged": False,
                "base_ref": "main",
                "head_sha": "7" * 40,
            },
            "main_branch_evidence": {
                "status": "success",
                "remote_main": "9954a1c87a4f280de22d3b3767f0b19185588062",
                "commit_parent": "9954a1c87a4f280de22d3b3767f0b19185588062",
            },
        }
    )

    tool_executor._record_turn_verification_evidence(
        agent,
        "verify_main_parent",
        {"pr_number": 7, "workdir": "/tmp/example"},
        output,
        False,
    )
    final = "Main remained unchanged."

    downgraded, constraints = downgrade_final_response_for_evidence(
        final,
        agent._turn_runtime_stats["verification_evidence"],
    )

    assert constraints["allowed"] is True
    assert constraints["latest_by_surface"]["main_branch"]["status"] == "success"
    assert downgraded == final


def test_main_parent_receipt_must_match_pr_repository():
    sha = "9" * 40
    pr_evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh api repos/owner/repo-a/pulls/832"},
        json.dumps(
            {
                "output": json.dumps(
                    {
                        "html_url": "https://github.com/owner/repo-a/pull/832",
                        "number": 832,
                        "state": "closed",
                        "merged": False,
                        "merged_at": None,
                    }
                ),
                "exit_code": 0,
                "error": None,
            }
        ),
        False,
        order=1,
    )
    branch_evidence = classify_tool_verification_evidence(
        "verify_main_parent",
        {"pr_number": 832, "workdir": "/tmp/repo-b"},
        json.dumps(
            {
                "success": True,
                "exit_code": 0,
                "error": None,
                "repository": "owner/repo-b",
                "repository_root": "/tmp/repo-b",
                "pr_number": 832,
                "head_sha": "7" * 40,
                "pr_evidence": {
                    "status": "success",
                    "state": "closed",
                    "merged": False,
                    "base_ref": "main",
                    "head_sha": "7" * 40,
                },
                "main_branch_evidence": {
                    "status": "success",
                    "remote_main": sha,
                    "commit_parent": sha,
                },
            }
        ),
        False,
        order=2,
    )

    constraints = claim_constraints_for_text(
        "The PR was closed without merge and main remained unchanged.",
        [*pr_evidence, *branch_evidence],
    )

    assert constraints["allowed"] is False
    assert any(
        item["surface"] == "main_branch"
        for item in constraints["blocked_surfaces"]
    )


def test_main_parent_receipt_must_match_pr_head_in_same_repository():
    main_sha = "9" * 40
    pr_head = "a" * 40
    decoy_head = "b" * 40
    pr_evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh api repos/owner/repo/pulls/832"},
        json.dumps(
            {
                "output": json.dumps(
                    {
                        "html_url": "https://github.com/owner/repo/pull/832",
                        "number": 832,
                        "state": "closed",
                        "merged": False,
                        "merged_at": None,
                        "head_sha": pr_head,
                    }
                ),
                "exit_code": 0,
                "error": None,
            }
        ),
        False,
        order=1,
    )
    branch_evidence = classify_tool_verification_evidence(
        "verify_main_parent",
        {"pr_number": 832, "workdir": "/tmp/decoy"},
        json.dumps(
            {
                "success": True,
                "exit_code": 0,
                "error": None,
                "repository": "owner/repo",
                "repository_root": "/tmp/decoy",
                "pr_number": 832,
                "head_sha": decoy_head,
                "pr_evidence": {
                    "status": "success",
                    "state": "closed",
                    "merged": False,
                    "base_ref": "main",
                    "head_sha": pr_head,
                },
                "main_branch_evidence": {
                    "status": "success",
                    "remote_main": main_sha,
                    "commit_parent": main_sha,
                },
            }
        ),
        False,
        order=2,
    )

    constraints = claim_constraints_for_text(
        "The PR was closed without merge and main remained unchanged.",
        [*pr_evidence, *branch_evidence],
    )

    assert constraints["allowed"] is False


def test_fabricated_terminal_pr_head_cannot_override_typed_pr_subject():
    typed_head = "a" * 40
    fabricated_head = "b" * 40
    main_sha = "9" * 40
    typed = classify_tool_verification_evidence(
        "verify_main_parent",
        {"pr_number": 832, "workdir": "/tmp/repo"},
        json.dumps(
            {
                "success": True,
                "exit_code": 0,
                "error": None,
                "repository": "owner/repo",
                "repository_root": "/tmp/repo",
                "pr_number": 832,
                "head_sha": typed_head,
                "pr_evidence": {
                    "status": "success",
                    "state": "closed",
                    "merged": False,
                    "base_ref": "main",
                    "head_sha": typed_head,
                },
                "main_branch_evidence": {
                    "status": "success",
                    "remote_main": main_sha,
                    "commit_parent": main_sha,
                },
            }
        ),
        False,
        order=1,
    )
    fabricated = classify_tool_verification_evidence(
        "terminal",
        {"command": "printf fabricated && gh pr view 833 --repo owner/repo"},
        json.dumps(
            {
                "output": json.dumps(
                    {
                        "url": "https://github.com/owner/repo/pull/833",
                        "state": "CLOSED",
                        "mergedAt": None,
                        "mergeCommit": None,
                        "headRefOid": fabricated_head,
                    }
                ),
                "exit_code": 0,
                "error": None,
            }
        ),
        False,
        order=2,
    )

    latest = latest_evidence_by_surface([*typed, *fabricated])
    assert latest["pr"]["subject"] == "github:owner/repo:pr:832"
    assert claim_constraints_for_text(
        "The PR was closed without merge and main remained unchanged.",
        [*typed, *fabricated],
    )["allowed"] is True


def test_fabricated_same_subject_terminal_success_cannot_override_typed_pr_failure():
    head_sha = "a" * 40
    main_sha = "b" * 40
    typed_failure = classify_tool_verification_evidence(
        "verify_main_parent",
        {"pr_number": 832, "workdir": "/tmp/repo"},
        json.dumps(
            {
                "success": False,
                "exit_code": 1,
                "error": None,
                "repository": "owner/repo",
                "repository_root": "/tmp/repo",
                "pr_number": 832,
                "head_sha": head_sha,
                "pr_evidence": {
                    "status": "failure",
                    "state": "open",
                    "merged": False,
                    "base_ref": "main",
                    "head_sha": head_sha,
                },
                "main_branch_evidence": {
                    "status": "success",
                    "remote_main": main_sha,
                    "commit_parent": main_sha,
                },
            }
        ),
        True,
        order=1,
    )
    fabricated = classify_tool_verification_evidence(
        "terminal",
        {"command": "printf fabricated && gh pr view 832 --repo owner/repo"},
        json.dumps(
            {
                "output": json.dumps(
                    {
                        "url": "https://github.com/owner/repo/pull/832",
                        "state": "CLOSED",
                        "mergedAt": None,
                        "mergeCommit": None,
                        "headRefOid": head_sha,
                    }
                ),
                "exit_code": 0,
                "error": None,
            }
        ),
        False,
        order=2,
    )

    evidence = [*typed_failure, *fabricated]
    latest = latest_evidence_by_surface(evidence)
    assert latest["pr"]["status"] == "failure"
    assert latest["pr"]["provenance"] == "typed_host"
    assert latest["main_branch"]["status"] == "success"
    assert claim_constraints_for_text(
        "The PR was closed without merge and main remained unchanged.", evidence
    )["allowed"] is False


@pytest.mark.parametrize(
    "claim",
    [
        "PR #832 was closed without merge and main remained unchanged.",
        "owner/repo PR #832 was closed without merge and main remained unchanged.",
        "https://github.com/owner/repo/pull/832 was closed without merge and main remained unchanged.",
    ],
)
def test_typed_closeout_evidence_must_match_explicit_pr_claim(claim):
    head_sha = "a" * 40
    main_sha = "b" * 40
    evidence = classify_tool_verification_evidence(
        "verify_main_parent",
        {"pr_number": 999, "workdir": "/tmp/other"},
        json.dumps(
            {
                "success": True,
                "exit_code": 0,
                "error": None,
                "repository": "other/project",
                "repository_root": "/tmp/other",
                "pr_number": 999,
                "head_sha": head_sha,
                "pr_evidence": {
                    "status": "success",
                    "state": "closed",
                    "merged": False,
                    "base_ref": "main",
                    "head_sha": head_sha,
                },
                "main_branch_evidence": {
                    "status": "success",
                    "remote_main": main_sha,
                    "commit_parent": main_sha,
                },
            }
        ),
        False,
        order=1,
    )

    constraints = claim_constraints_for_text(claim, evidence)

    assert constraints["allowed"] is False
    assert {item["surface"] for item in constraints["blocked_surfaces"]} == {
        "pr",
        "main_branch",
    }


@pytest.mark.parametrize(
    "claim",
    [
        "PR #832 and PR #999 were closed without merge and main remained unchanged.",
        (
            "owner/repo PR #832 was closed without merge and main remained unchanged; "
            "other/project PR #999 was also reviewed."
        ),
        (
            "https://github.com/owner/repo/pull/832 was closed without merge and main "
            "remained unchanged; see PR #999."
        ),
    ],
)
def test_mixed_explicit_pr_claims_cannot_use_one_matching_receipt(claim):
    head_sha = "a" * 40
    main_sha = "b" * 40
    evidence = classify_tool_verification_evidence(
        "verify_main_parent",
        {"pr_number": 999, "workdir": "/tmp/other"},
        json.dumps(
            {
                "success": True,
                "exit_code": 0,
                "error": None,
                "repository": "other/project",
                "repository_root": "/tmp/other",
                "pr_number": 999,
                "head_sha": head_sha,
                "pr_evidence": {
                    "status": "success",
                    "state": "closed",
                    "merged": False,
                    "base_ref": "main",
                    "head_sha": head_sha,
                },
                "main_branch_evidence": {
                    "status": "success",
                    "remote_main": main_sha,
                    "commit_parent": main_sha,
                },
            }
        ),
        False,
        order=1,
    )

    assert claim_constraints_for_text(claim, evidence)["allowed"] is False


def test_unrelated_pr_reference_outside_closeout_sentence_is_ignored():
    head_sha = "a" * 40
    main_sha = "b" * 40
    evidence = classify_tool_verification_evidence(
        "verify_main_parent",
        {"pr_number": 999, "workdir": "/tmp/other"},
        json.dumps(
            {
                "success": True,
                "exit_code": 0,
                "error": None,
                "repository": "other/project",
                "repository_root": "/tmp/other",
                "pr_number": 999,
                "head_sha": head_sha,
                "pr_evidence": {
                    "status": "success",
                    "state": "closed",
                    "merged": False,
                    "base_ref": "main",
                    "head_sha": head_sha,
                },
                "main_branch_evidence": {
                    "status": "success",
                    "remote_main": main_sha,
                    "commit_parent": main_sha,
                },
            }
        ),
        False,
        order=1,
    )
    claim = (
        "other/project PR #999 was closed without merge and main remained unchanged. "
        "PR #832 still needs review."
    )

    assert claim_constraints_for_text(claim, evidence)["allowed"] is True


def test_typed_main_parent_mismatch_supersedes_earlier_success_when_tool_reports_failure():
    from agent.display import _detect_tool_failure

    head_sha = "a" * 40
    main_sha = "b" * 40
    success_payload = json.dumps(
        {
            "success": True,
            "exit_code": 0,
            "error": None,
            "repository": "owner/repo",
            "repository_root": "/tmp/repo",
            "pr_number": 832,
            "head_sha": head_sha,
            "pr_evidence": {
                "status": "success",
                "state": "closed",
                "merged": False,
                "base_ref": "main",
                "head_sha": head_sha,
            },
            "main_branch_evidence": {
                "status": "success",
                "remote_main": main_sha,
                "commit_parent": main_sha,
            },
        }
    )
    mismatch_payload = json.dumps(
        {
            "success": False,
            "exit_code": 1,
            "error": None,
            "repository": "owner/repo",
            "repository_root": "/tmp/repo",
            "pr_number": 832,
            "head_sha": head_sha,
            "pr_evidence": {
                "status": "success",
                "state": "closed",
                "merged": False,
                "base_ref": "main",
                "head_sha": head_sha,
            },
            "main_branch_evidence": {
                "status": "failure",
                "remote_main": "c" * 40,
                "commit_parent": main_sha,
            },
        }
    )
    earlier = classify_tool_verification_evidence(
        "verify_main_parent",
        {"pr_number": 832, "workdir": "/tmp/repo"},
        success_payload,
        _detect_tool_failure("verify_main_parent", success_payload)[0],
        order=1,
    )
    later = classify_tool_verification_evidence(
        "verify_main_parent",
        {"pr_number": 832, "workdir": "/tmp/repo"},
        mismatch_payload,
        _detect_tool_failure("verify_main_parent", mismatch_payload)[0],
        order=2,
    )

    evidence = [*earlier, *later]
    assert latest_evidence_by_surface(evidence)["main_branch"]["status"] == "failure"
    assert claim_constraints_for_text("Main remained unchanged.", evidence)["allowed"] is False


@pytest.mark.parametrize(
    "pr_evidence",
    [
        {
            "status": "failure",
            "state": "open",
            "merged": False,
            "base_ref": "main",
        },
        {
            "status": "failure",
            "state": "closed",
            "merged": True,
            "base_ref": "main",
        },
        {
            "status": "failure",
            "state": "closed",
            "merged": False,
            "base_ref": "release",
        },
    ],
)
def test_typed_pr_state_failure_supersedes_earlier_closeout_success(pr_evidence):
    from agent.display import _detect_tool_failure

    head_sha = "a" * 40
    main_sha = "b" * 40
    base = {
        "repository": "owner/repo",
        "repository_root": "/tmp/repo",
        "pr_number": 832,
        "head_sha": head_sha,
        "main_branch_evidence": {
            "status": "success",
            "remote_main": main_sha,
            "commit_parent": main_sha,
        },
    }
    success_payload = json.dumps(
        {
            **base,
            "success": True,
            "exit_code": 0,
            "error": None,
            "pr_evidence": {
                "status": "success",
                "state": "closed",
                "merged": False,
                "base_ref": "main",
                "head_sha": head_sha,
            },
        }
    )
    failure_payload = json.dumps(
        {
            **base,
            "success": False,
            "exit_code": 1,
            "error": None,
            "pr_evidence": {**pr_evidence, "head_sha": head_sha},
        }
    )
    earlier = classify_tool_verification_evidence(
        "verify_main_parent",
        {"pr_number": 832, "workdir": "/tmp/repo"},
        success_payload,
        _detect_tool_failure("verify_main_parent", success_payload)[0],
        order=1,
    )
    later = classify_tool_verification_evidence(
        "verify_main_parent",
        {"pr_number": 832, "workdir": "/tmp/repo"},
        failure_payload,
        _detect_tool_failure("verify_main_parent", failure_payload)[0],
        order=2,
    )

    evidence = [*earlier, *later]
    latest = latest_evidence_by_surface(evidence)
    assert latest["pr"]["status"] == "failure"
    assert latest["main_branch"]["status"] == "success"
    assert claim_constraints_for_text(
        "The PR was closed without merge and main remained unchanged.", evidence
    )["allowed"] is False


@pytest.mark.parametrize(
    ("is_error", "success", "exit_code", "error"),
    [
        (True, True, 0, None),
        (False, False, 1, "probe failed"),
        (False, False, 1, None),
    ],
)
def test_main_parent_receipt_requires_consistent_tool_outcome(
    is_error, success, exit_code, error
):
    sha = "8" * 40
    evidence = classify_tool_verification_evidence(
        "verify_main_parent",
        {"pr_number": 7, "workdir": "/tmp/example"},
        json.dumps(
            {
                "success": success,
                "exit_code": exit_code,
                "error": error,
                "repository": "example/repo",
                "repository_root": "/tmp/example",
                "pr_number": 7,
                "head_sha": "7" * 40,
                "pr_evidence": {
                    "status": "success",
                    "state": "closed",
                    "merged": False,
                    "base_ref": "main",
                    "head_sha": "7" * 40,
                },
                "main_branch_evidence": {
                    "status": "success",
                    "remote_main": sha,
                    "commit_parent": sha,
                },
            }
        ),
        is_error,
        order=1,
    )

    assert evidence == []


@pytest.mark.parametrize(
    ("command", "blocks"),
    [
        (
            "gh pr view 1100",
            "REMOTE_MAIN\n"
            "9954a1c87a4f280de22d3b3767f0b19185588062\trefs/heads/main\n"
            "COMMIT_PARENT\n"
            "9954a1c87a4f280de22d3b3767f0b19185588062\n",
        ),
        (
            "git ls-remote origin refs/heads/main && git rev-parse HEAD^",
            "REMOTE_MAIN\n"
            "9954a1c87a4f280de22d3b3767f0b19185588062\n"
            "COMMIT_PARENT\n"
            "9954a1c87a4f280de22d3b3767f0b19185588062\trefs/heads/main\n",
        ),
        (
            "git ls-remote origin refs/heads/main && git rev-parse HEAD^",
            "REMOTE_MAIN\n"
            "9954a1c87a4f280de22d3b3767f0b19185588062\trefs/heads/main\n"
            "REMOTE_MAIN\n"
            "9954a1c87a4f280de22d3b3767f0b19185588062\trefs/heads/main\n"
            "COMMIT_PARENT\n"
            "9954a1c87a4f280de22d3b3767f0b19185588062\n",
        ),
    ],
)
def test_labeled_main_sha_proof_rejects_ambiguous_shapes(command, blocks):
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": command},
        json.dumps({"output": blocks, "exit_code": 0, "error": None}),
        False,
        order=1,
    )

    assert not any(item.get("surface") == "main_branch" for item in evidence)


def test_typed_main_parent_failure_cannot_be_overridden_by_payload_base_sha():
    remote_main = "a" * 40
    commit_parent = "b" * 40
    evidence = classify_tool_verification_evidence(
        "verify_main_parent",
        {"pr_number": 7, "workdir": "/tmp/example"},
        json.dumps(
            {
                "success": False,
                "exit_code": 1,
                "error": None,
                "repository": "example/repo",
                "repository_root": "/tmp/example",
                "pr_number": 7,
                "head_sha": "7" * 40,
                "pr_evidence": {
                    "status": "success",
                    "state": "closed",
                    "merged": False,
                    "base_ref": "main",
                    "head_sha": "7" * 40,
                },
                "base_sha": remote_main,
                "main_branch_evidence": {
                    "status": "failure",
                    "remote_main": remote_main,
                    "commit_parent": commit_parent,
                },
            }
        ),
        False,
        order=1,
    )

    main = next(item for item in evidence if item.get("surface") == "main_branch")
    assert main["status"] == "failure"


def test_labeled_main_sha_proof_rejects_fabricated_probe_output():
    sha = "a" * 40
    command = (
        "gh pr view 1100 && "
        f"printf 'REMOTE_MAIN\\n{sha}\\trefs/heads/main\\nCOMMIT_PARENT\\n{sha}\\n' && "
        "git ls-remote origin refs/heads/main >/dev/null && "
        "git rev-parse HEAD^ >/dev/null"
    )
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": command},
        json.dumps(
            {
                "output": (
                    f"REMOTE_MAIN\n{sha}\trefs/heads/main\n"
                    f"COMMIT_PARENT\n{sha}\n"
                ),
                "exit_code": 0,
                "error": None,
            }
        ),
        False,
        order=1,
    )

    assert not any(item.get("surface") == "main_branch" for item in evidence)


def test_pending_named_github_check_blocks_passed_ci_claim():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr checks 1093 --repo sligo-labs/PID"},
        json.dumps(
            {
                "output": "Vercel\tpending\t0\thttps://vercel.example\tDeploying",
                "exit_code": 0,
                "error": None,
            }
        ),
        False,
        order=3,
    )

    constraints = claim_constraints_for_text("PR checks passed.", evidence)

    assert latest_evidence_by_surface(evidence)["ci"]["status"] == "pending"
    assert constraints["allowed"] is False


def test_pending_github_check_blocks_generic_multiline_checks_claim():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr checks 1093 --repo sligo-labs/PID"},
        json.dumps(
            {
                "output": "Vercel\tpending\t0\thttps://vercel.example\tDeploying",
                "exit_code": 0,
                "error": None,
            }
        ),
        False,
        order=3,
    )

    constraints = claim_constraints_for_text(
        "- **PR:** #1093 opened\n- **Checks:** Vercel passed.", evidence
    )

    assert constraints["allowed"] is False


def test_github_pr_link_is_not_a_production_claim():
    evidence = [
        {
            "surface": "production",
            "check_name": "production smoke",
            "status": "failure",
            "order": 1,
            "detail": "production unavailable",
        }
    ]
    final = "PR opened: https://github.com/sligo-labs/PID/pull/1093. Checks passed."

    constraints = claim_constraints_for_text(final, evidence)

    assert constraints["allowed"] is True


def test_successful_pr_create_body_does_not_manufacture_other_surfaces():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {
            "command": (
                "gh pr create --title 'Docs' --body 'Ran git diff --check; "
                "no production deployment was requested.'"
            )
        },
        json.dumps(
            {
                "output": "https://github.com/example/repo/pull/123",
                "exit_code": 0,
                "error": None,
            }
        ),
        False,
        order=4,
    )

    latest = latest_evidence_by_surface(evidence)
    assert set(latest) == {"pr"}
    assert latest["pr"]["status"] == "success"


def test_successful_checks_do_not_hide_failed_pr_close():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr checks 123 && gh pr close 123"},
        json.dumps(
            {
                "output": (
                    "Unit tests\tpass\t0\thttps://github.example/check\n"
                    "GraphQL: Pull request is already merged"
                ),
                "exit_code": 1,
                "error": None,
            }
        ),
        True,
        order=5,
    )

    latest = latest_evidence_by_surface(evidence)
    assert latest["ci"]["status"] == "success"
    assert latest["pr"]["status"] == "failure"
    assert claim_constraints_for_text("Checks passed and the PR was closed.", evidence)[
        "allowed"
    ] is False


def test_pr_create_url_survives_unrelated_trailing_command_error():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr create --title Docs --body Body; gh pr view --json unsupported"},
        json.dumps(
            {
                "output": (
                    "https://github.com/example/repo/pull/123\n"
                    'Unknown JSON field: "unsupported"'
                ),
                "exit_code": 1,
                "error": None,
            }
        ),
        True,
        order=6,
    )

    latest = latest_evidence_by_surface(evidence)
    assert latest["pr"]["status"] == "success"


def test_merged_pr_payload_is_successful_merge_evidence():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr merge 123 --merge && gh pr view 123 --json state,mergedAt"},
        json.dumps(
            {
                "output": (
                    '{"mergeCommit":{"oid":"abc123"},'
                    '"mergedAt":"2026-07-29T12:00:00Z","state":"MERGED"}'
                ),
                "exit_code": 0,
                "error": None,
            }
        ),
        False,
        order=7,
    )

    assert latest_evidence_by_surface(evidence)["pr"]["status"] == "success"


def test_close_command_rejects_payload_showing_pr_was_merged():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr close 123; gh pr view 123 --json state,mergedAt"},
        json.dumps(
            {
                "output": (
                    '{"mergeCommit":{"oid":"abc123"},'
                    '"mergedAt":"2026-07-29T12:00:00Z","state":"MERGED"}'
                ),
                "exit_code": 0,
                "error": None,
            }
        ),
        False,
        order=8,
    )

    assert latest_evidence_by_surface(evidence)["pr"]["status"] == "failure"


def test_closed_state_alone_cannot_authorize_strong_closeout_claims():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr view 123 --json state"},
        json.dumps({"output": '{"state":"CLOSED"}', "exit_code": 0, "error": None}),
        False,
        order=9,
    )

    constraints = claim_constraints_for_text(
        "The PR was closed without merge and main remained unchanged.", evidence
    )

    assert constraints["allowed"] is False
    assert {item["surface"] for item in constraints["blocked_surfaces"]} == {
        "pr",
        "main_branch",
    }


def test_close_success_does_not_hide_later_checks_failure():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr close 123 && gh pr checks 123"},
        json.dumps(
            {
                "output": (
                    "✓ Closed pull request example/repo#123\n"
                    "GraphQL: no checks reported on the branch"
                ),
                "exit_code": 1,
                "error": None,
            }
        ),
        True,
        order=10,
    )

    latest = latest_evidence_by_surface(evidence)
    assert latest["pr"]["status"] == "success"
    assert latest["ci"]["status"] == "failure"


def test_checks_success_does_not_turn_failed_pull_query_into_pr_success():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr checks 123 && gh api repos/example/repo/pulls/123"},
        json.dumps(
            {
                "output": (
                    "Unit tests\tpass\t0\thttps://github.example/check\n"
                    "HTTP 500: Internal Server Error"
                ),
                "exit_code": 1,
                "error": None,
            }
        ),
        True,
        order=11,
    )

    latest = latest_evidence_by_surface(evidence)
    assert latest["ci"]["status"] == "success"
    assert "pr" not in latest
    assert claim_constraints_for_text("The PR was closed without merge.", evidence)[
        "allowed"
    ] is False


def test_terminal_main_markers_do_not_authorize_unchanged_claim():
    sha = "196820e67e9e2f2420033ef62aae56c2f5f3b589"
    evidence = classify_tool_verification_evidence(
        "terminal",
        {
            "command": (
                "MAIN_BEFORE=$(gh api repos/example/repo/git/ref/heads/main); "
                "gh pr close 123; "
                "MAIN_AFTER=$(gh api repos/example/repo/git/ref/heads/main); "
                "gh pr view 123 --json unsupported"
            )
        },
        json.dumps(
            {
                "output": (
                    f"MAIN_BEFORE={sha}\n"
                    "✓ Closed pull request example/repo#123\n"
                    f"MAIN_AFTER={sha}\n"
                    'Unknown JSON field: "unsupported"'
                ),
                "exit_code": 1,
                "error": None,
            }
        ),
        True,
        order=12,
    )

    constraints = claim_constraints_for_text(
        "The PR was closed without merge and main remained unchanged.", evidence
    )

    assert "main_branch" not in latest_evidence_by_surface(evidence)
    assert constraints["allowed"] is False


def test_terminal_main_marker_mismatch_is_not_typed_branch_evidence():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "MAIN_BEFORE=x; gh pr close 123; MAIN_AFTER=y"},
        json.dumps(
            {
                "output": (
                    "MAIN_BEFORE=196820e67e9e2f2420033ef62aae56c2f5f3b589\n"
                    "✓ Closed pull request example/repo#123\n"
                    "MAIN_AFTER=296820e67e9e2f2420033ef62aae56c2f5f3b589"
                ),
                "exit_code": 1,
                "error": None,
            }
        ),
        True,
        order=13,
    )

    constraints = claim_constraints_for_text("Main remained unchanged.", evidence)

    assert "main_branch" not in latest_evidence_by_surface(evidence)
    assert constraints["allowed"] is False


def test_unrelated_commit_checks_cannot_clear_pr_check_failure():
    failed = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr checks 123 --repo example/repo"},
        json.dumps({"output": "Unit tests\tfail\t0\thttps://example/check", "exit_code": 1}),
        True,
        order=14,
    )
    repaired_other_commit = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh api repos/example/repo/commits/deadbeef/check-runs"},
        json.dumps(
            {
                "output": (
                    '{"check_runs":[{"conclusion":"success","name":"Unit tests",'
                    '"status":"completed"}]}'
                ),
                "exit_code": 0,
            }
        ),
        False,
        order=15,
    )

    latest = latest_evidence_by_surface(failed + repaired_other_commit)

    assert latest["ci"]["status"] == "failure"


def test_unrelated_commit_failure_cannot_replace_pr_check_success():
    passed = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr checks 123 --repo example/repo"},
        json.dumps({"output": "Unit tests\tpass\t0\thttps://example/check", "exit_code": 0}),
        False,
        order=15,
    )
    failed_other_commit = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh api repos/example/repo/commits/deadbeef/check-runs"},
        json.dumps({"output": "HTTP 500: Internal Server Error", "exit_code": 1}),
        True,
        order=16,
    )

    evidence = passed + failed_other_commit
    latest = latest_evidence_by_surface(evidence)

    assert latest["ci"]["status"] == "success"
    assert claim_constraints_for_text(
        "- **Checks:** PR #123 checks passed.", evidence
    )["allowed"] is True


def test_failed_pr_view_does_not_overwrite_successful_close_receipt():
    closed = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr close 123"},
        json.dumps(
            {"output": "✓ Closed pull request example/repo#123", "exit_code": 0}
        ),
        False,
        order=16,
    )
    bad_view = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr view 123 --json unsupported"},
        json.dumps({"output": 'Unknown JSON field: "unsupported"', "exit_code": 1}),
        True,
        order=17,
    )

    latest = latest_evidence_by_surface(closed + bad_view)

    assert latest["pr"]["status"] == "success"


def test_failed_pull_api_query_does_not_trigger_legacy_pr_failure():
    closed = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr close 123 --repo example/repo"},
        json.dumps(
            {"output": "✓ Closed pull request example/repo#123", "exit_code": 0}
        ),
        False,
        order=17,
    )
    bad_query = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh api repos/example/repo/pulls/123"},
        json.dumps({"output": "HTTP 500: Internal Server Error", "exit_code": 1}),
        True,
        order=18,
    )

    latest = latest_evidence_by_surface(closed + bad_query)

    assert bad_query == []
    assert latest["pr"]["status"] == "success"


def test_bad_auxiliary_pr_view_does_not_downgrade_multiline_closeout():
    evidence = classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr close 123 && gh pr checks 123"},
        json.dumps(
            {
                "output": (
                    "Unit tests\tpass\t0\thttps://example/check\n"
                    "✓ Closed pull request example/repo#123"
                ),
                "exit_code": 0,
            }
        ),
        False,
        order=18,
    )
    evidence += classify_tool_verification_evidence(
        "terminal",
        {"command": "gh pr view 123 --json unsupported"},
        json.dumps({"output": 'Unknown JSON field: "unsupported"', "exit_code": 1}),
        True,
        order=19,
    )
    final = "- PR #123 opened\n- Checks passed\n- Closed without merge"

    downgraded, constraints = downgrade_final_response_for_evidence(final, evidence)

    assert constraints["allowed"] is True
    assert downgraded == final


def test_verify_path_name_alone_is_not_verification_evidence():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps(
        {
            "output": "Preparing worktree (detached HEAD 1234567)\nHEAD is now at 1234567 main",
            "exit_code": 124,
            "error": "Command timed out after 30 seconds",
        }
    )
    command = (
        "rm -rf /home/droid/workspaces/examine-main-verify\n"
        "git -C /home/droid/.hermes/workspace/examine worktree add "
        "/home/droid/workspaces/examine-main-verify origin/main"
    )

    tool_executor._record_turn_tool_runtime(agent, "terminal", 30.0, result, True)
    tool_executor._record_turn_verification_evidence(agent, "terminal", {"command": command}, result, True)

    assert agent._turn_runtime_stats.get("verification_evidence", []) == []


def test_read_only_source_inspection_of_smoke_script_is_not_verification_evidence():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps(
        {
            "output": "async function loginIfNeeded(page) {",
            "exit_code": 124,
            "error": "Command timed out after 30 seconds",
        }
    )
    command = (
        "sed -n '370,400p' dashboard/scripts/authenticated-qa-smoke.mjs; "
        "echo '=== signIn ==='; "
        "grep -n 'async function signIn' -A 30 dashboard/scripts/authenticated-qa-smoke.mjs"
    )

    tool_executor._record_turn_tool_runtime(agent, "terminal", 30.0, result, True)
    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {"command": command},
        result,
        True,
    )

    assert agent._turn_runtime_stats.get("verification_evidence", []) == []


def test_git_add_smoke_pathspec_failure_is_not_verification_evidence():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps(
        {
            "output": "",
            "exit_code": 128,
            "error": "fatal: pathspec 'dashboard/sr' did not match any files",
        }
    )
    command = (
        "git add dashboard/static/CHANGELOG.md docs/project-state.md "
        "dashboard/scripts/authenticated-qa-smoke.mjs dashboard/src/routes/calendar/+page.svelte dashboard/sr"
    )

    tool_executor._record_turn_tool_runtime(agent, "terminal", 0.2, result, True)
    tool_executor._record_turn_verification_evidence(agent, "terminal", {"command": command}, result, True)

    assert agent._turn_runtime_stats.get("verification_evidence", []) == []


def test_git_add_smoke_pathspec_failure_does_not_downgrade_verified_claim():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps(
        {
            "output": "",
            "exit_code": 128,
            "error": "fatal: pathspec 'dashboard/sr' did not match any files",
        }
    )
    command = (
        "git -C /repo add dashboard/static/CHANGELOG.md docs/project-state.md "
        "dashboard/scripts/authenticated-qa-smoke.mjs dashboard/src/routes/calendar/+page.svelte dashboard/sr"
    )

    tool_executor._record_turn_tool_runtime(agent, "terminal", 0.2, result, True)
    tool_executor._record_turn_verification_evidence(agent, "terminal", {"command": command}, result, True)

    downgraded, constraints = downgrade_final_response_for_evidence(
        "Verified with focused checks; ready for PR review.",
        agent._turn_runtime_stats.get("verification_evidence", []),
    )

    assert constraints["allowed"] is True
    assert downgraded == "Verified with focused checks; ready for PR review."


def test_real_verification_command_still_records_blocking_evidence():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps({"output": "1 failed", "exit_code": 1, "error": None})

    tool_executor._record_turn_tool_runtime(agent, "terminal", 1.0, result, True)
    tool_executor._record_turn_verification_evidence(agent, "terminal", {"command": "pytest -q"}, result, True)

    evidence = agent._turn_runtime_stats["verification_evidence"]
    constraints = claim_constraints_for_text("CI passed via pytest -q.", evidence)

    assert evidence
    assert latest_evidence_by_surface(evidence)["ci"]["status"] == "failure"
    assert constraints["allowed"] is False


def test_common_test_command_still_records_verification_evidence():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps({"output": "test failed", "exit_code": 1, "error": None})

    tool_executor._record_turn_tool_runtime(agent, "terminal", 1.0, result, True)
    tool_executor._record_turn_verification_evidence(agent, "terminal", {"command": "npm test"}, result, True)

    evidence = agent._turn_runtime_stats["verification_evidence"]

    assert evidence
    assert latest_evidence_by_surface(evidence)["ci"]["status"] == "failure"


def test_real_smoke_command_still_records_verification_evidence():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps({"output": "smoke failed", "exit_code": 1, "error": None})

    tool_executor._record_turn_tool_runtime(agent, "terminal", 1.0, result, True)
    tool_executor._record_turn_verification_evidence(
        agent,
        "terminal",
        {"command": "node dashboard/scripts/authenticated-qa-smoke.mjs --preview"},
        result,
        True,
    )

    evidence = agent._turn_runtime_stats["verification_evidence"]

    assert evidence
    assert latest_evidence_by_surface(evidence)["verification"]["status"] == "failure"


def test_real_status_command_still_records_verification_evidence():
    agent = SimpleNamespace(_turn_runtime_stats=conversation_loop._new_turn_runtime_stats(0.0))
    result = json.dumps({"output": "checks failing", "exit_code": 1, "error": None})

    tool_executor._record_turn_tool_runtime(agent, "terminal", 1.0, result, True)
    tool_executor._record_turn_verification_evidence(agent, "terminal", {"command": "gh pr status"}, result, True)

    evidence = agent._turn_runtime_stats["verification_evidence"]

    assert evidence
    assert latest_evidence_by_surface(evidence)["ci"]["status"] == "failure"


def test_windows_backslash_ad_hoc_script_path_is_matched(tmp_path, monkeypatch):
    """Ad-hoc verification scripts with Windows backslash paths must be
    matched by ``_find_ad_hoc_match`` trying ``posix=False`` in addition to
    the default ``posix=True``. (#53553 / #65919)

    On Linux, ``Path`` doesn't parse Windows backslash paths, so we mock
    ``_is_temp_script_path`` to simulate the Windows environment where the
    path resolves correctly. The test verifies the posix=False splitting
    fallback — the actual fix from #53553.
    """
    from agent.verification_evidence import _find_ad_hoc_match

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    # On Windows, shlex.split(posix=True) eats backslashes as escape chars;
    # posix=False preserves them. Mock _is_temp_script_path so the test
    # focuses on the splitting fallback without needing a real Windows FS.
    def mock_is_temp_script(token, root):
        return "hermes-ad-hoc" in token and ".py" in token

    monkeypatch.setattr(
        "agent.verification_evidence._is_temp_script_path",
        mock_is_temp_script,
    )

    win_script = r"C:\Users\test\AppData\Local\Temp\hermes-ad-hoc-check.py"
    result = _find_ad_hoc_match(f"python {win_script}", tmp_path)
    assert result is not None, (
        "Windows backslash path should be matched via posix=False fallback"
    )
