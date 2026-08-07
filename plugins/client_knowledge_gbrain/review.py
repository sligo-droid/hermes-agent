"""Durable Discord review notification and deterministic decision handling."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping

from gateway.config import Platform, load_gateway_config
from hermes_cli.config import load_config
from hermes_cli.plugin_command_context import get_plugin_command_context

from .derived import DerivedStore
from .scope import validate_project_key
from .store import IntakeStore

_REVIEW_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_DECISION_RE = re.compile(
    r"^(approve|reject)\s+([0-9a-f]{64})(?:\s+(.{1,500}))?$", re.DOTALL
)


class ReviewFailure(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProjectReviewConfig:
    project_key: str
    guild_id: str
    channel_id: str
    role_id: str
    reviewer_user_ids: frozenset[str]

    @classmethod
    def from_config(cls, config: Mapping[str, Any], project_key: str) -> "ProjectReviewConfig":
        project_key = validate_project_key(project_key)
        projects = config.get("projects")
        project = projects.get(project_key) if isinstance(projects, Mapping) else None
        raw = project.get("client_knowledge_review") if isinstance(project, Mapping) else None
        if not isinstance(raw, Mapping):
            raise ReviewFailure("project review configuration is missing")
        guild_id = str(raw.get("guild_id") or "").strip()
        channel_id = str(raw.get("channel_id") or "").strip()
        role_id = str(raw.get("reviewer_role_id") or "").strip()
        users = raw.get("reviewer_user_ids") or []
        if not all(value.isdigit() for value in (guild_id, channel_id, role_id)):
            raise ReviewFailure("project review Discord IDs must be numeric")
        if not isinstance(users, (list, tuple, set)):
            raise ReviewFailure("reviewer_user_ids must be a list")
        reviewer_users = frozenset(str(value) for value in users if str(value).isdigit())
        return cls(project_key, guild_id, channel_id, role_id, reviewer_users)


def _render_notification(
    review: Mapping[str, Any], proposal: Mapping[str, Any], config: ProjectReviewConfig
) -> tuple[str, str, str]:
    operations = proposal.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ReviewFailure("review proposal has no operations")
    summaries: list[str] = []

    def bounded(value: Any, limit: int) -> str:
        text = str(value or "")
        if len(text) <= limit:
            return text
        digest = hashlib.sha256(text.encode()).hexdigest()[:10]
        return f"{text[:limit - 13]}...#{digest}"

    for index, operation in enumerate(operations, start=1):
        if not isinstance(operation, Mapping):
            raise ReviewFailure("review proposal operation is invalid")
        refs = operation.get("source_refs")
        notion_ref = bounded(refs[-1], 72) if isinstance(refs, list) and refs else ""
        target = bounded(operation.get("target_slug"), 32)
        summaries.append(
            f"{index}. {operation.get('operation')} | "
            f"{target} | {notion_ref}"
        )
    operation_summary = "\n".join(summaries)
    marker = f"[ck-review:{review['review_id']}:{str(review['proposal_sha256'])[:16]}]"
    content = (
        f"<@&{config.role_id}> Client-knowledge review required\n\n"
        f"Project: {config.project_key}\n"
        f"Review: {review['review_id']}\n"
        f"Reason: {review['reason_code']}\n\n"
        f"Operations:\n{operation_summary}\n\n"
        f"Approve: /client-knowledge approve {review['review_id']}\n"
        f"Reject: /client-knowledge reject {review['review_id']} [reason]\n\n"
        f"{marker}"
    )
    if len(content) > 1800:
        raise ReviewFailure("review notification exceeds the single-message limit")
    mentions = re.findall(r"<@&?(\d+)>", content)
    if mentions != [config.role_id] or "@everyone" in content or "@here" in content:
        raise ReviewFailure("review notification contains an unsafe mention")
    return content, hashlib.sha256(content.encode()).hexdigest(), marker


async def _default_sender(
    *, channel_id: str, content: str, role_id: str
) -> Mapping[str, Any]:
    gateway = load_gateway_config()
    pconfig = gateway.platforms.get(Platform.DISCORD)
    if pconfig is None:
        return {"error": "Discord is not configured", "side_effect_state": "proven_none"}
    from gateway.platform_registry import platform_registry

    entry = platform_registry.get("discord")
    if entry is None or entry.standalone_sender_fn is None:
        return {"error": "Discord standalone sender is unavailable", "side_effect_state": "proven_none"}
    return await entry.standalone_sender_fn(
        pconfig,
        channel_id,
        content,
        metadata={
            "require_single_message": True,
            "allowed_role_mentions": [role_id],
            "strict_role_mentions": True,
        },
    )


async def send_pending_review_notifications(
    *,
    store: IntakeStore,
    derived: DerivedStore,
    config: Mapping[str, Any] | None = None,
    sender: Callable[..., Awaitable[Mapping[str, Any]]] | None = None,
) -> dict[str, int]:
    effective = dict(config or load_config() or {})
    ck = effective.get("client_knowledge")
    notification_config = ck.get("review_notifications") if isinstance(ck, Mapping) else None
    if not isinstance(notification_config, Mapping) or not bool(
        notification_config.get("enabled", False)
    ):
        return {"processed": 0, "confirmed": 0, "proven_none": 0, "uncertain": 0}
    send = sender or _default_sender
    result = {"processed": 0, "confirmed": 0, "proven_none": 0, "uncertain": 0}
    for review in store.list_pending_reviews(limit=50):
        state = str(review["notification_state"] or "pending")
        if state == "confirmed" or state == "uncertain":
            continue
        assimilation = store.get_assimilation(str(review["assimilation_id"]))
        if assimilation is None:
            continue
        value = derived.read_json(
            "assimilations", assimilation["assimilation_id"],
            assimilation["output_sha256"], assimilation["output_bytes"],
        )
        project = ProjectReviewConfig.from_config(effective, str(review["project_key"]))
        content, digest, marker = _render_notification(review, value["proposal"], project)
        if not store.claim_review_notification(
            str(review["review_id"]), content_sha256=digest,
            guild_id=project.guild_id, channel_id=project.channel_id,
            role_id=project.role_id, marker=marker,
        ):
            continue
        result["processed"] += 1
        try:
            send_result = send(
                channel_id=project.channel_id, content=content, role_id=project.role_id
            )
            if inspect.isawaitable(send_result):
                send_result = await send_result
        except Exception:
            send_result = {"error": "Discord send failed", "side_effect_state": "uncertain"}
        side_effect = str(send_result.get("side_effect_state") or "uncertain")
        message_id = str(send_result.get("message_id") or "")
        if send_result.get("success") and message_id and side_effect == "confirmed":
            durable_state = "confirmed"
        elif side_effect == "proven_none":
            durable_state = "proven_none"
        else:
            # Any request that may have been transmitted remains uncertain. A
            # later bounded scan may adopt a found message, but no-match never
            # proves absence and never causes automatic retry.
            durable_state = "uncertain"
            message_id = ""
        store.record_review_notification(
            str(review["review_id"]), state=durable_state, content_sha256=digest,
            guild_id=project.guild_id, channel_id=project.channel_id,
            role_id=project.role_id, marker=marker, message_id=message_id,
        )
        result[durable_state] += 1
    return result


def reconcile_uncertain_notification(
    store: IntakeStore,
    review_id: str,
    messages: list[Mapping[str, Any]],
) -> bool:
    """Adopt one exact Discord message; a bounded no-match remains uncertain."""
    review = store.get_review(review_id)
    if review is None or review["notification_state"] != "uncertain":
        return False
    matches = [
        item for item in messages
        if str(item.get("guild_id") or "") == str(review["notification_guild_id"])
        and str(item.get("channel_id") or "") == str(review["notification_channel_id"])
        and str(item.get("content_sha256") or "") == str(review["notification_content_sha256"])
        and str(review["notification_marker"] or "") in str(item.get("content") or "")
        and item.get("author_is_bot") is True
        and [str(value) for value in (item.get("allowed_role_mentions") or [])]
        == [str(review["notification_role_id"])]
        and str(item.get("message_id") or "")
    ]
    if len(matches) != 1:
        return False
    store.record_review_notification(
        review_id, state="confirmed",
        content_sha256=str(review["notification_content_sha256"]),
        guild_id=str(review["notification_guild_id"]),
        channel_id=str(review["notification_channel_id"]),
        role_id=str(review["notification_role_id"]),
        marker=str(review["notification_marker"]),
        message_id=str(matches[0]["message_id"]),
    )
    return True


def fetch_and_reconcile_notification(store: IntakeStore, review_id: str, message_id: str) -> bool:
    """Fetch one operator-selected Discord message and adopt only an exact match."""
    review = store.get_review(review_id)
    if (
        review is None
        or review["notification_state"] != "uncertain"
        or not str(message_id).isdigit()
    ):
        return False
    from tools.discord_tool import _discord_request, _get_bot_token

    token = _get_bot_token()
    if not token:
        raise ReviewFailure("Discord bot token is unavailable")
    channel_id = str(review["notification_channel_id"] or "")
    message = _discord_request(
        "GET", f"/channels/{channel_id}/messages/{message_id}", token
    )
    if not isinstance(message, Mapping):
        return False
    content = str(message.get("content") or "")
    candidate = {
        "guild_id": str(message.get("guild_id") or review["notification_guild_id"] or ""),
        "channel_id": str(message.get("channel_id") or channel_id),
        "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "content": content,
        "message_id": str(message.get("id") or ""),
        "author_is_bot": bool((message.get("author") or {}).get("bot")),
        "allowed_role_mentions": [str(value) for value in message.get("mention_roles", [])],
    }
    return reconcile_uncertain_notification(store, review_id, [candidate])


def _member_role_ids(raw_message: Any) -> set[str]:
    member = getattr(raw_message, "user", None) or getattr(raw_message, "author", None)
    return {
        str(getattr(role, "id", ""))
        for role in (getattr(member, "roles", None) or [])
        if str(getattr(role, "id", "")).isdigit()
    }


def handle_client_knowledge_review_command(
    raw_args: str,
    *,
    store: IntakeStore | None = None,
    config: Mapping[str, Any] | None = None,
) -> str:
    match = _DECISION_RE.fullmatch(str(raw_args or "").strip())
    if not match:
        return "Usage: /client-knowledge approve <review-id> | reject <review-id> [reason]"
    action, review_id, reason = match.groups()
    context = get_plugin_command_context()
    if (
        context is None
        or not context.authorization_passed
        or context.internal
        or context.canonical_command != "client-knowledge"
        or context.raw_args != str(raw_args or "").strip()
    ):
        return "Review decision rejected."
    event = context.event
    source = getattr(event, "source", None)
    if source is None or getattr(getattr(source, "platform", None), "value", "") != "discord":
        return "Review decision rejected."
    review_store = store or IntakeStore()
    review = review_store.get_review(review_id)
    if review is None or review["state"] != "pending":
        return "Review decision rejected."
    try:
        project = ProjectReviewConfig.from_config(
            dict(config or load_config() or {}), str(review["project_key"])
        )
    except Exception:
        return "Review decision rejected."
    if (
        getattr(source, "chat_type", "") in {"dm", "thread"}
        or getattr(source, "thread_id", None)
        or str(getattr(source, "scope_id", None) or getattr(source, "guild_id", None) or "") != project.guild_id
        or str(getattr(source, "chat_id", "")) != project.channel_id
        or str(getattr(source, "project_channel_id", "")) != project.channel_id
        or str(getattr(source, "project_key", "")) != project.project_key
        or getattr(source, "project_mapping_resolved", None) is not True
        or str(review["notification_state"]) != "confirmed"
        or str(review["notification_guild_id"]) != project.guild_id
        or str(review["notification_channel_id"]) != project.channel_id
        or str(review["notification_role_id"]) != project.role_id
    ):
        return "Review decision rejected."
    user_id = str(getattr(source, "user_id", "") or "")
    roles = _member_role_ids(getattr(event, "raw_message", None))
    if user_id not in project.reviewer_user_ids and project.role_id not in roles:
        return "Review decision rejected."
    if action == "approve" and reason:
        return "Review decision rejected."
    changed = review_store.decide_review(
        review_id,
        decision="approved" if action == "approve" else "rejected",
        reviewer_user_id=user_id,
        reviewer_role_id=project.role_id if project.role_id in roles else "",
        decision_message_id=str(getattr(source, "message_id", None) or getattr(event, "message_id", None) or getattr(getattr(event, "raw_message", None), "id", "")),
        reason=str(reason or "").strip(),
    )
    return "Review approved." if changed and action == "approve" else "Review rejected." if changed else "Review decision rejected."


def run_notification_once(**kwargs: Any) -> dict[str, int]:
    return asyncio.run(send_pending_review_notifications(**kwargs))


__all__ = [
    "ProjectReviewConfig", "ReviewFailure", "fetch_and_reconcile_notification",
    "handle_client_knowledge_review_command", "reconcile_uncertain_notification", "run_notification_once",
    "send_pending_review_notifications",
]
