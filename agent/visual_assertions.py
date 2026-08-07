"""Validation and aggregation for trusted declarative visual assertions."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from agent.visual_qa import (
    ASSERTION_RESULT_CODES,
    VISUAL_QA_STATUSES,
    normalize_visual_requirement,
    visual_requirement_uses_orchestrator_contract,
)


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
_HOST_ASSERTION_ID_RE = re.compile(r"^vassert_[0-9a-f]{24}$")
_EXECUTION_CONTRACT_ID_RE = re.compile(r"^vac_[0-9a-f]{24}$")
_UNSAFE_RE = re.compile(
    r"(?:https?://|\b(?:authorization|bearer|cookie|password|secret|token)\b|"
    r"\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token)\b)",
    re.IGNORECASE,
)
_UNSAFE_CSS_RE = re.compile(r"[{};]|(?:javascript|expression|fetch|eval)\s*\(", re.IGNORECASE)
_DIAGNOSTIC_CURSOR_RE = re.compile(r"^dcur_[0-9]+_[0-9a-f]{24}$")
_PAGE_STATES = frozenset({"already_open", "prepared"})
_SCREENSHOT_ARTIFACT_KINDS = frozenset({"context", "focused", "responsive"})
_MAX_TARGET_DESCRIPTION = 160
_MAX_PAGE_DESCRIPTION = 160
_MAX_VIEWPORT_DESCRIPTION = 120
_MAX_STATE_DESCRIPTION = 160
_MAX_STATE_ITEMS = 4
_MAX_SCREENSHOT_ARTIFACTS = 4

_CONTRACT_CORRECTIONS = {
    "contract_not_object": "Provide one visual contract object.",
    "contract_unknown_fields": "Use only target, page, viewport, state, artifacts, and assertions.",
    "contract_missing_fields": "Provide target, page, viewport, state, and at least one assertion.",
    "contract_invalid_target": "Provide a bounded target description and an optional trusted locator.",
    "contract_invalid_page": "Provide an already_open or prepared page with a bounded description.",
    "contract_invalid_viewport": "Provide viewport.description and either both bounded width and height or neither.",
    "contract_invalid_state": "Provide one to four distinct bounded state assumptions.",
    "contract_invalid_artifacts": "Provide one to four valid, distinct screenshot artifact specifications.",
    "contract_assertion_limit": "Provide no more than the configured bounded assertion count.",
    "contract_invalid_assertion": "Use only supported assertion fields with all required values.",
    "contract_duplicate_assertion": "Remove duplicate assertions from the visual contract.",
    "contract_missing_appearance": "Include at least one bounded screenshot_appearance assertion.",
}


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
            or set(raw) - _assertion_allowed_fields(kind)
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


def _opaque_contract_id(prefix: str, value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"{prefix}_" + hashlib.sha256(encoded).hexdigest()[:24]


def _normalize_contract_target(value: Any) -> dict[str, Any] | None:
    raw = value if isinstance(value, dict) else {}
    if set(raw) - {"description", "locator"}:
        return None
    description = _bounded_text(raw.get("description"), _MAX_TARGET_DESCRIPTION)
    if not description:
        return None
    target: dict[str, Any] = {"description": description}
    if raw.get("locator") is not None:
        locator = _normalize_locator(raw.get("locator"))
        if locator is None:
            return None
        target["locator"] = locator
    return target


def _normalize_contract_page(value: Any) -> dict[str, str] | None:
    raw = value if isinstance(value, dict) else {}
    if set(raw) - {"state", "description"}:
        return None
    state = str(raw.get("state") or "").strip().lower()
    description = _bounded_text(raw.get("description"), _MAX_PAGE_DESCRIPTION)
    if state not in _PAGE_STATES or not description:
        return None
    return {"state": state, "description": description}


def _normalize_contract_viewport(value: Any) -> dict[str, Any] | None:
    raw = value if isinstance(value, dict) else {}
    if set(raw) - {"description", "width", "height"}:
        return None
    description = _bounded_text(raw.get("description"), _MAX_VIEWPORT_DESCRIPTION)
    if not description:
        return None
    viewport: dict[str, Any] = {"description": description}
    width = raw.get("width")
    height = raw.get("height")
    if (width is None) != (height is None):
        return None
    if width is not None:
        try:
            normalized_width = int(width)
            normalized_height = int(height)
        except (TypeError, ValueError):
            return None
        if not (200 <= normalized_width <= 7680 and 200 <= normalized_height <= 4320):
            return None
        viewport.update({"width": normalized_width, "height": normalized_height})
    return viewport


def _normalize_contract_state(value: Any) -> list[str]:
    if not isinstance(value, list) or not 1 <= len(value) <= _MAX_STATE_ITEMS:
        return []
    state: list[str] = []
    for raw in value:
        item = _bounded_text(raw, _MAX_STATE_DESCRIPTION)
        if not item or item in state:
            return []
        state.append(item)
    return state


def _normalize_contract_artifacts(
    value: Any,
    *,
    target: dict[str, Any],
    viewport: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_artifacts = value if isinstance(value, list) and value else None
    if raw_artifacts is None:
        raw_artifacts = []
        if isinstance(target.get("locator"), dict):
            raw_artifacts.append(
                {
                    "kind": "focused",
                    "description": target["description"],
                    "locator": target["locator"],
                }
            )
        raw_artifacts.append(
            {
                "kind": "context",
                "description": "Surrounding page context",
            }
        )
    if not 1 <= len(raw_artifacts) <= _MAX_SCREENSHOT_ARTIFACTS:
        return []

    artifacts: list[dict[str, Any]] = []
    seen_capture_specs: set[str] = set()
    for raw_value in raw_artifacts:
        raw = raw_value if isinstance(raw_value, dict) else {}
        if set(raw) - {"kind", "description", "locator", "viewport"}:
            return []
        kind = str(raw.get("kind") or "").strip().lower()
        description = _bounded_text(raw.get("description"), _MAX_STATE_DESCRIPTION)
        if kind not in _SCREENSHOT_ARTIFACT_KINDS or not description:
            return []
        locator = None
        if raw.get("locator") is not None:
            locator = _normalize_locator(raw.get("locator"))
            if locator is None:
                return []
        elif kind == "focused" and isinstance(target.get("locator"), dict):
            locator = target["locator"]
        # The public tool schema permits a locator on any artifact.  A context
        # capture may intentionally scope to a surrounding region, and a focused
        # capture without a locator can still use the contract target or current
        # viewport.  Keep those safe forms executable instead of rejecting the
        # entire receipt contract.

        artifact_viewport = viewport
        if raw.get("viewport") is not None:
            artifact_viewport = _normalize_contract_viewport(raw.get("viewport"))
            if artifact_viewport is None:
                return []
        if kind == "responsive" and not {
            "width",
            "height",
        }.issubset(artifact_viewport):
            return []

        capture_spec = json.dumps(
            {
                "locator": locator,
                "width": artifact_viewport.get("width"),
                "height": artifact_viewport.get("height"),
            },
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if capture_spec in seen_capture_specs:
            continue
        seen_capture_specs.add(capture_spec)
        artifact: dict[str, Any] = {
            "kind": kind,
            "description": description,
            "viewport": artifact_viewport,
        }
        if locator is not None:
            artifact["locator"] = locator
        artifacts.append(artifact)
    return artifacts[:_MAX_SCREENSHOT_ARTIFACTS]


def _assertion_allowed_fields(kind: str) -> set[str]:
    common = {"id", "kind"}
    if kind in _ELEMENT_KINDS:
        fields = common | {"locator"}
        if kind == "count":
            fields |= {"min", "max"}
        return fields
    if kind == "text_present":
        return common | {"text", "policy"}
    if kind == "screenshot_appearance":
        return common | {"expectation"}
    if kind == "no_new_diagnostics":
        return common | {"cursor"}
    return common


def diagnose_orchestrated_visual_contract(
    value: Any,
    *,
    max_assertions: int = 6,
) -> dict[str, Any]:
    """Return a normalized contract or one bounded host-authored correction."""

    def invalid(reason_code: str) -> dict[str, Any]:
        return {
            "contract": {},
            "reason_code": reason_code,
            "correction": _CONTRACT_CORRECTIONS[reason_code],
        }

    if not isinstance(value, dict):
        return invalid("contract_not_object")
    raw = value
    if set(raw) - {"target", "page", "viewport", "state", "artifacts", "assertions"}:
        return invalid("contract_unknown_fields")
    if not {"target", "page", "viewport", "state", "assertions"}.issubset(raw):
        return invalid("contract_missing_fields")
    target = _normalize_contract_target(raw.get("target"))
    if target is None:
        return invalid("contract_invalid_target")
    page = _normalize_contract_page(raw.get("page"))
    if page is None:
        return invalid("contract_invalid_page")
    viewport = _normalize_contract_viewport(raw.get("viewport"))
    if viewport is None:
        return invalid("contract_invalid_viewport")
    state = _normalize_contract_state(raw.get("state"))
    if not state:
        return invalid("contract_invalid_state")
    artifacts = _normalize_contract_artifacts(
        raw.get("artifacts"),
        target=target,
        viewport=viewport,
    )
    if not artifacts:
        return invalid("contract_invalid_artifacts")
    raw_assertions = raw.get("assertions")
    if not isinstance(raw_assertions, list) or not raw_assertions:
        return invalid("contract_missing_fields")
    try:
        limit = max(1, min(int(max_assertions), 6))
    except (TypeError, ValueError):
        limit = 6
    if len(raw_assertions) > limit:
        return invalid("contract_assertion_limit")

    assertions: list[dict[str, Any]] = []
    seen_payloads: set[str] = set()
    for raw_value in raw_assertions:
        raw_assertion = raw_value if isinstance(raw_value, dict) else {}
        kind = str(raw_assertion.get("kind") or "").strip().lower()
        candidate = dict(raw_assertion)
        # A diagnostics assertion is executable only with a host-issued cursor
        # from the active browser supervisor. Models frequently include this
        # optional check by habit after repository-native screenshot QA, where
        # no cursor exists. Omit that unavailable auxiliary assertion instead
        # of invalidating an otherwise complete appearance contract.
        if kind == "no_new_diagnostics" and not _DIAGNOSTIC_CURSOR_RE.fullmatch(
            str(candidate.get("cursor") or "").strip().lower()
        ):
            continue
        # Locators on page-wide text and appearance checks are harmless but not
        # consumed by their executors; the target/artifact contract owns capture
        # scope.  The tool schema exposes locator uniformly, so discard it here
        # rather than turning a safe model-authored contract into an opaque
        # invalid_visual_contract failure.
        if kind in {"text_present", "screenshot_appearance"}:
            candidate.pop("locator", None)
        if kind == "text_present":
            candidate.setdefault("policy", "literal_request_text")
        elif kind == "screenshot_appearance":
            # Models sometimes attach the literal-request policy to an
            # appearance assertion.  It carries no execution authority here.
            candidate.pop("policy", None)
        if set(candidate) - _assertion_allowed_fields(kind):
            return invalid("contract_invalid_assertion")
        candidate["id"] = "contract-slot"
        validated = validate_visual_assertions([candidate], max_assertions=1)
        if len(validated) != 1:
            return invalid("contract_invalid_assertion")
        item = dict(validated[0])
        item.pop("id", None)
        payload = json.dumps(
            item,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if payload in seen_payloads:
            return invalid("contract_duplicate_assertion")
        seen_payloads.add(payload)
        item["id"] = _opaque_contract_id(
            "vassert",
            {"ordinal": len(assertions), "assertion": item},
        )
        assertions.append({"id": item.pop("id"), **item})
    if not any(item["kind"] == "screenshot_appearance" for item in assertions):
        return invalid("contract_missing_appearance")
    return {
        "contract": {
            "target": target,
            "page": page,
            "viewport": viewport,
            "state": state,
            "artifacts": artifacts,
            "assertions": assertions,
        },
        "reason_code": "",
        "correction": "",
    }


def normalize_orchestrated_visual_contract(
    value: Any,
    *,
    max_assertions: int = 6,
) -> dict[str, Any]:
    """Normalize one transient semantic contract and assign opaque assertion IDs."""

    return diagnose_orchestrated_visual_contract(
        value,
        max_assertions=max_assertions,
    )["contract"]


def validate_visual_execution_contract(
    requirement: Any,
    value: Any,
    *,
    max_assertions: int = 6,
) -> dict[str, Any]:
    """Bind one executable contract to the trusted requirement shape."""

    normalized_requirement = normalize_visual_requirement(requirement)
    if visual_requirement_uses_orchestrator_contract(normalized_requirement):
        return normalize_orchestrated_visual_contract(
            value,
            max_assertions=max_assertions,
        )
    raw = value if isinstance(value, dict) else {}
    assertions = validate_visual_assertion_coverage(
        normalized_requirement,
        raw.get("assertions"),
        max_assertions=max_assertions,
    )
    return {"assertions": assertions} if assertions else {}


def visual_execution_contract_id(value: Any) -> str:
    """Hash a normalized transient execution contract into an opaque ID."""

    raw = value if isinstance(value, dict) else {}
    assertions = raw.get("assertions")
    if not isinstance(assertions, list) or not assertions:
        return ""
    encoded = json.dumps(
        raw,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "vac_" + hashlib.sha256(encoded).hexdigest()[:24]


def visual_requirement_for_execution_contract(
    value: Any,
    *,
    max_assertions: int = 6,
) -> dict[str, Any]:
    """Create a stable transient requirement for standalone visual QA."""

    contract = normalize_orchestrated_visual_contract(
        value,
        max_assertions=max_assertions,
    )
    if not contract:
        return {"level": "none", "target": "", "assertions": []}
    return normalize_visual_requirement(
        {
            "level": "surface",
            "target": _opaque_contract_id("vtarget", contract.get("target")),
            "assertions": [
                {
                    "id": _opaque_contract_id("vassert", contract),
                    "kind": "orchestrator_contract",
                }
            ],
        }
    )


def storage_safe_visual_qa_args(value: Any) -> dict[str, Any]:
    """Drop transient semantics while retaining opaque contract evidence."""

    normalized = normalize_orchestrated_visual_contract(value)
    if normalized:
        return {
            "contract_id": visual_execution_contract_id(normalized),
            "assertions": [
                {"id": item["id"], "kind": item["kind"]}
                for item in normalized["assertions"]
            ],
        }
    raw = value if isinstance(value, dict) else {}
    if any(key in raw for key in ("target", "page", "viewport", "state", "artifacts")):
        return {"assertions": []}
    assertions = []
    for item in raw.get("assertions") if isinstance(raw.get("assertions"), list) else []:
        if not isinstance(item, dict):
            continue
        assertion_id = str(item.get("id") or "")[:48]
        kind = str(item.get("kind") or "")[:48]
        if _HOST_ASSERTION_ID_RE.fullmatch(assertion_id) and kind in ASSERTION_KINDS:
            assertions.append({"id": assertion_id, "kind": kind})
        if len(assertions) >= 6:
            break
    return {"assertions": assertions}


def is_storage_safe_visual_qa_args(value: Any) -> bool:
    """Return whether ``value`` is opaque durable evidence, not an executable contract."""

    if not isinstance(value, dict):
        return False
    keys = set(value)
    if keys not in ({"assertions"}, {"contract_id", "assertions"}):
        return False
    assertions = value.get("assertions")
    if not isinstance(assertions, list) or len(assertions) > 6:
        return False
    contract_id = value.get("contract_id")
    if contract_id is not None:
        if not _EXECUTION_CONTRACT_ID_RE.fullmatch(str(contract_id)) or not assertions:
            return False
    seen_ids: set[str] = set()
    for item in assertions:
        if not isinstance(item, dict) or set(item) != {"id", "kind"}:
            return False
        assertion_id = str(item.get("id") or "")
        kind = str(item.get("kind") or "")
        if (
            not _HOST_ASSERTION_ID_RE.fullmatch(assertion_id)
            or assertion_id in seen_ids
            or kind not in ASSERTION_KINDS
        ):
            return False
        seen_ids.add(assertion_id)
    return True


def validate_visual_assertion_coverage(
    requirement: Any,
    assertions: Any,
    *,
    max_assertions: int = 6,
) -> list[dict[str, Any]]:
    """Return assertions only for exact host-owned ID/kind coverage.

    Syntax validation intentionally omits malformed entries for general callers.
    Coverage validation is stricter: every supplied entry must survive, and the
    executable contract must bind one-to-one to all host requirement assertions.
    """

    normalized_requirement = normalize_visual_requirement(requirement)
    required = normalized_requirement.get("assertions") or []
    if normalized_requirement.get("level") == "none" or not required:
        return []
    if not isinstance(assertions, list):
        return []
    normalized = validate_visual_assertions(
        assertions,
        max_assertions=max_assertions,
    )
    if len(normalized) != len(assertions) or len(required) != len(normalized):
        return []

    required_by_id: dict[str, str] = {}
    for item in required:
        if not isinstance(item, dict):
            return []
        assertion_id = str(item.get("id") or "")
        kind = str(item.get("kind") or "")
        if (
            not _HOST_ASSERTION_ID_RE.fullmatch(assertion_id)
            or kind not in ASSERTION_KINDS
            or assertion_id in required_by_id
        ):
            return []
        required_by_id[assertion_id] = kind

    covered_ids: set[str] = set()
    for item in normalized:
        assertion_id = item["id"]
        if required_by_id.get(assertion_id) != item["kind"] or assertion_id in covered_ids:
            return []
        covered_ids.add(assertion_id)
    if covered_ids != set(required_by_id):
        return []
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
    correction = _bounded_text(raw.get("correction"), 240)
    if correction and status in {"failed", "uncertain"}:
        result["correction"] = correction
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


def normalize_assertion_result_coverage(
    value: Any,
    expected_ids: Any,
    *,
    invalid_code: str = "invalid_assertion_results",
) -> dict[str, Any]:
    """Require exactly one valid result for every expected host assertion ID."""

    expected = [str(item or "") for item in expected_ids if str(item or "")][:6]

    def invalid() -> dict[str, Any]:
        return {
            "valid": False,
            "results": [
                {"id": assertion_id, "status": "uncertain", "code": invalid_code}
                for assertion_id in expected
            ],
        }

    if (
        not expected
        or len(expected) != len(set(expected))
        or not isinstance(value, list)
        or len(value) != len(expected)
    ):
        return invalid()
    normalized: dict[str, dict[str, Any]] = {}
    for raw in value:
        if not isinstance(raw, dict):
            return invalid()
        assertion_id = str(raw.get("id") or "").strip()
        status = str(raw.get("status") or "").strip().lower()
        code = str(raw.get("code") or "").strip().lower()
        if (
            assertion_id not in expected
            or assertion_id in normalized
            or status not in VISUAL_QA_STATUSES
            or code not in ASSERTION_RESULT_CODES
        ):
            return invalid()
        result = sanitize_assertion_result(raw)
        if result is None:
            return invalid()
        normalized[assertion_id] = result
    if set(normalized) != set(expected):
        return invalid()
    return {"valid": True, "results": [normalized[item] for item in expected]}


__all__ = [
    "ASSERTION_KINDS",
    "ASSERTION_RESULT_CODES",
    "aggregate_assertion_results",
    "diagnose_orchestrated_visual_contract",
    "normalize_assertion_result_coverage",
    "normalize_orchestrated_visual_contract",
    "is_storage_safe_visual_qa_args",
    "sanitize_assertion_result",
    "storage_safe_visual_qa_args",
    "validate_visual_assertion_coverage",
    "validate_visual_assertions",
    "validate_visual_execution_contract",
    "visual_assertion_contract_id",
    "visual_execution_contract_id",
    "visual_requirement_for_execution_contract",
]
