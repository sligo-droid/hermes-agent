"""Safe, low-cardinality runtime routing audit fields for agent turns."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_CONTEXT_FIELDS = (
    "model_tier",
    "model_tier_source",
    "runtime_route",
    "runtime_role",
    "runtime_pass",
    "reasoning_source",
    "service_tier_source",
)


def _text(value: Any, *, limit: int = 128) -> str:
    """Return a bounded single-line value suitable for structured logs."""
    if value is None:
        return ""
    return " ".join(str(value).split())[:limit]


def _reasoning_fields(agent: Any, context: Mapping[str, str]) -> dict[str, Any]:
    config = getattr(agent, "reasoning_config", None)
    source = context.get("reasoning_source") or (
        "explicit" if isinstance(config, Mapping) and bool(config) else "default"
    )
    if not isinstance(config, Mapping) or not config:
        return {
            "reasoning_effort": "default",
            "reasoning_mode": "default",
            "reasoning_enabled": None,
            "reasoning_source": source,
        }

    effort = _text(config.get("effort")).lower()
    if config.get("enabled") is False or effort == "none":
        return {
            "reasoning_effort": "none",
            "reasoning_mode": "disabled",
            "reasoning_enabled": False,
            "reasoning_source": source,
        }
    if effort:
        return {
            "reasoning_effort": effort,
            "reasoning_mode": "effort",
            "reasoning_enabled": True,
            "reasoning_source": source,
        }
    if config.get("enabled") is True:
        return {
            "reasoning_effort": "default",
            "reasoning_mode": "enabled",
            "reasoning_enabled": True,
            "reasoning_source": source,
        }
    return {
        "reasoning_effort": "default",
        "reasoning_mode": "default",
        "reasoning_enabled": None,
        "reasoning_source": source,
    }


def runtime_audit_fields(agent: Any) -> dict[str, Any]:
    """Return the whitelisted runtime-routing fields for an agent turn."""
    raw_context = getattr(agent, "_runtime_audit_context", None)
    context = raw_context if isinstance(raw_context, Mapping) else {}
    service_tier = _text(getattr(agent, "service_tier", None)).lower()
    fields: dict[str, Any] = {
        "model_tier": _text(context.get("model_tier")).lower(),
        "model_tier_source": _text(context.get("model_tier_source")).lower()
        or "none",
        "runtime_route": _text(context.get("runtime_route")).lower()
        or _text(getattr(agent, "platform", None)).lower()
        or "agent",
        "runtime_role": _text(context.get("runtime_role")).lower()
        or _text(getattr(agent, "session_role", None)).lower(),
        "runtime_pass": _text(context.get("runtime_pass")).lower(),
        "service_tier": service_tier or "default",
        "service_tier_source": _text(context.get("service_tier_source")).lower()
        or ("explicit" if service_tier else "default"),
        "api_mode": _text(getattr(agent, "api_mode", None)).lower() or "default",
    }
    fields.update(_reasoning_fields(agent, context))
    return fields


def set_runtime_audit_context(agent: Any, **values: Any) -> dict[str, Any]:
    """Update an agent's approved audit context and return its full snapshot.

    Unknown keys are intentionally ignored so callers cannot accidentally put
    prompts, task text, credentials, or other high-cardinality data into logs.
    """
    current = getattr(agent, "_runtime_audit_context", None)
    context = dict(current) if isinstance(current, Mapping) else {}
    for key in _CONTEXT_FIELDS:
        if key in values:
            context[key] = _text(values[key])
    agent._runtime_audit_context = context

    snapshot = runtime_audit_fields(agent)
    session_config = getattr(agent, "_session_init_model_config", None)
    if isinstance(session_config, dict):
        session_config["reasoning_config"] = getattr(agent, "reasoning_config", None)
        session_config["runtime_audit"] = dict(snapshot)
    return snapshot
