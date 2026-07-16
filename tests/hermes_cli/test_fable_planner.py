from __future__ import annotations

from types import SimpleNamespace

from hermes_cli.fable_planner import (
    FABLE_DEFAULT_TOOLSETS,
    FABLE_GIT_LIFECYCLE_MERGE,
    FABLE_GIT_LIFECYCLE_NONE,
    FABLE_GIT_LIFECYCLE_PR,
    FABLE_IMPLEMENTATION_MODE,
    FABLE_MODEL,
    FABLE_PLAN_MODE,
    FablePlanRequest,
    build_fable_implementation_instruction,
    build_fable_plan_invocation,
    build_fable_user_instruction,
    fable_enabled_toolsets,
    fable_git_lifecycle_mode,
    fable_metadata,
    fable_reasoning_config,
    fable_session_model_override,
    parse_fable_command_args,
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


def test_fable_session_model_override_uses_configured_proxy_secret(monkeypatch):
    monkeypatch.setenv("CLI_PROXY_API_KEY", "cliproxy-key")

    override, error = fable_session_model_override(
        config={
            "fable": {
                "route": "anthropic_proxy",
                "key_env": "CLI_PROXY_API_KEY",
                "base_url": "http://127.0.0.1:8317/v1",
            }
        }
    )

    assert error == ""
    assert override == {
        "model": FABLE_MODEL,
        "provider": "anthropic",
        "api_key": "cliproxy-key",
        "base_url": "http://127.0.0.1:8317/v1",
        "api_mode": "anthropic_messages",
        "disable_fallback": "true",
    }


def test_fable_proxy_route_rejects_incomplete_configuration(monkeypatch):
    monkeypatch.setenv("CLI_PROXY_API_KEY", "cliproxy-key")

    override, error = fable_session_model_override(
        config={"fable": {"route": "anthropic_proxy", "key_env": "CLI_PROXY_API_KEY"}}
    )

    assert override is None
    assert "fable.base_url" in error


def test_fable_proxy_route_rejects_missing_key(monkeypatch):
    monkeypatch.delenv("CLI_PROXY_API_KEY", raising=False)

    override, error = fable_session_model_override(
        config={
            "fable": {
                "route": "anthropic_proxy",
                "key_env": "CLI_PROXY_API_KEY",
                "base_url": "http://127.0.0.1:8317",
            }
        }
    )

    assert override is None
    assert "CLI_PROXY_API_KEY" in error


def test_fable_proxy_route_rejects_public_anthropic_endpoint(monkeypatch):
    monkeypatch.setenv("CLI_PROXY_API_KEY", "cliproxy-key")

    override, error = fable_session_model_override(
        config={
            "fable": {
                "route": "anthropic_proxy",
                "key_env": "CLI_PROXY_API_KEY",
                "base_url": "https://api.anthropic.com",
            }
        }
    )

    assert override is None
    assert "refuses api.anthropic.com" in error


def test_fable_proxy_route_rejects_invalid_endpoint(monkeypatch):
    monkeypatch.setenv("CLI_PROXY_API_KEY", "cliproxy-key")

    override, error = fable_session_model_override(
        config={
            "fable": {
                "route": "anthropic_proxy",
                "key_env": "CLI_PROXY_API_KEY",
                "base_url": "http://proxy.test:invalid",
            }
        }
    )

    assert override is None
    assert "invalid fable.base_url" in error


def test_fable_proxy_route_rejects_unsupported_api_mode(monkeypatch):
    monkeypatch.setenv("CLI_PROXY_API_KEY", "cliproxy-key")

    override, error = fable_session_model_override(
        config={
            "fable": {
                "route": "anthropic_proxy",
                "key_env": "CLI_PROXY_API_KEY",
                "base_url": "http://127.0.0.1:8317",
                "api_mode": "chat_completions",
            }
        }
    )

    assert override is None
    assert "api_mode=anthropic_messages" in error


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


def test_discord_fable_parser_routes_explicit_and_natural_plan_requests_plan_only():
    assert parse_fable_command_args("implement the feature") == (
        FABLE_IMPLEMENTATION_MODE,
        "implement the feature",
    )
    assert parse_fable_command_args("plan implement the feature") == (
        FABLE_PLAN_MODE,
        "implement the feature",
    )
    assert parse_fable_command_args("PLAN") == (FABLE_PLAN_MODE, "")
    for request in (
        "make a plan for the feature",
        "help me plan to ship the feature",
        "create a plan for the migration",
        "could you draft a plan for the rollout?",
    ):
        assert parse_fable_command_args(request) == (FABLE_PLAN_MODE, request)


def test_build_fable_implementation_instruction_requires_codex_delegation():
    packet = build_fable_implementation_instruction(
        FablePlanRequest(prompt="add feature", platform="discord", session_id="s1")
    )

    assert "add feature" in packet
    assert "delegate_coding_task" in packet
    assert "Codex coding worker" in packet
    assert "Choose `worker_tier` deliberately" in packet
    assert "Front-load what you learned into `relevant_files`" in packet
    assert "Do not fall back to OpenCode" in packet
    assert "`fable_git_result.recovery_required`" in packet
    assert "call `delegate_coding_task` again with the same `cwd`" in packet
    assert "`merge_performed`" in packet
    assert "`merge_observed`" in packet
    assert "Never say that Codex committed" in packet


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
    assert DEFAULT_CONFIG["fable"]["git_lifecycle"] == FABLE_GIT_LIFECYCLE_NONE
    assert DEFAULT_CONFIG["fable"]["enabled_toolsets"] == FABLE_DEFAULT_TOOLSETS


def test_fable_enabled_toolsets_defaults_to_compact_budget():
    assert fable_enabled_toolsets(config={}) == ["file", "terminal", "web", "browser", "discord"]


def test_fable_enabled_toolsets_allows_config_override():
    assert fable_enabled_toolsets(config={"fable": {"enabled_toolsets": ["file", "web"]}}) == ["file", "web"]


def test_fable_git_lifecycle_defaults_closed_and_normalizes_known_modes():
    assert fable_git_lifecycle_mode({}) == FABLE_GIT_LIFECYCLE_NONE
    assert fable_git_lifecycle_mode({"fable": {"git_lifecycle": "PR"}}) == FABLE_GIT_LIFECYCLE_PR
    assert fable_git_lifecycle_mode({"fable": {"git_lifecycle": "merge"}}) == FABLE_GIT_LIFECYCLE_MERGE
    assert fable_git_lifecycle_mode({"fable": {"git_lifecycle": "anything"}}) == FABLE_GIT_LIFECYCLE_NONE


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


def test_fable_implementation_metadata_is_not_a_plan_artifact():
    metadata = fable_metadata(mode=FABLE_IMPLEMENTATION_MODE)

    assert metadata["fable_mode"] == FABLE_IMPLEMENTATION_MODE
    assert metadata["git_lifecycle"] == FABLE_GIT_LIFECYCLE_NONE
    assert metadata["kind"] == "fable_implementation"
    assert "plan_artifact_kind" not in metadata


def test_fable_metadata_identifies_configured_proxy_route():
    metadata = fable_metadata(config={"fable": {"route": "anthropic_proxy"}})

    assert metadata["route"] == "anthropic_proxy"
    assert metadata["transport"] == "anthropic_proxy"
    assert metadata["anthropic_oauth_tool_name_compat"] is True
    assert fable_metadata()["anthropic_oauth_tool_name_compat"] is False
