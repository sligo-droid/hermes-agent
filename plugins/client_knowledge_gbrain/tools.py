"""Hermes tool schemas and bounded handlers for client knowledge."""

from __future__ import annotations

from typing import Any

from gateway.session_context import get_trusted_project_key
from tools.registry import tool_error, tool_result

from .client import GBrainClient, load_settings
from .scope import (
    ClientKnowledgeValidationError,
    bounded_text,
    full_project_slug,
    public_frontmatter,
    validate_page,
    validate_project_key,
    validate_search_results,
)


CLIENT_KNOWLEDGE_SEARCH_SCHEMA = {
    "description": (
        "Search canonical synthesized knowledge for exactly one mapped client project. "
        "Results are source- and slug-validated and include GBrain citations."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_key": {
                "type": "string",
                "description": "Mapped lowercase project key, for example pid.",
            },
            "query": {"type": "string", "description": "Focused project knowledge query."},
            "limit": {
                "type": "integer",
                "description": "Maximum visible results (1-10, default 5).",
                "minimum": 1,
                "maximum": 10,
            },
        },
        "required": ["project_key", "query"],
    },
}

CLIENT_KNOWLEDGE_GET_SCHEMA = {
    "description": (
        "Read one exact canonical client-knowledge page inside a mapped project. "
        "The slug is relative to projects/<project-key>/ and fuzzy lookup is disabled."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "project_key": {
                "type": "string",
                "description": "Mapped lowercase project key, for example pid.",
            },
            "slug": {
                "type": "string",
                "description": "Project-relative page slug, for example requirements/reporting-cadence.",
            },
        },
        "required": ["project_key", "slug"],
    },
}


def check_client_knowledge_available() -> bool:
    try:
        settings = load_settings()
        client = GBrainClient(settings)
        client.assert_pinned_checkout()
        client.assert_pinned_version()
        client.assert_keyword_only()
        return True
    except Exception:
        return False


def _client() -> GBrainClient:
    settings = load_settings()
    client = GBrainClient(settings)
    client.assert_pinned_checkout()
    client.assert_pinned_version()
    client.assert_keyword_only()
    return client


def _authorized_project_key(requested: Any) -> str:
    project_key = validate_project_key(requested)
    mapped = get_trusted_project_key().strip()
    if not mapped:
        raise ClientKnowledgeValidationError(
            "client knowledge requires a trusted mapped project session"
        )
    if validate_project_key(mapped) != project_key:
        raise ClientKnowledgeValidationError(
            "requested project_key does not match the mapped project session"
        )
    return project_key


def handle_client_knowledge_search(args: dict[str, Any], **_: Any) -> str:
    try:
        project_key = _authorized_project_key(args.get("project_key"))
        query = str(args.get("query") or "").strip()
        if not query or len(query) > 1_000:
            raise ClientKnowledgeValidationError("query must contain 1-1000 characters")
        try:
            visible_limit = int(args.get("limit") or 5)
        except (TypeError, ValueError):
            visible_limit = 5
        visible_limit = max(1, min(visible_limit, 10))
        client = _client()
        raw = client.search(query, limit=min(50, visible_limit * 5))
        accepted, foreign_count = validate_search_results(
            raw,
            project_key=project_key,
            source_id=client.settings.source_id,
        )
        results = []
        total_chars = 0
        for item in accepted:
            if len(results) >= visible_limit:
                break
            excerpt = bounded_text(item.get("chunk_text"), 1_200)
            remaining = client.settings.max_context_chars - total_chars
            if remaining <= 0:
                break
            excerpt = bounded_text(excerpt, remaining)
            total_chars += len(excerpt)
            results.append(
                {
                    "reference": f"gbrain:{item['slug']}",
                    "slug": item["slug"],
                    "title": bounded_text(item.get("title"), 300),
                    "kind": bounded_text(item.get("type"), 100),
                    "excerpt": excerpt,
                    "stale": bool(item.get("stale", False)),
                }
            )
        return tool_result(
            success=True,
            project_key=project_key,
            source_id=client.settings.source_id,
            retrieval_mode="keyword_only",
            results=results,
            result_count=len(results),
            foreign_results_filtered=foreign_count,
            empty=len(results) == 0,
        )
    except Exception as exc:
        return tool_error(f"Client knowledge search failed: {exc}")


def handle_client_knowledge_get(args: dict[str, Any], **_: Any) -> str:
    try:
        project_key = _authorized_project_key(args.get("project_key"))
        slug = full_project_slug(project_key, args.get("slug"))
        client = _client()
        page = validate_page(
            client.get_page(slug),
            project_key=project_key,
            source_id=client.settings.source_id,
        )
        frontmatter = page["frontmatter"]
        visible_frontmatter = public_frontmatter(frontmatter)
        compiled_truth = bounded_text(page.get("compiled_truth"), client.settings.max_context_chars)
        remaining = max(0, client.settings.max_context_chars - len(compiled_truth))
        timeline = bounded_text(page.get("timeline"), remaining)
        return tool_result(
            success=True,
            project_key=project_key,
            source_id=client.settings.source_id,
            reference=f"gbrain:{page['slug']}",
            slug=page["slug"],
            title=bounded_text(page.get("title"), 300),
            frontmatter=visible_frontmatter,
            source_refs=visible_frontmatter["source_refs"],
            compiled_truth=compiled_truth,
            timeline=timeline,
        )
    except Exception as exc:
        return tool_error(f"Client knowledge get failed: {exc}")
