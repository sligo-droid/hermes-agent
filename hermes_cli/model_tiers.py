"""Shared named model tiers for Hermes runtime routes.

The tiers intentionally describe a model together with its reasoning effort.
They are distinct from Hermes runtime *profiles* (separate ``HERMES_HOME``
instances) and can be referenced by gateway, cron, and Kanban role settings.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hermes_constants import VALID_REASONING_EFFORTS, parse_reasoning_effort


DEFAULT_MODEL_TIERS: dict[str, dict[str, str]] = {
    "trivial": {
        "model": "gpt-5.6-luna",
        "opencode_model": "hermes-codex/gpt-5.6-luna",
        "reasoning_effort": "medium",
    },
    "basic": {
        "model": "gpt-5.6-terra",
        "opencode_model": "hermes-codex/gpt-5.6-terra",
        "reasoning_effort": "high",
    },
    "intermediate": {
        "model": "gpt-5.6-terra",
        "opencode_model": "hermes-codex/gpt-5.6-terra",
        "reasoning_effort": "max",
    },
    "advanced": {
        "model": "gpt-5.6-sol",
        "opencode_model": "hermes-codex/gpt-5.6-sol",
        "reasoning_effort": "xhigh",
    },
    # Route-specific tier for ordinary Discord action orchestration. It is
    # intentionally outside MODEL_TIER_LADDER so worker/delegation stepping
    # continues to treat ``advanced`` as the shared Sol/xhigh ceiling.
    "discord_action": {
        "model": "gpt-5.6-sol",
        "opencode_model": "hermes-codex/gpt-5.6-sol",
        "reasoning_effort": "medium",
    },
}

# Ordered built-in execution tiers. Custom tier names remain valid for normal
# routing, but cannot be stepped because their relative ordering is unknown.
MODEL_TIER_LADDER: tuple[str, ...] = (
    "trivial",
    "basic",
    "intermediate",
    "advanced",
)

_SIMPLE_TASK_SIGNALS = (
    "typo",
    "comment",
    "formatting",
    "small docs",
    "documentation",
    "readme",
    "changelog",
    "one-line",
    "one line",
    "trivial",
    "mechanical",
)

_COMPLEX_OR_RISKY_TASK_SIGNALS = (
    "security",
    "auth",
    "permission",
    "sandbox",
    "secret",
    "credential",
    "payment",
    "wallet",
    "signing",
    "race",
    "deadlock",
    "concurrency",
    "data loss",
    "migration",
    "schema migration",
    "breaking change",
    "architecture",
    "design review",
    "audit",
    "incident",
    "production",
    "unsafe",
    "dangerous",
    "rewrite",
    "upgrade",
    "rebase",
    "merge conflict",
    "flaky",
    "intermittent",
    "root cause",
    "state machine",
    "async",
    "cache",
    "performance",
)

_EXPLICIT_PLANNING_SIGNALS = (
    "plan first",
    "first plan",
    "planning pass",
    "two phase",
    "two-phase",
    "design before",
)


@dataclass(frozen=True)
class ModelTier:
    """A validated model-and-effort pair resolved from ``model_tiers``."""

    name: str
    model: str
    opencode_model: str
    reasoning_effort: str

    def reasoning_config(self) -> dict[str, Any] | None:
        """Return the OpenAI-compatible reasoning payload for this tier."""
        return parse_reasoning_effort(self.reasoning_effort)


def _normalized_name(value: Any) -> str:
    return str(value or "").strip().lower()


def _contains_task_signal(text: str, signal: str) -> bool:
    if " " in signal or "-" in signal:
        return signal in text
    return bool(re.search(rf"\b{re.escape(signal)}\b", text))


def classify_task_complexity(task: Any, context: Any = "") -> str:
    """Classify task text as ``simple``, ``ordinary``, or ``complex``.

    This deterministic policy is shared by named-tier routes and coding-worker
    plan selection so their signals cannot drift into duplicate keyword lists.
    Risk and explicit planning always win over simple wording.
    """
    text = f"{task or ''}\n{context or ''}".lower()
    if not text.strip():
        return "ordinary"
    if any(signal in text for signal in _EXPLICIT_PLANNING_SIGNALS):
        return "complex"
    if any(_contains_task_signal(text, signal) for signal in _COMPLEX_OR_RISKY_TASK_SIGNALS):
        return "complex"
    if any(_contains_task_signal(text, signal) for signal in _SIMPLE_TASK_SIGNALS):
        return "simple"
    return "ordinary"


def _merged_model_tiers(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge partial user tier overrides onto the built-in catalog.

    Gateway and cron deliberately use raw config reads, so this merge must not
    rely on ``load_config()`` having already deep-merged ``DEFAULT_CONFIG``.
    """
    tiers: dict[str, Any] = copy.deepcopy(DEFAULT_MODEL_TIERS)
    configured = config.get("model_tiers") if isinstance(config, Mapping) else None
    if not isinstance(configured, Mapping):
        return tiers

    for raw_name, raw_tier in configured.items():
        name = _normalized_name(raw_name)
        if not name:
            continue
        if not isinstance(raw_tier, Mapping):
            tiers[name] = raw_tier
            continue
        inherited = tiers.get(name)
        if isinstance(inherited, Mapping):
            merged = dict(inherited)
            merged.update(raw_tier)
            # A tier is an atomic model/effort choice. If a user changes only
            # its runtime model, do not silently keep the old worker model.
            if "model" in raw_tier and "opencode_model" not in raw_tier:
                merged["opencode_model"] = raw_tier["model"]
            tiers[name] = merged
        else:
            tiers[name] = dict(raw_tier)
    return tiers


def resolve_model_tier(config: Mapping[str, Any] | None, name: Any) -> ModelTier | None:
    """Resolve a configured tier, returning ``None`` for blank or invalid input."""
    normalized = _normalized_name(name)
    if not normalized or normalized in {"none", "off", "disabled"}:
        return None

    raw_tier = _merged_model_tiers(config).get(normalized)
    if not isinstance(raw_tier, Mapping):
        return None

    model = str(raw_tier.get("model") or "").strip()
    opencode_model = str(raw_tier.get("opencode_model") or model).strip()
    reasoning_effort = _normalized_name(raw_tier.get("reasoning_effort"))
    if not model or not opencode_model or reasoning_effort not in VALID_REASONING_EFFORTS:
        return None

    return ModelTier(
        name=normalized,
        model=model,
        opencode_model=opencode_model,
        reasoning_effort=reasoning_effort,
    )


def resolve_model_tier_offset(
    config: Mapping[str, Any] | None,
    name: Any,
    offset: int,
) -> ModelTier | None:
    """Resolve a built-in tier at a relative offset, or ``None`` at an edge."""
    if not isinstance(offset, int) or isinstance(offset, bool) or offset == 0:
        return None
    try:
        index = MODEL_TIER_LADDER.index(_normalized_name(name))
    except ValueError:
        return None
    target_index = index + offset
    if target_index < 0 or target_index >= len(MODEL_TIER_LADDER):
        return None
    return resolve_model_tier(config, MODEL_TIER_LADDER[target_index])


def resolve_adjacent_model_tier(
    config: Mapping[str, Any] | None,
    name: Any,
    direction: int,
) -> ModelTier | None:
    """Resolve the adjacent built-in tier, or ``None`` at an invalid edge."""
    if direction not in {-1, 1}:
        return None
    return resolve_model_tier_offset(config, name, direction)
