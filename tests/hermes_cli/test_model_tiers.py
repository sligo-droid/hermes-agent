"""Focused contract tests for reusable model-tier configuration."""

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.model_tiers import (
    MODEL_TIER_LADDER,
    classify_task_complexity,
    classify_task_purpose,
    resolve_adjacent_model_tier,
    resolve_model_tier,
    resolve_model_tier_offset,
    restrict_model_tier_for_task,
    restrict_reasoning_effort_for_task,
)


def test_default_routes_reference_resolvable_tiers():
    tiers = DEFAULT_CONFIG["model_tiers"]
    assert tiers == {}
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
        "gateway": "basic",
        "cron": "trivial",
        "discord_action_request": "discord_action",
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
        for name in ("trivial", "basic", "intermediate", "advanced", "discord_action")
    } == {
        "trivial": "medium",
        "basic": "max",
        "intermediate": "low",
        "advanced": "medium",
        "discord_action": "low",
    }
    assert {
        name: resolve_model_tier({"model_tiers": tiers}, name).model
        for name in ("trivial", "basic", "intermediate", "advanced", "discord_action")
    } == {
        "trivial": "gpt-5.6-luna",
        "basic": "gpt-5.6-luna",
        "intermediate": "gpt-5.6-sol",
        "advanced": "gpt-5.6-sol",
        "discord_action": "gpt-5.6-sol",
    }
    assert {
        name: resolve_model_tier({"model_tiers": tiers}, name).fast_mode
        for name in ("trivial", "basic", "intermediate", "advanced")
    } == {
        "trivial": True,
        "basic": True,
        "intermediate": False,
        "advanced": False,
    }
    for role in ("planner", "dev", "foreman", "reviewer"):
        assert "reasoning" not in DEFAULT_CONFIG["kanban"]["discord_worker"]["roles"][role]


def test_visual_tiers_resolve_with_intended_models_and_efforts():
    tiers = DEFAULT_CONFIG["model_tiers"]
    resolved = {
        name: resolve_model_tier({"model_tiers": tiers}, name)
        for name in (
            "visual_sweep",
            "visual_inspector",
            "visual_critique",
        )
    }
    assert all(tier is not None for tier in resolved.values())

    assert {name: tier.model for name, tier in resolved.items()} == {
        "visual_sweep": "gpt-5.6-luna",
        "visual_inspector": "claude-sonnet-5",
        "visual_critique": "claude-opus-5",
    }
    assert {name: tier.reasoning_effort for name, tier in resolved.items()} == {
        "visual_sweep": "medium",
        "visual_inspector": "medium",
        "visual_critique": "medium",
    }
    assert {name: tier.provider for name, tier in resolved.items()} == {
        "visual_sweep": "openai-codex",
        "visual_inspector": "anthropic",
        "visual_critique": "anthropic",
    }
    assert {name: tier.opencode_model for name, tier in resolved.items()} == {
        "visual_sweep": "hermes-codex/gpt-5.6-luna",
        "visual_inspector": "anthropic/claude-sonnet-5",
        "visual_critique": "anthropic/claude-opus-5",
    }


def test_visual_tiers_are_outside_the_steppable_ladder():
    for name in (
        "visual_sweep",
        "visual_inspector",
        "visual_critique",
    ):
        assert name not in MODEL_TIER_LADDER
        assert resolve_adjacent_model_tier({}, name, 1) is None
        assert resolve_adjacent_model_tier({}, name, -1) is None


def test_deep_review_is_reserved_sol_high_outside_the_ladder():
    tier = resolve_model_tier({}, "deep_review")

    assert tier is not None
    assert tier.model == "gpt-5.6-sol"
    assert tier.opencode_model == "hermes-codex/gpt-5.6-sol"
    assert tier.reasoning_effort == "high"
    assert "deep_review" not in MODEL_TIER_LADDER
    assert resolve_adjacent_model_tier({}, "deep_review", 1) is None


def test_visual_tier_names_are_reserved_against_user_override():
    tier = resolve_model_tier(
        {
            "model_tiers": {
                "visual_inspector": {
                    "model": "gpt-5.6-sol",
                    "opencode_model": "hermes-codex/gpt-5.6-sol",
                    "reasoning_effort": "xhigh",
                }
            }
        },
        "visual_inspector",
    )

    assert tier is not None
    assert tier.model == "claude-sonnet-5"
    assert tier.reasoning_effort == "medium"


def test_builtin_tier_ladder_steps_in_order_and_rejects_custom_or_edge_tiers():
    assert MODEL_TIER_LADDER == ("trivial", "basic", "intermediate", "advanced")

    assert resolve_adjacent_model_tier({}, "basic", -1).name == "trivial"
    assert resolve_adjacent_model_tier({}, "basic", 1).name == "intermediate"
    assert resolve_adjacent_model_tier({}, "intermediate", 1).name == "advanced"
    assert resolve_adjacent_model_tier({}, "trivial", -1) is None
    assert resolve_adjacent_model_tier({}, "advanced", 1) is None
    assert resolve_adjacent_model_tier({}, "discord_action", 1) is None
    assert resolve_adjacent_model_tier({}, "custom", 1) is None
    assert resolve_adjacent_model_tier({}, "basic", 0) is None

    assert resolve_model_tier_offset({}, "basic", 2).name == "advanced"
    assert resolve_model_tier_offset({}, "trivial", 2).name == "intermediate"
    assert resolve_model_tier_offset({}, "advanced", 2) is None
    assert resolve_model_tier_offset({}, "basic", 0) is None


def test_reserved_builtin_tier_override_is_ignored():
    tier = resolve_model_tier(
        {
            "model_tiers": {
                "basic": {
                    "model": "gpt-5.6-terra",
                    "opencode_model": "hermes-codex/gpt-5.6-terra",
                    "reasoning_effort": "high",
                }
            }
        },
        "basic",
    )

    assert tier is not None
    assert tier.model == "gpt-5.6-luna"
    assert tier.opencode_model == "hermes-codex/gpt-5.6-luna"
    assert tier.reasoning_effort == "max"


def test_custom_non_reserved_tier_resolves_atomically():
    tier = resolve_model_tier(
        {
            "model_tiers": {
                "feature": {
                    "model": "custom/feature",
                    "opencode_model": "custom/feature-worker",
                    "reasoning_effort": "high",
                }
            }
        },
        "feature",
    )

    assert tier is not None
    assert tier.model == "custom/feature"
    assert tier.opencode_model == "custom/feature-worker"
    assert tier.reasoning_effort == "high"


def test_invalid_tier_is_rejected_without_leaking_into_runtime():
    assert resolve_model_tier(
        {"model_tiers": {"broken": {"model": "x", "reasoning_effort": "extreme"}}},
        "broken",
    ) is None


def test_review_spills_high_to_xhigh_and_implementation_caps_at_high():
    assert classify_task_purpose("Review the authentication flow and report findings") == "review"
    assert classify_task_purpose("Review the flow and fix the race") == "implementation"
    assert classify_task_purpose("Analyze the request") == "implementation"

    high_effort = resolve_model_tier(
        {
            "model_tiers": {
                "high_effort": {
                    "model": "custom/high",
                    "opencode_model": "custom/high-worker",
                    "reasoning_effort": "high",
                }
            }
        },
        "high_effort",
    )
    assert restrict_model_tier_for_task({}, high_effort, "Implement the fix").name == "high_effort"
    assert restrict_model_tier_for_task({}, high_effort, "Implement the fix").reasoning_effort == "high"
    assert restrict_model_tier_for_task({}, high_effort, "Audit the auth flow").name == "high_effort"
    assert restrict_model_tier_for_task({}, high_effort, "Audit the auth flow").reasoning_effort == "xhigh"
    assert restrict_reasoning_effort_for_task("max", "Apply the patch") == "max"
    assert restrict_reasoning_effort_for_task("ultra", "Review the authentication flow") == "ultra"
    assert restrict_reasoning_effort_for_task("high", "Review the authentication flow") == "xhigh"
    assert restrict_reasoning_effort_for_task("xhigh", "Diagnose the incident") == "xhigh"


def test_implementation_cap_preserves_custom_tier_model():
    config = {
        "model_tiers": {
            "feature": {"model": "custom/feature", "reasoning_effort": "xhigh"},
        }
    }

    restricted = restrict_model_tier_for_task(
        config,
        resolve_model_tier(config, "feature"),
        "Build the feature",
    )

    assert restricted.name == "feature"
    assert restricted.model == "custom/feature"
    assert restricted.reasoning_effort == "high"


def test_task_restriction_preserves_named_tier_fast_mode():
    config = {
        "model_tiers": {
            "fast_review": {
                "model": "custom/fast",
                "opencode_model": "custom/fast-worker",
                "reasoning_effort": "high",
                "fast_mode": True,
            }
        }
    }

    restricted = restrict_model_tier_for_task(
        config,
        resolve_model_tier(config, "fast_review"),
        "Audit the auth flow",
    )

    assert restricted is not None
    assert restricted.fast_mode is True
    assert restricted.reasoning_effort == "xhigh"


def test_coding_worker_has_no_independent_or_deprecated_tier_catalog():
    assert "worker_tiers" not in DEFAULT_CONFIG["coding_worker"]
    for legacy_name in ("quick", "standard", "thorough", "deep", "max"):
        assert resolve_model_tier({}, legacy_name) is None


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


def test_explicit_maximum_tier_efforts_are_preserved():
    for effort in ("max", "ultra"):
        tier = resolve_model_tier(
            {"model_tiers": {"custom": {"model": "custom/model", "reasoning_effort": effort}}},
            "custom",
        )

        assert tier is not None
        assert tier.reasoning_effort == effort
