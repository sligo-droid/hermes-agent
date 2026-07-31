import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.vision_assertions import (
    _resolve_visual_inspector_runtime,
    _resolve_visual_sweep_runtime,
    evaluate_screenshot_assertions,
    parse_vision_assertion_output,
    run_visual_sweep,
)


def test_visual_inspector_preflight_falls_back_through_configured_main_route(monkeypatch):
    seen = {}

    monkeypatch.setattr(
        "hermes_cli.opus_planner._anthropic_budget_preflight_error",
        lambda: "Opus extra usage exhausted",
    )

    def fake_runtime_provider(*, requested, target_model, **kwargs):
        seen.update(requested=requested, target_model=target_model)
        return {
            "provider": "cli-proxy-api",
            "model": target_model,
            "base_url": "http://proxy.example/v1",
            "api_key": "proxy-token",
            "api_mode": "codex_responses",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_runtime_provider,
    )

    runtime = _resolve_visual_inspector_runtime({})

    assert seen == {
        "requested": None,
        "target_model": "gpt-5.6-luna",
    }
    assert runtime["provider"] == "cli-proxy-api"
    assert runtime["model"] == "gpt-5.6-luna"
    assert runtime["fallback_used"] is True
    assert runtime["fallback_reason"] == "opus_preflight_unavailable"


def test_visual_routes_use_configured_main_provider(monkeypatch):
    seen = []

    monkeypatch.setattr(
        "hermes_cli.opus_planner._anthropic_budget_preflight_error",
        lambda: "",
    )

    def fake_runtime_provider(*, requested, target_model, **kwargs):
        seen.append((requested, target_model))
        return {
            "provider": "custom",
            "model": "configured-main-default",
            "base_url": "http://proxy.example/v1",
            "api_key": "proxy-token",
            "api_mode": "codex_responses",
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider",
        fake_runtime_provider,
    )

    sweep = _resolve_visual_sweep_runtime({})
    inspector = _resolve_visual_inspector_runtime({})

    assert seen == [
        (None, "gpt-5.6-luna"),
        (None, "claude-sonnet-5"),
    ]
    assert sweep["provider"] == "custom"
    assert sweep["model"] == "gpt-5.6-luna"
    assert inspector["provider"] == "custom"
    assert inspector["model"] == "claude-sonnet-5"


def test_parse_compact_vision_assertion_output():
    parsed = parse_vision_assertion_output(
        json.dumps(
            {
                "results": [
                    {
                        "id": "balanced-layout",
                        "status": "passed",
                        "confidence": "high",
                        "code": "appearance_satisfied",
                    }
                ]
            }
        ),
        expected_ids=["balanced-layout"],
    )

    assert parsed == {
        "status": "passed",
        "results": [
            {
                "id": "balanced-layout",
                "status": "passed",
                "confidence": "high",
                "code": "appearance_satisfied",
            }
        ],
    }


@pytest.mark.asyncio
async def test_visual_inspector_route_and_single_attempt_are_forwarded():
    seen = {}
    events = []

    async def call_llm(**kwargs):
        events.append("request")
        seen.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"results":[{"id":"appearance","status":"passed","confidence":"high"}]}'
                    )
                )
            ]
        )

    with patch(
        "agent.vision_assertions._resolve_visual_inspector_runtime",
        return_value={
            "provider": "anthropic",
            "model": "claude-opus-5",
            "base_url": "https://inspector.example/v1",
            "api_key": "inspector-credential",
            "api_mode": "anthropic_messages",
        },
    ):
        result = await evaluate_screenshot_assertions(
            [
                "data:image/png;base64,Zm9jdXNlZA==",
                "data:image/png;base64,Y29udGV4dA==",
            ],
            [{"id": "appearance", "expectation": "balanced layout"}],
            # A distinct orchestrator route must never reach the visual call.
            provider="custom",
            model="gpt-5.6-sol",
            base_url="https://orchestrator.example/v1",
            api_key="orchestrator-credential",
            api_mode="codex_responses",
            cfg={},
            execution_context={
                "target": {"description": "Issue Attention graph region"},
                "page": {
                    "state": "prepared",
                    "description": "State Brief page",
                },
                "viewport": {"description": "current desktop viewport"},
                "state": ["chart data loaded"],
            },
            call_llm=call_llm,
            on_provider_start=lambda: events.append("start"),
        )

    assert result["status"] == "passed"
    assert events == ["start", "request"]
    assert seen["provider"] == "anthropic"
    assert seen["model"] == "claude-opus-5"
    assert seen["base_url"] == "https://inspector.example/v1"
    assert seen["api_key"] == "inspector-credential"
    assert seen["api_mode"] == "anthropic_messages"
    assert seen["single_attempt"] is True
    assert seen["strict_vision_capability"] is True
    prompt = seen["messages"][0]["content"][0]["text"]
    assert "Issue Attention graph region" in prompt
    assert "State Brief page" in prompt
    assert "chart data loaded" in prompt
    assert len(
        [
            item
            for item in seen["messages"][0]["content"]
            if item["type"] == "image_url"
        ]
    ) == 2


@pytest.mark.asyncio
async def test_visual_sweep_accepts_multiple_images_in_one_provider_call():
    seen = {}
    starts = []

    async def call_llm(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="READY"))
            ]
        )

    with patch(
        "agent.vision_assertions._resolve_visual_sweep_runtime",
        return_value={
            "provider": "openrouter",
            "model": "moonshotai/kimi-k2.5",
            "base_url": "https://sweep.example/v1",
            "api_key": "sweep-credential",
            "api_mode": "openai_chat_completions",
        },
    ):
        ready = await run_visual_sweep(
            [
                "data:image/png;base64,Zm9jdXNlZA==",
                "data:image/png;base64,Y29udGV4dA==",
            ],
            call_llm=call_llm,
            on_provider_start=lambda: starts.append(True),
        )

    assert ready is True
    assert starts == [True]
    assert len(
        [
            item
            for item in seen["messages"][0]["content"]
            if item["type"] == "image_url"
        ]
    ) == 2


@pytest.mark.asyncio
async def test_strict_assertion_routes_once_to_known_vision_backend():
    calls = 0

    async def create(**_kwargs):
        nonlocal calls
        calls += 1
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"results":[{"id":"appearance","status":"passed","confidence":"high"}]}'
                    )
                )
            ]
        )

    async_client = SimpleNamespace(
        base_url="https://openrouter.ai/api/v1",
        chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
    )
    async_client.with_options = lambda **_kwargs: async_client
    fallback_client = SimpleNamespace()

    with patch(
        "agent.vision_assertions._resolve_visual_inspector_runtime",
        return_value={
            "provider": "anthropic",
            "model": "claude-opus-5",
            "base_url": "https://inspector.example/v1",
            "api_key": "inspector-credential",
            "api_mode": "anthropic_messages",
        },
    ), patch(
        "agent.auxiliary_client._resolve_task_provider_model",
        return_value=("auto", None, None, None, None),
    ), patch(
        "agent.auxiliary_client._read_main_provider", return_value="experimental",
    ), patch(
        "agent.auxiliary_client._read_main_model", return_value="new-multimodal",
    ), patch(
        "agent.vision_capabilities._lookup_models_dev_support", return_value=None,
    ), patch(
        "agent.image_routing._lookup_supports_vision", return_value=None,
    ), patch(
        "agent.auxiliary_client.resolve_provider_client",
    ) as main_resolver, patch(
        "agent.auxiliary_client._resolve_strict_vision_backend",
        side_effect=lambda provider, model=None: (
            (fallback_client, "known-vision-model")
            if provider == "openrouter"
            else (None, None)
        ),
    ), patch(
        "agent.auxiliary_client._to_async_client",
        return_value=(async_client, "known-vision-model"),
    ):
        result = await evaluate_screenshot_assertions(
            "data:image/png;base64,cG5n",
            [{"id": "appearance", "expectation": "balanced layout"}],
            provider="experimental",
            model="new-multimodal",
            cfg={},
        )

    assert result["status"] == "passed"
    assert calls == 1
    main_resolver.assert_not_called()


@pytest.mark.parametrize("raw", ["looks good", "{}", "{broken", "```json\n{}\n```"])
def test_invalid_or_prose_only_output_is_uncertain(raw):
    parsed = parse_vision_assertion_output(raw, expected_ids=["balanced-layout"])

    assert parsed["status"] == "uncertain"
    assert parsed["results"] == [
        {
            "id": "balanced-layout",
            "status": "uncertain",
            "code": "invalid_vision_output",
        }
    ]


def test_failed_visual_review_keeps_one_bounded_correction():
    parsed = parse_vision_assertion_output(
        json.dumps(
            {
                "results": [
                    {
                        "id": "balanced-layout",
                        "status": "failed",
                        "confidence": "high",
                        "correction": "Reduce the card gutter and align the primary action to the title baseline.",
                    }
                ]
            }
        ),
        expected_ids=["balanced-layout"],
    )

    assert parsed["status"] == "failed"
    assert parsed["results"][0]["correction"] == (
        "Reduce the card gutter and align the primary action to the title baseline."
    )


def test_unexpected_or_missing_ids_cannot_pass():
    parsed = parse_vision_assertion_output(
        '{"results":[{"id":"other","status":"passed","code":"ok"}]}',
        expected_ids=["balanced-layout"],
    )

    assert parsed["status"] == "uncertain"
