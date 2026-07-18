import json
from types import SimpleNamespace

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
