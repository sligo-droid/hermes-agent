from __future__ import annotations

import asyncio
import hashlib
from types import SimpleNamespace

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from hermes_cli.plugin_command_context import bind_plugin_command_context
from plugins.client_knowledge_gbrain.review import (
    fetch_and_reconcile_notification,
    handle_client_knowledge_review_command,
    reconcile_uncertain_notification,
    send_pending_review_notifications,
)


CFG = {
    "client_knowledge": {"review_notifications": {"enabled": True}},
    "projects": {
        "pid": {
            "client_knowledge_review": {
                "guild_id": "100", "channel_id": "200", "reviewer_role_id": "300",
                "reviewer_user_ids": [],
            }
        }
    }
}


class _Store:
    def __init__(self):
        self.review = {
            "review_id": "a" * 64, "assimilation_id": "b" * 64,
            "artifact_id": "c" * 64, "project_key": "pid", "proposal_sha256": "d" * 64,
            "assimilation_version": "v1", "state": "pending",
            "reason_code": "outside_auto_publication_allowlist",
            "notification_state": "pending", "notification_message_id": None,
            "notification_guild_id": None, "notification_channel_id": None,
            "notification_role_id": None, "notification_marker": None,
            "notification_content_sha256": None,
        }
        self.decisions = []

    def list_pending_reviews(self, limit=50):
        return [dict(self.review)]

    def get_assimilation(self, _):
        return {"assimilation_id": "b" * 64, "output_sha256": "e" * 64, "output_bytes": 1}

    def record_review_notification(self, _id, **kwargs):
        self.review.update({
            "notification_state": kwargs["state"],
            "notification_content_sha256": kwargs["content_sha256"],
            "notification_message_id": kwargs.get("message_id") or self.review.get("notification_message_id"),
            "notification_guild_id": kwargs["guild_id"],
            "notification_channel_id": kwargs["channel_id"],
            "notification_role_id": kwargs["role_id"],
            "notification_marker": kwargs["marker"],
        })

    def claim_review_notification(self, _id, **kwargs):
        if self.review["notification_state"] not in {"pending", "proven_none"}:
            return False
        self.record_review_notification(_id, state="uncertain", **kwargs)
        return True

    def get_review(self, _):
        return dict(self.review)

    def decide_review(self, review_id, **kwargs):
        self.decisions.append((review_id, kwargs))
        self.review["state"] = kwargs["decision"]
        return True


class _Derived:
    def read_json(self, *_args):
        return {"proposal": {"operations": [{
            "operation": "refine", "target_slug": "requirements/reporting",
            "source_refs": ["notion:page:test"],
        }]}}


class _MultiDerived:
    def read_json(self, *_args):
        return {"proposal": {"operations": [
            {"operation": "refine", "target_slug": "requirements/reporting", "source_refs": ["notion:page:one"]},
            {"operation": "add", "target_slug": "requirements/invoicing", "source_refs": ["notion:page:two"]},
        ]}}


def test_timed_out_post_remains_uncertain_and_no_match_never_retries():
    store = _Store()
    calls = 0

    async def sender(**_kwargs):
        nonlocal calls
        calls += 1
        return {"error": "timeout", "side_effect_state": "uncertain"}

    result = asyncio.run(send_pending_review_notifications(
        store=store, derived=_Derived(), config=CFG, sender=sender,
    ))
    assert result["uncertain"] == 1
    assert store.review["notification_state"] == "uncertain"
    assert reconcile_uncertain_notification(store, "a" * 64, []) is False
    asyncio.run(send_pending_review_notifications(
        store=store, derived=_Derived(), config=CFG, sender=sender,
    ))
    assert calls == 1


def test_disabled_notifications_do_not_claim_or_send():
    store = _Store()
    calls = 0

    async def sender(**_kwargs):
        nonlocal calls
        calls += 1
        return {"success": True, "message_id": "400", "side_effect_state": "confirmed"}

    result = asyncio.run(send_pending_review_notifications(
        store=store, derived=_Derived(), config={"projects": CFG["projects"]}, sender=sender,
    ))
    assert result == {"processed": 0, "confirmed": 0, "proven_none": 0, "uncertain": 0}
    assert calls == 0
    assert store.review["notification_state"] == "pending"


def test_multi_operation_review_notification_renders_complete_batch():
    store = _Store()
    sent = []

    async def sender(**kwargs):
        sent.append(kwargs["content"])
        return {"success": True, "message_id": "400", "side_effect_state": "confirmed"}

    result = asyncio.run(send_pending_review_notifications(
        store=store, derived=_MultiDerived(), config=CFG, sender=sender,
    ))
    assert result["confirmed"] == 1
    assert "1. refine | gbrain:projects/pid/requirements/reporting | notion:page:one" in sent[0]
    assert "2. add | gbrain:projects/pid/requirements/invoicing | notion:page:two" in sent[0]


def test_uncertain_delivery_adopts_exact_one_message():
    store = _Store()
    store.record_review_notification(
        "a" * 64, state="uncertain", content_sha256="f" * 64,
        guild_id="100", channel_id="200", role_id="300", marker="[marker]",
    )
    assert reconcile_uncertain_notification(store, "a" * 64, [{
        "guild_id": "100", "channel_id": "200", "content_sha256": "f" * 64,
        "content": "x [marker]", "message_id": "400", "author_is_bot": True,
        "allowed_role_mentions": ["300"],
    }]) is True
    assert store.review["notification_message_id"] == "400"


def test_operator_fetch_adopts_only_exact_message(monkeypatch):
    store = _Store()
    content = "review [marker]"
    store.record_review_notification(
        "a" * 64, state="uncertain", content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        guild_id="100", channel_id="200", role_id="300", marker="[marker]",
    )
    monkeypatch.setattr("tools.discord_tool._get_bot_token", lambda: "token")
    monkeypatch.setattr("tools.discord_tool._discord_request", lambda *_args, **_kwargs: {
        "id": "400", "guild_id": "100", "channel_id": "200", "content": content,
        "author": {"bot": True}, "mention_roles": ["300"],
    })
    assert fetch_and_reconcile_notification(store, "a" * 64, "400") is True
    assert store.review["notification_message_id"] == "400"


def _event(*, role=True, chat_type="group", channel="200", project="pid", guild="100"):
    role_obj = SimpleNamespace(id=300)
    raw = SimpleNamespace(id=500, user=SimpleNamespace(roles=[role_obj] if role else []))
    source = SessionSource(
        platform=Platform.DISCORD, chat_id=channel, chat_type=chat_type,
        user_id="600", scope_id=guild, thread_id="700" if chat_type == "thread" else None,
        project_key=project, project_channel_id="200", project_mapping_resolved=True,
        message_id="500",
    )
    return MessageEvent(
        text=f"/client-knowledge approve {'a' * 64}", message_type=MessageType.COMMAND,
        source=source, raw_message=raw, message_id="500",
    )


def test_review_authorization_requires_exact_top_level_project_and_role():
    store = _Store()
    store.review.update({
        "notification_state": "confirmed", "notification_message_id": "400",
        "notification_guild_id": "100", "notification_channel_id": "200",
        "notification_role_id": "300",
    })
    event = _event()
    with bind_plugin_command_context(
        event=event, canonical_command="client-knowledge", raw_args=f"approve {'a' * 64}"
    ):
        assert handle_client_knowledge_review_command(
            f"approve {'a' * 64}", store=store, config=CFG
        ) == "Review approved."
    assert store.decisions[0][1]["reviewer_role_id"] == "300"

    for event in (
        _event(role=False), _event(chat_type="thread"), _event(channel="201"),
        _event(project="decoy"), _event(guild="101"),
    ):
        rejected = _Store()
        rejected.review.update({
            "notification_state": "confirmed", "notification_message_id": "400",
            "notification_guild_id": "100", "notification_channel_id": "200",
            "notification_role_id": "300",
        })
        with bind_plugin_command_context(
            event=event, canonical_command="client-knowledge", raw_args=f"approve {'a' * 64}"
        ):
            assert handle_client_knowledge_review_command(
                f"approve {'a' * 64}", store=rejected, config=CFG
            ) == "Review decision rejected."
        assert not rejected.decisions


def test_prose_reactions_and_stale_ids_do_not_decide():
    store = _Store()
    assert handle_client_knowledge_review_command("looks good", store=store, config=CFG).startswith("Usage")
    event = _event()
    with bind_plugin_command_context(
        event=event, canonical_command="client-knowledge", raw_args=f"approve {'f' * 64}"
    ):
        assert handle_client_knowledge_review_command(
            f"approve {'f' * 64}", store=store, config=CFG
        ) == "Review decision rejected."
    assert not store.decisions
