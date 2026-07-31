"""Shared named model tiers for Hermes runtime routes.

The tiers intentionally describe a model, optional direct-agent provider, and
reasoning effort. They are distinct from Hermes runtime *profiles* (separate
``HERMES_HOME`` instances) and can be referenced by gateway, cron, and Kanban
role settings.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from hermes_constants import (
    VALID_REASONING_EFFORTS,
    normalize_reasoning_effort,
    parse_reasoning_effort,
)


DEFAULT_MODEL_TIERS: dict[str, dict[str, str]] = {
    "trivial": {
        "model": "gpt-5.6-luna",
        "opencode_model": "hermes-codex/gpt-5.6-luna",
        "reasoning_effort": "medium",
    },
    "basic": {
        "model": "gpt-5.6-luna",
        "opencode_model": "hermes-codex/gpt-5.6-luna",
        "reasoning_effort": "max",
    },
    "intermediate": {
        "model": "gpt-5.6-sol",
        "opencode_model": "hermes-codex/gpt-5.6-sol",
        "reasoning_effort": "low",
    },
    "advanced": {
        "model": "gpt-5.6-sol",
        "opencode_model": "hermes-codex/gpt-5.6-sol",
        "reasoning_effort": "medium",
    },
    # Human-requested deep reviews only. Delegation enforces the explicit
    # root-turn authorization; this tier is intentionally outside the ladder.
    "deep_review": {
        "model": "gpt-5.6-sol",
        "opencode_model": "hermes-codex/gpt-5.6-sol",
        "reasoning_effort": "high",
    },
    # Route-specific tier for ordinary Discord action orchestration. It is
    # intentionally outside MODEL_TIER_LADDER so worker/delegation stepping
    # continues to treat ``advanced`` as the shared Sol/medium ceiling.
    "discord_action": {
        "model": "gpt-5.6-sol",
        "opencode_model": "hermes-codex/gpt-5.6-sol",
        "reasoning_effort": "low",
    },
    # Rendered-UI sweep workers: drive the browser across viewports/routes and
    # collect evidence. The work is navigation and protocol-following with
    # bounded recovery, so medium effort keeps it reliable without overspending.
    # Outside MODEL_TIER_LADDER for the same reason as ``discord_action``.
    "visual_sweep": {
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "opencode_model": "hermes-codex/gpt-5.6-luna",
        "reasoning_effort": "medium",
    },
    # Screenshot-appearance judgement. Sonnet is used once, after the cheap Luna
    # evidence sweep, because visual judgement is the premium part of the loop.
    "visual_inspector": {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "opencode_model": "anthropic/claude-sonnet-5",
        "reasoning_effort": "medium",
    },
    # Unbounded aesthetic critique over already-collected evidence. The
    # strongest available judgement model is worth one call — this is the slot
    # that should replace fanned-out premium workers.
    "visual_critique": {
        "provider": "anthropic",
        "model": "claude-opus-5",
        "opencode_model": "anthropic/claude-opus-5",
        "reasoning_effort": "medium",
    },
}

VISUAL_DELEGATION_PURPOSE_TIERS: dict[str, str] = {
    "visual_advisor": "visual_critique",
    "visual_sweep": "visual_sweep",
    "visual_inspector": "visual_inspector",
    "visual_critique": "visual_critique",
}

NEWLY_RESERVED_VISUAL_TIER_NAMES: frozenset[str] = frozenset(
    VISUAL_DELEGATION_PURPOSE_TIERS.values()
)

# Built-in names are reserved runtime policy, not user configuration. Persisted
# ``model_tiers`` entries may add genuinely custom names, but they must never
# shadow these code-owned definitions.
RESERVED_MODEL_TIER_NAMES: frozenset[str] = frozenset(DEFAULT_MODEL_TIERS)

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

_IMPLEMENTATION_TASK_SIGNALS = (
    "implement",
    "implementation",
    "fix",
    "fixes",
    "patch",
    "apply",
    "build",
    "change",
    "edit",
    "modify",
    "update",
    "add",
    "remove",
    "delete",
    "refactor",
    "rewrite",
    "create",
    "ship",
    "deploy",
    "merge",
    "commit",
)

_REVIEW_TASK_SIGNALS = (
    "review",
    "audit",
    "diagnose",
    "diagnosis",
    "investigate",
    "inspect",
    "assess",
    "root cause",
    "trace execution",
    "read-only",
    "read only",
    "no changes",
    "do not edit",
    "report findings",
)

_REVIEW_SPILLOVER_REASONING_EFFORT = "xhigh"


@dataclass(frozen=True)
class ModelTier:
    """A validated runtime route resolved from ``model_tiers``."""

    name: str
    model: str
    opencode_model: str
    reasoning_effort: str
    provider: str | None = None

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


def classify_task_purpose(task: Any, context: Any = "") -> str:
    """Classify a task as explicit review or implementation-capable work.

    Mutation signals always win. Ambiguous analysis defaults to implementation
    so review-only reasoning cannot leak into a task that may edit state.
    """

    text = f"{task or ''}\n{context or ''}".lower()
    if any(_contains_task_signal(text, signal) for signal in _IMPLEMENTATION_TASK_SIGNALS):
        return "implementation"
    if any(_contains_task_signal(text, signal) for signal in _REVIEW_TASK_SIGNALS):
        return "review"
    return "implementation"


def restrict_reasoning_effort_for_task(
    reasoning_effort: Any,
    task: Any,
    context: Any = "",
    *,
    purpose: str | None = None,
) -> str:
    """Apply the review spillover and implementation ceiling for a task."""

    effort = normalize_reasoning_effort(reasoning_effort)
    resolved_purpose = _normalized_name(purpose) or classify_task_purpose(task, context)
    if resolved_purpose == "review" and effort == "high":
        return _REVIEW_SPILLOVER_REASONING_EFFORT
    if resolved_purpose != "review" and effort == _REVIEW_SPILLOVER_REASONING_EFFORT:
        return "high"
    return effort


def restrict_model_tier_for_task(
    config: Mapping[str, Any] | None,
    tier: ModelTier | None,
    task: Any,
    context: Any = "",
    *,
    worker: bool = False,
    purpose: str | None = None,
) -> ModelTier | None:
    """Preserve the selected tier while capping implementation reasoning."""

    if tier is None:
        return None
    resolved_purpose = _normalized_name(purpose) or classify_task_purpose(task, context)
    safe_effort = restrict_reasoning_effort_for_task(
        tier.reasoning_effort,
        task,
        context,
        purpose=resolved_purpose,
    )
    if safe_effort == tier.reasoning_effort:
        return tier
    return ModelTier(
        name=tier.name,
        provider=tier.provider,
        model=tier.model,
        opencode_model=tier.opencode_model,
        reasoning_effort=safe_effort,
    )


def _merged_model_tiers(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Combine code-owned built-ins with user-defined custom tiers.

    Gateway and cron deliberately use raw config reads, so this merge must not
    rely on ``load_config()`` having already deep-merged ``DEFAULT_CONFIG``.
    Reserved built-in names are ignored even when raw YAML still contains an
    obsolete persisted copy from an older Hermes release.
    """
    tiers: dict[str, Any] = copy.deepcopy(DEFAULT_MODEL_TIERS)
    configured = config.get("model_tiers") if isinstance(config, Mapping) else None
    if not isinstance(configured, Mapping):
        return tiers

    for raw_name, raw_tier in configured.items():
        name = _normalized_name(raw_name)
        if not name or name in RESERVED_MODEL_TIER_NAMES:
            continue
        if not isinstance(raw_tier, Mapping):
            tiers[name] = raw_tier
            continue
        tiers[name] = dict(raw_tier)
    return tiers


def strip_reserved_model_tier_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return config without obsolete built-in tier copies or hidden routing.

    Custom tier names and every unrelated setting are preserved. The helper is
    shared by load/save normalization and the versioned migration so stale raw
    YAML cannot affect runtime behavior or be written back on a later save.
    """
    if not isinstance(config, Mapping):
        return {}

    result = dict(config)
    changed = False

    configured = result.get("model_tiers")
    if isinstance(configured, Mapping):
        custom_tiers = {
            raw_name: copy.deepcopy(raw_tier)
            for raw_name, raw_tier in configured.items()
            if _normalized_name(raw_name) not in RESERVED_MODEL_TIER_NAMES
        }
        if len(custom_tiers) != len(configured):
            result["model_tiers"] = custom_tiers
            changed = True

    delegation = result.get("delegation")
    if isinstance(delegation, Mapping) and "model_tier_routing" in delegation:
        clean_delegation = dict(delegation)
        clean_delegation.pop("model_tier_routing", None)
        result["delegation"] = clean_delegation
        changed = True

    return result if changed else dict(config)


def rename_newly_reserved_visual_tier_config(
    config: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Preserve pre-existing custom tiers that collide with new visual names."""
    if not isinstance(config, Mapping):
        return {}, {}

    result = copy.deepcopy(dict(config))
    configured = result.get("model_tiers")
    if not isinstance(configured, Mapping):
        return result, {}

    renamed_tiers = copy.deepcopy(dict(configured))
    renames: dict[str, str] = {}
    for raw_name in list(configured):
        normalized = _normalized_name(raw_name)
        if normalized not in NEWLY_RESERVED_VISUAL_TIER_NAMES:
            continue
        candidate = f"legacy_{normalized}"
        suffix = 2
        normalized_existing = {_normalized_name(name) for name in renamed_tiers}
        while candidate in normalized_existing:
            candidate = f"legacy_{normalized}_{suffix}"
            suffix += 1
        renamed_tiers[candidate] = renamed_tiers.pop(raw_name)
        renames[str(raw_name)] = candidate

    if renames:
        result["model_tiers"] = renamed_tiers
        normalized_renames = {_normalized_name(old): new for old, new in renames.items()}

        def rewrite_route_references(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if (
                        isinstance(value, str)
                        and (str(key) == "model_tier" or str(key).endswith("_model_tier"))
                        and _normalized_name(value) in normalized_renames
                    ):
                        node[key] = normalized_renames[_normalized_name(value)]
                    else:
                        rewrite_route_references(value)
            elif isinstance(node, list):
                for value in node:
                    rewrite_route_references(value)

        rewrite_route_references(result)
    return result, renames


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
    provider = str(raw_tier.get("provider") or "").strip().lower() or None
    reasoning_effort = normalize_reasoning_effort(raw_tier.get("reasoning_effort"))
    if not model or not opencode_model or reasoning_effort not in VALID_REASONING_EFFORTS:
        return None

    return ModelTier(
        name=normalized,
        provider=provider,
        model=model,
        opencode_model=opencode_model,
        reasoning_effort=reasoning_effort,
    )


def require_worker_model_tier(
    config: Mapping[str, Any] | None,
    name: Any,
) -> ModelTier | None:
    """Resolve a worker tier or fail before launch for invalid configuration."""
    normalized = _normalized_name(name)
    if not normalized or normalized in {"none", "off", "disabled"}:
        return None
    tier = resolve_model_tier(config, normalized)
    if (
        tier is None
        or "/" not in tier.opencode_model
        or tier.opencode_model.startswith("/")
        or tier.opencode_model.endswith("/")
    ):
        raise ValueError(
            f"Invalid worker model_tier {normalized!r}: define model, reasoning_effort, "
            "and opencode_model in provider/model form."
        )
    return tier


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
