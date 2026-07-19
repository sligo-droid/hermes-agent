"""Centralized, provider-neutral vision capability resolution.

Transport support and model image-input capability are intentionally separate:
a model may understand images while its active API mode cannot serialize an
image-bearing tool result, or vice versa. Native tool-result vision is safe only
when both checks pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


_TRUE_TOKENS = frozenset({"true", "yes", "on", "1"})
_FALSE_TOKENS = frozenset({"false", "no", "off", "0"})


@dataclass(frozen=True)
class VisionCapabilityResolution:
    transport_supports_image_results: bool
    model_supports_image_input: Optional[bool]
    transport_source: str
    model_capability_source: str
    diagnostic_code: str

    @property
    def native_tool_result_supported(self) -> bool:
        return (
            self.transport_supports_image_results
            and self.model_supports_image_input is True
        )


def coerce_capability_bool(raw: Any) -> Optional[bool]:
    """Return a strict configuration Boolean, or ``None`` when malformed."""

    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return bool(raw) if raw in (0, 1) else None
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in _TRUE_TOKENS:
            return True
        if value in _FALSE_TOKENS:
            return False
    return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def configured_model_vision_support(
    cfg: Optional[dict[str, Any]],
    provider: str,
    model: str,
) -> tuple[Optional[bool], str]:
    """Resolve config overrides in the historical first-hit-wins order."""

    if not isinstance(cfg, dict):
        return None, ""
    model_cfg = _as_dict(cfg.get("model"))
    top = coerce_capability_bool(model_cfg.get("supports_vision"))
    if top is not None:
        return top, "config:model.supports_vision"

    config_provider = str(model_cfg.get("provider") or "").strip()
    providers_cfg = _as_dict(cfg.get("providers"))
    for candidate in dict.fromkeys(filter(None, (provider, config_provider))):
        entry = _as_dict(providers_cfg.get(candidate))
        per_model = _as_dict(_as_dict(entry.get("models")).get(model))
        override = coerce_capability_bool(per_model.get("supports_vision"))
        if override is not None:
            return override, f"config:providers.{candidate}.models"

    custom_providers = cfg.get("custom_providers")
    if isinstance(custom_providers, list):
        names: set[str] = set()
        for candidate in filter(None, (provider, config_provider)):
            names.add(candidate)
            if candidate.startswith("custom:"):
                names.add(candidate[len("custom:") :])
            else:
                names.add(f"custom:{candidate}")
        for raw_entry in custom_providers:
            entry = _as_dict(raw_entry)
            entry_name = str(entry.get("name") or "").strip()
            if entry_name not in names:
                continue
            per_model = _as_dict(_as_dict(entry.get("models")).get(model))
            override = coerce_capability_bool(per_model.get("supports_vision"))
            if override is not None:
                return override, "config:custom_providers.models"
    return None, ""


def _configured_api_mode(
    cfg: Optional[dict[str, Any]], provider: str, model: str
) -> str:
    if not isinstance(cfg, dict):
        return ""
    model_cfg = _as_dict(cfg.get("model"))
    direct = str(model_cfg.get("api_mode") or "").strip().lower()
    if direct:
        return direct
    config_provider = str(model_cfg.get("provider") or "").strip()
    providers_cfg = _as_dict(cfg.get("providers"))
    for candidate in dict.fromkeys(filter(None, (provider, config_provider))):
        entry = _as_dict(providers_cfg.get(candidate))
        per_model = _as_dict(_as_dict(entry.get("models")).get(model))
        mode = str(per_model.get("api_mode") or entry.get("api_mode") or "").strip().lower()
        if mode:
            return mode
    return ""


def transport_supports_image_tool_results(
    provider: str,
    model: str,
    *,
    api_mode: str = "",
) -> bool:
    """Return whether the active transport can carry image-valued tool output."""

    mode = str(api_mode or "").strip().lower()
    if mode == "codex_responses":
        return True
    if mode == "anthropic_messages":
        return True
    if mode and mode not in {"chat_completions", "responses"}:
        return False

    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider in {
        "anthropic",
        "claude",
        "anthropic-direct",
        "openrouter",
        "nous",
        "vertex",
        "bedrock",
        "anthropic-vertex",
        "google-vertex",
        "openai",
        "openai-chat",
        "openai-codex",
        "azure-openai",
    }:
        return True
    if normalized_provider in {
        "google",
        "gemini",
        "google-gemini",
        "google-vertex-gemini",
    }:
        normalized_model = str(model or "").strip().lower()
        return any(
            marker in normalized_model
            for marker in ("gemini-3", "gemini-pro-3", "gemini-flash-3")
        )
    return False


def _lookup_models_dev_support(provider: str, model: str) -> Optional[bool]:
    if not provider or not model:
        return None
    try:
        from agent.models_dev import get_model_capabilities

        capabilities = get_model_capabilities(provider, model)
    except Exception:
        return None
    if capabilities is None:
        return None
    return bool(getattr(capabilities, "supports_vision", False))


def resolve_vision_capabilities(
    provider: str,
    model: str,
    cfg: Optional[dict[str, Any]] = None,
    *,
    api_mode: str = "",
) -> VisionCapabilityResolution:
    """Resolve bounded transport/model facts for the active inference route."""

    resolved_mode = str(api_mode or "").strip().lower() or _configured_api_mode(
        cfg, provider, model
    )
    transport = transport_supports_image_tool_results(
        provider, model, api_mode=resolved_mode
    )
    transport_source = (
        f"api_mode:{resolved_mode}"
        if resolved_mode
        else f"provider:{str(provider or '').strip().lower() or 'unknown'}"
    )

    model_support, model_source = configured_model_vision_support(
        cfg, provider, model
    )
    if model_support is None:
        model_support = _lookup_models_dev_support(provider, model)
        model_source = "models_dev" if model_support is not None else "unknown"

    if model_support is None:
        diagnostic = "model_capability_unknown"
    elif not model_support:
        diagnostic = "model_image_input_unsupported"
    elif not transport:
        diagnostic = "transport_image_result_unsupported"
    else:
        diagnostic = "native_vision_supported"

    return VisionCapabilityResolution(
        transport_supports_image_results=transport,
        model_supports_image_input=model_support,
        transport_source=transport_source[:80],
        model_capability_source=model_source[:80],
        diagnostic_code=diagnostic,
    )


__all__ = [
    "VisionCapabilityResolution",
    "coerce_capability_bool",
    "configured_model_vision_support",
    "resolve_vision_capabilities",
    "transport_supports_image_tool_results",
]
