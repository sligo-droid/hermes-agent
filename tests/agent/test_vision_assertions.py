import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.vision_assertions import (
    evaluate_screenshot_assertions,
    parse_vision_assertion_output,
)


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
async def test_active_custom_route_and_single_attempt_are_forwarded():
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

    result = await evaluate_screenshot_assertions(
        "data:image/png;base64,cG5n",
        [{"id": "appearance", "expectation": "balanced layout"}],
        provider="openrouter",
        model="anthropic/claude-opus-4.6",
        base_url="https://runtime.example/v1",
        api_key="runtime-credential",
        api_mode="codex_responses",
        cfg={"model": {"supports_vision": True}},
        call_llm=call_llm,
        on_provider_start=lambda: events.append("start"),
    )

    assert result["status"] == "passed"
    assert events == ["start", "request"]
    assert seen["base_url"] == "https://runtime.example/v1"
    assert seen["api_key"] == "runtime-credential"
    assert seen["api_mode"] == "codex_responses"
    assert seen["single_attempt"] is True
    assert seen["strict_vision_capability"] is True


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


def test_unexpected_or_missing_ids_cannot_pass():
    parsed = parse_vision_assertion_output(
        '{"results":[{"id":"other","status":"passed","code":"ok"}]}',
        expected_ids=["balanced-layout"],
    )

    assert parsed["status"] == "uncertain"
