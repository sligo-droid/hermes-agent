"""Opt-in, project-scoped GBrain client-knowledge tools."""

from __future__ import annotations

from plugins.client_knowledge_gbrain.tools import (
    CLIENT_KNOWLEDGE_GET_SCHEMA,
    CLIENT_KNOWLEDGE_SEARCH_SCHEMA,
    check_client_knowledge_available,
    handle_client_knowledge_get,
    handle_client_knowledge_search,
)


def register(ctx) -> None:
    """Register the two bounded, read-only client-knowledge tools."""
    ctx.register_tool(
        name="client_knowledge_search",
        toolset="client_knowledge",
        schema=CLIENT_KNOWLEDGE_SEARCH_SCHEMA,
        handler=handle_client_knowledge_search,
        check_fn=check_client_knowledge_available,
        emoji="🧠",
        effect="read",
    )
    ctx.register_tool(
        name="client_knowledge_get",
        toolset="client_knowledge",
        schema=CLIENT_KNOWLEDGE_GET_SCHEMA,
        handler=handle_client_knowledge_get,
        check_fn=check_client_knowledge_available,
        emoji="📖",
        effect="read",
    )
