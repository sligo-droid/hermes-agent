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

    def metadata(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "enabled": self.enabled,
            "reason": self.reason,
            "provider": self.provider,
            "model": self.model,
            "backend": self.backend,
            "fallback_allowed": self.fallback_allowed,
            "error": self.error,
        }


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

    negative = _first_match(body_text, negative_keywords)
    if negative:
        return False, f"negative keyword: {negative}"

    # CWD/project names are deliberately not positive evidence: repository names
    # like PID or Command Center should not route backend, docs, test, or review
    # work to the visual specialist model.
    action = _first_match(body_text, action_keywords)
    if not action:
        return False, "no visual ui action"

    non_visual_domain = _first_match(body_text, non_visual_domain_keywords)
    visual_intent = _first_match(body_text, visual_intent_keywords)
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
) -> UIWorkRouteDecision:
    """Return a secret-free UI-work routing decision.

    This function is pure: it reads no files, no environment secrets, and makes
    no network calls. Callers pass already-loaded Hermes config.
    """
    ui_cfg = _as_dict((loaded_config or {}).get("ui_work"))
    enabled = bool(ui_cfg.get("enabled", False))
    fallback_cfg = _as_dict(ui_cfg.get("fallback"))
    fallback_allowed = bool(fallback_cfg.get("allow_default_worker", True))
    matched, reason = _classify_ui_work(
        config=ui_cfg,
        task=task,
        title=title,
        context=context,
        cwd=cwd,
        project=project,
    )
    provider = str(ui_cfg.get("provider") or "").strip()
    model = str(ui_cfg.get("model") or "").strip()
    normalized_backend = str(backend or "codex").strip().lower() or "codex"

    if not matched:
        return UIWorkRouteDecision(
            matched=False,
            enabled=enabled,
            reason=reason,
            provider=provider,
            model=model,
            backend=normalized_backend,
            fallback_allowed=fallback_allowed,
        )
    if not enabled:
        return UIWorkRouteDecision(
            matched=True,
            enabled=False,
            reason="ui routing disabled",
            provider=provider,
            model=model,
            backend=normalized_backend,
            fallback_allowed=fallback_allowed,
        )
    if not provider or not model:
        error = "ui_work routing matched but ui_work.provider and ui_work.model must both be configured"
        return UIWorkRouteDecision(
            matched=True,
            enabled=True,
            reason="missing provider/model",
            provider=provider,
            model=model,
            backend=normalized_backend,
            fallback_allowed=fallback_allowed,
            error=error,
        )

    backend_cfg = _as_dict(ui_cfg.get(normalized_backend))
    if normalized_backend == "codex":
        provider_key = str(backend_cfg.get("provider_config_key") or "model_provider").strip()
        model_key = str(backend_cfg.get("model_config_key") or "model").strip()
        if not provider_key or not model_key:
            error = "ui_work routing matched but ui_work.codex provider/model config keys are incomplete"
            return UIWorkRouteDecision(
                matched=True,
                enabled=True,
                reason="incomplete codex config",
                provider=provider,
                model=model,
                backend=normalized_backend,
                fallback_allowed=fallback_allowed,
                error=error,
            )
        route_backend_config = {
            "provider_config_key": provider_key,
            "model_config_key": model_key,
            "extra_args": _as_list(backend_cfg.get("extra_args")),
        }
    else:
        route_backend_config = _as_dict(backend_cfg)
        if not route_backend_config:
            return UIWorkRouteDecision(
                matched=True,
                enabled=True,
                reason=f"missing {normalized_backend} config",
                provider=provider,
                model=model,
                backend=normalized_backend,
                fallback_allowed=fallback_allowed,
                error=f"ui_work routing matched but ui_work.{normalized_backend} is not configured",
            )

    return UIWorkRouteDecision(
        matched=True,
        enabled=True,
        reason=reason,
        provider=provider,
        model=model,
        backend=normalized_backend,
        backend_config=route_backend_config,
        fallback_allowed=fallback_allowed,
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
