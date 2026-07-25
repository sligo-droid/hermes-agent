"""Pure, safe visual-QA requirement and receipt helpers.

This module deliberately does not know how a browser, vision model, or worker
is run.  It defines the compact contract those callers exchange and keeps that
contract safe to include in a runtime breakdown or work-item handoff.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import re
from typing import Any, Iterable

from agent.request_text import flatten_request_for_matching, merge_request_fragments


VISUAL_QA_MODES = frozenset({"off", "shadow", "enforce_explicit"})
VISUAL_QA_LEVELS = frozenset({"none", "surface", "artifact"})
VISUAL_QA_STATUSES = frozenset({"passed", "failed", "blocked", "uncertain"})
ASSERTION_RESULT_CODES = frozenset(
    {
        "appearance_mismatch",
        "appearance_satisfied",
        "appearance_uncertain",
        "attempt_timeout",
        "count_mismatch",
        "count_satisfied",
        "element_lookup_unavailable",
        "exists_mismatch",
        "exists_satisfied",
        "invalid_assertion_code",
        "invalid_diagnostic_cursor",
        "diagnostic_history_evicted",
        "invalid_vision_input",
        "invalid_vision_output",
        "new_page_diagnostics",
        "no_horizontal_overflow_mismatch",
        "no_horizontal_overflow_satisfied",
        "no_new_diagnostics",
        "not_exists_mismatch",
        "not_exists_satisfied",
        "screenshot_unavailable",
        "text_check_unavailable",
        "text_missing",
        "text_present",
        "unsupported_assertion",
        "viewport_contained_mismatch",
        "viewport_contained_satisfied",
        "visible_mismatch",
        "visible_satisfied",
        "vision_budget_exhausted",
        "vision_call_failed",
    }
)

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
_RECEIPT_ID_RE = re.compile(r"^(?:vrq|vac)_[0-9a-f]{24}$")
_OPAQUE_REQUIREMENT_RE = re.compile(r"^(?:vtarget|vassert)_[0-9a-f]{24}$")
_ASSERTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")
_HORIZONTAL_OVERFLOW_RE = re.compile(
    r"\b(?:horizontal(?:ly)?\s+(?:overflow|scroll)|overflow(?:ing)?\s+(?:horizontally|on\s+the\s+x[- ]axis)|x[- ]axis\s+overflow)\b",
    re.IGNORECASE,
)
_VIEWPORT_CONTAINMENT_RE = re.compile(
    r"\b(?:inside|within|contained\s+(?:inside|within))\b.{0,40}\bviewport\b|"
    r"\bviewport\b.{0,40}\b(?:inside|within|contained|bounds?)\b",
    re.IGNORECASE,
)
_HOST_ASSERTION_KINDS = frozenset(
    {"no_horizontal_overflow", "viewport_contained", "screenshot_appearance"}
)
_ACTIVE_VISUAL_REQUIREMENT: contextvars.ContextVar[dict[str, Any] | None] = (
    contextvars.ContextVar("hermes_active_visual_requirement", default=None)
)


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


def _clamped_int(raw: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _clamped_float(raw: Any, *, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def normalize_visual_qa_config(config: Any = None) -> dict[str, Any]:
    """Normalize ``agent.visual_qa`` into a hard-bounded execution contract.

    ``config`` may be the visual-QA section, an ``agent`` mapping, or the full
    application config. Invalid modes fall back to non-enforcing ``shadow``.
    Every work/output budget has an implementation-owned ceiling.
    """
    raw = config if isinstance(config, dict) else {}
    if isinstance(raw.get("agent"), dict):
        raw = raw["agent"].get("visual_qa") if isinstance(raw["agent"].get("visual_qa"), dict) else {}
    elif isinstance(raw.get("visual_qa"), dict):
        raw = raw["visual_qa"]
    mode = str(raw.get("mode") or "shadow").strip().lower()
    if mode not in VISUAL_QA_MODES:
        mode = "shadow"
    max_vision_calls = _clamped_int(
        raw.get("max_vision_calls"), default=2, minimum=0, maximum=2
    )
    # A positive visual-QA budget is the mandatory Luna sweep plus Sonnet
    # assertion inspection. Preserve zero as an explicit no-vision setting,
    # but upgrade legacy one-call configuration to the required pair.
    if max_vision_calls == 1:
        max_vision_calls = 2
    return {
        "mode": mode,
        "max_receipts_per_turn": 1,
        "max_followup_turns": 1,
        "max_attempts": _clamped_int(
            raw.get("max_attempts"), default=2, minimum=1, maximum=2
        ),
        "max_assertions": _clamped_int(
            raw.get("max_assertions"), default=6, minimum=1, maximum=6
        ),
        "max_vision_calls": max_vision_calls,
        "attempt_timeout_s": _clamped_float(
            raw.get("attempt_timeout_s"), default=30.0, minimum=1.0, maximum=30.0
        ),
        "total_timeout_s": _clamped_float(
            raw.get("total_timeout_s"), default=60.0, minimum=1.0, maximum=60.0
        ),
        "max_output_chars": _clamped_int(
            raw.get("max_output_chars"), default=6000, minimum=512, maximum=6000
        ),
    }


def _opaque_requirement_value(value: Any, prefix: str) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    if _OPAQUE_REQUIREMENT_RE.fullmatch(text) and text.startswith(f"{prefix}_"):
        return text
    bounded = text[:4096]
    encoded = f"{len(text)}:{bounded}".encode("utf-8")
    return f"{prefix}_" + hashlib.sha256(encoded).hexdigest()[:24]


def _required_assertion_kind(text: Any) -> str:
    """Derive the host-owned executable kind before requirement prose is dropped."""

    value = " ".join(str(text or "").split())
    if _HORIZONTAL_OVERFLOW_RE.search(value):
        return "no_horizontal_overflow"
    if _VIEWPORT_CONTAINMENT_RE.search(value):
        return "viewport_contained"
    return "screenshot_appearance"


def normalize_visual_requirement(value: Any) -> dict[str, Any]:
    """Return an opaque durable requirement, or a ``none`` requirement.

    Fresh host assertions retain only an opaque ID and a deterministic required
    kind. Legacy opaque-string assertions remain recognizable but intentionally
    non-covering because their required kind cannot be recovered safely.
    """
    raw = value if isinstance(value, dict) else {}
    level = str(raw.get("level") or "none").strip().lower()
    if level not in VISUAL_QA_LEVELS:
        level = "none"
    target = _opaque_requirement_value(raw.get("target"), "vtarget")
    assertions: list[Any] = []
    seen_ids: set[str] = set()
    for item in raw.get("assertions") or []:
        if isinstance(item, dict):
            raw_id = item.get("id")
            assertion_id = _opaque_requirement_value(raw_id, "vassert")
            kind = str(item.get("kind") or "").strip().lower()
            if (
                assertion_id
                and assertion_id not in seen_ids
                and _OPAQUE_REQUIREMENT_RE.fullmatch(str(raw_id or "").strip())
                and kind in _HOST_ASSERTION_KINDS
            ):
                assertions.append({"id": assertion_id, "kind": kind})
                seen_ids.add(assertion_id)
        else:
            raw_text = " ".join(str(item or "").split())
            assertion_id = _opaque_requirement_value(raw_text, "vassert")
            if not assertion_id or assertion_id in seen_ids:
                continue
            if _OPAQUE_REQUIREMENT_RE.fullmatch(raw_text):
                # Older durable contracts had IDs but no trustworthy kind.
                assertions.append(assertion_id)
            else:
                assertions.append(
                    {"id": assertion_id, "kind": _required_assertion_kind(raw_text)}
                )
            seen_ids.add(assertion_id)
        if len(assertions) >= _MAX_ASSERTIONS:
            break
    if level == "none":
        return {"level": "none", "target": "", "assertions": []}
    if not target or not assertions:
        # A visual level without a concrete target/assertion cannot require a
        # receipt. This prevents broad keyword detection from becoming a gate.
        return {"level": "none", "target": "", "assertions": []}
    return {"level": level, "target": target, "assertions": assertions}


def visual_requirement_id(value: Any) -> str:
    """Return a stable opaque ID for one normalized trusted requirement."""

    normalized = normalize_visual_requirement(value)
    if normalized["level"] == "none":
        return ""
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "vrq_" + hashlib.sha256(encoded).hexdigest()[:24]


def set_active_visual_requirement(value: Any) -> None:
    """Bind the trusted visual requirement to the current task context."""

    normalized = normalize_visual_requirement(value)
    _ACTIVE_VISUAL_REQUIREMENT.set(
        normalized if normalized["level"] != "none" else None
    )


def get_active_visual_requirement() -> dict[str, Any]:
    """Return a copy of the current task-local trusted requirement."""

    value = _ACTIVE_VISUAL_REQUIREMENT.get()
    return dict(value) if isinstance(value, dict) else {
        "level": "none",
        "target": "",
        "assertions": [],
    }


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
    structured_text = merge_request_fragments(request_text)
    text = flatten_request_for_matching(structured_text)
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
        {
            "level": level,
            "target": target,
            "assertions": _assertions_from_text(structured_text, level, target),
        }
    )


def sanitize_visual_receipt(receipt: Any, requirement: Any = None) -> dict[str, Any] | None:
    """Validate one prose-free, assertion-driven durable receipt.

    Durable receipts contain only opaque requirement/contract identifiers,
    assertion identifiers, statuses, bounded counters, and allowlisted host
    diagnostic codes. Selectors, literal text, appearance prose, URLs, and
    evidence descriptions are never retained.
    """

    raw = receipt if isinstance(receipt, dict) else {}
    allowed_fields = {
        "requirement_id",
        "contract_id",
        "assertion_ids",
        "status",
        "attempts",
        "vision_calls",
        "duration_ms",
        "diagnostic_codes",
        "order",
    }
    if set(raw) - allowed_fields:
        return None
    requirement_id = str(raw.get("requirement_id") or "").strip().lower()
    contract_id = str(raw.get("contract_id") or "").strip().lower()
    assertion_ids = raw.get("assertion_ids")
    status = str(raw.get("status") or "").strip().lower()
    status = {
        "pass": "passed",
        "success": "passed",
        "fail": "failed",
        "failure": "failed",
    }.get(status, status)
    if (
        not _RECEIPT_ID_RE.fullmatch(requirement_id)
        or not requirement_id.startswith("vrq_")
        or not _RECEIPT_ID_RE.fullmatch(contract_id)
        or not contract_id.startswith("vac_")
        or status not in VISUAL_QA_STATUSES
        or not isinstance(assertion_ids, (list, tuple))
        or not assertion_ids
        or len(assertion_ids) > _MAX_ASSERTIONS
    ):
        return None
    normalized_ids: list[str] = []
    for item in assertion_ids:
        assertion_id = str(item or "").strip()
        if not _ASSERTION_ID_RE.fullmatch(assertion_id) or assertion_id in normalized_ids:
            return None
        normalized_ids.append(assertion_id)

    requirement_value = normalize_visual_requirement(requirement)
    expected_requirement_id = visual_requirement_id(requirement_value)
    if expected_requirement_id and requirement_id != expected_requirement_id:
        return None
    if expected_requirement_id:
        required_assertions = requirement_value.get("assertions") or []
        if not all(isinstance(item, dict) for item in required_assertions):
            return None
        required_ids = [str(item.get("id") or "") for item in required_assertions]
        if len(required_ids) != len(normalized_ids) or set(required_ids) != set(normalized_ids):
            return None

    def _metric(name: str, maximum: int) -> int:
        try:
            value = int(raw.get(name) or 0)
        except (TypeError, ValueError):
            value = 0
        return max(0, min(value, maximum))

    diagnostic_codes: list[str] = []
    raw_diagnostic_codes = raw.get("diagnostic_codes")
    if raw_diagnostic_codes is not None and not isinstance(raw_diagnostic_codes, list):
        return None
    for item in raw_diagnostic_codes or []:
        code = str(item or "").strip().lower()
        if code not in ASSERTION_RESULT_CODES:
            return None
        if code not in diagnostic_codes:
            diagnostic_codes.append(code)
        if len(diagnostic_codes) >= 12:
            break

    safe: dict[str, Any] = {
        "requirement_id": requirement_id,
        "contract_id": contract_id,
        "assertion_ids": normalized_ids,
        "status": status,
        "attempts": _metric("attempts", 2),
        "vision_calls": _metric("vision_calls", 1),
        "duration_ms": _metric("duration_ms", 60_000),
        "diagnostic_codes": diagnostic_codes,
    }
    order = _metric("order", 2_147_483_647)
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
    return (
        "[System: This explicit visual change needs one compact visual-QA receipt before finishing. "
        f"You must call the `visual_qa` tool for the smallest relevant {normalized['level']} target "
        "described by the accepted request and check every classified visual requirement. "
        "Use declarative assertions and an explicit passed/failed/blocked/uncertain result. "
        "Do not attach receipt arguments to terminal, "
        "browser, or vision tools. Do not treat navigation, a generic screenshot, or console success "
        "as proof; if inspection is unavailable, let `visual_qa` record the concrete blocker.]"
    )


__all__ = [
    "VISUAL_QA_LEVELS",
    "VISUAL_QA_MODES",
    "VISUAL_QA_STATUSES",
    "build_visual_qa_followup_nudge",
    "classify_visual_requirement",
    "get_active_visual_requirement",
    "normalize_visual_qa_config",
    "normalize_visual_requirement",
    "sanitize_visual_receipt",
    "set_active_visual_requirement",
    "visual_receipt_completion",
    "visual_requirement_id",
]
