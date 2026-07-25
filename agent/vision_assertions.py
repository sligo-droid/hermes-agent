"""Compact screenshot-appearance assertion evaluation."""

from __future__ import annotations

import json
import re
from typing import Any, Awaitable, Callable, Optional

from agent.visual_assertions import aggregate_assertion_results, sanitize_assertion_result


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL | re.IGNORECASE)


def _uncertain_results(expected_ids: list[str], code: str) -> dict[str, Any]:
    return {
        "status": "uncertain",
        "results": [
            {"id": assertion_id, "status": "uncertain", "code": code}
            for assertion_id in expected_ids[:6]
        ],
    }


def parse_vision_assertion_output(
    value: Any,
    *,
    expected_ids: list[str],
) -> dict[str, Any]:
    """Parse strict compact JSON; malformed/prose output is never passing."""

    text = str(value or "").strip()
    fence = _JSON_FENCE_RE.fullmatch(text)
    if fence:
        text = fence.group(1).strip()
    try:
        raw = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        return _uncertain_results(expected_ids, "invalid_vision_output")
    raw_results = raw.get("results") if isinstance(raw, dict) else None
    if not isinstance(raw_results, list):
        return _uncertain_results(expected_ids, "invalid_vision_output")
    host_results = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").strip().lower()
        code = {
            "passed": "appearance_satisfied",
            "failed": "appearance_mismatch",
            "uncertain": "appearance_uncertain",
        }.get(status, "invalid_assertion_code")
        host_results.append({**item, "code": code})
    parsed = [sanitize_assertion_result(item) for item in host_results]
    results = [item for item in parsed if item is not None]
    expected = expected_ids[:6]
    if len(results) != len(expected) or {item["id"] for item in results} != set(expected):
        return _uncertain_results(expected, "invalid_vision_output")
    return aggregate_assertion_results(results)


def _assertion_prompt(assertions: list[dict[str, Any]]) -> str:
    compact = [
        {"id": item["id"], "expectation": item["expectation"]}
        for item in assertions[:6]
    ]
    return (
        "Evaluate only the listed visual appearance assertions against the image. "
        "Return JSON only with exactly this shape: "
        '{"results":[{"id":"...","status":"passed|failed|uncertain",'
        '"confidence":"high|medium|low"}]}. '
        "Do not describe the page, quote visible text, include URLs, selectors, or add prose. "
        f"Assertions: {json.dumps(compact, ensure_ascii=True, separators=(',', ':'))}"
    )


def _resolve_visual_inspector_runtime(cfg: Optional[dict[str, Any]]) -> dict[str, str]:
    """Resolve the dedicated model route used only for screenshot judgement."""

    from hermes_cli.model_tiers import resolve_model_tier
    from hermes_cli.runtime_provider import resolve_runtime_provider

    tier = resolve_model_tier(cfg, "visual_inspector")
    if tier is None or not tier.provider:
        raise RuntimeError("visual_inspector model tier is unavailable")
    runtime = resolve_runtime_provider(
        requested=tier.provider,
        target_model=tier.model,
    )
    return {
        "provider": str(runtime.get("provider") or tier.provider),
        "model": tier.model,
        "base_url": str(runtime.get("base_url") or ""),
        "api_key": str(runtime.get("api_key") or ""),
        "api_mode": str(runtime.get("api_mode") or ""),
    }


async def evaluate_screenshot_assertions(
    image_data_url: str,
    assertions: list[dict[str, Any]],
    *,
    provider: str = "",
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    api_mode: str = "",
    cfg: Optional[dict[str, Any]] = None,
    timeout_s: float = 30.0,
    call_llm: Optional[Callable[..., Awaitable[Any]]] = None,
    on_provider_start: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """Run one bounded compact vision call and return assertion-safe fields."""

    expected_ids = [str(item.get("id") or "") for item in assertions[:6]]
    if not image_data_url.startswith("data:image/") or not expected_ids:
        return _uncertain_results(expected_ids, "invalid_vision_input")
    if call_llm is None:
        from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning

        call_llm = async_call_llm
    else:
        from agent.auxiliary_client import extract_content_or_reasoning

    # Screenshot assertions are a dedicated visual-inspection phase. Never
    # inherit the parent/orchestrator route, even when it accepts image input.
    # The Luna sweep and Opus critique remain explicit delegation purposes;
    # this bounded receipt evaluator is the Sonnet inspector stage.
    try:
        inspector = _resolve_visual_inspector_runtime(cfg)
    except Exception:
        return _uncertain_results(expected_ids, "vision_call_failed")
    call_kwargs: dict[str, Any] = {
        "task": "vision",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _assertion_prompt(assertions)},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }
        ],
        "temperature": 0.0,
        "max_tokens": 550,
        "timeout": max(1.0, min(float(timeout_s), 30.0)),
        "single_attempt": True,
        "strict_vision_capability": True,
        "provider": inspector["provider"],
        "model": inspector["model"],
        "base_url": inspector["base_url"],
        "api_key": inspector["api_key"],
        "api_mode": inspector["api_mode"],
    }
    try:
        if on_provider_start is not None:
            on_provider_start()
        response = await call_llm(**call_kwargs)
        content = extract_content_or_reasoning(response)
    except Exception:
        return _uncertain_results(expected_ids, "vision_call_failed")
    return parse_vision_assertion_output(content, expected_ids=expected_ids)


__all__ = [
    "evaluate_screenshot_assertions",
    "parse_vision_assertion_output",
]
