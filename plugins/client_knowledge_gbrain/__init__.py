"""Opt-in, project-scoped GBrain client-knowledge tools."""

from __future__ import annotations

from plugins.client_knowledge_gbrain.tools import (
    CLIENT_KNOWLEDGE_GET_SCHEMA,
    CLIENT_KNOWLEDGE_SEARCH_SCHEMA,
    check_client_knowledge_available,
    handle_client_knowledge_get,
    handle_client_knowledge_search,
)
from plugins.client_knowledge_gbrain.cli import client_knowledge_command, register_cli


def _handle_review_command(*args, **kwargs):
    from plugins.client_knowledge_gbrain.review import (
        handle_client_knowledge_review_command,
    )

    return handle_client_knowledge_review_command(*args, **kwargs)


def register(ctx) -> None:
    """Register the two bounded read-only tools and operator-only controls."""
    ctx.register_tool(
        name="client_knowledge_search",
        toolset="client_knowledge",
        schema=CLIENT_KNOWLEDGE_SEARCH_SCHEMA,
        handler=handle_client_knowledge_search,
        check_fn=check_client_knowledge_available,
        emoji="🧠",
        effect="read",
    )
    ctx.register_cli_command(
        name="client-knowledge",
        help="Inspect intake and operate source ingestion",
        setup_fn=register_cli,
        handler_fn=client_knowledge_command,
        description=(
            "Operator-only queue, bounded Gmail polling, and Notion archive controls."
        ),
    )
    ctx.register_command(
        name="client-knowledge",
        handler=_handle_review_command,
        description="Approve or reject a client-knowledge review.",
        args_hint="approve|reject <review-id> [reason]",
    )
    ctx.register_auxiliary_task(
        key="client_knowledge_interpret",
        display_name="Client knowledge interpretation",
        description="Interpret a validated client-knowledge artifact.",
        defaults={
            "model_tier": "advanced",
            "required_model_tier": "advanced",
            "configurable": False,
        },
    )
    ctx.register_auxiliary_task(
        key="client_knowledge_assimilate",
        display_name="Client knowledge assimilation",
        description="Assimilate validated interpretation into canonical knowledge.",
        defaults={
            "model_tier": "advanced",
            "required_model_tier": "advanced",
            "configurable": False,
        },
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
