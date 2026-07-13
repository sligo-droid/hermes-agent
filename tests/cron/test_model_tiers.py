"""Cron model-tier resolution tests."""

from unittest.mock import patch

from cron.scheduler import _resolve_cron_agent_model
from hermes_cli.config import DEFAULT_CONFIG


def test_unpinned_cron_uses_the_global_trivial_tier(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL", raising=False)

    model, tier = _resolve_cron_agent_model(
        {"model": {"default": "unrelated/default"}},
        {"model": None},
    )

    assert tier is not None
    assert DEFAULT_CONFIG["cron"]["model_tier"] == "trivial"
    assert tier.name == "trivial"
    assert model == tier.model
    assert tier.reasoning_config() == {"enabled": True, "effort": "xhigh"}


def test_job_model_tier_precedes_global_tier(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL", raising=False)

    model, tier = _resolve_cron_agent_model(
        {"cron": {"model_tier": "trivial"}},
        {"model_tier": "advanced"},
    )

    assert tier is not None
    assert tier.name == "advanced"
    assert model == tier.model


def test_any_raw_job_override_bypasses_job_and_global_tiers(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    config = {
        "cron": {"model_tier": "trivial"},
        "model": {"default": "configured/model"},
    }

    for override in (
        {"model": "job/model"},
        {"provider": "job-provider"},
        {"reasoning_effort": "max"},
    ):
        model, tier = _resolve_cron_agent_model(
            config,
            {"model_tier": "advanced", **override},
        )
        assert tier is None
        assert model == override.get("model", "configured/model")


def test_cron_explicit_model_bypasses_the_route_tier(monkeypatch):
    monkeypatch.setenv("HERMES_MODEL", "environment/model")

    model, tier = _resolve_cron_agent_model({}, {"model": "job/model"})

    assert model == "job/model"
    assert tier is None


def test_cron_environment_model_bypasses_the_route_tier(monkeypatch):
    monkeypatch.setenv("HERMES_MODEL", "environment/model")

    model, tier = _resolve_cron_agent_model({}, {"model": None})

    assert model == "environment/model"
    assert tier is None


def test_disabled_cron_tier_uses_the_legacy_model_config(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL", raising=False)

    model, tier = _resolve_cron_agent_model(
        {
            "cron": {"model_tier": ""},
            "model": {"default": "configured/model"},
        },
        {"model": None},
    )

    assert model == "configured/model"
    assert tier is None


def test_resolved_tier_constructs_real_ai_agent_with_model_and_reasoning(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL", raising=False)
    model, tier = _resolve_cron_agent_model({}, {})
    assert tier is not None

    from run_agent import AIAgent

    with patch("agent.agent_init.init_agent") as init_agent:
        agent = AIAgent(
            model=model,
            reasoning_config=tier.reasoning_config(),
            skip_context_files=True,
            skip_memory=True,
        )

    assert isinstance(agent, AIAgent)
    kwargs = init_agent.call_args.kwargs
    assert kwargs["model"] == tier.model
    assert kwargs["reasoning_config"] == tier.reasoning_config()
