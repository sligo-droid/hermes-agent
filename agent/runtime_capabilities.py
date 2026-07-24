"""Explicit runtime and tool-effect capabilities shared across Hermes."""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class RuntimeMode(str, Enum):
    """Authority granted to one agent turn."""

    READ_ONLY = "read_only"
    ACTION = "action"


class ToolEffect(str, Enum):
    """Durable side-effect posture declared by one tool implementation."""

    READ_ONLY = "read_only"
    CONDITIONAL = "conditional"
    MUTATING = "mutating"
    UNKNOWN = "unknown"


def normalize_runtime_mode(
    value: Any,
    *,
    legacy_action_intent: Optional[bool] = None,
    default: RuntimeMode = RuntimeMode.ACTION,
) -> RuntimeMode:
    """Return one explicit mode while containing legacy bool compatibility."""

    if isinstance(value, RuntimeMode):
        return value
    normalized = str(value or "").strip().lower().replace("-", "_")
    aliases = {
        "read_only": RuntimeMode.READ_ONLY,
        "readonly": RuntimeMode.READ_ONLY,
        "question": RuntimeMode.READ_ONLY,
        "intake": RuntimeMode.READ_ONLY,
        "action": RuntimeMode.ACTION,
        "mutable": RuntimeMode.ACTION,
    }
    if normalized in aliases:
        return aliases[normalized]
    if legacy_action_intent is True:
        return RuntimeMode.ACTION
    if legacy_action_intent is False:
        return RuntimeMode.READ_ONLY
    return default


def normalize_tool_effect(value: Any) -> ToolEffect:
    if isinstance(value, ToolEffect):
        return value
    normalized = str(value or "").strip().lower().replace("-", "_")
    try:
        return ToolEffect(normalized)
    except ValueError:
        return ToolEffect.UNKNOWN


def is_read_only_mode(value: Any, *, legacy_action_intent: Optional[bool] = None) -> bool:
    return normalize_runtime_mode(
        value,
        legacy_action_intent=legacy_action_intent,
    ) is RuntimeMode.READ_ONLY
