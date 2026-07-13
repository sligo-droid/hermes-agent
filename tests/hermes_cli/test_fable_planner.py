from __future__ import annotations

from types import SimpleNamespace

from hermes_cli.fable_planner import (
    FABLE_DEFAULT_TOOLSETS,
    FABLE_MODEL,
    FablePlanRequest,
    build_fable_plan_invocation,
    build_fable_user_instruction,
    fable_enabled_toolsets,
    fable_metadata,
    fable_reasoning_config,
    fable_session_model_override,
)


class EmptyPool:
    def has_credentials(self):
        return False

    def has_available(self):
        return False

    def entries(self):
        return []


def test_fable_session_model_override_uses_anthropic_oauth_route(monkeypatch):
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: EmptyPool())
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda requested, target_model, credential_preference: {
            "provider": requested,
            "api_mode": "anthropic_messages",
            "base_url": "https://api.anthropic.com",
            "api_key": "sk-ant-oat01-test",
        },
    )

    override, error = fable_session_model_override(config={})

    assert error == ""
    assert override is not None
    assert override == {
        "model": FABLE_MODEL,
        "provider": "anthropic",
        "api_key": "sk-ant-oat01-test",
        "base_url": "https://api.anthropic.com",
        "api_mode": "anthropic_messages",
        "disable_fallback": "true",
    }


def test_fable_session_model_override_requests_pool_first_credentials(monkeypatch):
    calls = []
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: EmptyPool())

    def fake_resolve_runtime_provider(**kwargs):
        calls.append(kwargs)
        return {
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
            "base_url": "https://api.anthropic.com",
            "api_key": "sk-ant-oat01-test",
        }

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fake_resolve_runtime_provider)

    override, error = fable_session_model_override(config={})

    assert error == ""
    assert override is not None
    assert calls == [
        {
            "requested": "anthropic",
            "target_model": FABLE_MODEL,
            "credential_preference": "pool_first",
        }
    ]


def test_fable_session_model_override_errors_when_route_unavailable(monkeypatch):
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: EmptyPool())

    def fail_resolve(**_kwargs):
        raise RuntimeError("no credentials")

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fail_resolve)

    override, error = fable_session_model_override(config={})

    assert override is None
    assert "Anthropic route" in error


def test_fable_session_model_override_fails_closed_when_anthropic_budget_already_exhausted(monkeypatch):
    class ExhaustedPool:
        def has_credentials(self):
            return True

        def has_available(self):
            return False

        def entries(self):
            return [
                SimpleNamespace(
                    last_status="exhausted",
                    last_error_code=402,
                    last_error_reason="budget_expended",
                    last_error_message="budget is already expended",
                )
            ]

    def fail_resolve(**_kwargs):
        raise AssertionError("/fable must not resolve a fallback route when Anthropic budget is exhausted")

    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: ExhaustedPool())
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fail_resolve)

    override, error = fable_session_model_override(config={})

    assert override is None
    assert "budget/quota is expended" in error
    assert "will not fall back to another model or provider" in error
    assert "status=402" in error


def test_fable_session_model_override_rejects_api_key_anthropic(monkeypatch):
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: EmptyPool())
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
            "base_url": "https://api.anthropic.com",
            "api_key": "sk-ant-api03-test",
        },
    )

    override, error = fable_session_model_override(config={})

    assert override is None
    assert "direct api.anthropic.com" in error


def test_fable_session_model_override_accepts_proxy_managed_api_key(monkeypatch):
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: EmptyPool())
    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        lambda **_kwargs: {
            "provider": "anthropic",
            "api_mode": "anthropic_messages",
            "base_url": "http://127.0.0.1:8317",
            "api_key": "cliproxy-key",
        },
    )

    override, error = fable_session_model_override(config={})

    assert error == ""
    assert override is not None
    assert override["provider"] == "anthropic"
    assert override["model"] == FABLE_MODEL
    assert override["base_url"] == "http://127.0.0.1:8317"
    assert override["api_key"] == "cliproxy-key"
    assert override["api_mode"] == "anthropic_messages"
    assert override["disable_fallback"] == "true"


def test_fable_config_fails_closed_for_wrong_model(monkeypatch):
    def fail_resolve(**_kwargs):
        raise AssertionError("route should not resolve for unsupported configured model")

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fail_resolve)

    override, error = fable_session_model_override(config={"fable": {"model": "claude-sonnet-4-6"}})

    assert override is None
    assert "claude-fable-5" in error


def test_build_fable_user_instruction_contains_plan_only_contract():
    packet = build_fable_user_instruction(FablePlanRequest(prompt="add feature", platform="discord", session_id="s1"))

    assert "add feature" in packet
    assert "Fable Plan-Only Contract" in packet
    assert "Do not edit files" in packet
    assert "discord" in packet


def test_build_fable_plan_invocation_uses_plan_skill(monkeypatch):
    calls = []

    monkeypatch.setattr("agent.skill_commands.resolve_skill_command_key", lambda command: "/plan" if command == "plan" else None)

    def fake_build(cmd_key, user_instruction, task_id=None, runtime_note=""):
        calls.append((cmd_key, user_instruction, task_id, runtime_note))
        return "loaded plan skill"

    monkeypatch.setattr("agent.skill_commands.build_skill_invocation_message", fake_build)

    result = build_fable_plan_invocation(FablePlanRequest(prompt="plan x", session_id="s1"))

    assert result == "loaded plan skill"
    assert calls[0][0] == "/plan"
    assert calls[0][2] == "s1"
    assert "plan x" in calls[0][1]
    assert "planning only" in calls[0][3]


def test_default_config_pins_fable_route():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["fable"]["provider"] == "anthropic"
    assert DEFAULT_CONFIG["fable"]["model"] == FABLE_MODEL
    assert DEFAULT_CONFIG["fable"]["route"] == "anthropic_oauth"
    assert DEFAULT_CONFIG["fable"]["enabled_toolsets"] == FABLE_DEFAULT_TOOLSETS


def test_fable_enabled_toolsets_defaults_to_compact_budget():
    assert fable_enabled_toolsets(config={}) == ["file", "terminal", "web", "browser", "discord"]


def test_fable_enabled_toolsets_allows_config_override():
    assert fable_enabled_toolsets(config={"fable": {"enabled_toolsets": ["file", "web"]}}) == ["file", "web"]


def test_fable_reasoning_config_uses_configured_discord_feature_effort():
    assert fable_reasoning_config(
        {
            "agent": {"reasoning_effort": "medium"},
            "discord": {"feature_request_reasoning_effort": "high"},
        }
    ) == {"enabled": True, "effort": "high"}


def test_fable_reasoning_config_does_not_inherit_medium_when_unconfigured():
    assert fable_reasoning_config({"agent": {"reasoning_effort": "medium"}}) == {
        "enabled": True,
        "effort": "high",
    }


def test_fable_reasoning_config_falls_back_high_for_invalid_config():
    assert fable_reasoning_config({"discord": {"feature_request_reasoning_effort": "bogus"}}) == {
        "enabled": True,
        "effort": "high",
    }


def test_fable_metadata_for_artifact():
    metadata = fable_metadata()

    assert metadata["command"] == "fable"
    assert metadata["plan_artifact_kind"] == "fable_plan"
    assert metadata["model"] == FABLE_MODEL
    assert metadata["reply_to_mode"] == "all"
