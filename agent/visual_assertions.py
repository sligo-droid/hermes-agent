"""Validation and aggregation for trusted declarative visual assertions."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from agent.visual_qa import ASSERTION_RESULT_CODES, VISUAL_QA_STATUSES


ASSERTION_KINDS = frozenset(
    {
        "exists",
        "not_exists",
        "visible",
        "viewport_contained",
        "no_horizontal_overflow",
        "count",
        "text_present",
        "screenshot_appearance",
        "no_new_diagnostics",
    }
)
_LOCATOR_KINDS = frozenset({"test_id", "role", "css"})
_ELEMENT_KINDS = frozenset(
    {
        "exists",
        "not_exists",
        "visible",
        "viewport_contained",
        "no_horizontal_overflow",
        "count",
    }
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")
_UNSAFE_RE = re.compile(
    r"(?:https?://|\b(?:authorization|bearer|cookie|password|secret|token)\b)",
    re.IGNORECASE,
)
_UNSAFE_CSS_RE = re.compile(r"[{};]|(?:javascript|expression|fetch|eval)\s*\(", re.IGNORECASE)
_DIAGNOSTIC_CURSOR_RE = re.compile(r"^dcur_[0-9]+_[0-9a-f]{24}$")


def _bounded_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if not text or len(text) > limit or _UNSAFE_RE.search(text):
        return ""
    return text


def _normalize_locator(value: Any) -> dict[str, str] | None:
    raw = value if isinstance(value, dict) else {}
    by = str(raw.get("by") or "").strip().lower()
    locator_value = _bounded_text(raw.get("value"), 200)
    if by not in _LOCATOR_KINDS or not locator_value:
        return None
    if by == "css" and _UNSAFE_CSS_RE.search(locator_value):
        return None
    locator = {"by": by, "value": locator_value}
    if by == "role":
        name = _bounded_text(raw.get("name"), 120)
        if name:
            locator["name"] = name
    return locator


def validate_visual_assertions(
    value: Any,
    *,
    max_assertions: int = 6,
) -> list[dict[str, Any]]:
    """Return a bounded trusted schema; invalid entries are omitted."""

    try:
        limit = max(1, min(int(max_assertions), 6))
    except (TypeError, ValueError):
        limit = 6
    if not isinstance(value, list):
        return []

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw_value in value:
        raw = raw_value if isinstance(raw_value, dict) else {}
        assertion_id = str(raw.get("id") or "").strip()
        kind = str(raw.get("kind") or "").strip().lower()
        if (
            not _ID_RE.fullmatch(assertion_id)
            or assertion_id in seen_ids
            or kind not in ASSERTION_KINDS
        ):
            continue
        item: dict[str, Any] = {"id": assertion_id, "kind": kind}
        if kind in _ELEMENT_KINDS:
            locator = _normalize_locator(raw.get("locator"))
            if locator is None:
                continue
            item["locator"] = locator
        if kind == "count":
            try:
                minimum = max(0, min(int(raw.get("min", 0)), 10_000))
                maximum = max(minimum, min(int(raw.get("max", 10_000)), 10_000))
            except (TypeError, ValueError):
                continue
            item.update({"min": minimum, "max": maximum})
        elif kind == "text_present":
            text = _bounded_text(raw.get("text"), 80)
            if not text or raw.get("policy") != "literal_request_text":
                continue
            item.update({"text": text, "policy": "literal_request_text"})
        elif kind == "screenshot_appearance":
            expectation = _bounded_text(raw.get("expectation"), 240)
            if not expectation:
                continue
            item["expectation"] = expectation
        elif kind == "no_new_diagnostics":
            cursor = str(raw.get("cursor") or "").strip().lower()
            if not _DIAGNOSTIC_CURSOR_RE.fullmatch(cursor):
                continue
            item["cursor"] = cursor
        normalized.append(item)
        seen_ids.add(assertion_id)
        if len(normalized) >= limit:
            break
    return normalized


def visual_assertion_contract_id(assertions: Any) -> str:
    """Hash the validated executable assertion contract into an opaque ID."""

    normalized = validate_visual_assertions(assertions)
    if not normalized:
        return ""
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "vac_" + hashlib.sha256(encoded).hexdigest()[:24]


def sanitize_assertion_result(value: Any) -> dict[str, Any] | None:
    raw = value if isinstance(value, dict) else {}
    assertion_id = str(raw.get("id") or "").strip()
    status = str(raw.get("status") or "").strip().lower()
    if not _ID_RE.fullmatch(assertion_id) or status not in VISUAL_QA_STATUSES:
        return None
    code = str(raw.get("code") or "").strip().lower()
    if code not in ASSERTION_RESULT_CODES:
        code = "invalid_assertion_code"
        status = "uncertain"
    result = {"id": assertion_id, "status": status, "code": code}
    confidence = str(raw.get("confidence") or "").strip().lower()
    if confidence in {"high", "medium", "low"}:
        result["confidence"] = confidence
    return result


def aggregate_assertion_results(value: Any) -> dict[str, Any]:
    """Aggregate compact results; only an all-passed set can pass."""

    results: list[dict[str, Any]] = []
    for raw in value if isinstance(value, list) else []:
        result = sanitize_assertion_result(raw)
        if result is not None:
            results.append(result)
    statuses = {item["status"] for item in results}
    if not results:
        status = "uncertain"
    elif "failed" in statuses:
        status = "failed"
    elif "blocked" in statuses:
        status = "blocked"
    elif "uncertain" in statuses:
        status = "uncertain"
    elif statuses == {"passed"}:
        status = "passed"
    else:
        status = "uncertain"
    return {"status": status, "results": results[:6]}


__all__ = [
    "ASSERTION_KINDS",
    "ASSERTION_RESULT_CODES",
    "aggregate_assertion_results",
    "sanitize_assertion_result",
    "validate_visual_assertions",
    "visual_assertion_contract_id",
]
