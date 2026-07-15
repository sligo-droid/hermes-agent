"""Pure, safe visual-QA requirement and receipt helpers.

This module deliberately does not know how a browser, vision model, or worker
is run.  It defines the compact contract those callers exchange and keeps that
contract safe to include in a runtime breakdown or work-item handoff.
"""

from __future__ import annotations

import re
from typing import Any, Iterable


VISUAL_QA_MODES = frozenset({"off", "shadow", "enforce_explicit"})
VISUAL_QA_LEVELS = frozenset({"none", "surface", "artifact"})
VISUAL_QA_STATUSES = frozenset({"passed", "failed", "blocked"})

_MAX_TARGET_CHARS = 120
_MAX_ASSERTION_CHARS = 240
_MAX_CHECK_CHARS = 120
_MAX_EVIDENCE_REF_CHARS = 240
_MAX_ASSERTIONS = 6
_UNSAFE_REFERENCE_RE = re.compile(
    r"(?:https?://|\b(?:cookie|set-cookie|authorization|bearer)\b|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b|"
    r"(?:^|[?&])[^\s=&]*(?:token|key|secret|password)[^\s=&]*=)",
    re.IGNORECASE,
)
_ACTION_RE = re.compile(
    r"\b(?:add|build|change|create|fix|implement|make|redesign|render|replace|update)\b",
    re.IGNORECASE,
)
_DOC_ONLY_RE = re.compile(r"\b(?:docs?|documentation|readme|changelog)\b", re.IGNORECASE)
_CONCRETE_IMPLEMENTATION_ACTION_RE = re.compile(
    r"\b(?:add|build|create|fix|implement|redesign|render|replace)\b",
    re.IGNORECASE,
)
_ARTIFACT_RE = re.compile(
    r"\b(?:png|pdf|canvas|image|jpeg|jpg|webp|svg|(?:rendered?|downloadable?)\s+"
    r"(?:artifact|export)|export(?:ed|ing)?\s+(?:image|png|pdf|artifact))\b",
    re.IGNORECASE,
)
_SURFACE_RE = re.compile(
    r"\b(?:ui|user interface|layout|responsive|viewport|mobile|desktop|toolbar|"
    r"sidebar|modal|dialog|control|button|chart|map|dashboard|page|screen|overflow)\b",
    re.IGNORECASE,
)
_SAFE_TEXT_RE = re.compile(r"[^A-Za-z0-9 .,:;()/_+-]+")


def _clean_text(value: Any, *, limit: int) -> str:
    text = " ".join(str(value or "").split())
    text = _SAFE_TEXT_RE.sub("", text).strip(" .,:;/-")
    return text[:limit].rstrip()


def _safe_reference(value: Any) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > _MAX_EVIDENCE_REF_CHARS or _UNSAFE_REFERENCE_RE.search(text):
        return ""
    return _clean_text(text, limit=_MAX_EVIDENCE_REF_CHARS)


def _safe_requirement_text(value: Any, *, limit: int) -> str:
    """Return display-safe request metadata, dropping credential-bearing text."""
    text = " ".join(str(value or "").split())
    if not text or _UNSAFE_REFERENCE_RE.search(text):
        return ""
    return _clean_text(text, limit=limit)


def _bounded_safe_value(value: Any, *, limit: int) -> bool:
    """Reject (rather than silently truncate) possible raw tool output/secrets."""
    text = " ".join(str(value or "").split())
    return bool(text) and len(text) <= limit and not _UNSAFE_REFERENCE_RE.search(text)


def normalize_visual_qa_config(config: Any = None) -> dict[str, Any]:
    """Normalize ``agent.visual_qa`` config to bounded, non-enforcing defaults.

    ``config`` may be the visual-QA section, an ``agent`` mapping, or the full
    application config.  Invalid values always fall back to ``shadow``.
    """
    raw = config if isinstance(config, dict) else {}
    if isinstance(raw.get("agent"), dict):
        raw = raw["agent"].get("visual_qa") if isinstance(raw["agent"].get("visual_qa"), dict) else {}
    elif isinstance(raw.get("visual_qa"), dict):
        raw = raw["visual_qa"]
    mode = str(raw.get("mode") or "shadow").strip().lower()
    if mode not in VISUAL_QA_MODES:
        mode = "shadow"
    # These are deliberately fixed caps.  A bad config must not turn a single
    # visual check into an unbounded retry/output channel.
    return {"mode": mode, "max_receipts_per_turn": 1, "max_followup_turns": 1}


def normalize_visual_requirement(value: Any) -> dict[str, Any]:
    """Return the stable public requirement shape, or a ``none`` requirement."""
    raw = value if isinstance(value, dict) else {}
    level = str(raw.get("level") or "none").strip().lower()
    if level not in VISUAL_QA_LEVELS:
        level = "none"
    target = _safe_requirement_text(raw.get("target"), limit=_MAX_TARGET_CHARS)
    assertions: list[str] = []
    for item in raw.get("assertions") or []:
        assertion = _safe_requirement_text(item, limit=_MAX_ASSERTION_CHARS)
        if assertion and assertion not in assertions:
            assertions.append(assertion)
        if len(assertions) >= _MAX_ASSERTIONS:
            break
    if level == "none":
        return {"level": "none", "target": "", "assertions": []}
    if not target or not assertions:
        # A visual level without a concrete target/assertion cannot require a
        # receipt.  This prevents broad keyword detection from becoming a gate.
        return {"level": "none", "target": "", "assertions": []}
    return {"level": level, "target": target, "assertions": assertions}


def _target_from_text(text: str, level: str) -> str:
    if level == "artifact":
        match = re.search(r".{0,45}\b(?:png|pdf|canvas|image|svg|export)\b.{0,45}", text, re.IGNORECASE)
    else:
        match = re.search(r".{0,45}" + _SURFACE_RE.pattern + r".{0,45}", text, re.IGNORECASE)
    target = _safe_requirement_text(match.group(0) if match else "", limit=_MAX_TARGET_CHARS)
    return target or ("rendered artifact" if level == "artifact" else "rendered surface")


def _assertions_from_text(text: str, level: str, target: str) -> list[str]:
    candidates: list[str] = []
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text):
        if not sentence.strip():
            continue
        if level == "artifact" and _ARTIFACT_RE.search(sentence):
            candidates.append(sentence)
        elif level == "surface" and _SURFACE_RE.search(sentence):
            candidates.append(sentence)
    cleaned = [_safe_requirement_text(item, limit=_MAX_ASSERTION_CHARS) for item in candidates]
    cleaned = [item for item in cleaned if item]
    if cleaned:
        return cleaned[:_MAX_ASSERTIONS]
    # A classified explicit implementation request still gets a narrowly
    # phrased assertion.  It is a receipt requirement, not a claim that a
    # generic navigation already proved the result.
    if level == "artifact":
        return [f"{target} renders as requested without clipping or missing content"]
    return [f"{target} renders at the requested viewport without unintended overflow"]


def classify_visual_requirement(
    request_text: Any,
    *,
    worker_route: Any = None,
    changed_paths: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Conservatively classify an explicit implementation request.

    Route/path metadata may support an already actionable request but cannot
    turn a review, documentation, or screenshot-reading request into visual
    work by itself.
    """
    text = " ".join(str(request_text or "").split())
    if not text or not _ACTION_RE.search(text):
        return {"level": "none", "target": "", "assertions": []}
    # A concrete implementation action wins over review/audit framing (for
    # example, "review the dashboard and fix mobile overflow").  Generic
    # update/change/make verbs remain insufficient for documentation-only work.
    if _DOC_ONLY_RE.search(text) and not _CONCRETE_IMPLEMENTATION_ACTION_RE.search(text):
        return {"level": "none", "target": "", "assertions": []}
    level = "artifact" if _ARTIFACT_RE.search(text) else ("surface" if _SURFACE_RE.search(text) else "none")
    if level == "none":
        return {"level": "none", "target": "", "assertions": []}
    # Metadata is intentionally not a classifier signal.  Reading it avoids
    # callers having to special-case the API, while keeping routing advisory.
    _ = worker_route, changed_paths
    target = _target_from_text(text, level)
    return normalize_visual_requirement(
        {"level": level, "target": target, "assertions": _assertions_from_text(text, level, target)}
    )


def sanitize_visual_receipt(receipt: Any, requirement: Any = None) -> dict[str, Any] | None:
    """Validate and serialize one safe, assertion-driven visual receipt.

    The source must explicitly provide every field.  In particular, this never
    promotes a successful browser navigation, screenshot, or console call into
    a receipt by inference.
    """
    raw = receipt if isinstance(receipt, dict) else {}
    raw_assertions = raw.get("assertions")
    if not isinstance(raw_assertions, (list, tuple)) or not raw_assertions or len(raw_assertions) > _MAX_ASSERTIONS:
        return None
    if not _bounded_safe_value(raw.get("target"), limit=_MAX_TARGET_CHARS):
        return None
    if not _bounded_safe_value(raw.get("check") or raw.get("check_type"), limit=_MAX_CHECK_CHARS):
        return None
    if any(not _bounded_safe_value(item, limit=_MAX_ASSERTION_CHARS) for item in raw_assertions):
        return None
    level = str(raw.get("level") or "").strip().lower()
    target = _clean_text(raw.get("target"), limit=_MAX_TARGET_CHARS)
    assertions = [_clean_text(item, limit=_MAX_ASSERTION_CHARS) for item in raw.get("assertions") or []]
    assertions = list(dict.fromkeys(item for item in assertions if item))[:_MAX_ASSERTIONS]
    check = _clean_text(raw.get("check") or raw.get("check_type"), limit=_MAX_CHECK_CHARS)
    status = str(raw.get("status") or "").strip().lower()
    status = {"pass": "passed", "success": "passed", "fail": "failed", "failure": "failed"}.get(status, status)
    evidence_ref = _safe_reference(raw.get("evidence_ref") or raw.get("evidence"))
    if level not in VISUAL_QA_LEVELS - {"none"} or not target or not assertions or not check:
        return None
    if status not in VISUAL_QA_STATUSES or not evidence_ref:
        return None
    requirement_value = normalize_visual_requirement(requirement)
    if requirement_value["level"] != "none":
        if level != requirement_value["level"] or target != requirement_value["target"]:
            return None
        if not set(requirement_value["assertions"]).issubset(assertions):
            return None
    safe = {
        "level": level,
        "target": target,
        "assertions": assertions,
        "check": check,
        "status": status,
        "evidence_ref": evidence_ref,
    }
    try:
        order = int(raw.get("order") or 0)
    except (TypeError, ValueError):
        order = 0
    if order > 0:
        safe["order"] = order
    return safe


def visual_receipt_completion(
    requirement: Any,
    receipts: Any,
    *,
    min_order: int = 0,
) -> dict[str, Any]:
    """Return whether the latest matching, fresh receipt satisfies a requirement."""
    normalized = normalize_visual_requirement(requirement)
    if normalized["level"] == "none":
        return {"status": "not_applicable", "receipt": None}
    latest: dict[str, Any] | None = None
    for raw in receipts if isinstance(receipts, list) else []:
        receipt = sanitize_visual_receipt(raw, normalized)
        if receipt is None or int(receipt.get("order") or 0) < max(0, int(min_order or 0)):
            continue
        if latest is None or int(receipt.get("order") or 0) >= int(latest.get("order") or 0):
            latest = receipt
    if latest is None:
        return {"status": "missing", "receipt": None}
    return {"status": str(latest["status"]), "receipt": latest}


def build_visual_qa_followup_nudge(
    requirement: Any,
    changed_paths: Iterable[Any],
    receipts: Any,
    *,
    attempts: int = 0,
    max_attempts: int = 1,
    min_order: int = 0,
) -> str | None:
    """Return one focused visual-QA follow-up only when a receipt is missing."""
    normalized = normalize_visual_requirement(requirement)
    paths = [str(path) for path in changed_paths or [] if str(path).strip()]
    if normalized["level"] == "none" or not paths or attempts >= max(0, max_attempts):
        return None
    completion = visual_receipt_completion(normalized, receipts, min_order=min_order)
    if completion["status"] == "passed":
        return None
    assertion_text = "; ".join(normalized["assertions"])
    return (
        "[System: This explicit visual change needs one compact visual-QA receipt before finishing. "
        f"Inspect the smallest relevant {normalized['level']} target `{normalized['target']}` and check: "
        f"{assertion_text}. On the terminal, browser, or vision tool call, attach "
        "`visual_qa_receipt: {level, target, assertions, check, status, evidence_ref}` with an explicit "
        "passed/failed/blocked status and a safe textual evidence reference. Do not treat navigation, "
        "a generic screenshot, or console success as proof; if inspection is unavailable, record the concrete blocker.]"
    )


__all__ = [
    "VISUAL_QA_LEVELS",
    "VISUAL_QA_MODES",
    "VISUAL_QA_STATUSES",
    "build_visual_qa_followup_nudge",
    "classify_visual_requirement",
    "normalize_visual_qa_config",
    "normalize_visual_requirement",
    "sanitize_visual_receipt",
    "visual_receipt_completion",
]
