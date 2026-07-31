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


def _bounded_image_data_urls(value: Any) -> list[str]:
    raw_values = value if isinstance(value, list) else [value]
    images: list[str] = []
    for raw in raw_values[:4]:
        image = str(raw or "")
        if not image.startswith("data:image/"):
            return []
        images.append(image)
    return images


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


def _assertion_prompt(
    assertions: list[dict[str, Any]],
    execution_context: Optional[dict[str, Any]] = None,
) -> str:
    compact = [
        {"id": item["id"], "expectation": item["expectation"]}
        for item in assertions[:6]
    ]
    context = execution_context if isinstance(execution_context, dict) else {}
    bounded_context = {
        key: context[key]
        for key in ("target", "page", "viewport", "state", "artifacts")
        if key in context
    }
    context_note = (
        " Inspect only this orchestrator-supplied scope and assumptions: "
        f"{json.dumps(bounded_context, ensure_ascii=True, separators=(',', ':'))}."
        if bounded_context
        else ""
    )
    return (
        "Evaluate only the listed visual appearance assertions against the ordered images. "
        "Artifact descriptions in the supplied context use the same image order. "
        "Return JSON only with exactly this shape: "
        '{"results":[{"id":"...","status":"passed|failed|uncertain",'
        '"confidence":"high|medium|low","correction":"brief concrete correction or empty"}]}. '
        "For each failed or uncertain assertion, give one implementation-oriented correction "
        "under 240 characters. For passed assertions use an empty correction. "
        "Do not describe the page, quote visible text, include URLs, selectors, or add prose."
        f"{context_note} "
        f"Assertions: {json.dumps(compact, ensure_ascii=True, separators=(',', ':'))}"
    )


def _resolve_visual_inspector_runtime(cfg: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Resolve the dedicated model route used only for screenshot judgement."""

    from hermes_cli.model_tiers import resolve_model_tier
    from hermes_cli.runtime_provider import resolve_runtime_provider

    fallback_reason = ""
    try:
        from hermes_cli.opus_planner import _anthropic_budget_preflight_error

        fallback_reason = _anthropic_budget_preflight_error()
    except Exception:
        fallback_reason = ""
    tier_name = "visual_sweep" if fallback_reason else "visual_inspector"
    tier = resolve_model_tier(cfg, tier_name)
    if tier is None or not tier.provider:
        raise RuntimeError(f"{tier_name} model tier is unavailable")
    # Opus preflight failures fall back through the already-configured main
    # provider route. In Sligo deployments that is CLIProxyAPI's OpenAI
    # subscription; do not invent an external aggregator dependency here.
    runtime = resolve_runtime_provider(
        requested=None,
        target_model=tier.model,
    )
    return {
        "provider": str(runtime.get("provider") or tier.provider),
        "model": tier.model,
        "base_url": str(runtime.get("base_url") or ""),
        "api_key": str(runtime.get("api_key") or ""),
        "api_mode": str(runtime.get("api_mode") or ""),
        "fallback_used": bool(fallback_reason),
        "fallback_reason": "opus_preflight_unavailable" if fallback_reason else "",
    }


def _resolve_visual_sweep_runtime(cfg: Optional[dict[str, Any]]) -> dict[str, str]:
    """Resolve the dedicated Luna route used to sweep captured evidence."""

    from hermes_cli.model_tiers import resolve_model_tier
    from hermes_cli.runtime_provider import resolve_runtime_provider

    tier = resolve_model_tier(cfg, "visual_sweep")
    if tier is None or not tier.provider:
        raise RuntimeError("visual_sweep model tier is unavailable")
    runtime = resolve_runtime_provider(
        requested=None,
        target_model=tier.model,
    )
    return {
        "provider": str(runtime.get("provider") or tier.provider),
        "model": tier.model,
        "base_url": str(runtime.get("base_url") or ""),
        "api_key": str(runtime.get("api_key") or ""),
        "api_mode": str(runtime.get("api_mode") or ""),
    }


async def run_visual_sweep(
    image_data_url: Any,
    *,
    cfg: Optional[dict[str, Any]] = None,
    timeout_s: float = 30.0,
    call_llm: Optional[Callable[..., Awaitable[Any]]] = None,
    on_provider_start: Optional[Callable[[], None]] = None,
) -> bool:
    """Use Luna to validate that fresh browser evidence is inspectable."""

    images = _bounded_image_data_urls(image_data_url)
    if not images:
        return False
    if call_llm is None:
        from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning

        call_llm = async_call_llm
    else:
        from agent.auxiliary_client import extract_content_or_reasoning
    try:
        sweep = _resolve_visual_sweep_runtime(cfg)
        if on_provider_start is not None:
            on_provider_start()
        response = await call_llm(
            task="vision",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Confirm every attached image is a rendered UI screenshot suitable "
                                "for one subsequent bounded visual assertion pass. Return exactly READY."
                            ),
                        },
                        *[
                            {"type": "image_url", "image_url": {"url": image}}
                            for image in images
                        ],
                    ],
                }
            ],
            temperature=0.0,
            max_tokens=20,
            timeout=max(1.0, min(float(timeout_s), 30.0)),
            single_attempt=True,
            strict_vision_capability=True,
            provider=sweep["provider"],
            model=sweep["model"],
            base_url=sweep["base_url"],
            api_key=sweep["api_key"],
            api_mode=sweep["api_mode"],
        )
        return extract_content_or_reasoning(response).strip().upper() == "READY"
    except Exception:
        return False


async def evaluate_screenshot_assertions(
    image_data_url: Any,
    assertions: list[dict[str, Any]],
    *,
    provider: str = "",
    model: str = "",
    base_url: str = "",
    api_key: str = "",
    api_mode: str = "",
    cfg: Optional[dict[str, Any]] = None,
    execution_context: Optional[dict[str, Any]] = None,
    timeout_s: float = 30.0,
    call_llm: Optional[Callable[..., Awaitable[Any]]] = None,
    on_provider_start: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """Run one bounded compact vision call and return assertion-safe fields."""

    expected_ids = [str(item.get("id") or "") for item in assertions[:6]]
    images = _bounded_image_data_urls(image_data_url)
    if not images or not expected_ids:
        return _uncertain_results(expected_ids, "invalid_vision_input")
    if call_llm is None:
        from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning

        call_llm = async_call_llm
    else:
        from agent.auxiliary_client import extract_content_or_reasoning

    # Screenshot assertions are a dedicated visual-inspection phase. Preserve
    # the configured main transport and credentials, but never inherit the
    # parent/orchestrator model. The Luna sweep remains the cheap evidence gate;
    # this bounded receipt evaluator is the single premium rendered-result stage.
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
                    {
                        "type": "text",
                        "text": _assertion_prompt(assertions, execution_context),
                    },
                    *[
                        {"type": "image_url", "image_url": {"url": image}}
                        for image in images
                    ],
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
    result = parse_vision_assertion_output(content, expected_ids=expected_ids)
    result["review_model"] = inspector["model"]
    if inspector.get("fallback_used"):
        result["review_fallback"] = inspector.get("fallback_reason") or "opus_unavailable"
    return result


__all__ = [
    "evaluate_screenshot_assertions",
    "parse_vision_assertion_output",
    "run_visual_sweep",
]
