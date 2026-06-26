"""Lightweight verification evidence classification and claim gating."""

from __future__ import annotations

import json
import re
from typing import Any


_VERIFY_COMMAND_RE = re.compile(
    r"\b(pytest|vitest|playwright|chromium|browser|smoke|check|status|ci|deploy|deployed|"
    r"production|prod|preview|modal|health|run_tests\.sh)\b|"
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:[^\s;&|]*[-:]?)?(?:test|tests|verify|verification)\b|"
    r"\b(?:cargo|go)\s+test\b|"
    r"(?:^|[\s;&|])(?:\./)?(?:scripts/)?(?:test|tests|run_tests)\.sh\b|"
    r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+\S*(?:verify|verification)\b",
    re.IGNORECASE,
)
_PROTECTED_CHECKOUT_GUARDRAIL_RE = re.compile(
    r"\bBLOCKED:\s*refusing to run a non-read-only terminal command from a protected canonical checkout\b",
    re.IGNORECASE,
)
_BROWSER_RE = re.compile(r"\b(browser|playwright|chromium|chrome|modal)\b", re.IGNORECASE)
_PRODUCTION_RE = re.compile(r"\b(production|prod|deployed?|live)\b|https?://", re.IGNORECASE)
_CI_RE = re.compile(r"\b(ci|checks?|status|gh\s+pr\s+checks|test|tests|pytest|vitest)\b", re.IGNORECASE)
_DEPLOY_RE = re.compile(r"\b(deploy|deployed|deployment)\b", re.IGNORECASE)
_MERGE_RE = re.compile(r"\b(merge|merged|pull|pr)\b", re.IGNORECASE)
_SUCCESS_RE = re.compile(r"\b(success|passed|pass|ok|complete|completed|visible|found|healthy)\b", re.IGNORECASE)
_TIMEOUT_RE = re.compile(r"\b(timed?\s*out|timeout|deadline|expired)\b", re.IGNORECASE)

_CLAIM_WORD_RE = re.compile(r"\b(shipped|verified|visible|checked|confirmed|passed|deployed|merged)\b", re.IGNORECASE)
_NEGATED_CLAIM_RE = re.compile(r"\b(?:not|isn['’]?t|failed|failure|blocked|unverified|not_verified)\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:;|\b(?:and|but)\b)\s*", re.IGNORECASE)

_SURFACE_LABELS = {
    "browser": "browser verification",
    "production": "production verification",
    "production_browser": "production browser verification",
    "ci": "CI verification",
    "deployment": "deployment verification",
    "pr": "PR/merge verification",
    "verification": "verification",
}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        data = json.loads(value)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _text(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _surfaces_for(tool_name: str, check_name: str, detail: str) -> list[str]:
    haystack = f"{tool_name} {check_name} {detail}"
    surfaces: list[str] = []
    if _CI_RE.search(haystack):
        surfaces.append("ci")
    if _MERGE_RE.search(haystack):
        surfaces.append("pr")
    if _DEPLOY_RE.search(haystack):
        surfaces.append("deployment")
    if _BROWSER_RE.search(haystack):
        surfaces.append("browser")
    if _PRODUCTION_RE.search(haystack):
        surfaces.append("production")
    if "browser" in surfaces and "production" in surfaces:
        surfaces.append("production_browser")
    return surfaces or ["verification"]


def classify_tool_verification_evidence(
    tool_name: str,
    tool_args: dict[str, Any] | None,
    result: Any,
    is_error: bool,
    *,
    order: int | None = None,
) -> list[dict[str, Any]]:
    """Return normalized verification evidence emitted by an explicit check.

    The classifier is intentionally conservative: terminal failures are only
    captured when the command itself looks like a verification/status/smoke
    attempt, while browser tool failures are inherently browser evidence.
    """
    name = str(tool_name or "")
    args = tool_args if isinstance(tool_args, dict) else {}
    data = _json_object(result)
    result_text = _text(data.get("output") or data.get("error") or result)
    check_name = _text(args.get("command") or args.get("url") or args.get("route") or name, limit=160)

    if name == "terminal" and _PROTECTED_CHECKOUT_GUARDRAIL_RE.search(result_text):
        return []

    if name == "terminal":
        if not _VERIFY_COMMAND_RE.search(check_name):
            return []
    elif not name.startswith("browser") and name not in {"webfetch", "web_search"}:
        return []

    timed_out = bool(_TIMEOUT_RE.search(f"{check_name}\n{result_text}"))
    status = "timeout" if timed_out else ("failure" if is_error else "success")
    if not is_error and name != "terminal" and data:
        if data.get("success") is False or data.get("ok") is False:
            status = "timeout" if timed_out else "failure"
        elif data.get("success") is True or data.get("ok") is True:
            status = "success"
    if status == "success" and result_text and _TIMEOUT_RE.search(result_text):
        status = "timeout"

    surfaces = _surfaces_for(name, check_name, result_text)
    return [
        {
            "schema_version": 1,
            "surface": surface,
            "check_name": check_name or name,
            "status": status,
            "order": int(order or 0),
            "detail": result_text[:240],
        }
        for surface in surfaces
    ]


def latest_evidence_by_surface(evidence: Any) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not isinstance(evidence, list):
        return latest
    for item in evidence:
        if not isinstance(item, dict):
            continue
        surface = str(item.get("surface") or "").strip()
        if not surface:
            continue
        order = int(item.get("order") or 0)
        current = latest.get(surface)
        if current is None or order >= int(current.get("order") or 0):
            latest[surface] = item
    return latest


def evidence_from_runtime_breakdown(runtime_breakdown: Any) -> list[dict[str, Any]]:
    if not isinstance(runtime_breakdown, dict):
        return []
    evidence = runtime_breakdown.get("verification_evidence")
    if isinstance(evidence, list):
        return [item for item in evidence if isinstance(item, dict)]
    return []


def _surface_claimed(text: str, surface: str) -> bool:
    relevant_text = text
    if surface in {"browser", "production", "production_browser", "deployment"}:
        sentences = [part for part in _SENTENCE_SPLIT_RE.split(text) if part.strip()]
        relevant = [part for part in sentences if _surface_terms_present(part, surface)]
        if relevant:
            relevant_text = " ".join(relevant)
    claim_match = _CLAIM_WORD_RE.search(relevant_text)
    if not claim_match:
        return False
    prefix = relevant_text[max(0, claim_match.start() - 80) : claim_match.start()]
    if _NEGATED_CLAIM_RE.search(prefix):
        return False
    lowered = relevant_text.lower()
    if surface == "production_browser":
        return bool(_PRODUCTION_RE.search(relevant_text) and _BROWSER_RE.search(relevant_text))
    if surface == "browser":
        return bool(_BROWSER_RE.search(relevant_text) or "modal" in lowered or "visible" in lowered)
    if surface == "production":
        return bool(_PRODUCTION_RE.search(relevant_text))
    if surface == "ci":
        return bool(_CI_RE.search(relevant_text))
    if surface == "deployment":
        return bool(_DEPLOY_RE.search(relevant_text))
    if surface == "pr":
        return bool(_MERGE_RE.search(relevant_text))
    return bool(_CLAIM_WORD_RE.search(relevant_text))


def _surface_terms_present(text: str, surface: str) -> bool:
    lowered = text.lower()
    if surface == "production_browser":
        return bool(_PRODUCTION_RE.search(text) and (_BROWSER_RE.search(text) or "modal" in lowered))
    if surface == "browser":
        return bool(_BROWSER_RE.search(text) or "modal" in lowered or "visible" in lowered)
    if surface == "production":
        return bool(_PRODUCTION_RE.search(text))
    if surface == "deployment":
        return bool(_DEPLOY_RE.search(text))
    return True


def _clause_mentions_blocked_surface(text: str, surface: str) -> bool:
    lowered = text.lower()
    if surface == "production_browser":
        return bool(_PRODUCTION_RE.search(text) or _BROWSER_RE.search(text) or "modal" in lowered or "visible" in lowered)
    return _surface_terms_present(text, surface)


def _surface_downgraded(text: str, surface: str, item: dict[str, Any]) -> bool:
    label = _SURFACE_LABELS.get(surface, surface.replace("_", " "))
    check = str(item.get("check_name") or "").strip()
    downgrade_lines = [line for line in str(text or "").splitlines() if "verification downgrade" in line.lower()]
    for line in downgrade_lines:
        lowered = line.lower()
        if label.lower() not in lowered and not _surface_terms_present(line, surface):
            continue
        if not _NEGATED_CLAIM_RE.search(line):
            continue
        if check and check[:80].lower() not in lowered:
            continue
        return True
    return False


def claim_constraints_for_text(final_text: str, evidence: Any) -> dict[str, Any]:
    latest = latest_evidence_by_surface(evidence)
    blocked = []
    for surface, item in sorted(latest.items()):
        status = str(item.get("status") or "").lower()
        if status not in {"failure", "timeout"}:
            continue
        if _surface_claimed(final_text, surface):
            blocked.append(
                {
                    "surface": surface,
                    "status": status,
                    "check_name": str(item.get("check_name") or "verification"),
                    "detail": str(item.get("detail") or "")[:240],
                }
            )
    return {
        "allowed": not blocked,
        "blocked_surfaces": blocked,
        "latest_by_surface": latest,
    }


def _blocked_surface_clause(item: dict[str, Any]) -> str:
    surface = str(item.get("surface") or "verification")
    label = _SURFACE_LABELS.get(surface, surface.replace("_", " ") + " verification")
    status = str(item.get("status") or "failed").lower()
    check = str(item.get("check_name") or "verification")
    if len(check) > 180:
        check = check[:177].rstrip() + "..."
    return f"{label} is not verified: latest check `{check}` {status}."


def _rewrite_blocked_surface_claims(final_text: str, blocked: list[dict[str, Any]], downgrade: str) -> str:
    sentences = [part for part in _SENTENCE_SPLIT_RE.split(str(final_text or "")) if part.strip()]
    if not sentences:
        return downgrade

    rewritten: list[str] = []
    inserted = False
    for sentence in sentences:
        sentence = sentence.strip()
        blocked_surfaces = [
            str(item.get("surface") or "")
            for item in blocked
            if isinstance(item, dict) and _surface_claimed(sentence, str(item.get("surface") or ""))
        ]
        if not blocked_surfaces:
            rewritten.append(sentence)
            continue

        clauses = [part.strip() for part in _CLAUSE_SPLIT_RE.split(sentence) if part.strip()]
        kept = []
        for clause in clauses:
            if any(_clause_mentions_blocked_surface(clause, surface) for surface in blocked_surfaces):
                continue
            kept.append(clause.rstrip(".!?"))
        if kept:
            rewritten.append(". ".join(kept) + ".")
        if not inserted:
            rewritten.append(downgrade)
            inserted = True

    if not inserted:
        rewritten.append(downgrade)
    return "\n\n".join(part for part in rewritten if part.strip())


def downgrade_final_response_for_evidence(final_text: str, evidence: Any) -> tuple[str, dict[str, Any]]:
    """Downgrade final-answer success claims contradicted by latest evidence.

    This runs after model synthesis and before host delivery. The evidence ledger
    remains the source of truth; this helper only adds user-visible qualifiers so
    a streamed/returned final answer cannot overclaim a failed or timed-out check.
    """
    text = str(final_text or "")
    constraints = claim_constraints_for_text(text, evidence)
    blocked = constraints.get("blocked_surfaces")
    if not text.strip() or not isinstance(blocked, list) or not blocked:
        return text, constraints

    clauses = [_blocked_surface_clause(item) for item in blocked if isinstance(item, dict)]
    seen: set[str] = set()
    unique_clauses = []
    for clause in clauses:
        if clause not in seen:
            seen.add(clause)
            unique_clauses.append(clause)
    if not unique_clauses:
        return text, constraints

    downgrade = "Verification downgrade: " + " ".join(unique_clauses)
    if downgrade.lower() in text.lower():
        return text, constraints
    return _rewrite_blocked_surface_claims(text, blocked, downgrade), constraints


def metadata_has_verified_claim(value: Any) -> bool:
    if isinstance(value, dict):
        return any(metadata_has_verified_claim(v) for v in value.values())
    if isinstance(value, list):
        return any(metadata_has_verified_claim(v) for v in value)
    if isinstance(value, str):
        return bool(re.search(r"\b(verified|shipped)\b", value, flags=re.IGNORECASE))
    return False


def downgrade_verified_metadata(value: Any, blocked_surfaces: list[dict[str, Any]]) -> Any:
    """Return metadata with explicit verified/shipped strings downgraded."""
    if not blocked_surfaces:
        return value
    if isinstance(value, dict):
        updated = {str(k): downgrade_verified_metadata(v, blocked_surfaces) for k, v in value.items()}
        if metadata_has_verified_claim(value):
            updated["verification_guard"] = {
                "status": "not_verified",
                "blocked_surfaces": blocked_surfaces,
            }
        return updated
    if isinstance(value, list):
        return [downgrade_verified_metadata(item, blocked_surfaces) for item in value]
    if isinstance(value, str):
        return re.sub(r"\b(verified|shipped)\b", "not_verified", value, flags=re.IGNORECASE)
    return value
