"""Gateway defaults and overrides for named model tiers."""

import gateway.run as gateway_run


def test_gateway_uses_basic_tier_instead_of_global_model_default(monkeypatch):
    config = {
        "model": {"default": "global/model"},
        "agent": {"reasoning_effort": "low"},
    }

    assert gateway_run._resolve_gateway_model(config) == "gpt-5.6-terra"
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: config)
    assert gateway_run.GatewayRunner._load_reasoning_config() == {
        "enabled": True,
        "effort": "medium",
    }


def test_gateway_can_disable_its_tier_and_fall_back_to_global_config(monkeypatch):
    config = {
        "gateway": {"model_tier": ""},
        "model": {"default": "global/model"},
        "agent": {"reasoning_effort": "low"},
    }

    assert gateway_run._resolve_gateway_model(config) == "global/model"
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: config)
    assert gateway_run.GatewayRunner._load_reasoning_config() == {
        "enabled": True,
        "effort": "low",
    }


def test_discord_action_request_uses_configured_routine_tier():
    tier = gateway_run._discord_action_request_model_tier(
        {
            "discord": {"action_request_model_tier": "intermediate"},
        }
    )

    assert tier.name == "intermediate"
    assert tier.reasoning_effort == "low"


def test_discord_read_only_uses_its_dedicated_sol_low_tier():
    tier = gateway_run._discord_read_only_model_tier({})

    assert tier.name == "discord_read_only"
    assert tier.model == "gpt-5.6-sol"
    assert tier.reasoning_effort == "low"
