from __future__ import annotations

import json
from types import SimpleNamespace

from agent import conversation_loop, tool_executor
from agent.verification_evidence import claim_constraints_for_text, latest_evidence_by_surface


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
