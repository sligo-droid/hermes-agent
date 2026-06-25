"""Tests for the `hermes proxy` subcommand and its upstream adapters."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli.proxy.adapters import ADAPTERS, get_adapter
from hermes_cli.proxy.adapters.base import UpstreamAdapter, UpstreamCredential
from hermes_cli.proxy.adapters.nous_portal import NousPortalAdapter
from hermes_cli.proxy.adapters.openai_codex import OpenAICodexAdapter
from hermes_cli.proxy.adapters.xai import XAIGrokAdapter


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------


def test_registry_lists_supported_providers():
    assert "nous" in ADAPTERS
    assert "openai-codex" in ADAPTERS


def test_registry_lists_xai():
    assert "xai" in ADAPTERS


def test_get_adapter_returns_instance():
    adapter = get_adapter("nous")
    assert isinstance(adapter, NousPortalAdapter)
    assert isinstance(adapter, UpstreamAdapter)


def test_get_adapter_returns_xai_instance():
    adapter = get_adapter("xai")
    assert isinstance(adapter, XAIGrokAdapter)
    assert isinstance(adapter, UpstreamAdapter)


def test_get_adapter_case_insensitive():
    assert isinstance(get_adapter("NOUS"), NousPortalAdapter)
    assert isinstance(get_adapter("  Nous  "), NousPortalAdapter)
    assert isinstance(get_adapter("OPENAI-CODEX"), OpenAICodexAdapter)
    assert isinstance(get_adapter("XAI"), XAIGrokAdapter)


def test_get_adapter_unknown_provider_raises():
    with pytest.raises(ValueError, match="anthropic"):
        get_adapter("anthropic")  # not yet implemented


# ---------------------------------------------------------------------------
# OpenAICodexAdapter
# ---------------------------------------------------------------------------


def test_codex_adapter_metadata():
    adapter = OpenAICodexAdapter()
    assert adapter.name == "openai-codex"
    assert adapter.display_name == "OpenAI Codex"
    assert "/chat/completions" in adapter.allowed_paths
    assert "/models" in adapter.allowed_paths


def test_codex_adapter_not_authenticated_when_credentials_missing(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.proxy.adapters.openai_codex.get_codex_auth_status",
        MagicMock(return_value={"logged_in": False}),
    )
    monkeypatch.setattr(
        "hermes_cli.proxy.adapters.openai_codex.resolve_codex_runtime_credentials",
        MagicMock(side_effect=RuntimeError("missing")),
    )
    assert not OpenAICodexAdapter().is_authenticated()


def test_codex_adapter_uses_pooled_credentials_before_legacy(monkeypatch):
    legacy_resolver = MagicMock(side_effect=RuntimeError("legacy missing"))
    monkeypatch.setattr(
        "hermes_cli.proxy.adapters.openai_codex.get_codex_auth_status",
        MagicMock(return_value={
            "logged_in": True,
            "api_key": "pool-token",
            "source": "pool:secondary",
        }),
    )
    monkeypatch.setattr(
        "hermes_cli.proxy.adapters.openai_codex.resolve_codex_runtime_credentials",
        legacy_resolver,
    )

    adapter = OpenAICodexAdapter()

    assert adapter.is_authenticated()
    cred = adapter.get_credential()
    assert cred.bearer == "pool-token"
    assert cred.base_url == "https://chatgpt.com/backend-api/codex"
    legacy_resolver.assert_not_called()


def test_codex_adapter_get_credential(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.proxy.adapters.openai_codex.get_codex_auth_status",
        MagicMock(return_value={"logged_in": False}),
    )
    monkeypatch.setattr(
        "hermes_cli.proxy.adapters.openai_codex.resolve_codex_runtime_credentials",
        MagicMock(return_value={
            "api_key": "codex-token",
            "base_url": "https://chatgpt.com/backend-api/codex",
        }),
    )
    cred = OpenAICodexAdapter().get_credential()
    assert cred.bearer == "codex-token"
    assert cred.base_url == "https://chatgpt.com/backend-api/codex"


def test_codex_adapter_response_format_conversion():
    fmt = OpenAICodexAdapter._responses_text_format({
        "type": "json_schema",
        "json_schema": {
            "name": "Thing",
            "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            "strict": True,
        },
    })
    assert fmt == {
        "type": "json_schema",
        "name": "Thing",
        "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
        "strict": True,
    }


def test_codex_adapter_json_word_detector_requires_standalone_word():
    assert not OpenAICodexAdapter._messages_include_json_word([
        {"role": "user", "content": "Please jsonify the answer."}
    ])
    assert OpenAICodexAdapter._messages_include_json_word([
        {"role": "user", "content": "Return application/JSON."}
    ])
    assert OpenAICodexAdapter._messages_include_json_word([
        {
            "role": "user",
            "content": [{"type": "text", "text": "Return a JSON-only object."}],
        }
    ])


@pytest.mark.parametrize(
    "response_format",
    [
        {"type": "json_object"},
        {
            "type": "json_schema",
            "json_schema": {
                "name": "Thing",
                "schema": {"type": "object", "properties": {"x": {"type": "string"}}},
            },
        },
    ],
)
def test_codex_adapter_adds_json_mode_instruction_without_json_word(
    monkeypatch,
    response_format,
):
    captured: dict[str, Any] = {}

    class FakeRequest:
        async def json(self):
            return {
                "model": "gpt-5.4-mini",
                "messages": [{"role": "user", "content": "Return an object with x."}],
                "response_format": response_format,
            }

    class FakeResponse:
        def model_dump(self):
            return {"id": "resp-json", "created_at": 123, "usage": {}}

    async def fake_run(responses_payload, cred):
        captured["payload"] = responses_payload
        return FakeResponse()

    monkeypatch.setattr(
        OpenAICodexAdapter,
        "get_credential",
        lambda self: UpstreamCredential(
            bearer="token",
            base_url="https://example.test/backend-api/codex",
        ),
    )
    monkeypatch.setattr(
        OpenAICodexAdapter,
        "_run_responses_stream_with_retry",
        staticmethod(fake_run),
    )
    monkeypatch.setattr(
        "hermes_cli.proxy.adapters.openai_codex._normalize_codex_response",
        lambda _response: (SimpleNamespace(content='{"x":"ok"}', tool_calls=[]), "stop"),
    )

    response = asyncio.run(OpenAICodexAdapter()._handle_chat_completions(FakeRequest()))
    body = json.loads(response.text)

    assert response.status == 200
    assert body["choices"][0]["message"]["content"] == '{"x":"ok"}'
    assert captured["payload"]["instructions"] == "Respond with JSON."
    assert captured["payload"]["input"][0] == {
        "role": "user",
        "content": "Return an object with x.",
    }


def test_codex_adapter_allows_json_mode_with_multimodal_text_part(monkeypatch):
    captured: dict[str, Any] = {}

    class FakeRequest:
        async def json(self):
            return {
                "model": "gpt-5.4-mini",
                "messages": [
                    {"role": "system", "content": "Formatting rules."},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Return a JSON object with x."},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,ZmFrZQ=="},
                            },
                        ],
                    },
                ],
                "response_format": {"type": "json_object"},
            }

    class FakeResponse:
        def model_dump(self):
            return {"id": "resp-json", "created_at": 123, "usage": {}}

    async def fake_run(responses_payload, cred):
        captured["payload"] = responses_payload
        captured["cred"] = cred
        return FakeResponse()

    monkeypatch.setattr(
        OpenAICodexAdapter,
        "get_credential",
        lambda self: UpstreamCredential(
            bearer="token",
            base_url="https://example.test/backend-api/codex",
        ),
    )
    monkeypatch.setattr(
        OpenAICodexAdapter,
        "_run_responses_stream_with_retry",
        staticmethod(fake_run),
    )
    monkeypatch.setattr(
        "hermes_cli.proxy.adapters.openai_codex._normalize_codex_response",
        lambda _response: (SimpleNamespace(content='{"x":"ok"}', tool_calls=[]), "stop"),
    )

    response = asyncio.run(OpenAICodexAdapter()._handle_chat_completions(FakeRequest()))
    body = json.loads(response.text)

    assert response.status == 200
    assert body["choices"][0]["message"]["content"] == '{"x":"ok"}'
    assert captured["payload"]["text"] == {"format": {"type": "json_object"}}
    assert captured["payload"]["input"][0]["content"][0] == {
        "type": "input_text",
        "text": "Return a JSON object with x.",
    }


def test_codex_adapter_preserves_minimal_reasoning_effort():
    assert OpenAICodexAdapter._responses_reasoning("minimal") == {
        "effort": "minimal",
        "summary": "auto",
    }
    assert OpenAICodexAdapter._responses_reasoning(" low ") == {
        "effort": "low",
        "summary": "auto",
    }
    assert OpenAICodexAdapter._responses_reasoning("off") == {
        "effort": "none",
        "summary": "auto",
    }
    assert OpenAICodexAdapter._responses_reasoning("") is None


def test_codex_adapter_rejects_invalid_reasoning_effort():
    with pytest.raises(ValueError, match="Invalid reasoning_effort"):
        OpenAICodexAdapter._responses_reasoning("maximum")


def test_codex_adapter_resolves_speed_and_service_tier():
    assert OpenAICodexAdapter._responses_service_tier({"speed": "fast"}) == "priority"
    assert OpenAICodexAdapter._responses_service_tier({"speed": "normal"}) == "default"
    assert (
        OpenAICodexAdapter._responses_service_tier({
            "speed": "fast",
            "service_tier": "flex",
        })
        == "flex"
    )
    assert (
        OpenAICodexAdapter._responses_service_tier({
            "speed": "fast",
            "service_tier": "",
        })
        == "priority"
    )


def test_codex_adapter_rejects_invalid_speed():
    with pytest.raises(ValueError, match="Invalid speed"):
        OpenAICodexAdapter._responses_service_tier({"speed": "urgent"})


@pytest.mark.parametrize(
    "reasoning_effort",
    ["none", "minimal", "low", "medium", "high", "xhigh"],
)
def test_codex_adapter_chat_completion_forwards_model_reasoning_and_tier(
    monkeypatch,
    reasoning_effort,
):
    captured: dict[str, Any] = {}

    class FakeRequest:
        async def json(self):
            return {
                "model": "gpt-5.4-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "reasoning_effort": reasoning_effort,
                "speed": "fast",
            }

    class FakeResponse:
        def model_dump(self):
            return {
                "id": "resp-test",
                "created_at": 123,
                "usage": {"input_tokens": 3, "output_tokens": 2},
            }

    async def fake_run(responses_payload, cred):
        captured["payload"] = responses_payload
        captured["cred"] = cred
        return FakeResponse()

    monkeypatch.setattr(
        OpenAICodexAdapter,
        "get_credential",
        lambda self: UpstreamCredential(
            bearer="token",
            base_url="https://example.test/backend-api/codex",
        ),
    )
    monkeypatch.setattr(
        OpenAICodexAdapter,
        "_run_responses_stream_with_retry",
        staticmethod(fake_run),
    )
    monkeypatch.setattr(
        "hermes_cli.proxy.adapters.openai_codex._normalize_codex_response",
        lambda _response: (SimpleNamespace(content="ok", tool_calls=[]), "stop"),
    )

    response = asyncio.run(OpenAICodexAdapter()._handle_chat_completions(FakeRequest()))
    body = json.loads(response.text)

    assert response.status == 200
    assert body["model"] == "gpt-5.4-mini"
    assert captured["payload"]["model"] == "gpt-5.4-mini"
    assert captured["payload"]["reasoning"] == {
        "effort": reasoning_effort,
        "summary": "auto",
    }
    assert captured["payload"]["service_tier"] == "priority"


def test_codex_adapter_cools_down_failed_model_and_routes_to_fallback(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(
        "HERMES_CODEX_PROXY_MODEL_COOLDOWNS",
        "gpt-5.3-codex-spark:gpt-5.4-mini:3600",
    )
    calls: list[str] = []

    class FakeRequest:
        async def json(self):
            return {
                "model": "gpt-5.3-codex-spark",
                "messages": [{"role": "user", "content": "hello"}],
            }

    class FakeResponse:
        def model_dump(self):
            return {"id": "resp-test", "created_at": 123, "usage": {}}

    async def fake_run(responses_payload, cred):
        calls.append(responses_payload["model"])
        if responses_payload["model"] == "gpt-5.3-codex-spark":
            raise RuntimeError("spark quota exhausted")
        return FakeResponse()

    monkeypatch.setattr(
        OpenAICodexAdapter,
        "get_credential",
        lambda self: UpstreamCredential(
            bearer="token",
            base_url="https://example.test/backend-api/codex",
        ),
    )
    monkeypatch.setattr(
        OpenAICodexAdapter,
        "_run_responses_stream_with_retry",
        staticmethod(fake_run),
    )
    monkeypatch.setattr(
        "hermes_cli.proxy.adapters.openai_codex._normalize_codex_response",
        lambda _response: (SimpleNamespace(content="ok", tool_calls=[]), "stop"),
    )

    first = asyncio.run(OpenAICodexAdapter()._handle_chat_completions(FakeRequest()))  # type: ignore[arg-type]
    assert first.status == 502

    state_file = tmp_path / "state" / "codex-proxy-model-cooldowns.json"
    state = json.loads(state_file.read_text())
    assert state["models"]["gpt-5.3-codex-spark"]["fallback_model"] == "gpt-5.4-mini"

    second = asyncio.run(OpenAICodexAdapter()._handle_chat_completions(FakeRequest()))  # type: ignore[arg-type]
    body = json.loads(second.text or "{}")

    assert second.status == 200
    assert calls == ["gpt-5.3-codex-spark", "gpt-5.4-mini"]
    assert body["model"] == "gpt-5.4-mini"


def test_codex_adapter_expired_model_cooldown_is_pruned(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv(
        "HERMES_CODEX_PROXY_MODEL_COOLDOWNS",
        "gpt-5.3-codex-spark:gpt-5.4-mini:3600",
    )
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_file = state_dir / "codex-proxy-model-cooldowns.json"
    state_file.write_text(json.dumps({
        "models": {
            "gpt-5.3-codex-spark": {
                "fallback_model": "gpt-5.4-mini",
                "expires_at": 1,
            }
        }
    }))

    assert (
        OpenAICodexAdapter._effective_model_for_cooldown("gpt-5.3-codex-spark")
        == "gpt-5.3-codex-spark"
    )
    assert json.loads(state_file.read_text())["models"] == {}


def test_codex_adapter_serializes_streaming_chat_chunks():
    chunks = OpenAICodexAdapter._chat_completion_stream_chunks({
        "id": "chatcmpl-test",
        "created": 123,
        "model": "gpt-5.3-codex",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "hello"},
            "finish_reason": "stop",
        }],
    })

    assert chunks[-1] == b"data: [DONE]\n\n"
    decoded = [chunk.decode("utf-8") for chunk in chunks]
    assert '"object":"chat.completion.chunk"' in decoded[0]
    assert '"role":"assistant"' in decoded[0]
    assert '"content":"hello"' in decoded[1]
    assert '"finish_reason":"stop"' in decoded[-2]


def test_codex_stream_synthesizes_collected_output_after_sdk_null_output(monkeypatch):
    output_item = SimpleNamespace(
        type="message",
        role="assistant",
        status="completed",
        content=[SimpleNamespace(type="output_text", text="hello")],
    )

    class FakeStream:
        def __init__(self):
            self._events = [
                SimpleNamespace(type="response.output_text.delta", delta="hello"),
                SimpleNamespace(type="response.output_item.done", item=output_item),
            ]
            self._index = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, exc_tb):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._index < len(self._events):
                event = self._events[self._index]
                self._index += 1
                return event
            raise TypeError("'NoneType' object is not iterable")

        async def get_final_response(self):  # pragma: no cover - regression guard
            raise AssertionError("SDK failure should bypass final response parsing")

    class FakeResponses:
        def stream(self, **kwargs):
            return FakeStream()

    class FakeClient:
        instances = []

        def __init__(self, **kwargs):
            self.responses = FakeResponses()
            self.closed = False
            self.kwargs = kwargs
            self.instances.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(
        "hermes_cli.proxy.adapters.openai_codex.AsyncOpenAI",
        FakeClient,
    )

    result = asyncio.run(OpenAICodexAdapter._run_responses_stream(
        {"model": "gpt-5.5", "input": [{"role": "user", "content": "hi"}], "store": False},
        UpstreamCredential(bearer="token", base_url="https://example.test/v1"),
    ))

    assert result.status == "completed"
    assert result.output == [output_item]
    assert result.output_text == "hello"
    assert FakeClient.instances[0].closed is True


def test_codex_stream_wraps_immutable_empty_final_response(monkeypatch):
    output_item = SimpleNamespace(
        type="message",
        role="assistant",
        status="completed",
        content=[SimpleNamespace(type="output_text", text="hello")],
    )

    class ImmutableFinalResponse:
        def __init__(self):
            self.status = "completed"
            self.output = []
            self.usage = {"input_tokens": 3, "output_tokens": 1}

        @property
        def output_text(self):
            return ""

        def model_dump(self, *args, **kwargs):
            return {"usage": self.usage, "output": [], "output_text": ""}

    class FakeStream:
        def __init__(self):
            self._events = [
                SimpleNamespace(type="response.output_text.delta", delta="hello"),
                SimpleNamespace(type="response.output_item.done", item=output_item),
            ]
            self._index = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, exc_tb):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._index < len(self._events):
                event = self._events[self._index]
                self._index += 1
                return event
            raise StopAsyncIteration

        async def get_final_response(self):
            return ImmutableFinalResponse()

    class FakeResponses:
        def stream(self, **kwargs):
            return FakeStream()

    class FakeClient:
        instances = []

        def __init__(self, **kwargs):
            self.responses = FakeResponses()
            self.closed = False
            self.instances.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(
        "hermes_cli.proxy.adapters.openai_codex.AsyncOpenAI",
        FakeClient,
    )

    result = asyncio.run(OpenAICodexAdapter._run_responses_stream(
        {"model": "gpt-5.5", "input": [{"role": "user", "content": "hi"}], "store": False},
        UpstreamCredential(bearer="token", base_url="https://example.test/v1"),
    ))

    assert result.status == "completed"
    assert result.output == [output_item]
    assert result.output_text == "hello"
    assert result.model_dump()["usage"] == {"input_tokens": 3, "output_tokens": 1}
    assert FakeClient.instances[0].closed is True


def test_codex_stream_retries_null_output_without_collected_events(monkeypatch):
    output_item = SimpleNamespace(
        type="message",
        role="assistant",
        status="completed",
        content=[SimpleNamespace(type="output_text", text="after retry")],
    )
    calls = {"stream": 0}

    class FailingStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, exc_tb):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise TypeError("'NoneType' object is not iterable")

        async def get_final_response(self):  # pragma: no cover - iteration fails first
            raise AssertionError("first stream should fail before final response")

    class GoodStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, exc_tb):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def get_final_response(self):
            return SimpleNamespace(
                status="completed",
                output=[output_item],
                output_text="after retry",
            )

    class FakeResponses:
        def stream(self, **kwargs):
            calls["stream"] += 1
            if calls["stream"] == 1:
                return FailingStream()
            return GoodStream()

    class FakeClient:
        instances = []

        def __init__(self, **kwargs):
            self.responses = FakeResponses()
            self.closed = False
            self.instances.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(
        "hermes_cli.proxy.adapters.openai_codex.AsyncOpenAI",
        FakeClient,
    )

    result = asyncio.run(OpenAICodexAdapter._run_responses_stream_with_retry(
        {"model": "gpt-5.5", "input": [{"role": "user", "content": "hi"}], "store": False},
        UpstreamCredential(bearer="token", base_url="https://example.test/v1"),
    ))

    assert calls["stream"] == 2
    assert result.status == "completed"
    assert result.output == [output_item]
    assert result.output_text == "after retry"
    assert [client.closed for client in FakeClient.instances] == [True, True]


# ---------------------------------------------------------------------------
# NousPortalAdapter
# ---------------------------------------------------------------------------


def _write_auth_store(hermes_home: Path, nous_state: Dict[str, Any]) -> Path:
    """Write an auth.json with the given nous state into a hermetic HERMES_HOME."""
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 1,
        "providers": {"nous": nous_state},
    }))
    return auth_path


def test_nous_adapter_metadata():
    adapter = NousPortalAdapter()
    assert adapter.name == "nous"
    assert adapter.display_name == "Nous Portal"
    assert "/chat/completions" in adapter.allowed_paths
    assert "/embeddings" in adapter.allowed_paths
    assert "/completions" in adapter.allowed_paths
    assert "/models" in adapter.allowed_paths


def test_nous_adapter_not_authenticated_when_no_auth_file(tmp_path, monkeypatch):
    # HERMES_HOME is already set by conftest, but make doubly sure
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = NousPortalAdapter()
    assert not adapter.is_authenticated()


def test_nous_adapter_not_authenticated_when_provider_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {},
    }))
    assert not NousPortalAdapter().is_authenticated()


def test_nous_adapter_authenticated_with_agent_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "agent_key": "ov-test-key",
        "agent_key_expires_at": "2099-01-01T00:00:00Z",
        "inference_base_url": "https://inference-api.nousresearch.com/v1",
    })
    assert NousPortalAdapter().is_authenticated()


def test_nous_adapter_authenticated_with_refresh_token_only(tmp_path, monkeypatch):
    """If access_token+refresh_token exist but no agent_key yet, we can still refresh."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
    })
    assert NousPortalAdapter().is_authenticated()


def test_nous_adapter_get_credential_uses_runtime_resolver(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
        "client_id": "hermes-cli",
        "portal_base_url": "https://portal.nousresearch.com",
        "inference_base_url": "https://inference-api.nousresearch.com/v1",
    })

    refreshed_state = {
        "api_key": "jwt-bearer",
        "base_url": "https://inference-api.nousresearch.com/v1",
        "expires_at": "2099-01-01T00:00:00Z",
    }

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
        return_value=refreshed_state,
    ) as mock_resolve:
        adapter = NousPortalAdapter()
        cred = adapter.get_credential()

    mock_resolve.assert_called_once()
    assert cred.bearer == "jwt-bearer"
    assert cred.base_url == "https://inference-api.nousresearch.com/v1"
    assert cred.expires_at == "2099-01-01T00:00:00Z"
    assert cred.token_type == "Bearer"


def test_nous_adapter_retry_credential_force_refreshes_on_jwt_401(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "jwt-access",
        "refresh_token": "refresh-tok",
        "client_id": "hermes-cli",
        "portal_base_url": "https://portal.nousresearch.com",
        "inference_base_url": "https://inference-api.nousresearch.com/v1",
        "agent_key": "jwt-access",
    })
    refreshed_state = {
        "api_key": "fresh-jwt-bearer",
        "base_url": "https://inference-api.nousresearch.com/v1",
        "expires_at": "2099-01-01T00:00:00Z",
    }

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
        return_value=refreshed_state,
    ) as mock_resolve:
        adapter = NousPortalAdapter()
        cred = adapter.get_retry_credential(
            failed_credential=UpstreamCredential(
                bearer="header.jwt.signature",
                base_url="https://inference-api.nousresearch.com/v1",
            ),
            status_code=401,
        )

    assert cred is not None
    assert cred.bearer == "fresh-jwt-bearer"
    assert mock_resolve.call_args.kwargs["force_refresh"] is True


def test_nous_adapter_retry_credential_skips_non_401(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "jwt-access",
        "refresh_token": "refresh-tok",
        "agent_key": "opaque-bearer",
    })

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
    ) as mock_resolve:
        adapter = NousPortalAdapter()
        cred = adapter.get_retry_credential(
            failed_credential=UpstreamCredential(
                bearer="opaque-bearer",
                base_url="https://inference-api.nousresearch.com/v1",
            ),
            status_code=403,
        )

    assert cred is None
    mock_resolve.assert_not_called()


def test_nous_adapter_get_credential_raises_when_not_logged_in(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = NousPortalAdapter()
    with pytest.raises(RuntimeError, match="hermes auth add nous"):
        adapter.get_credential()


def test_nous_adapter_get_credential_raises_on_refresh_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
    })

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
        side_effect=RuntimeError("Refresh session has been revoked"),
    ):
        adapter = NousPortalAdapter()
        with pytest.raises(RuntimeError, match="Refresh session has been revoked"):
            adapter.get_credential()


def test_nous_adapter_quarantines_terminal_refresh_failure(tmp_path, monkeypatch):
    from hermes_cli.auth import AuthError
    from agent.credential_pool import load_pool

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
        "agent_key": "stale-agent-key",
    })
    assert load_pool("nous").select() is not None

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
        side_effect=AuthError(
            "Refresh session has been revoked",
            provider="nous",
            code="invalid_grant",
            relogin_required=True,
        ),
    ):
        adapter = NousPortalAdapter()
        with pytest.raises(RuntimeError, match="Refresh session has been revoked"):
            adapter.get_credential()

    stored = json.loads((tmp_path / "auth.json").read_text())
    nous_state = stored["providers"]["nous"]
    assert not nous_state.get("refresh_token")
    assert not nous_state.get("access_token")
    assert not nous_state.get("agent_key")
    assert nous_state["last_auth_error"]["code"] == "invalid_grant"
    assert stored.get("credential_pool", {}).get("nous") == []


def test_nous_adapter_get_credential_raises_when_no_jwt_returned(tmp_path, monkeypatch):
    """If the refresh helper succeeds but produces no JWT, we surface a clear error."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "access-tok",
        "refresh_token": "refresh-tok",
    })

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
        return_value={"access_token": "a", "refresh_token": "r"},
    ):
        adapter = NousPortalAdapter()
        with pytest.raises(RuntimeError, match="did not return a usable inference JWT"):
            adapter.get_credential()


def test_nous_adapter_concurrent_refresh_serialized(tmp_path, monkeypatch):
    """Two parallel get_credential() calls must serialize through the lock."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_auth_store(tmp_path, {
        "access_token": "a", "refresh_token": "r",
    })

    call_log: list = []
    in_flight = threading.Event()
    overlap_detected = threading.Event()
    counter = [0]
    counter_lock = threading.Lock()

    def serializing_refresh(**kwargs):
        # If another thread is already inside refresh, the lock is broken.
        if in_flight.is_set():
            overlap_detected.set()
        in_flight.set()
        try:
            call_log.append(threading.current_thread().ident)
            # Simulate refresh latency so any race window is exposed.
            import time
            time.sleep(0.05)
            with counter_lock:
                counter[0] += 1
                idx = counter[0]
            return {
                "api_key": f"key-{idx}",
                "expires_at": "2099-01-01T00:00:00Z",
                "base_url": "https://inference-api.nousresearch.com/v1",
            }
        finally:
            in_flight.clear()

    adapter = NousPortalAdapter()
    results: list = []
    errors: list = []

    def worker():
        try:
            results.append(adapter.get_credential().bearer)
        except Exception as exc:  # pragma: no cover - shouldn't happen
            errors.append(exc)

    with patch(
        "hermes_cli.proxy.adapters.nous_portal.resolve_nous_runtime_credentials",
        side_effect=serializing_refresh,
    ):
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not errors, f"workers errored: {errors}"
    assert len(results) == 3
    assert len(call_log) == 3
    assert not overlap_detected.is_set(), "refresh calls overlapped — lock is broken"
    assert all(r.startswith("key-") for r in results)


# ---------------------------------------------------------------------------
# XAIGrokAdapter
# ---------------------------------------------------------------------------


def _write_xai_pool_entry(
    hermes_home: Path,
    *,
    access_token: str = "xai-access-token",
    refresh_token: str = "xai-refresh-token",
    base_url: str = "https://api.x.ai/v1",
    source: str = "manual:xai_pkce",
) -> Path:
    """Write an xai-oauth pool entry into a hermetic HERMES_HOME."""
    auth_path = hermes_home / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 1,
        "providers": {},
        "credential_pool": {
            "xai-oauth": [
                {
                    "id": "xai123",
                    "label": "xai-test",
                    "auth_type": "oauth",
                    "priority": 0,
                    "source": source,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "base_url": base_url,
                }
            ]
        },
    }))
    return auth_path


def test_xai_adapter_metadata():
    adapter = XAIGrokAdapter()
    assert adapter.name == "xai"
    assert adapter.display_name == "xAI Grok OAuth"
    assert "/responses" in adapter.allowed_paths
    assert "/chat/completions" in adapter.allowed_paths
    assert "/models" in adapter.allowed_paths


def test_xai_adapter_not_authenticated_when_no_pool_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(json.dumps({
        "version": 1,
        "providers": {},
        "credential_pool": {},
    }))
    assert not XAIGrokAdapter().is_authenticated()


def test_xai_adapter_authenticated_with_pool_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_xai_pool_entry(tmp_path)
    assert XAIGrokAdapter().is_authenticated()


def test_xai_adapter_get_credential_uses_oauth_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_xai_pool_entry(
        tmp_path,
        access_token="pool-access-token",
        base_url="https://api.x.ai/v1/",
    )

    cred = XAIGrokAdapter().get_credential()

    assert cred.bearer == "pool-access-token"
    assert cred.base_url == "https://api.x.ai/v1"
    assert cred.token_type == "Bearer"


def test_xai_adapter_get_credential_defaults_base_url(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_xai_pool_entry(tmp_path, base_url="")

    cred = XAIGrokAdapter().get_credential()

    assert cred.base_url == "https://api.x.ai/v1"


def test_xai_adapter_retry_refreshes_current_pool_entry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_xai_pool_entry(tmp_path, access_token="old-access-token")

    def fake_refresh(access_token, refresh_token, **kwargs):
        assert access_token == "old-access-token"
        assert refresh_token == "xai-refresh-token"
        return {
            "access_token": "new-access-token",
            "refresh_token": "new-refresh-token",
            "last_refresh": "2026-05-19T00:00:00Z",
        }

    monkeypatch.setattr("hermes_cli.auth.refresh_xai_oauth_pure", fake_refresh)

    adapter = XAIGrokAdapter()
    failed = adapter.get_credential()
    retry = adapter.get_retry_credential(
        failed_credential=failed,
        status_code=401,
    )

    assert retry is not None
    assert retry.bearer == "new-access-token"


def test_xai_adapter_retry_rotates_pool_entry_on_429(tmp_path, monkeypatch):
    """429 from xAI must rotate to the next pool entry, not attempt refresh.

    Pre-fix (#28932) ``get_retry_credential`` only fired on 401, so a 429
    rate-limit response flowed back to the client unchanged AND the
    rate-limited bearer stayed active for the next request — defeating
    the whole point of pool rotation.

    Post-fix: 429 lands on ``mark_exhausted_and_rotate`` (no refresh —
    that's irrelevant for rate limits), stamps the 1-hour cooldown
    via ``EXHAUSTED_TTL_429_SECONDS`` on the offending key, and
    returns the next available credential.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    # Two pool entries so rotation has somewhere to go.
    auth_path = tmp_path / "auth.json"
    auth_path.write_text(json.dumps({
        "version": 1,
        "providers": {},
        "credential_pool": {
            "xai-oauth": [
                {
                    "id": "xai-first",
                    "label": "xai-first",
                    "auth_type": "oauth",
                    "priority": 0,
                    "source": "manual:xai_pkce",
                    "access_token": "first-access-token",
                    "refresh_token": "first-refresh-token",
                    "base_url": "https://api.x.ai/v1",
                },
                {
                    "id": "xai-second",
                    "label": "xai-second",
                    "auth_type": "oauth",
                    "priority": 1,
                    "source": "manual:xai_pkce",
                    "access_token": "second-access-token",
                    "refresh_token": "second-refresh-token",
                    "base_url": "https://api.x.ai/v1",
                },
            ]
        },
    }))

    # Refresh must NOT be called on the 429 path — guard against
    # the fix accidentally trying to refresh-on-rate-limit.
    def _refresh_must_not_run(*args, **kwargs):
        raise AssertionError("refresh_xai_oauth_pure must not run on 429")

    monkeypatch.setattr("hermes_cli.auth.refresh_xai_oauth_pure", _refresh_must_not_run)

    adapter = XAIGrokAdapter()
    failed = adapter.get_credential()
    assert failed.bearer == "first-access-token", "starting bearer should be the first entry"

    retry = adapter.get_retry_credential(
        failed_credential=failed,
        status_code=429,
    )

    assert retry is not None, "429 must rotate to next pool entry"
    assert retry.bearer == "second-access-token", (
        f"expected rotation to second entry, got {retry.bearer!r}"
    )


def test_xai_adapter_retry_returns_none_on_429_when_pool_exhausted(tmp_path, monkeypatch):
    """Single-entry pool: 429 has nowhere to rotate to → return None
    so the 429 flows back to the client unchanged (existing behavior
    preserved)."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_xai_pool_entry(tmp_path)  # single entry

    def _refresh_must_not_run(*args, **kwargs):
        raise AssertionError("refresh_xai_oauth_pure must not run on 429")

    monkeypatch.setattr("hermes_cli.auth.refresh_xai_oauth_pure", _refresh_must_not_run)

    adapter = XAIGrokAdapter()
    failed = adapter.get_credential()
    retry = adapter.get_retry_credential(
        failed_credential=failed,
        status_code=429,
    )

    assert retry is None, (
        "single-entry pool: 429 must return None so the response "
        "flows back to the client unchanged"
    )


def test_xai_adapter_retry_returns_none_for_unrelated_status(tmp_path, monkeypatch):
    """Non-{401, 429} statuses must NOT trigger any retry — pool
    untouched, no refresh attempted, return None immediately."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_xai_pool_entry(tmp_path)

    def _refresh_must_not_run(*args, **kwargs):
        raise AssertionError("refresh_xai_oauth_pure must not run on non-retry status")

    monkeypatch.setattr("hermes_cli.auth.refresh_xai_oauth_pure", _refresh_must_not_run)

    adapter = XAIGrokAdapter()
    failed = adapter.get_credential()
    for status in (200, 400, 403, 500, 502, 503):
        retry = adapter.get_retry_credential(
            failed_credential=failed,
            status_code=status,
        )
        assert retry is None, (
            f"status {status} must not trigger retry, got {retry!r}"
        )


# ---------------------------------------------------------------------------
# Server: path filtering + forwarding
#
# We run the proxy AND a fake upstream as real aiohttp servers on ephemeral
# ports. Avoids pytest-aiohttp's fixtures (extra dependency for one test file).
# ---------------------------------------------------------------------------

aiohttp = pytest.importorskip("aiohttp")
from aiohttp import web  # noqa: E402

from hermes_cli.proxy.server import create_app  # noqa: E402


class FakeAdapter(UpstreamAdapter):
    """A test adapter that returns a fixed credential without touching disk."""

    def __init__(self, base_url: str, bearer: str = "test-bearer",
                 allowed=None, raise_on_credential=False,
                 retry_bearer: str | None = None):
        self._base_url = base_url
        self._bearer = bearer
        self._allowed = frozenset(allowed or ["/chat/completions"])
        self._raise = raise_on_credential
        self._retry_bearer = retry_bearer
        self.calls = 0
        self.retry_calls = 0

    @property
    def name(self): return "fake"

    @property
    def display_name(self): return "Fake Provider"

    @property
    def allowed_paths(self): return self._allowed

    def is_authenticated(self): return True

    def get_credential(self):
        self.calls += 1
        if self._raise:
            raise RuntimeError("simulated auth failure")
        return UpstreamCredential(
            bearer=self._bearer, base_url=self._base_url,
            expires_at="2099-01-01T00:00:00Z",
        )

    def get_retry_credential(self, *, failed_credential, status_code):
        _ = failed_credential
        self.retry_calls += 1
        if status_code != 401 or not self._retry_bearer:
            return None
        return UpstreamCredential(
            bearer=self._retry_bearer,
            base_url=self._base_url,
            expires_at="2099-01-01T00:00:00Z",
        )


async def _start_runner(app: "web.Application"):
    """Spin up an aiohttp app on an ephemeral localhost port. Returns (runner, base_url)."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    sockets = list(site._server.sockets)  # type: ignore[union-attr]
    port = sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def _build_fake_upstream(captured: Dict[str, Any]) -> "web.Application":
    async def echo(request):
        body = await request.read()
        captured["requests"].append({
            "method": request.method,
            "path": request.path,
            "auth": request.headers.get("Authorization"),
            "body": body.decode("utf-8") if body else "",
        })
        return web.json_response({"echoed": True, "path": request.path})

    async def sse(request):
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"},
        )
        await resp.prepare(request)
        for chunk in [b"data: hello\n\n", b"data: world\n\n", b"data: [DONE]\n\n"]:
            await resp.write(chunk)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_route("*", "/v1/chat/completions", echo)
    app.router.add_route("*", "/v1/embeddings", echo)
    app.router.add_route("*", "/v1/sse", sse)
    return app


def _build_retrying_fake_upstream(captured: Dict[str, Any]) -> "web.Application":
    async def maybe_unauthorized(request):
        body = await request.read()
        auth = request.headers.get("Authorization")
        captured["requests"].append({
            "method": request.method,
            "path": request.path,
            "auth": auth,
            "body": body.decode("utf-8") if body else "",
        })
        if auth == "Bearer jwt-bearer":
            return web.json_response({"error": "bad token"}, status=401)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/v1/chat/completions", maybe_unauthorized)
    return app


def test_server_forwards_chat_completions():
    async def run():
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))
        adapter = FakeAdapter(f"{upstream_base}/v1", bearer="real-portal-key")
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{proxy_base}/v1/chat/completions",
                    json={"model": "Hermes-4-70B",
                          "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer client-dummy-key"},
                ) as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["echoed"] is True

            assert len(captured["requests"]) == 1
            req = captured["requests"][0]
            assert req["auth"] == "Bearer real-portal-key"
            assert "Hermes-4-70B" in req["body"]
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(run())


def test_server_retries_once_with_adapter_retry_credential_on_401():
    async def run():
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(
            _build_retrying_fake_upstream(captured)
        )
        adapter = FakeAdapter(
            f"{upstream_base}/v1",
            bearer="jwt-bearer",
            retry_bearer="legacy-bearer",
        )
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{proxy_base}/v1/chat/completions",
                    json={"model": "Hermes-4-70B"},
                ) as resp:
                    assert resp.status == 200
                    data = await resp.json()
                    assert data["ok"] is True

            assert adapter.retry_calls == 1
            assert [req["auth"] for req in captured["requests"]] == [
                "Bearer jwt-bearer",
                "Bearer legacy-bearer",
            ]
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(run())


def test_server_rejects_disallowed_path():
    async def run():
        adapter = FakeAdapter("http://unused.example/v1", allowed=["/chat/completions"])
        runner, base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/v1/random/endpoint") as resp:
                    assert resp.status == 404
                    body = await resp.json()
                    assert body["error"]["type"] == "path_not_allowed"
                    assert "/chat/completions" in body["error"]["message"]
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_server_returns_401_when_adapter_fails():
    async def run():
        adapter = FakeAdapter("http://unused.example/v1", raise_on_credential=True)
        runner, base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(f"{base}/v1/chat/completions", json={}) as resp:
                    assert resp.status == 401
                    body = await resp.json()
                    assert body["error"]["type"] == "upstream_auth_failed"
                    assert "simulated auth failure" in body["error"]["message"]
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_server_health_endpoint():
    async def run():
        adapter = FakeAdapter("http://unused.example/v1")
        runner, base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{base}/health") as resp:
                    assert resp.status == 200
                    body = await resp.json()
                    assert body["status"] == "ok"
                    assert body["upstream"] == "Fake Provider"
                    assert body["authenticated"] is True
        finally:
            await runner.cleanup()

    asyncio.run(run())


def test_server_streams_sse():
    async def run():
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))
        adapter = FakeAdapter(f"{upstream_base}/v1", allowed=["/sse"])
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{proxy_base}/v1/sse") as resp:
                    assert resp.status == 200
                    chunks = []
                    async for chunk in resp.content.iter_any():
                        chunks.append(chunk)
                    full = b"".join(chunks)
                    assert b"data: hello" in full
                    assert b"data: [DONE]" in full
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(run())


def test_server_strips_client_auth_header():
    """The client's Authorization header MUST NOT reach the upstream."""
    async def run():
        captured: Dict[str, Any] = {"requests": []}
        upstream_runner, upstream_base = await _start_runner(_build_fake_upstream(captured))
        adapter = FakeAdapter(f"{upstream_base}/v1", bearer="ours")
        proxy_runner, proxy_base = await _start_runner(create_app(adapter))
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{proxy_base}/v1/chat/completions",
                    json={},
                    headers={"Authorization": "Bearer SHOULD_NOT_LEAK"},
                ) as resp:
                    await resp.read()
            assert captured["requests"][0]["auth"] == "Bearer ours"
            assert "SHOULD_NOT_LEAK" not in captured["requests"][0]["auth"]
        finally:
            await proxy_runner.cleanup()
            await upstream_runner.cleanup()

    asyncio.run(run())


# ---------------------------------------------------------------------------
# CLI handlers
# ---------------------------------------------------------------------------


def test_cmd_proxy_status_runs(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.proxy.cli import cmd_proxy_status

    args = MagicMock()
    rc = cmd_proxy_status(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "nous" in out
    assert "Nous Portal" in out
    assert "not logged in" in out


def test_cmd_proxy_providers_runs(capsys):
    from hermes_cli.proxy.cli import cmd_proxy_list_providers

    args = MagicMock()
    rc = cmd_proxy_list_providers(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "nous" in out
    assert "Nous Portal" in out


def test_cmd_proxy_start_refuses_unknown_provider(capsys):
    from hermes_cli.proxy.cli import cmd_proxy_start

    args = MagicMock()
    args.provider = "no-such-provider"
    args.host = None
    args.port = None
    rc = cmd_proxy_start(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "no-such-provider" in err


def test_cmd_proxy_start_refuses_when_unauthenticated(capsys, tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from hermes_cli.proxy.cli import cmd_proxy_start

    args = MagicMock()
    args.provider = "nous"
    args.host = None
    args.port = None
    rc = cmd_proxy_start(args)
    assert rc == 2
    err = capsys.readouterr().err
    assert "hermes auth add nous" in err
