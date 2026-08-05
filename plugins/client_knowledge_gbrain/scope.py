"""Fail-closed project, source, frontmatter, and citation validation."""

from __future__ import annotations

import re
from typing import Any


PROJECT_KEY_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
SLUG_SEGMENT_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,126}[a-z0-9])?$")
VALID_STATUSES = frozenset({"current", "tentative", "disputed", "superseded", "archived"})
VALID_KINDS = frozenset(
    {"decision", "requirement", "fact", "preference", "risk", "stakeholder", "concept"}
)
VALID_CONFIDENCE = frozenset({"low", "medium", "high"})
VALID_SENSITIVITY = frozenset({"public", "internal", "confidential", "restricted"})
NOTION_REF_RE = re.compile(r"^notion:page:[A-Za-z0-9_-]{1,200}$")


class ClientKnowledgeValidationError(ValueError):
    """Raised when GBrain data crosses or cannot prove the project boundary."""


def validate_project_key(value: Any) -> str:
    project_key = str(value or "").strip()
    if not PROJECT_KEY_RE.fullmatch(project_key):
        raise ClientKnowledgeValidationError(
            "project_key must be lowercase ASCII letters, digits, or interior hyphens"
        )
    return project_key


def project_prefix(project_key: str) -> str:
    return f"projects/{validate_project_key(project_key)}/"


def validate_relative_slug(value: Any) -> str:
    raw = str(value or "").strip()
    if (
        not raw
        or len(raw) > 500
        or raw.startswith("/")
        or raw.endswith("/")
        or "\\" in raw
    ):
        raise ClientKnowledgeValidationError("slug must be a non-empty relative project slug")
    if raw == "projects" or raw.startswith("projects/"):
        raise ClientKnowledgeValidationError("slug must be relative to the requested project")
    segments = raw.split("/")
    if len(segments) > 16:
        raise ClientKnowledgeValidationError("slug may contain at most 16 path segments")
    if any(not SLUG_SEGMENT_RE.fullmatch(segment) for segment in segments):
        raise ClientKnowledgeValidationError(
            "slug segments must be lowercase ASCII letters, digits, or interior hyphens"
        )
    return "/".join(segments)


def full_project_slug(project_key: str, relative_slug: Any) -> str:
    return project_prefix(project_key) + validate_relative_slug(relative_slug)


def validate_canonical_project_slug(value: Any, *, project_key: str) -> str:
    slug = str(value or "")
    prefix = project_prefix(project_key)
    if not slug.startswith(prefix):
        raise ClientKnowledgeValidationError("GBrain result is outside the requested project prefix")
    relative = validate_relative_slug(slug[len(prefix):])
    canonical = prefix + relative
    if canonical != slug:
        raise ClientKnowledgeValidationError("GBrain result slug is not canonical")
    return canonical


def validate_result_identity(
    result: Any,
    *,
    project_key: str,
    source_id: str,
) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise ClientKnowledgeValidationError("GBrain result must be an object")
    slug = result.get("slug")
    actual_source = result.get("source_id")
    if not isinstance(slug, str) or not slug:
        raise ClientKnowledgeValidationError("GBrain result is missing a canonical slug")
    if actual_source != source_id:
        raise ClientKnowledgeValidationError("GBrain result source_id does not match the configured source")
    validate_canonical_project_slug(slug, project_key=project_key)
    return result


def _require_string(frontmatter: dict[str, Any], key: str) -> str:
    value = frontmatter.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ClientKnowledgeValidationError(f"frontmatter.{key} must be a non-empty string")
    return value.strip()


def validate_source_refs(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ClientKnowledgeValidationError("frontmatter.source_refs must contain a Notion citation")
    refs = []
    for item in value:
        if not isinstance(item, str) or not NOTION_REF_RE.fullmatch(item):
            raise ClientKnowledgeValidationError(
                "every source_ref must use notion:page:<id>"
            )
        refs.append(item)
    return refs


def validate_frontmatter(
    frontmatter: Any,
    *,
    project_key: str,
    slug: str,
) -> dict[str, Any]:
    if not isinstance(frontmatter, dict):
        raise ClientKnowledgeValidationError("page frontmatter must be an object")
    if _require_string(frontmatter, "project") != project_key:
        raise ClientKnowledgeValidationError("frontmatter.project does not match the path project")
    status = _require_string(frontmatter, "status")
    kind = _require_string(frontmatter, "kind")
    confidence = _require_string(frontmatter, "confidence")
    sensitivity = _require_string(frontmatter, "sensitivity")
    if status not in VALID_STATUSES:
        raise ClientKnowledgeValidationError("frontmatter.status is not supported")
    if kind not in VALID_KINDS:
        raise ClientKnowledgeValidationError("frontmatter.kind is not supported")
    if confidence not in VALID_CONFIDENCE:
        raise ClientKnowledgeValidationError("frontmatter.confidence is not supported")
    if sensitivity not in VALID_SENSITIVITY:
        raise ClientKnowledgeValidationError("frontmatter.sensitivity is not supported")
    _require_string(frontmatter, "effective_at")
    _require_string(frontmatter, "updated_at")
    validate_source_refs(frontmatter.get("source_refs"))
    supersedes = frontmatter.get("supersedes")
    if not isinstance(supersedes, list):
        raise ClientKnowledgeValidationError("frontmatter.supersedes must be a list")
    for item in supersedes:
        if not isinstance(item, str):
            raise ClientKnowledgeValidationError("supersedes references must remain in the project")
        validate_canonical_project_slug(item, project_key=project_key)
    validate_canonical_project_slug(slug, project_key=project_key)
    return frontmatter


def validate_page(
    page: Any,
    *,
    project_key: str,
    source_id: str,
) -> dict[str, Any]:
    validated = validate_result_identity(
        page,
        project_key=project_key,
        source_id=source_id,
    )
    validate_frontmatter(
        validated.get("frontmatter"),
        project_key=project_key,
        slug=validated["slug"],
    )
    return validated


def validate_search_results(
    results: Any,
    *,
    project_key: str,
    source_id: str,
) -> tuple[list[dict[str, Any]], int]:
    if not isinstance(results, list):
        raise ClientKnowledgeValidationError("GBrain search response must be a list")
    accepted: list[dict[str, Any]] = []
    foreign = 0
    prefix = project_prefix(project_key)
    for item in results:
        if not isinstance(item, dict):
            raise ClientKnowledgeValidationError("GBrain search result must be an object")
        slug = item.get("slug")
        actual_source = item.get("source_id")
        if not isinstance(slug, str) or not slug or not isinstance(actual_source, str):
            raise ClientKnowledgeValidationError("GBrain search result lacks source/slug metadata")
        if actual_source != source_id or not slug.startswith(prefix):
            foreign += 1
            continue
        validate_canonical_project_slug(slug, project_key=project_key)
        accepted.append(item)
    return accepted, foreign


def bounded_text(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def citations_from_frontmatter(frontmatter: dict[str, Any]) -> list[str]:
    return validate_source_refs(frontmatter.get("source_refs"))


def public_frontmatter(frontmatter: dict[str, Any], *, max_items: int = 20) -> dict[str, Any]:
    """Return only validated, bounded fields safe for the tool response."""
    return {
        "project": frontmatter["project"],
        "status": frontmatter["status"],
        "kind": frontmatter["kind"],
        "effective_at": bounded_text(frontmatter["effective_at"], 100),
        "updated_at": bounded_text(frontmatter["updated_at"], 100),
        "source_refs": citations_from_frontmatter(frontmatter)[:max_items],
        "supersedes": list(frontmatter["supersedes"])[:max_items],
        "confidence": frontmatter["confidence"],
        "sensitivity": frontmatter["sensitivity"],
    }
