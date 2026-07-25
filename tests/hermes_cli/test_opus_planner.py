from __future__ import annotations

from types import SimpleNamespace

from hermes_cli.opus_planner import (
    OPUS_DEFAULT_TOOLSETS,
    OPUS_IMPLEMENTATION_MODE,
    OPUS_MODEL,
    OPUS_PLAN_MODE,
    OpusPlanRequest,
    build_opus_implementation_instruction,
    build_opus_plan_invocation,
    build_opus_user_instruction,
    opus_enabled_toolsets,
    opus_metadata,
    opus_reasoning_config,
    opus_session_model_override,
    parse_opus_command_args,
)


class EmptyPool:
    def has_credentials(self):
        return False

    def has_available(self):
        return False

    def entries(self):
        return []


def test_opus_session_model_override_uses_anthropic_oauth_route(monkeypatch):
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

    override, error = opus_session_model_override(config={})

    assert error == ""
    assert override is not None
    assert override == {
        "model": OPUS_MODEL,
        "provider": "anthropic",
        "api_key": "sk-ant-oat01-test",
        "base_url": "https://api.anthropic.com",
        "api_mode": "anthropic_messages",
        "disable_fallback": "true",
    }


def test_opus_session_model_override_requests_pool_first_credentials(monkeypatch):
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

    override, error = opus_session_model_override(config={})

    assert error == ""
    assert override is not None
    assert calls == [
        {
            "requested": "anthropic",
            "target_model": OPUS_MODEL,
            "credential_preference": "pool_first",
        }
    ]


def test_opus_session_model_override_errors_when_route_unavailable(monkeypatch):
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: EmptyPool())

    def fail_resolve(**_kwargs):
        raise RuntimeError("no credentials")

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fail_resolve)

    override, error = opus_session_model_override(config={})

    assert override is None
    assert "Anthropic route" in error


def test_opus_session_model_override_fails_closed_when_anthropic_budget_already_exhausted(monkeypatch):
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
        raise AssertionError("/opus must not resolve a fallback route when Anthropic budget is exhausted")

    monkeypatch.setattr("agent.credential_pool.load_pool", lambda _provider: ExhaustedPool())
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fail_resolve)

    override, error = opus_session_model_override(config={})

    assert override is None
    assert "budget/quota is expended" in error
    assert "will not fall back to another model or provider" in error
    assert "status=402" in error


def test_opus_session_model_override_rejects_api_key_anthropic(monkeypatch):
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

    override, error = opus_session_model_override(config={})

    assert override is None
    assert "direct api.anthropic.com" in error


def test_opus_session_model_override_accepts_proxy_managed_api_key(monkeypatch):
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

    override, error = opus_session_model_override(config={})

    assert error == ""
    assert override is not None
    assert override["provider"] == "anthropic"
    assert override["model"] == OPUS_MODEL
    assert override["base_url"] == "http://127.0.0.1:8317"
    assert override["api_key"] == "cliproxy-key"
    assert override["api_mode"] == "anthropic_messages"
    assert override["disable_fallback"] == "true"


def test_opus_session_model_override_uses_configured_proxy_secret(monkeypatch):
    monkeypatch.setenv("CLI_PROXY_API_KEY", "cliproxy-key")

    override, error = opus_session_model_override(
        config={
            "opus": {
                "route": "anthropic_proxy",
                "key_env": "CLI_PROXY_API_KEY",
                "base_url": "http://127.0.0.1:8317/v1",
            }
        }
    )

    assert error == ""
    assert override == {
        "model": OPUS_MODEL,
        "provider": "anthropic",
        "api_key": "cliproxy-key",
        "base_url": "http://127.0.0.1:8317/v1",
        "api_mode": "anthropic_messages",
        "disable_fallback": "true",
    }


def test_opus_proxy_route_rejects_incomplete_configuration(monkeypatch):
    monkeypatch.setenv("CLI_PROXY_API_KEY", "cliproxy-key")

    override, error = opus_session_model_override(
        config={"opus": {"route": "anthropic_proxy", "key_env": "CLI_PROXY_API_KEY"}}
    )

    assert override is None
    assert "opus.base_url" in error


def test_opus_proxy_route_rejects_missing_key(monkeypatch):
    monkeypatch.delenv("CLI_PROXY_API_KEY", raising=False)

    override, error = opus_session_model_override(
        config={
            "opus": {
                "route": "anthropic_proxy",
                "key_env": "CLI_PROXY_API_KEY",
                "base_url": "http://127.0.0.1:8317",
            }
        }
    )

    assert override is None
    assert "CLI_PROXY_API_KEY" in error


def test_opus_proxy_route_rejects_public_anthropic_endpoint(monkeypatch):
    monkeypatch.setenv("CLI_PROXY_API_KEY", "cliproxy-key")

    override, error = opus_session_model_override(
        config={
            "opus": {
                "route": "anthropic_proxy",
                "key_env": "CLI_PROXY_API_KEY",
                "base_url": "https://api.anthropic.com",
            }
        }
    )

    assert override is None
    assert "refuses api.anthropic.com" in error


def test_opus_proxy_route_rejects_invalid_endpoint(monkeypatch):
    monkeypatch.setenv("CLI_PROXY_API_KEY", "cliproxy-key")

    override, error = opus_session_model_override(
        config={
            "opus": {
                "route": "anthropic_proxy",
                "key_env": "CLI_PROXY_API_KEY",
                "base_url": "http://proxy.test:invalid",
            }
        }
    )

    assert override is None
    assert "invalid opus.base_url" in error


def test_opus_proxy_route_rejects_unsupported_api_mode(monkeypatch):
    monkeypatch.setenv("CLI_PROXY_API_KEY", "cliproxy-key")

    override, error = opus_session_model_override(
        config={
            "opus": {
                "route": "anthropic_proxy",
                "key_env": "CLI_PROXY_API_KEY",
                "base_url": "http://127.0.0.1:8317",
                "api_mode": "chat_completions",
            }
        }
    )

    assert override is None
    assert "api_mode=anthropic_messages" in error


def test_opus_config_fails_closed_for_wrong_model(monkeypatch):
    def fail_resolve(**_kwargs):
        raise AssertionError("route should not resolve for unsupported configured model")

    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", fail_resolve)

    override, error = opus_session_model_override(config={"opus": {"model": "claude-sonnet-4-6"}})

    assert override is None
    assert "claude-opus-5" in error


def test_build_opus_user_instruction_contains_plan_only_contract():
    packet = build_opus_user_instruction(OpusPlanRequest(prompt="add feature", platform="discord", session_id="s1"))

    assert "add feature" in packet
    assert "Opus Plan-Only Contract" in packet
    assert "Do not edit files" in packet
    assert "discord" in packet


def test_discord_opus_parser_routes_explicit_and_natural_plan_requests_plan_only():
    assert parse_opus_command_args("implement the feature") == (
        OPUS_IMPLEMENTATION_MODE,
        "implement the feature",
    )
    assert parse_opus_command_args("plan implement the feature") == (
        OPUS_PLAN_MODE,
        "implement the feature",
    )
    assert parse_opus_command_args("PLAN") == (OPUS_PLAN_MODE, "")
    for request in (
        "make a plan for the feature",
        "help me plan to ship the feature",
        "create a plan for the migration",
        "could you draft a plan for the rollout?",
    ):
        assert parse_opus_command_args(request) == (OPUS_PLAN_MODE, request)


def test_build_opus_implementation_instruction_pins_codex_worker_policy():
    packet = build_opus_implementation_instruction(
        OpusPlanRequest(prompt="add feature", platform="discord", session_id="s1")
    )

    assert "add feature" in packet
    assert "normal Discord action-request policy" in packet
    assert "changes the orchestration model, provenance, and coding-worker backend only" in packet
    assert "delegate_task(read_only=true)" in packet
    assert "delegate_coding_task" in packet
    assert "pins to the Codex backend" in packet
    assert "Do not use OpenCode coding workers" in packet
    assert "Choose `model_tier` deliberately" in packet
    assert "`trivial`, `basic`, `intermediate`, or `advanced`" in packet
    assert "reasoning_effort" in packet
    assert "worker_tier" not in packet
    assert "scope reservations" in packet
    assert "isolated parallel worktrees" in packet
    assert "gateway-owned trusted closeout state machine" in packet
    assert "handles commit, push, PR, CI, merge" in packet
    assert "exactly as it does for an ordinary Discord action request" in packet
    assert "worker prose alone" in packet
    assert "opus_git_result" not in packet


def test_build_opus_plan_invocation_uses_plan_skill(monkeypatch):
    calls = []

    monkeypatch.setattr("agent.skill_commands.resolve_skill_command_key", lambda command: "/plan" if command == "plan" else None)

    def fake_build(cmd_key, user_instruction, task_id=None, runtime_note=""):
        calls.append((cmd_key, user_instruction, task_id, runtime_note))
        return "loaded plan skill"

    monkeypatch.setattr("agent.skill_commands.build_skill_invocation_message", fake_build)

    result = build_opus_plan_invocation(OpusPlanRequest(prompt="plan x", session_id="s1"))

    assert result == "loaded plan skill"
    assert calls[0][0] == "/plan"
    assert calls[0][2] == "s1"
    assert "plan x" in calls[0][1]
    assert "planning only" in calls[0][3]


def test_default_config_pins_opus_route():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["opus"]["provider"] == "anthropic"
    assert DEFAULT_CONFIG["opus"]["model"] == OPUS_MODEL
    assert DEFAULT_CONFIG["opus"]["route"] == "anthropic_oauth"
    assert "git_lifecycle" not in DEFAULT_CONFIG["opus"]
    assert DEFAULT_CONFIG["opus"]["enabled_toolsets"] == OPUS_DEFAULT_TOOLSETS


def test_opus_enabled_toolsets_defaults_to_compact_budget():
    assert opus_enabled_toolsets(config={}) == ["file", "terminal", "web", "browser", "discord"]


def test_opus_enabled_toolsets_allows_config_override():
    assert opus_enabled_toolsets(config={"opus": {"enabled_toolsets": ["file", "web"]}}) == ["file", "web"]


def test_opus_reasoning_config_is_pinned_and_ignores_discord_effort_config():
    """The route's effort is fixed policy, not a knob shared with actions."""
    for effort in ("xhigh", "max", "low", "minimal", "bogus"):
        assert opus_reasoning_config(
            {"discord": {"action_request_reasoning_effort": effort}}
        ) == {"enabled": True, "effort": "medium"}
        assert opus_reasoning_config(
            {"discord": {"feature_request_reasoning_effort": effort}}
        ) == {"enabled": True, "effort": "medium"}


def test_opus_reasoning_config_does_not_inherit_the_global_agent_default():
    assert opus_reasoning_config({"agent": {"reasoning_effort": "minimal"}}) == {
        "enabled": True,
        "effort": "medium",
    }


def test_opus_reasoning_config_handles_missing_and_malformed_config():
    for cfg in (None, {}, {"discord": None}, {"discord": "nonsense"}):
        assert opus_reasoning_config(cfg) == {"enabled": True, "effort": "medium"}

def test_opus_metadata_for_artifact():
    metadata = opus_metadata()

    assert metadata["command"] == "opus"
    assert metadata["plan_artifact_kind"] == "opus_plan"
    assert metadata["model"] == OPUS_MODEL
    assert metadata["reply_to_mode"] == "all"


def test_opus_implementation_metadata_is_not_a_plan_artifact():
    metadata = opus_metadata(mode=OPUS_IMPLEMENTATION_MODE)

    assert metadata["opus_mode"] == OPUS_IMPLEMENTATION_MODE
    assert "git_lifecycle" not in metadata
    assert metadata["kind"] == "opus_implementation"
    assert metadata["coding_worker_backend"] == "codex"
    assert "plan_artifact_kind" not in metadata


def test_opus_metadata_identifies_configured_proxy_route():
    metadata = opus_metadata(config={"opus": {"route": "anthropic_proxy"}})

    assert metadata["route"] == "anthropic_proxy"
    assert metadata["transport"] == "anthropic_proxy"
    assert metadata["anthropic_oauth_tool_name_compat"] is True
    assert opus_metadata()["anthropic_oauth_tool_name_compat"] is False
