from __future__ import annotations

import json
from types import SimpleNamespace

from agent import conversation_loop, tool_executor
from agent.verification_evidence import (
    claim_constraints_for_text,
    downgrade_final_response_for_evidence,
    latest_evidence_by_surface,
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
