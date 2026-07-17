"""Regression test for #17929: AIAgent.__init__ should try fallback_model
when primary provider credentials are exhausted."""
import json
import time

import pytest
from unittest.mock import patch, MagicMock
from run_agent import AIAgent


def _make_tool_defs():
    return [{"type": "function", "function": {"name": "web_search",
             "description": "search", "parameters": {"type": "object", "properties": {}}}}]


def _mock_client(api_key="fb-key-1234567890", base_url="https://fb.example.com/v1"):
    c = MagicMock()
    c.api_key = api_key
    c.base_url = base_url
    c._default_headers = None
    return c


def _write_exhausted_codex_auth(tmp_path, reset_at: float) -> None:
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(json.dumps({
        "version": 1,
        "credential_pool": {
            "openai-codex": [{
                "id": "codex-oauth",
                "label": "ChatGPT OAuth",
                "auth_type": "oauth",
                "priority": 0,
                "source": "manual:device_code",
                "access_token": "secret-codex-access-token",
                "refresh_token": "secret-codex-refresh-token",
                "last_status": "exhausted",
                "last_status_at": time.time(),
                "last_error_code": 429,
                "last_error_reason": "usage_limit_reached",
                "last_error_message": "usage limit reached",
                "last_error_reset_at": reset_at,
            }],
        },
    }))


def test_init_tries_fallback_when_primary_returns_none():
    """When resolve_provider_client returns None for primary but succeeds for
    a fallback entry, __init__ should NOT raise RuntimeError."""
    fb = _mock_client()

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        if provider == "tencent-token-plan":
            return fb, "kimi2.5"
        return None, None  # primary exhausted

    with patch("agent.auxiliary_client.resolve_provider_client", side_effect=fake_resolve), \
         patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI", return_value=MagicMock()):

        agent = AIAgent(
            provider="alibaba-coding-plan",
            model="qwen3.6-plus",
            api_key=None,
            base_url=None,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            fallback_model=[{"provider": "tencent-token-plan", "model": "kimi2.5"}],
        )
        assert agent.provider == "tencent-token-plan"
        assert agent.model == "kimi2.5"
        assert agent._fallback_activated is True


def test_init_raises_when_no_fallback_configured():
    """When primary returns None and no fallback is set, should raise."""
    with patch("agent.auxiliary_client.resolve_provider_client", return_value=(None, None)), \
         patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI", return_value=MagicMock()):

        with pytest.raises(RuntimeError, match="ALIBABA_CODING_PLAN_API_KEY"):
            AIAgent(
                provider="alibaba-coding-plan",
                model="qwen3.6-plus",
                api_key=None,
                base_url=None,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                fallback_model=None,
            )


def test_init_reports_exhausted_codex_oauth_after_fallback_failure(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr("hermes_cli.auth._import_codex_cli_tokens", lambda: None)
    _write_exhausted_codex_auth(tmp_path, time.time() + 600)
    resolve_calls = []

    def fake_resolve(provider, model=None, raw_codex=False,
                     explicit_base_url=None, explicit_api_key=None):
        resolve_calls.append((provider, model))
        return None, None

    with patch("agent.auxiliary_client.resolve_provider_client", side_effect=fake_resolve), \
         patch("run_agent.get_tool_definitions", return_value=_make_tool_defs()), \
         patch("run_agent.check_toolset_requirements", return_value={}), \
         patch("run_agent.OpenAI", return_value=MagicMock()):

        with pytest.raises(RuntimeError) as exc_info:
            AIAgent(
                provider="openai-codex",
                model="gpt-5.4",
                api_key=None,
                base_url=None,
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
                fallback_model=[{"provider": "alibaba", "model": "qwen-plus"}],
            )

    assert resolve_calls == [
        ("openai-codex", "gpt-5.4"),
        ("alibaba", "qwen-plus"),
    ]
    message = str(exc_info.value)
    assert "OpenAI Codex OAuth credentials are exhausted" in message
    assert "HTTP 429" in message
    assert "usage_limit_reached" in message
    assert "API_KEY" not in message
    assert "secret-codex" not in message
