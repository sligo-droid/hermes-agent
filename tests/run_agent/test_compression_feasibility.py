"""Tests for _check_compression_model_feasibility() — warns when the
auxiliary compression model's context is smaller than the main model's
compression threshold.

Two-phase design:
  1. __init__  → runs the check, prints via _vprint (CLI), stores warning
  2. run_conversation (first call) → replays stored warning through
     status_callback (gateway platforms)
"""

import json
import time
from unittest.mock import MagicMock, patch

import pytest

import agent.auxiliary_client as auxiliary_client
from run_agent import AIAgent
from agent.context_compressor import ContextCompressor
from agent.auxiliary_client import CodexAuxiliaryClient


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


@pytest.fixture(autouse=True)
def _stable_aux_provider_config():
    """Keep feasibility tests independent from the developer's config.yaml."""
    with patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("auto", None, None, None, None),
    ):
        yield


def _make_agent(
    *,
    compression_enabled: bool = True,
    threshold_percent: float = 0.50,
    main_context: int = 200_000,
) -> AIAgent:
    """Build a minimal AIAgent with a compressor, skipping __init__."""
    agent = AIAgent.__new__(AIAgent)
    agent.model = "test-main-model"
    agent.provider = "openrouter"
    agent.base_url = "https://openrouter.ai/api/v1"
    agent.api_key = "sk-test"
    agent.api_mode = "chat_completions"
    agent.quiet_mode = True
    agent.log_prefix = ""
    agent.compression_enabled = compression_enabled
    agent._print_fn = None
    agent.suppress_status_output = False
    agent._stream_consumers = []
    agent._executing_tools = False
    agent._mute_post_response = False
    agent.status_callback = None
    agent.tool_progress_callback = None
    agent._compression_warning = None
    agent._aux_compression_context_length_config = None
    agent._custom_providers = []
    agent.tools = []

    compressor = MagicMock(spec=ContextCompressor)
    compressor.context_length = main_context
    compressor.threshold_tokens = int(main_context * threshold_percent)
    compressor.summary_target_ratio = 0.20
    compressor.tail_token_budget = int(
        compressor.threshold_tokens * compressor.summary_target_ratio
    )
    agent.context_compressor = compressor

    return agent


# ── Core warning logic ──────────────────────────────────────────────


@patch("agent.model_metadata.get_model_context_length", return_value=80_000)
@patch("agent.auxiliary_client.get_text_auxiliary_client")
def test_auto_corrects_threshold_when_aux_context_below_threshold(mock_get_client, mock_ctx_len):
    """Auto-correction: aux >= 64K floor but < threshold → lower threshold
    to aux_context so compression still works this session."""
    agent = _make_agent(main_context=200_000, threshold_percent=0.50)
    # threshold = 100,000 — aux has 80,000 (above 64K floor, below threshold)
    mock_client = MagicMock()
    mock_client.base_url = "https://openrouter.ai/api/v1"
    mock_client.api_key = "sk-aux"
    mock_get_client.return_value = (mock_client, "google/gemini-3-flash-preview")

    messages = []
    agent._emit_status = lambda msg: messages.append(msg)

    agent._check_compression_model_feasibility()

    assert len(messages) == 1
    assert "Compression model" in messages[0]
    assert "80,000" in messages[0]        # aux context
    assert "100,000" in messages[0]       # old threshold
    assert "Auto-lowered" in messages[0]
    # Actionable persistence guidance included
    assert "config.yaml" in messages[0]
    assert "auxiliary:" in messages[0]
    assert "compression:" in messages[0]
    # 200K main is under the 512K small-context limit and 80K/200K = 40% sits
    # below the 75% floor — a `threshold:` suggestion would be raised back to
    # 75% and ignored (#67422), so the message must not offer one and must
    # explain the recomputed trigger instead (0.75 * 200K = 150K).
    assert "threshold:" not in messages[0]
    assert "150,000" in messages[0]
    # Warning stored for gateway replay
    assert agent._compression_warning is not None
    # Threshold on the live compressor was actually lowered to aux_context.
    assert agent.context_compressor.threshold_tokens == 80_000
    # Every threshold-derived budget must move with it. Keeping the original
    # 20K tail here would protect 25% of the lowered threshold instead of the
    # configured 20%, and larger real-world mismatches can make the tail's 1.5x
    # soft ceiling wider than the entire compression trigger.
    assert agent.context_compressor.tail_token_budget == 16_000


@patch("agent.model_metadata.get_model_context_length", return_value=32_768)
@patch("agent.auxiliary_client.get_text_auxiliary_client")
def test_rejects_aux_below_minimum_context(mock_get_client, mock_ctx_len):
    """Hard floor: aux context < MINIMUM_CONTEXT_LENGTH (64K) → session
    refuses to start (ValueError), mirroring the main-model rejection."""
    agent = _make_agent(main_context=200_000, threshold_percent=0.50)
    mock_client = MagicMock()
    mock_client.base_url = "https://openrouter.ai/api/v1"
    mock_client.api_key = "sk-aux"
    mock_get_client.return_value = (mock_client, "tiny-aux-model")

    agent._emit_status = lambda msg: None

    with pytest.raises(ValueError) as exc_info:
        agent._check_compression_model_feasibility()

    err = str(exc_info.value)
    assert "tiny-aux-model" in err
    assert "32,768" in err
    assert "64,000" in err
    assert "below the minimum" in err




def test_fallback_client_uses_resolved_codex_provider_for_context_and_warning():
    agent = _make_agent(main_context=544_000, threshold_percent=0.50)
    agent.provider = "anthropic"
    real_client = MagicMock()
    real_client.base_url = "https://chatgpt.com/backend-api/codex"
    real_client.api_key = "codex-token"
    fallback_client = CodexAuxiliaryClient(real_client, "gpt-5.4-mini")
    messages = []
    agent._emit_status = messages.append

    with patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("anthropic", "claude-sonnet-4.6", None, None, None),
    ), patch(
        "agent.auxiliary_client._get_auxiliary_task_config",
        return_value={
            "fallback_chain": [
                {"provider": "openai-codex", "model": "gpt-5.4-mini"}
            ]
        },
    ), patch(
        "agent.auxiliary_client._try_configured_fallback_chain",
        wraps=auxiliary_client._try_configured_fallback_chain,
    ) as mock_fallback, patch(
        "agent.auxiliary_client.resolve_provider_client",
        side_effect=[(None, None), (fallback_client, "gpt-5.4-mini")],
    ), patch(
        "agent.model_metadata.get_model_context_length", return_value=200_000
    ) as mock_ctx_len:
        agent._check_compression_model_feasibility()

    mock_fallback.assert_called_once_with(
        "compression", "anthropic", reason="provider unavailable"
    )
    assert isinstance(fallback_client, CodexAuxiliaryClient)
    assert "gpt-5.4-mini (openai-codex)" in messages[0]
    assert "gpt-5.4-mini (anthropic)" not in messages[0]
    assert mock_ctx_len.call_args.kwargs["provider"] == "openai-codex"


def test_primary_client_keeps_configured_provider_for_context_and_warning():
    agent = _make_agent(main_context=400_000, threshold_percent=0.50)
    agent.provider = "openrouter"
    mock_client = MagicMock()
    mock_client.base_url = "https://api.anthropic.com"
    mock_client.api_key = "anthropic-key"
    messages = []
    agent._emit_status = messages.append

    with patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("anthropic", "claude-sonnet-4.6", None, None, None),
    ), patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        return_value=(mock_client, "claude-sonnet-4.6"),
    ), patch(
        "agent.model_metadata.get_model_context_length", return_value=128_000
    ) as mock_ctx_len:
        agent._check_compression_model_feasibility()

    assert "claude-sonnet-4.6 (anthropic)" in messages[0]
    assert mock_ctx_len.call_args.kwargs["provider"] == "anthropic"


def test_untrusted_codex_like_hostname_does_not_override_configured_provider():
    agent = _make_agent(main_context=400_000, threshold_percent=0.50)
    mock_client = MagicMock()
    mock_client.base_url = "https://evilchatgpt.com/backend-api/codex"
    mock_client.api_key = "custom-secret"
    messages = []
    agent._emit_status = messages.append

    with patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("named-custom", "custom-model", None, None, None),
    ), patch(
        "agent.auxiliary_client.resolve_provider_client",
        return_value=(mock_client, "custom-model"),
    ), patch(
        "agent.model_metadata.get_model_context_length", return_value=128_000
    ) as mock_ctx_len:
        agent._check_compression_model_feasibility()

    assert "custom-model (named-custom)" in messages[0]
    assert mock_ctx_len.call_args.kwargs["provider"] == "named-custom"


def test_auto_custom_codex_transport_uses_resolver_provider_for_metadata():
    agent = _make_agent(main_context=400_000, threshold_percent=0.50)
    agent.provider = "named-custom"
    real_client = MagicMock()
    real_client.base_url = "https://custom.example/v1"
    real_client.api_key = "custom-secret"
    custom_client = CodexAuxiliaryClient(real_client, "custom-model")

    with patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("auto", None, None, None, None),
    ), patch(
        "agent.auxiliary_client.get_text_auxiliary_client",
        side_effect=lambda *args, **kwargs: auxiliary_client._resolve_auto(
            main_runtime=None, task="compression"
        ),
    ), patch(
        "agent.auxiliary_client._read_main_provider", return_value="anthropic",
    ), patch(
        "agent.auxiliary_client._read_main_model", return_value="main-model",
    ), patch(
        "agent.auxiliary_client.resolve_provider_client", return_value=(None, None),
    ), patch(
        "agent.auxiliary_client._is_provider_unhealthy", return_value=False,
    ), patch(
        "agent.auxiliary_client._try_openrouter", return_value=(None, None),
    ), patch(
        "agent.auxiliary_client._try_nous", return_value=(None, None),
    ), patch(
        "agent.auxiliary_client._try_custom_endpoint",
        return_value=(custom_client, "custom-model"),
    ), patch(
        "agent.model_metadata.get_model_context_length", return_value=128_000
    ) as mock_ctx_len:
        agent._check_compression_model_feasibility()

    assert custom_client._hermes_provider == "custom"
    assert mock_ctx_len.call_args.kwargs["provider"] == "custom"
    assert mock_ctx_len.call_args.kwargs["provider"] != agent.provider
    assert mock_ctx_len.call_args.kwargs["base_url"] == "https://custom.example/v1"


def test_feasibility_check_passes_live_main_runtime():
    """Compression feasibility should probe using the live session runtime."""
    agent = _make_agent(main_context=200_000, threshold_percent=0.50)
    agent.model = "gpt-5.4"
    agent.provider = "openai-codex"
    agent.base_url = "https://chatgpt.com/backend-api/codex"
    agent.api_key = "codex-token"
    agent.api_mode = "codex_responses"

    mock_client = MagicMock()
    mock_client.base_url = "https://chatgpt.com/backend-api/codex"
    mock_client.api_key = "codex-token"

    with patch("agent.auxiliary_client.get_text_auxiliary_client", return_value=(mock_client, "gpt-5.4")) as mock_get_client, \
         patch("agent.model_metadata.get_model_context_length", return_value=200_000):
        agent._emit_status = lambda msg: None
        agent._check_compression_model_feasibility()

    mock_get_client.assert_called_once_with(
        "compression",
        main_runtime={
            "model": "gpt-5.4",
            "provider": "openai-codex",
            "base_url": "https://chatgpt.com/backend-api/codex",
            "api_key": "codex-token",
            "api_mode": "codex_responses",
            "auth_mode": "",
        },
    )


@patch("agent.model_metadata.get_model_context_length", return_value=1_000_000)
@patch("agent.auxiliary_client.get_text_auxiliary_client")
def test_feasibility_check_passes_config_context_length(mock_get_client, mock_ctx_len):
    """auxiliary.compression.context_length from config is forwarded to
    get_model_context_length so custom endpoints that lack /models still
    report the correct context window (fixes #8499)."""
    agent = _make_agent(main_context=200_000, threshold_percent=0.85)
    agent._aux_compression_context_length_config = 1_000_000
    mock_client = MagicMock()
    mock_client.base_url = "http://custom-endpoint:8080/v1"
    mock_client.api_key = "sk-custom"
    mock_get_client.return_value = (mock_client, "custom/big-model")

    agent._emit_status = lambda msg: None
    agent._check_compression_model_feasibility()

    mock_ctx_len.assert_called_once_with(
        "custom/big-model",
        base_url="http://custom-endpoint:8080/v1",
        api_key="sk-custom",
        config_context_length=1_000_000,
        provider="openrouter",
        custom_providers=[],
    )




def test_init_feasibility_check_uses_aux_context_override_from_config():
    """Real AIAgent init should cache and forward auxiliary.compression.context_length."""

    class _StubCompressor:
        def __init__(self, *args, **kwargs):
            self.context_length = 200_000
            self.threshold_tokens = 100_000
            self.threshold_percent = 0.50

        def get_tool_schemas(self):
            return []

        def on_session_start(self, *args, **kwargs):
            return None

    cfg = {
        "auxiliary": {
            "compression": {
                "context_length": 1_000_000,
            },
        },
    }
    mock_client = MagicMock()
    mock_client.base_url = "http://custom-endpoint:8080/v1"
    mock_client.api_key = "sk-custom"

    with (
        patch("hermes_cli.config.load_config", return_value=cfg), patch("hermes_cli.config.load_config_readonly", return_value=cfg),
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent.ContextCompressor", new=_StubCompressor),
        patch("agent.auxiliary_client.get_text_auxiliary_client", return_value=(mock_client, "custom/big-model")),
        patch("agent.model_metadata.get_model_context_length", return_value=1_000_000) as mock_ctx_len,
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert agent._aux_compression_context_length_config == 1_000_000
    mock_ctx_len.assert_called_once_with(
        "custom/big-model",
        base_url="http://custom-endpoint:8080/v1",
        api_key="sk-custom",
        config_context_length=1_000_000,
        provider="",
        custom_providers=[],
    )


@patch("agent.auxiliary_client.get_text_auxiliary_client")
def test_warns_when_no_auxiliary_provider(mock_get_client):
    """Warning emitted when no auxiliary provider is configured."""
    agent = _make_agent()
    mock_get_client.return_value = (None, None)

    messages = []
    agent._emit_status = lambda msg: messages.append(msg)

    agent._check_compression_model_feasibility()

    assert len(messages) == 1
    assert "No auxiliary LLM provider" in messages[0]
    assert agent._compression_warning is not None


@patch("agent.auxiliary_client.get_text_auxiliary_client", return_value=(None, None))
def test_exhausted_codex_oauth_warning_reports_actual_state_and_replays(
    mock_get_client, tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes"))
    monkeypatch.setattr("hermes_cli.auth._import_codex_cli_tokens", lambda: None)
    _write_exhausted_codex_auth(tmp_path, time.time() + 600)
    agent = _make_agent()
    emitted = []
    agent._emit_status = emitted.append

    with patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("openai-codex", "gpt-5.4", None, None, None),
    ):
        agent._check_compression_model_feasibility()

    assert mock_get_client.called
    assert len(emitted) == 1
    warning = emitted[0]
    assert warning == agent._compression_warning
    assert "OpenAI Codex OAuth credentials are exhausted" in warning
    assert "HTTP 429" in warning
    assert "usage_limit_reached" in warning
    assert "API_KEY" not in warning
    assert "secret-codex" not in warning
    assert "continue without LLM summaries" in warning

    replayed = []
    agent.status_callback = lambda event, message: replayed.append((event, message))
    agent._replay_compression_warning()
    assert replayed == [("lifecycle", warning)]


def test_no_unavailable_warning_when_configured_fallback_chain_resolves():
    """Primary compression provider can be down if configured fallback works."""
    agent = _make_agent(main_context=200_000, threshold_percent=0.50)
    real_client = MagicMock()
    real_client.base_url = "https://chatgpt.com/backend-api/codex"
    real_client.api_key = "codex-oauth-token"
    fallback_client = CodexAuxiliaryClient(real_client, "gpt-5.4-mini")

    messages = []
    agent._emit_status = lambda msg: messages.append(msg)

    with patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("ollama-cloud", "deepseek-v4-flash:cloud", None, None, None),
    ), patch(
        "agent.auxiliary_client._get_auxiliary_task_config",
        return_value={
            "fallback_chain": [
                {"provider": "openai-codex", "model": "gpt-5.4-mini"}
            ]
        },
    ), patch(
        "agent.auxiliary_client._try_configured_fallback_chain",
        wraps=auxiliary_client._try_configured_fallback_chain,
    ) as mock_fallback, patch(
        "agent.auxiliary_client.resolve_provider_client",
        side_effect=[(None, None), (fallback_client, "gpt-5.4-mini")],
    ), patch(
        "agent.model_metadata.get_model_context_length",
        return_value=200_000,
    ) as mock_ctx_len:
        agent._check_compression_model_feasibility()

    assert messages == []
    assert agent._compression_warning is None
    mock_fallback.assert_called_once_with(
        "compression", "ollama-cloud", reason="provider unavailable"
    )
    mock_ctx_len.assert_called_once()
    assert mock_ctx_len.call_args.args == ("gpt-5.4-mini",)
    assert mock_ctx_len.call_args.kwargs["provider"] == "openai-codex"










# ── Two-phase: __init__ + run_conversation replay ───────────────────


@patch("agent.model_metadata.get_model_context_length", return_value=80_000)
@patch("agent.auxiliary_client.get_text_auxiliary_client")
def test_warning_stored_for_gateway_replay(mock_get_client, mock_ctx_len):
    """__init__ stores the warning; _replay sends it through status_callback."""
    agent = _make_agent(main_context=200_000, threshold_percent=0.50)
    mock_client = MagicMock()
    mock_client.base_url = "https://openrouter.ai/api/v1"
    mock_client.api_key = "sk-aux"
    mock_get_client.return_value = (mock_client, "google/gemini-3-flash-preview")

    # Phase 1: __init__ — _emit_status prints (CLI) but callback is None
    vprint_messages = []
    agent._emit_status = lambda msg: vprint_messages.append(msg)
    agent._check_compression_model_feasibility()

    assert len(vprint_messages) == 1  # CLI got it
    assert agent._compression_warning is not None  # stored for replay

    # Phase 2: gateway wires callback post-init, then run_conversation replays
    callback_events = []
    agent.status_callback = lambda ev, msg: callback_events.append((ev, msg))
    agent._replay_compression_warning()

    assert any(
        ev == "lifecycle" and "Auto-lowered" in msg
        for ev, msg in callback_events
    )


@patch("agent.model_metadata.get_model_context_length", return_value=200_000)
@patch("agent.auxiliary_client.get_text_auxiliary_client")
def test_no_replay_when_no_warning(mock_get_client, mock_ctx_len):
    """_replay_compression_warning is a no-op when there's no stored warning."""
    agent = _make_agent(main_context=200_000, threshold_percent=0.50)
    mock_client = MagicMock()
    mock_client.base_url = "https://openrouter.ai/api/v1"
    mock_client.api_key = "sk-aux"
    mock_get_client.return_value = (mock_client, "big-model")

    agent._emit_status = lambda msg: None
    agent._check_compression_model_feasibility()

    assert agent._compression_warning is None

    callback_events = []
    agent.status_callback = lambda ev, msg: callback_events.append((ev, msg))
    agent._replay_compression_warning()

    assert len(callback_events) == 0






# ── #67422: threshold suggestion must survive the small-context floor ────────




@patch("agent.model_metadata.get_model_context_length", return_value=300_000)
@patch("agent.auxiliary_client.get_text_auxiliary_client")
def test_threshold_suggestion_kept_for_large_context_main(mock_get_client, mock_ctx_len):
    """Main window >= 512K has no floor — any suggestion is honored, so the
    `threshold:` option stays even below 75%."""
    agent = _make_agent(main_context=1_000_000, threshold_percent=0.50)
    # threshold = 500,000 — aux has 300,000
    mock_client = MagicMock()
    mock_client.base_url = "https://openrouter.ai/api/v1"
    mock_client.api_key = "sk-aux"
    mock_get_client.return_value = (mock_client, "google/gemini-3-flash-preview")

    messages = []
    agent._emit_status = lambda msg: messages.append(msg)

    agent._check_compression_model_feasibility()

    assert len(messages) == 1
    assert "threshold: 0.30" in messages[0]




