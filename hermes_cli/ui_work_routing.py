"""Deterministic routing for UI-shaped coding work."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

_BROAD_SURFACE_KEYWORDS = {
    "app interface",
    "button",
    "card",
    "chart",
    "command center",
    "component",
    "dashboard",
    "drawer",
    "footer",
    "form",
    "header",
    "interface",
    "layout",
    "modal",
    "page",
    "product surface",
    "screen",
    "sidebar",
    "table",
    "toolbar",
    "ui",
    "user interface",
    "web interface",
    "web ui",
}

_DEFAULT_ACTION_KEYWORDS = [
    "implement",
    "implementing",
    "build",
    "building",
    "create",
    "creating",
    "add",
    "adding",
    "develop",
    "developing",
    "development",
    "design",
    "designing",
    "redesign",
    "redesigning",
    "style",
    "styling",
    "restyle",
    "polish",
    "polishing",
    "visual design",
    "make responsive",
]

_DEFAULT_NON_VISUAL_DOMAIN_KEYWORDS = [
    "state",
    "app-state",
    "application state",
    "routing",
    "route",
    "router",
    "data",
    "dataset",
    "datasets",
    "config",
    "configuration",
    "default",
    "defaults",
    "selection",
    "selection model",
    "normalization",
    "normalize",
    "normalized",
    "plumbing",
    "wiring",
    "model",
    "models",
    "schema",
    "store",
    "storage",
    "query",
    "queries",
    "api",
    "backend",
    "server",
    "database",
    "db",
    "endpoint",
    "endpoints",
    "contract",
    "serialization",
    "parser",
    "ingestion",
    "pipeline",
]

_DEFAULT_VISUAL_INTENT_KEYWORDS = [
    "style",
    "styling",
    "restyle",
    "polish",
    "polishing",
    "visual design",
    "visual polish",
    "ui polish",
    "interface polish",
    "layout polish",
    "visual",
    "visuals",
    "appearance",
    "css",
    "responsive",
    "spacing",
    "color",
    "colors",
    "typography",
    "theme",
    "design system",
]

_LAYOUT_INTENT_KEYWORDS = [
    "centered",
    "center horizontally",
    "center vertically",
    "horizontally and vertically",
    "equal size",
    "equally sized",
    "fit within",
]

_SOFT_VERIFICATION_NEGATIVE_KEYWORDS = {
    "smoke",
    "verify",
    "validation",
    "screenshot",
    "visual check",
    "test",
    "tests",
    "pytest",
    "unit test",
    "regression",
}

_EXACT_VISUAL_VALUE_RE = re.compile(
    r"(?:\bexactly\b.{0,48}(?:#[0-9a-f]{3,8}\b|\d+(?:\.\d+)?(?:px|rem|em|%|vh|vw)\b))"
    r"|(?:\b(?:set|change|make|use|add)\b.{0,64}\bto\s+"
    r"(?:#[0-9a-f]{3,8}\b|\d+(?:\.\d+)?(?:px|rem|em|%|vh|vw)\b))",
    re.IGNORECASE,
)
_ADDITIVE_VISUAL_CLAUSE_RE = re.compile(
    r"(?:[,;:()]|\b(?:and|but|or|nor|yet|then|while|plus|also|as well as|"
    r"along with|together with)\b)",
    re.IGNORECASE,
)


def _is_single_clause_exact_visual_request(text: str) -> bool:
    """Return true only for one bounded exact-value visual instruction."""

    if not _EXACT_VISUAL_VALUE_RE.search(text):
        return False
    without_final_punctuation = text.rstrip().rstrip(".!?")
    if re.search(r"[!?]|(?<!\d)\.|\.(?!\d)", without_final_punctuation):
        return False
    return _ADDITIVE_VISUAL_CLAUSE_RE.search(without_final_punctuation) is None


@dataclass(frozen=True)
class UIWorkRouteDecision:
    matched: bool
    enabled: bool
    reason: str
    provider: str = ""
    model: str = ""
    backend: str = ""
    backend_config: dict[str, Any] = field(default_factory=dict)
    fallback_allowed: bool = False
    error: str = ""
    route_decision: str = "default_coding_worker"
    route_decision_source: str = "deterministic_default"
    route_decision_confidence: float | None = None
    route_decision_rationale: str = ""
    selected_route: str = "default_coding_worker"
    selected_provider: str = ""
    selected_model: str = ""
    fallback_used: bool = False
    fallback_reason: str = ""
    advisory_matched: bool = False
    advisory_reason: str = ""
    launch_worker: bool = True
    model_tier: str = ""
    visual_advisor_tier: str = "standard"

    def metadata(self) -> dict[str, Any]:
        metadata = {
            "matched": self.matched,
            "enabled": self.enabled,
            "reason": self.reason,
            "provider": self.provider,
            "model": self.model,
            "backend": self.backend,
            "fallback_allowed": self.fallback_allowed,
            "error": self.error,
            "route_decision": self.route_decision,
            "route_decision_source": self.route_decision_source,
            "route_decision_confidence": self.route_decision_confidence,
            "route_decision_rationale": self.route_decision_rationale,
            "selected_route": self.selected_route,
            "selected_provider": self.selected_provider,
            "selected_model": self.selected_model,
            "fallback_used": self.fallback_used,
            "fallback_reason": self.fallback_reason,
            "advisory_matched": self.advisory_matched,
            "advisory_reason": self.advisory_reason,
            "model_tier": self.model_tier,
            "visual_advisor_tier": self.visual_advisor_tier,
        }
        metadata["recommended_skills"] = (
            list(ui_specialist_skills_for_model_tier(self.model_tier))
            if self.selected_route == _UI_SPECIALIST_ROUTE
            else []
        )
        return metadata


def ui_specialist_skill_prompt(decision: UIWorkRouteDecision | None) -> str:
    """Return worker prompt text for UI-specialist skill loading."""
    if decision is None or decision.selected_route != _UI_SPECIALIST_ROUTE:
        return ""
    selected_skills = ui_specialist_skills_for_model_tier(decision.model_tier)
    skills = ", ".join(f"`{name}`" for name in selected_skills)
    return (
        "UI specialist skill loading: before frontend/UI edits, load and use "
        f"these bundled Hermes skills when available: {skills}. "
        "Use `taste-skill` as the anti-slop quality gate, `claude-design` when "
        "included for design workflow, and `popular-web-designs` when included "
        "for a known visual reference or design-system vocabulary. If a skill cannot be "
        "loaded by the worker runtime, continue with the task and report the "
        "limitation in the final summary."
    )


@dataclass(frozen=True)
class _RouteDecisionInput:
    route: str
    source: str
    confidence: float | None = None
    rationale: str = ""
    error: str = ""
    visual_advisor_tier: str = "standard"


_DEFAULT_ROUTE = "default_coding_worker"
_UI_SPECIALIST_ROUTE = "ui_visual_specialist"
UI_SPECIALIST_SKILLS = ("taste-skill", "claude-design", "popular-web-designs")
_UI_SKILLS_BY_MODEL_TIER = {
    "trivial": UI_SPECIALIST_SKILLS[:1],
    "basic": UI_SPECIALIST_SKILLS[:2],
    "intermediate": UI_SPECIALIST_SKILLS,
    "advanced": UI_SPECIALIST_SKILLS,
}
_NO_WORKER_ROUTES = {"review_only_no_worker", "ask_human"}


def ui_specialist_skills_for_model_tier(model_tier: Any) -> tuple[str, ...]:
    """Return model-tier-aware UI skills; unknown/omitted tiers use all skills."""

    normalized = str(model_tier or "").strip().lower()
    return _UI_SKILLS_BY_MODEL_TIER.get(normalized, UI_SPECIALIST_SKILLS)


_ROUTE_ALIASES = {
    "default": _DEFAULT_ROUTE,
    "default_worker": _DEFAULT_ROUTE,
    "default_coding_worker": _DEFAULT_ROUTE,
    "codex_default": _DEFAULT_ROUTE,
    "ui": _UI_SPECIALIST_ROUTE,
    "ui_specialist": _UI_SPECIALIST_ROUTE,
    "visual_specialist": _UI_SPECIALIST_ROUTE,
    "ui_visual_specialist": _UI_SPECIALIST_ROUTE,
    "review_only": "review_only_no_worker",
    "review_only_no_worker": "review_only_no_worker",
    "no_worker": "review_only_no_worker",
    "ask_human": "ask_human",
    "human": "ask_human",
}
_VALID_ROUTES = {_DEFAULT_ROUTE, _UI_SPECIALIST_ROUTE, *_NO_WORKER_ROUTES}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item or "").strip()]
    return []


def _normalize_text(*parts: Any) -> str:
    text = "\n".join(str(part or "") for part in parts if part is not None)
    return re.sub(r"\s+", " ", text.lower()).strip()


def _normalize_route_name(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    raw = re.sub(r"[\s-]+", "_", raw)
    return _ROUTE_ALIASES.get(raw, raw)


def _normalize_confidence(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        return None
    if 0.0 <= confidence <= 1.0:
        return confidence
    return None


def _normalize_route_decision(value: Any) -> _RouteDecisionInput:
    if value is None or value == "":
        return _RouteDecisionInput(
            route=_DEFAULT_ROUTE,
            source="deterministic_default",
            rationale=(
                "No orchestrator route decision was provided; default coding "
                "worker selected."
            ),
        )
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{"):
            try:
                parsed = json.loads(stripped)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                return _normalize_route_decision(parsed)
        route = _normalize_route_name(value)
        source = "orchestrator"
        confidence = None
        rationale = ""
        visual_advisor_tier = "standard"
    elif isinstance(value, dict):
        route = _normalize_route_name(
            value.get("route")
            or value.get("decision")
            or value.get("selected_route")
            or value.get("worker_route")
        )
        source = str(value.get("source") or "orchestrator").strip() or "orchestrator"
        confidence = _normalize_confidence(value.get("confidence"))
        rationale = str(value.get("rationale") or value.get("reason") or "").strip()
        visual_advisor_tier = str(
            value.get("visual_advisor_tier") or value.get("advisor_tier") or "standard"
        ).strip().lower()
    else:
        return _RouteDecisionInput(
            route="",
            source="orchestrator",
            error="route_decision must be a string or object",
        )
    if visual_advisor_tier not in {"standard", "advanced"}:
        return _RouteDecisionInput(
            route=route,
            source=source,
            confidence=confidence,
            rationale=rationale,
            visual_advisor_tier=visual_advisor_tier,
            error=(
                "visual_advisor_tier must be 'standard' or 'advanced', not "
                f"{visual_advisor_tier!r}"
            ),
        )
    if route not in _VALID_ROUTES:
        label = route or "<empty>"
        valid = ", ".join(sorted(_VALID_ROUTES))
        return _RouteDecisionInput(
            route=route,
            source=source,
            confidence=confidence,
            rationale=rationale,
            error=f"unknown route_decision route {label!r}; expected one of: {valid}",
        )
    return _RouteDecisionInput(
        route=route,
        source=source,
        confidence=confidence,
        rationale=rationale,
        visual_advisor_tier=visual_advisor_tier,
    )


def _contains_keyword(text: str, keyword: str) -> bool:
    raw = str(keyword or "").strip().lower()
    if not raw:
        return False
    if " " in raw or "-" in raw or "/" in raw:
        return raw in text
    return re.search(rf"(?<![a-z0-9]){re.escape(raw)}(?![a-z0-9])", text) is not None


def _first_match(text: str, keywords: list[str]) -> str:
    for keyword in keywords:
        if _contains_keyword(text, keyword):
            return keyword
    return ""


def _explicit_visual_keywords(keywords: list[str]) -> list[str]:
    return [
        keyword
        for keyword in keywords
        if _normalize_text(keyword) not in _BROAD_SURFACE_KEYWORDS
    ]


def _classify_ui_work(
    *,
    config: dict[str, Any],
    task: str = "",
    title: str = "",
    context: str = "",
    cwd: str = "",
    project: str = "",
) -> tuple[bool, str]:
    detection = _as_dict(config.get("detection"))
    action_keywords = _as_list(detection.get("action_keywords"))
    surface_keywords = _explicit_visual_keywords(
        _as_list(detection.get("visual_surface_keywords"))
    )
    negative_keywords = _as_list(detection.get("negative_keywords"))
    non_visual_domain_keywords = _as_list(
        detection.get("non_visual_domain_keywords")
    )
    visual_intent_keywords = _as_list(detection.get("visual_intent_keywords"))
    if not action_keywords:
        action_keywords = _DEFAULT_ACTION_KEYWORDS
    if not surface_keywords:
        surface_keywords = _explicit_visual_keywords(
            _as_list(detection.get("title_body_keywords"))
        )
    if not non_visual_domain_keywords:
        non_visual_domain_keywords = _DEFAULT_NON_VISUAL_DOMAIN_KEYWORDS
    if not visual_intent_keywords:
        visual_intent_keywords = _DEFAULT_VISUAL_INTENT_KEYWORDS

    primary_text = _normalize_text(title, task)
    body_text = _normalize_text(title, task, context)

    # Only bypass consultation when the whole request is one exact-value
    # clause. Any additional sentence, delimiter, or clause connector defaults
    # back to visual advice; under-classifying is cheaper than skipping needed
    # design judgment.
    if _is_single_clause_exact_visual_request(primary_text):
        return False, "deterministic exact visual value"

    # CWD/project names are deliberately not positive evidence: repository names
    # like PID or Command Center should not route backend, docs, test, or review
    # work to the visual specialist model.
    primary_negative = _first_match(primary_text, negative_keywords)
    primary_visual_intent = _first_match(
        primary_text,
        [*visual_intent_keywords, *_LAYOUT_INTENT_KEYWORDS],
    )
    primary_action = _first_match(primary_text, action_keywords)
    primary_surface = (
        _first_match(primary_text, surface_keywords) or primary_visual_intent
    )
    # Supplemental implementation context often names backend/schema constraints
    # for an otherwise explicit visual request.  Those words must not veto the
    # user-facing task itself.  Negative-only task framing remains authoritative,
    # while an explicit visual action may still mention backend/schema constraints
    # without losing its route. Context can supply positive evidence when the
    # primary request is not already an explicit visual action.
    explicit_primary_visual = bool(
        primary_action and primary_surface and primary_visual_intent
    )
    negative = (
        ""
        if explicit_primary_visual
        else primary_negative or _first_match(body_text, negative_keywords)
    )
    visual_intent = primary_visual_intent or _first_match(
        body_text, [*visual_intent_keywords, *_LAYOUT_INTENT_KEYWORDS]
    )
    if negative and not (
        negative in _SOFT_VERIFICATION_NEGATIVE_KEYWORDS and visual_intent
    ):
        return False, f"negative keyword: {negative}"

    action = primary_action or _first_match(body_text, action_keywords)
    if not action:
        return False, "no visual ui action"

    non_visual_domain = (
        ""
        if explicit_primary_visual
        else primary_negative or _first_match(body_text, non_visual_domain_keywords)
    )
    if non_visual_domain and not visual_intent:
        return False, f"non-visual domain keyword: {non_visual_domain}"

    surface = _first_match(body_text, surface_keywords) or visual_intent
    if not surface:
        return False, "no explicit visual ui intent"

    return True, f"visual ui work: {action} + {surface}"


def resolve_ui_work_route(
    loaded_config: dict[str, Any] | None,
    *,
    task: str = "",
    title: str = "",
    context: str = "",
    cwd: str = "",
    project: str = "",
    backend: str = "codex",
    route_decision: Any = None,
    model_tier: Any = None,
) -> UIWorkRouteDecision:
    """Return a secret-free UI-work routing decision.

    This function is pure: it reads no files, no environment secrets, and makes
    no network calls. Callers pass already-loaded Hermes config.
    """
    ui_cfg = _as_dict((loaded_config or {}).get("ui_work"))
    enabled = bool(ui_cfg.get("enabled", False))
    advisory_matched, advisory_reason = _classify_ui_work(
        config=ui_cfg,
        task=task,
        title=title,
        context=context,
        cwd=cwd,
        project=project,
    )
    requested = _normalize_route_decision(route_decision)
    normalized_backend = str(backend or "opencode").strip().lower() or "opencode"

    normalized_model_tier = str(model_tier or "").strip().lower()
    base_fields = {
        "backend": normalized_backend,
        "model_tier": normalized_model_tier,
        "route_decision": requested.route,
        "route_decision_source": requested.source,
        "route_decision_confidence": requested.confidence,
        "route_decision_rationale": requested.rationale,
        "advisory_matched": advisory_matched,
        "advisory_reason": advisory_reason,
        "visual_advisor_tier": requested.visual_advisor_tier,
    }

    if requested.error:
        return UIWorkRouteDecision(
            matched=False,
            enabled=enabled,
            reason=requested.error,
            selected_route=_DEFAULT_ROUTE,
            error=requested.error,
            **base_fields,
        )

    if requested.route in _NO_WORKER_ROUTES:
        reason = f"orchestrator route requested {requested.route}; no coding worker launched"
        return UIWorkRouteDecision(
            matched=False,
            enabled=enabled,
            reason=reason,
            selected_route=requested.route,
            launch_worker=False,
            **base_fields,
        )

    if requested.route == _DEFAULT_ROUTE:
        if (
            requested.source == "deterministic_default"
            and enabled
            and advisory_matched
        ):
            automatic_fields = {
                **base_fields,
                "route_decision": _UI_SPECIALIST_ROUTE,
                "route_decision_source": "deterministic_explicit_visual",
                "route_decision_rationale": advisory_reason,
            }
            return UIWorkRouteDecision(
                matched=True,
                enabled=True,
                reason="explicit visual UI work selected the visual advisor route",
                selected_route=_UI_SPECIALIST_ROUTE,
                **automatic_fields,
            )
        reason = (
            "orchestrator route selected default coding worker"
            if requested.source != "deterministic_default"
            else requested.rationale
        )
        return UIWorkRouteDecision(
            matched=False,
            enabled=enabled,
            reason=reason,
            selected_route=_DEFAULT_ROUTE,
            **base_fields,
        )

    if not enabled:
        return UIWorkRouteDecision(
            matched=True,
            enabled=False,
            reason="ui routing disabled; falling back to default worker",
            selected_route=_DEFAULT_ROUTE,
            fallback_used=True,
            fallback_reason="ui_work.enabled is false",
            **base_fields,
        )

    return UIWorkRouteDecision(
        matched=True,
        enabled=True,
        reason="orchestrator route selected ui visual specialist",
        selected_route=_UI_SPECIALIST_ROUTE,
        **base_fields,
    )
