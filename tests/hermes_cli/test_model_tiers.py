"""Focused contract tests for reusable model-tier configuration."""

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.model_tiers import classify_task_complexity, resolve_model_tier


def test_default_routes_reference_resolvable_tiers():
    tiers = DEFAULT_CONFIG["model_tiers"]
    route_names = {
        "gateway": DEFAULT_CONFIG["gateway"]["model_tier"],
        "cron": DEFAULT_CONFIG["cron"]["model_tier"],
        "discord_action_request": DEFAULT_CONFIG["discord"]["action_request_model_tier"],
        "coding_worker_simple_build": DEFAULT_CONFIG["coding_worker"]["simple_build_model_tier"],
        "coding_worker_complex_plan": DEFAULT_CONFIG["coding_worker"]["complex_plan_model_tier"],
        "coding_worker_complex_build": DEFAULT_CONFIG["coding_worker"]["complex_build_model_tier"],
        "planner": DEFAULT_CONFIG["kanban"]["discord_worker"]["roles"]["planner"]["model_tier"],
        "dev": DEFAULT_CONFIG["kanban"]["discord_worker"]["roles"]["dev"]["model_tier"],
        "foreman": DEFAULT_CONFIG["kanban"]["discord_worker"]["roles"]["foreman"]["model_tier"],
        "reviewer": DEFAULT_CONFIG["kanban"]["discord_worker"]["roles"]["reviewer"]["model_tier"],
    }

    assert route_names == {
        "gateway": "intermediate",
        "cron": "trivial",
        "discord_action_request": "intermediate",
        "coding_worker_simple_build": "intermediate",
        "coding_worker_complex_plan": "advanced",
        "coding_worker_complex_build": "intermediate",
        "planner": "advanced",
        "dev": "intermediate",
        "foreman": "advanced",
        "reviewer": "advanced",
    }
    for name in route_names.values():
        resolved = resolve_model_tier({"model_tiers": tiers}, name)
        assert resolved is not None
        assert resolved.model
        assert resolved.opencode_model
        assert resolved.reasoning_config() == {
            "enabled": True,
            "effort": resolved.reasoning_effort,
        }

    assert {
        name: resolve_model_tier({"model_tiers": tiers}, name).reasoning_effort
        for name in ("trivial", "basic", "intermediate", "advanced")
    } == {
        "trivial": "low",
        "basic": "medium",
        "intermediate": "high",
        "advanced": "xhigh",
    }
    for role in ("planner", "dev", "foreman", "reviewer"):
        assert "reasoning" not in DEFAULT_CONFIG["kanban"]["discord_worker"]["roles"][role]


def test_partial_custom_tier_override_keeps_required_default_fields():
    tier = resolve_model_tier(
        {
            "model_tiers": {
                "basic": {
                    "model": "custom/luna",
                    "reasoning_effort": "high",
                }
            }
        },
        "basic",
    )

    assert tier is not None
    assert tier.model == "custom/luna"
    assert tier.opencode_model == "custom/luna"
    assert tier.reasoning_effort == "high"


def test_invalid_tier_is_rejected_without_leaking_into_runtime():
    assert resolve_model_tier(
        {"model_tiers": {"broken": {"model": "x", "reasoning_effort": "ultra"}}},
        "broken",
    ) is None


def test_delegation_classifier_is_deterministic_and_risk_wins_over_simple_text():
    assert classify_task_complexity("Fix a typo in README") == "simple"
    assert classify_task_complexity("Summarize the release behavior") == "ordinary"
    assert classify_task_complexity("Fix a typo in the auth migration") == "complex"


def test_standalone_coding_worker_uses_its_named_tier():
    from agent.opencode_worker import load_opencode_config

    config = {
        "model_tiers": {
            "feature": {
                "model": "custom/feature-model",
                "opencode_model": "custom/feature-worker",
                "reasoning_effort": "max",
            }
        },
        "coding_worker": {
            "model_tier": "feature",
            "simple_build_reasoning_level": "low",
            "complex_plan_reasoning_level": "high",
            "complex_build_reasoning_level": "medium",
            "opencode": {"binary": "opencode", "model": "legacy/worker"},
        },
    }

    resolved = load_opencode_config(config)

    assert resolved["model"] == "custom/feature-worker"
    assert resolved["simple_build_reasoning_level"] == "max"
    assert resolved["complex_plan_reasoning_level"] == "max"
    assert resolved["complex_build_reasoning_level"] == "max"
