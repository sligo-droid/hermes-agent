"""Deterministic routing for UI-shaped coding work."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_.+-]*")


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


def _path_hints(cwd: str | os.PathLike[str] | None) -> str:
    if not cwd:
        return ""
    try:
        path = Path(cwd).expanduser()
    except TypeError:
        return str(cwd)
    parts = list(path.parts[-4:])
    return " ".join(parts)


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
    positive_keywords = _as_list(detection.get("title_body_keywords"))
    negative_keywords = _as_list(detection.get("negative_keywords"))

    body_text = _normalize_text(title, task, context)
    hint_text = _normalize_text(project, _path_hints(cwd))
    all_text = _normalize_text(body_text, hint_text)

    negative = _first_match(body_text, negative_keywords)
    if negative:
        return False, f"negative keyword: {negative}"

    positive = _first_match(body_text, positive_keywords)
    if positive:
        return True, f"ui keyword: {positive}"

    # Project/cwd names are intentionally weak. They can corroborate explicit
    # UI words, but a project named PID must not route all work to the UI model.
    if "pid" in set(_WORD_RE.findall(hint_text)):
        weak_ui = _first_match(body_text, ["dashboard", "frontend", "ui", "ux", "tui"])
        if weak_ui:
            return True, f"pid ui keyword: {weak_ui}"

    if _first_match(all_text, positive_keywords) and _first_match(body_text, ["dashboard", "frontend", "ui", "ux", "tui"]):
        return True, "ui keyword with project hint"

    return False, "no ui keyword"


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
        if fallback_allowed:
            error = ""
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
            if fallback_allowed:
                error = ""
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
        if not route_backend_config and not fallback_allowed:
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
    args.extend(str(item) for item in cfg.get("extra_args") or [])
    return args
