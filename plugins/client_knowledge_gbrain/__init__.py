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


def _capture_review_text(*args, **kwargs):
    from plugins.client_knowledge_gbrain.review import capture_review_text_hook

    return capture_review_text_hook(*args, **kwargs)


async def _handle_review_component(interaction, action):
    from plugins.client_knowledge_gbrain.review import handle_discord_review_interaction

    await handle_discord_review_interaction(interaction, action)


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
    ctx.register_hook("pre_gateway_dispatch", _capture_review_text)
    ctx.register_discord_component_view(
        name="client-knowledge-review",
        components=[
            {"action": "approve", "label": "✅", "style": "success"},
            {"action": "reject", "label": "❌", "style": "danger"},
            {
                "action": "instructions",
                "label": "✍️",
                "style": "secondary",
            },
        ],
        handler=_handle_review_component,
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
