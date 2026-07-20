"""Gateway defaults and overrides for named model tiers."""

import gateway.run as gateway_run


def test_gateway_uses_basic_tier_instead_of_global_model_default(monkeypatch):
    config = {
        "model": {"default": "global/model"},
        "agent": {"reasoning_effort": "low"},
    }

    assert gateway_run._resolve_gateway_model(config) == "gpt-5.6-luna"
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: config)
    assert gateway_run.GatewayRunner._load_reasoning_config() == {
        "enabled": True,
        "effort": "xhigh",
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


def test_discord_complex_implementation_caps_effort_but_keeps_advanced_model():
    implementation = gateway_run._discord_action_request_model_tier(
        {},
        {"initial_request": "Investigate the production race and fix it"},
    )
    review = gateway_run._discord_action_request_model_tier(
        {},
        {"initial_request": "Audit the production authentication race"},
    )

    assert implementation.name == "advanced"
    assert implementation.reasoning_effort == "high"
    assert review.name == "advanced"
    assert review.reasoning_effort == "xhigh"
