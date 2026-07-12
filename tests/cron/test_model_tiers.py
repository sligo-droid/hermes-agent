"""Cron model-tier resolution tests."""

from cron.scheduler import _resolve_cron_agent_model


def test_unpinned_cron_uses_the_basic_tier(monkeypatch):
    monkeypatch.delenv("HERMES_MODEL", raising=False)

    model, tier = _resolve_cron_agent_model(
        {"model": {"default": "unrelated/default"}},
        {"model": None},
    )

    assert tier is not None
    assert tier.name == "basic"
    assert model == tier.model
    assert tier.reasoning_config() == {"enabled": True, "effort": "max"}


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
