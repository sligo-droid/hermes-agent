"""Deterministic routing for UI-shaped coding work."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


_OPENROUTER_PROVIDER_CONFIG_ARGS = {
    "model_providers.openrouter.name": "openrouter",
    "model_providers.openrouter.base_url": "https://openrouter.ai/api/v1",
    "model_providers.openrouter.env_key": "OPENROUTER_API_KEY",
}

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
        }
        metadata["recommended_skills"] = (
            list(UI_SPECIALIST_SKILLS)
            if self.selected_route == _UI_SPECIALIST_ROUTE
            else []
        )
        return metadata


def ui_specialist_skill_prompt(decision: UIWorkRouteDecision | None) -> str:
    """Return worker prompt text for UI-specialist skill loading."""
    if decision is None or decision.selected_route != _UI_SPECIALIST_ROUTE:
        return ""
    skills = ", ".join(f"`{name}`" for name in UI_SPECIALIST_SKILLS)
    return (
        "UI specialist skill loading: before frontend/UI edits, load and use "
        f"these bundled Hermes skills when available: {skills}. "
        "Use `taste-skill` as the anti-slop quality gate, `claude-design` for "
        "design workflow, and `popular-web-designs` when a known visual "
        "reference or design-system vocabulary is useful. If a skill cannot be "
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


_DEFAULT_ROUTE = "default_coding_worker"
_UI_SPECIALIST_ROUTE = "ui_visual_specialist"
UI_SPECIALIST_SKILLS = ("taste-skill", "claude-design", "popular-web-designs")
_NO_WORKER_ROUTES = {"review_only_no_worker", "ask_human"}
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
    else:
        return _RouteDecisionInput(
            route="",
            source="orchestrator",
            error="route_decision must be a string or object",
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

    body_text = _normalize_text(title, task, context)

    # CWD/project names are deliberately not positive evidence: repository names
    # like PID or Command Center should not route backend, docs, test, or review
    # work to the visual specialist model.
    negative = _first_match(body_text, negative_keywords)
    visual_intent = _first_match(body_text, visual_intent_keywords)
    if negative and not (
        negative in _SOFT_VERIFICATION_NEGATIVE_KEYWORDS and visual_intent
    ):
        return False, f"negative keyword: {negative}"

    action = _first_match(body_text, action_keywords)
    if not action:
        return False, "no visual ui action"

    non_visual_domain = _first_match(body_text, non_visual_domain_keywords)
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
) -> UIWorkRouteDecision:
    """Return a secret-free UI-work routing decision.

    This function is pure: it reads no files, no environment secrets, and makes
    no network calls. Callers pass already-loaded Hermes config.
    """
    ui_cfg = _as_dict((loaded_config or {}).get("ui_work"))
    enabled = bool(ui_cfg.get("enabled", False))
    fallback_cfg = _as_dict(ui_cfg.get("fallback"))
    fallback_allowed = bool(fallback_cfg.get("allow_default_worker", True))
    advisory_matched, advisory_reason = _classify_ui_work(
        config=ui_cfg,
        task=task,
        title=title,
        context=context,
        cwd=cwd,
        project=project,
    )
    provider = str(ui_cfg.get("provider") or "").strip()
    model = str(ui_cfg.get("model") or "").strip()
    requested = _normalize_route_decision(route_decision)
    normalized_backend = str(backend or "opencode").strip().lower() or "opencode"

    base_fields = {
        "provider": provider,
        "model": model,
        "backend": normalized_backend,
        "fallback_allowed": fallback_allowed,
        "route_decision": requested.route,
        "route_decision_source": requested.source,
        "route_decision_confidence": requested.confidence,
        "route_decision_rationale": requested.rationale,
        "advisory_matched": advisory_matched,
        "advisory_reason": advisory_reason,
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
    if not provider or not model:
        error = "ui_work routing matched but ui_work.provider and ui_work.model must both be configured"
        if fallback_allowed:
            return UIWorkRouteDecision(
                matched=True,
                enabled=True,
                reason="missing provider/model; falling back to default worker",
                selected_route=_DEFAULT_ROUTE,
                fallback_used=True,
                fallback_reason="ui_work.provider or ui_work.model is missing",
                **base_fields,
            )
        return UIWorkRouteDecision(
            matched=True,
            enabled=True,
            reason="missing provider/model",
            selected_route=_UI_SPECIALIST_ROUTE,
            error=error,
            **base_fields,
        )

    backend_cfg = _as_dict(ui_cfg.get(normalized_backend))
    if normalized_backend == "codex":
        provider_key = str(backend_cfg.get("provider_config_key") or "model_provider").strip()
        model_key = str(backend_cfg.get("model_config_key") or "model").strip()
        if not provider_key or not model_key:
            error = "ui_work routing matched but ui_work.codex provider/model config keys are incomplete"
            if fallback_allowed:
                return UIWorkRouteDecision(
                    matched=True,
                    enabled=True,
                    reason="incomplete codex config; falling back to default worker",
                    selected_route=_DEFAULT_ROUTE,
                    fallback_used=True,
                    fallback_reason="ui_work.codex provider/model config keys are incomplete",
                    **base_fields,
                )
            return UIWorkRouteDecision(
                matched=True,
                enabled=True,
                reason="incomplete codex config",
                selected_route=_UI_SPECIALIST_ROUTE,
                error=error,
                **base_fields,
            )
        route_backend_config = {
            "provider_config_key": provider_key,
            "model_config_key": model_key,
            "extra_args": _as_list(backend_cfg.get("extra_args")),
        }
    else:
        route_backend_config = _as_dict(backend_cfg)

    return UIWorkRouteDecision(
        matched=True,
        enabled=True,
        reason="orchestrator route selected ui visual specialist",
        backend_config=route_backend_config,
        selected_route=_UI_SPECIALIST_ROUTE,
        selected_provider=provider,
        selected_model=model,
        **base_fields,
    )


def codex_ui_work_extra_args(decision: UIWorkRouteDecision) -> list[str]:
    """Build Codex CLI ``-c`` overrides for an enabled UI route."""
    if not decision.matched or not decision.enabled or decision.backend != "codex":
        return []
    cfg = decision.backend_config
    if not cfg:
        return []
    provider_key = str(cfg.get("provider_config_key") or "model_provider").strip()
    model_key = str(cfg.get("model_config_key") or "model").strip()
    args: list[str] = []
    for key, value in (
        (provider_key, decision.provider),
        (model_key, decision.model),
    ):
        if key and value:
            args.extend(["-c", f"{key}={json.dumps(value)}"])
    if decision.provider.strip().lower() == "openrouter":
        for key, value in _OPENROUTER_PROVIDER_CONFIG_ARGS.items():
            args.extend(["-c", f"{key}={json.dumps(value)}"])
    args.extend(str(item) for item in cfg.get("extra_args") or [])
    return args


def opencode_ui_work_worker_config(decision: UIWorkRouteDecision) -> dict[str, Any]:
    """Build OpenCode worker_config overrides for an enabled UI route."""
    if not decision.matched or not decision.enabled or decision.backend != "opencode":
        return {}
    provider = str(decision.provider or "").strip()
    model = str(decision.model or "").strip()
    if not provider or not model:
        return {}
    return {"opencode": {"model": f"{provider}/{model}"}}
