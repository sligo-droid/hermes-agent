from __future__ import annotations

import asyncio
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from plugins.client_knowledge_gbrain.models import IntakeArtifact
from plugins.client_knowledge_gbrain.derived import canonical_json
from plugins.client_knowledge_gbrain.store import IntakeStore
from plugins.client_knowledge_gbrain.review import (
    _validate_revision_batch,
    _render_notification,
    capture_review_text_hook,
    fetch_and_reconcile_notification,
    handle_discord_review_interaction,
    process_pending_review_revisions,
    repair_review_details,
    reconcile_uncertain_notification,
    send_pending_review_notifications,
)
from agent.plugin_llm import PluginLlmStructuredResult
from plugins.client_knowledge_gbrain.assimilation import _canonical_markdown


CFG = {
    "client_knowledge": {"review_notifications": {"enabled": True}},
    "projects": {
        "pid": {
            "display_name": "PID",
            "client_knowledge_review": {
                "guild_id": "100",
                "channel_id": "200",
                "reviewer_role_id": "300",
                "reviewer_user_ids": ["601"],
            },
        }
    },
}


def _interpretation(count: int = 6):
    evidence = []
    requirements = []
    for index in range(1, count + 1):
        quote = f"Synthetic evidence quote {index}: reports are due every Monday."
        evidence_id = f"evidence-{index:03d}"
        finding_id = f"requirement-{index}"
        evidence.append(
            {
                "id": evidence_id,
                "segment_id": "body-0001",
                "start": 0,
                "end": len(quote),
                "quote": quote,
            }
        )
        requirements.append(
            {
                "id": finding_id,
                "text": f"Synthetic requirement {index}: send the report every Monday.",
                "confidence": "high" if index % 2 else "medium",
                "sensitivity": "internal",
                "evidence_ids": [evidence_id],
            }
        )
    return {
        "summary": "Synthetic PID email summary.",
        "candidate_learnings": [],
        "decisions": [],
        "requirements": requirements,
        "preferences": [],
        "risks": [],
        "stakeholders": [],
        "deadlines": [],
        "open_questions": [],
        "suggested_actions": [],
        "evidence": evidence,
    }


def _operation(index: int, *, ignored: bool = False):
    if ignored:
        return {
            "operation": "ignore_transient",
            "target_slug": "",
            "title": "",
            "kind": "",
            "status": "",
            "confidence": "",
            "sensitivity": "",
            "impact": "",
            "honcho_projection": "",
            "effective_at": "",
            "source_refs": [],
            "supersedes": [],
            "claim": "",
            "timeline_entry": "",
            "expected_prior_sha256": "",
            "finding_id": f"requirement-{index}",
            "evidence_ids": [f"evidence-{index:03d}"],
            "final_markdown": "",
        }
    return {
        "operation": "add",
        "target_slug": f"requirements/reporting-{index}",
        "title": f"Weekly reporting rule {index}",
        "kind": "requirement",
        "status": "current",
        "confidence": "high" if index % 2 else "medium",
        "sensitivity": "internal",
        "impact": "ordinary",
        "honcho_projection": "eligible",
        "effective_at": "2026-08-07",
        "source_refs": ["notion:page:0123456789abcdef0123456789abcdef"],
        "supersedes": [],
        "claim": f"Synthetic requirement {index}: send the report every Monday.",
        "timeline_entry": "Synthetic timeline entry.",
        "expected_prior_sha256": "",
        "finding_id": f"requirement-{index}",
        "evidence_ids": [f"evidence-{index:03d}"],
        "final_markdown": "Synthetic final markdown.",
    }


def _proposal(count: int = 6):
    return {"operations": [
        *[_operation(index) for index in range(1, count // 2 + 1)],
        *[_operation(index, ignored=True) for index in range(count // 2 + 1, count + 1)],
    ]}


def _artifact():
    return IntakeArtifact.from_bytes(
        project_key="pid",
        provider_id="gmail",
        provider_artifact_id="mailbox@example.test:message:synthetic-1",
        provider_message_id="synthetic-1",
        occurred_at=1786089600,
        source_url="https://mail.google.com/mail/u/0/#all/synthetic-1",
        mime_type="message/rfc822",
        original_filename="message.eml",
        content=b"synthetic email",
    )


EXTRACTION = {
    "segments": [
        {"kind": "header", "label": "From", "text": "Alex Example <alex@example.test>"},
        {"kind": "header", "label": "Subject", "text": "PID weekly reporting"},
        {"kind": "header", "label": "Date", "text": "Fri, 7 Aug 2026 09:00:00 +0000"},
    ]
}


class _Store:
    def __init__(self):
        self.review = {
            "review_id": "a" * 64,
            "assimilation_id": "b" * 64,
            "artifact_id": "c" * 64,
            "project_key": "pid",
            "proposal_sha256": "d" * 64,
            "assimilation_version": "v1",
            "state": "pending",
            "reason_code": "finding_grounding_mismatch",
            "notification_state": "pending",
            "notification_message_id": None,
            "notification_guild_id": None,
            "notification_channel_id": None,
            "notification_role_id": None,
            "notification_marker": None,
            "notification_content_sha256": None,
            "detail_state": "pending",
            "detail_content_sha256": None,
            "detail_thread_id": None,
            "capture_mode": None,
            "capture_user_id": None,
            "capture_role_id": None,
            "capture_channel_id": None,
        }
        self.decisions = []
        self.instructions = []

    def list_pending_reviews(self, limit=50):
        return [dict(self.review)] if self.review["state"] == "pending" else []

    def list_open_reviews(self, limit=50):
        return [dict(self.review)] if self.review["state"] in {"pending", "instructions_pending"} else []

    def get_assimilation(self, _):
        return {
            "assimilation_id": "b" * 64,
            "interpretation_id": "f" * 64,
            "output_sha256": "e" * 64,
            "output_bytes": 1,
        }

    def get_interpretation(self, _):
        return {
            "interpretation_id": "f" * 64,
            "extraction_id": "1" * 64,
            "output_sha256": "2" * 64,
            "output_bytes": 1,
        }

    def get_extraction(self, _):
        return {"extraction_id": "1" * 64, "output_sha256": "3" * 64, "output_bytes": 1}

    def get_artifact(self, _):
        return _artifact()

    def get_completed_stage_receipt(self, _artifact_id, stage):
        assert stage == "notion_archived"
        return {"receipt_id": "notion:page:0123456789abcdef0123456789abcdef"}

    def record_review_notification(self, _id, **kwargs):
        self.review.update(
            {
                "notification_state": kwargs["state"],
                "notification_content_sha256": kwargs["content_sha256"],
                "notification_message_id": kwargs.get("message_id") or self.review.get("notification_message_id"),
                "notification_guild_id": kwargs["guild_id"],
                "notification_channel_id": kwargs["channel_id"],
                "notification_role_id": kwargs["role_id"],
                "notification_marker": kwargs["marker"],
                "detail_state": kwargs.get("detail_state", "pending"),
                "detail_content_sha256": kwargs.get("detail_content_sha256"),
                "detail_thread_id": kwargs.get("detail_thread_id") or self.review.get("detail_thread_id"),
            }
        )

    def claim_review_notification(self, _id, **kwargs):
        if self.review["notification_state"] not in {"pending", "proven_none"}:
            return False
        self.record_review_notification(_id, state="uncertain", **kwargs)
        return True

    def get_review(self, _):
        return dict(self.review)

    def get_review_by_notification_message(self, message_id):
        return dict(self.review) if message_id == self.review["notification_message_id"] else None

    def decide_review(self, review_id, **kwargs):
        if self.review["state"] != "pending":
            return False
        if self.review.get("capture_mode") and kwargs["decision"] == "approved":
            return False
        if kwargs["decision"] == "rejected" and self.review.get("capture_mode") != "reject_reason":
            return False
        self.decisions.append((review_id, kwargs))
        self.review["state"] = kwargs["decision"]
        self.review["capture_mode"] = None
        return True

    def begin_review_text_capture(self, review_id, **kwargs):
        if self.review["state"] != "pending" or self.review.get("capture_mode"):
            return False
        self.review.update(
            {
                "capture_mode": kwargs["mode"],
                "capture_user_id": kwargs["reviewer_user_id"],
                "capture_role_id": kwargs["reviewer_role_id"],
                "capture_channel_id": kwargs["channel_id"],
            }
        )
        return True

    def get_review_text_capture(self, **kwargs):
        if (
            self.review.get("capture_mode")
            and self.review["notification_guild_id"] == kwargs["guild_id"]
            and self.review["capture_channel_id"] == kwargs["channel_id"]
            and self.review["capture_user_id"] == kwargs["user_id"]
        ):
            return dict(self.review)
        return None

    def record_review_instruction(self, review_id, **kwargs):
        if self.review.get("capture_mode") != "instructions":
            return False
        self.instructions.append((review_id, kwargs))
        self.review["state"] = "instructions_pending"
        self.review["capture_mode"] = None
        return True


class _Derived:
    def __init__(self, count=6):
        self.count = count

    def read_json(self, kind, *_args):
        if kind == "assimilations":
            return {"proposal": _proposal(self.count)}
        if kind == "interpretations":
            return {"interpretation": _interpretation(self.count)}
        if kind == "extractions":
            return EXTRACTION
        raise AssertionError(kind)


def _rendered(count=6):
    review = _Store().review
    from plugins.client_knowledge_gbrain.review import ProjectReviewConfig

    return _render_notification(
        review,
        _proposal(count),
        _interpretation(count),
        _artifact(),
        EXTRACTION,
        ProjectReviewConfig.from_config(CFG, "pid"),
        "notion:page:0123456789abcdef0123456789abcdef",
    )


def test_human_rendering_is_compact_and_keeps_full_details_out_of_parent():
    content, _digest, marker, embed, details, _detail_digest = _rendered()
    assert content == "<@&300>"
    assert "Nothing has been published yet" not in content
    assert marker not in content
    assert embed == {
        "title": "Request to Learn",
        "description": (
            "**3 proposed additions · 3 findings not proposed**\n"
            "[Source in Notion](https://www.notion.so/0123456789abcdef0123456789abcdef)"
        ),
        "color": 0xF59E0B,
        "fields": [{
            "name": "Source",
            "value": (
                r"**Email sender:** Alex Example \<alex@example\.test\>" "\n"
                "**Email subject:** PID weekly reporting\n"
                r"**Email date:** Fri, 7 Aug 2026 09:00:00 \+0000"
            ),
        }],
    }
    assert "/client-knowledge" not in content
    assert "a" * 64 not in content
    assert "finding_grounding_mismatch" not in str(embed)
    assert "Why this needs review" not in str(embed)
    assert "How to decide" not in str(embed)
    assert "Alex Example" in str(embed)
    assert "Email sender" in str(embed)
    assert "Email subject" in str(embed)
    assert "Email date" in str(embed)
    assert "https://mail.google.com" not in str(embed)
    assert "https://www.notion.so/0123456789abcdef0123456789abcdef" in str(embed)
    joined = "\n".join(details)
    assert "Add new knowledge" in joined
    assert "Weekly reporting rule 1" in joined
    assert "Synthetic requirement 1" in joined
    assert "Requirements › Reporting 1" in joined
    assert "Synthetic evidence quote 1" in joined
    assert "Do not add — not proposed for publication" in joined
    assert "Synthetic requirement 4" in joined
    assert "ignore_transient" not in joined
    assert "notion:page:" not in joined
    assert "requirements/reporting" not in joined


def test_maximum_bounded_batch_preserves_every_claim_and_evidence():
    _content, _digest, _marker, _embed, details, _detail_digest = _rendered(10)
    joined = "\n".join(details)
    for index in range(1, 11):
        assert f"Synthetic requirement {index}" in joined
        assert f"Synthetic evidence quote {index}" in joined
    assert all(len(message) <= 1900 for message in details)


def test_uncertain_detail_repair_appends_only_missing_exact_messages(monkeypatch):
    store = _confirmed_store()
    store.review.update({
        "detail_state": "uncertain",
        "detail_content_sha256": _rendered()[5],
        "detail_thread_id": "401",
        "notification_content_sha256": _rendered()[1],
        "notification_marker": _rendered()[2],
    })
    details = _rendered()[4]
    existing = details[:-1]
    calls = []

    def request(method, path, _token, params=None, body=None, timeout=15):
        calls.append((method, path, body))
        if method == "GET":
            current = [*existing]
            if any(item[0] == "POST" for item in calls):
                current.append(details[-1])
            return [{"content": item, "author": {"bot": True}} for item in current]
        assert body["content"] == details[-1]
        return {"id": "new"}

    monkeypatch.setattr("tools.discord_tool._get_bot_token", lambda: "token")
    monkeypatch.setattr("tools.discord_tool._discord_request", request)
    assert repair_review_details(store, _Derived(), "a" * 64, config=CFG) is True
    posts = [item for item in calls if item[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2]["content"] == details[-1]
    assert store.review["detail_state"] == "confirmed"


def test_timed_out_parent_remains_uncertain_and_no_match_never_retries():
    store = _Store()
    calls = 0

    async def sender(**_kwargs):
        nonlocal calls
        calls += 1
        return {"error": "timeout", "side_effect_state": "uncertain"}

    result = asyncio.run(
        send_pending_review_notifications(store=store, derived=_Derived(), config=CFG, sender=sender)
    )
    assert result["uncertain"] == 1
    assert store.review["notification_state"] == "uncertain"
    assert reconcile_uncertain_notification(store, "a" * 64, []) is False
    asyncio.run(
        send_pending_review_notifications(store=store, derived=_Derived(), config=CFG, sender=sender)
    )
    assert calls == 1


def test_delivery_sends_structured_parent_and_full_detail_thread_batch():
    store = _Store()
    sent = []

    async def sender(**kwargs):
        sent.append(kwargs)
        return {
            "success": True,
            "message_id": "400",
            "thread_id": "401",
            "side_effect_state": "confirmed",
            "detail_state": "confirmed",
        }

    result = asyncio.run(
        send_pending_review_notifications(store=store, derived=_Derived(), config=CFG, sender=sender)
    )
    assert result["confirmed"] == 1
    assert sent[0]["embed"]["title"] == "Request to Learn"
    assert len(sent[0]["detail_messages"]) == 6
    assert store.review["notification_message_id"] == "400"
    assert store.review["detail_thread_id"] == "401"


def test_uncertain_delivery_adopts_exact_one_message():
    store = _Store()
    store.record_review_notification(
        "a" * 64,
        state="uncertain",
        content_sha256="f" * 64,
        guild_id="100",
        channel_id="200",
        role_id="300",
        marker="[marker]",
    )
    assert reconcile_uncertain_notification(
        store,
        "a" * 64,
        [
            {
                "guild_id": "100",
                "channel_id": "200",
                "content_sha256": "f" * 64,
                "content": "x [marker]",
                "message_id": "400",
                "author_is_bot": True,
                "allowed_role_mentions": ["300"],
            }
        ],
    ) is True
    assert store.review["notification_message_id"] == "400"


def test_operator_fetch_adopts_only_exact_message(monkeypatch):
    store = _Store()
    content = "review [marker]"
    store.record_review_notification(
        "a" * 64,
        state="uncertain",
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        guild_id="100",
        channel_id="200",
        role_id="300",
        marker="[marker]",
    )
    monkeypatch.setattr("tools.discord_tool._get_bot_token", lambda: "token")
    monkeypatch.setattr(
        "tools.discord_tool._discord_request",
        lambda *_args, **_kwargs: {
            "id": "400",
            "guild_id": "100",
            "channel_id": "200",
            "content": content,
            "author": {"bot": True},
            "mention_roles": ["300"],
        },
    )
    assert fetch_and_reconcile_notification(store, "a" * 64, "400") is True


def test_new_ux_reconciliation_requires_exact_embed_and_components():
    store = _Store()
    content, digest, marker, embed, _details, _detail_digest = _rendered()
    store.record_review_notification(
        "a" * 64,
        state="uncertain",
        content_sha256=digest,
        guild_id="100",
        channel_id="200",
        role_id="300",
        marker=marker,
    )
    from plugins.client_knowledge_gbrain.review import _parent_payload_digest, _review_components

    assert reconcile_uncertain_notification(
        store,
        "a" * 64,
        [{
            "guild_id": "100",
            "channel_id": "200",
            "content": content,
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "parent_payload_sha256": _parent_payload_digest(
                content, embed, _review_components()
            ),
            "message_id": "400",
            "author_is_bot": True,
            "allowed_role_mentions": ["300"],
        }],
    ) is True

    mismatch = _Store()
    mismatch.record_review_notification(
        "a" * 64,
        state="uncertain",
        content_sha256=digest,
        guild_id="100",
        channel_id="200",
        role_id="300",
        marker=marker,
    )
    assert reconcile_uncertain_notification(
        mismatch,
        "a" * 64,
        [{
            "guild_id": "100",
            "channel_id": "200",
            "content": content,
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
            "parent_payload_sha256": _parent_payload_digest(
                content, {**embed, "title": "Changed"}, _review_components()
            ),
            "message_id": "400",
            "author_is_bot": True,
            "allowed_role_mentions": ["300"],
        }],
    ) is False


def test_compact_ux_reconciliation_does_not_require_visible_marker():
    store = _Store()
    content, digest, marker, embed, _details, _detail_digest = _rendered()
    assert marker.endswith(":ux4]")
    assert marker not in content
    store.record_review_notification(
        "a" * 64,
        state="uncertain",
        content_sha256=digest,
        guild_id="100",
        channel_id="200",
        role_id="300",
        marker=marker,
    )
    from plugins.client_knowledge_gbrain.review import _parent_payload_digest, _review_components

    assert reconcile_uncertain_notification(
        store,
        "a" * 64,
        [{
            "guild_id": "100",
            "channel_id": "200",
            "content": content,
            "parent_payload_sha256": _parent_payload_digest(
                content, embed, _review_components()
            ),
            "message_id": "400",
            "author_is_bot": True,
            "allowed_role_mentions": ["300"],
        }],
    ) is True


def _interaction(*, user_id="600", roles=(300,), message_id="400", guild="100", channel="200"):
    guild_obj = SimpleNamespace(id=int(guild), name="Synthetic Guild")
    channel_obj = SimpleNamespace(id=int(channel), guild=guild_obj, name="pid", parent=None)
    response_state = {"done": False}

    async def defer(**_kwargs):
        response_state["done"] = True

    response = SimpleNamespace(
        defer=AsyncMock(side_effect=defer),
        send_message=AsyncMock(),
        is_done=lambda: response_state["done"],
    )
    return SimpleNamespace(
        id="500",
        guild_id=guild,
        channel_id=channel,
        user=SimpleNamespace(
            id=user_id,
            display_name="Reviewer",
            roles=[SimpleNamespace(id=value) for value in roles],
        ),
        message=SimpleNamespace(id=message_id),
        guild=guild_obj,
        channel=channel_obj,
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _confirmed_store():
    store = _Store()
    store.review.update(
        {
            "notification_state": "confirmed",
            "notification_message_id": "400",
            "notification_guild_id": "100",
            "notification_channel_id": "200",
            "notification_role_id": "300",
            "detail_state": "confirmed",
            "detail_thread_id": "401",
        }
    )
    return store


def test_authorized_approve_is_idempotent_and_unauthorized_or_stale_controls_do_not_mutate(monkeypatch):
    context = SimpleNamespace(
        resolved=True, guild_id="100", channel_id="200", project_key="pid"
    )
    monkeypatch.setattr(
        "gateway.discord_project_mapping.resolve_discord_project_context",
        lambda *_args, **_kwargs: context,
    )
    store = _confirmed_store()
    interaction = _interaction()
    asyncio.run(handle_discord_review_interaction(interaction, "approve", store=store, config=CFG))
    assert store.review["state"] == "approved"
    assert len(store.decisions) == 1
    interaction.response.defer.assert_awaited()
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
    asyncio.run(handle_discord_review_interaction(interaction, "approve", store=store, config=CFG))
    assert len(store.decisions) == 1

    unauthorized = _confirmed_store()
    denied = _interaction(user_id="602", roles=())
    asyncio.run(handle_discord_review_interaction(denied, "approve", store=unauthorized, config=CFG))
    assert not unauthorized.decisions
    assert denied.followup.send.await_args.kwargs["ephemeral"] is True

    stale = _confirmed_store()
    stale_interaction = _interaction(message_id="999")
    asyncio.run(handle_discord_review_interaction(stale_interaction, "approve", store=stale, config=CFG))
    assert not stale.decisions


def test_parent_without_confirmed_detail_thread_cannot_be_decided(monkeypatch):
    monkeypatch.setattr(
        "gateway.discord_project_mapping.resolve_discord_project_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            resolved=True, guild_id="100", channel_id="200", project_key="pid"
        ),
    )
    store = _confirmed_store()
    store.review.update({"detail_state": "uncertain", "detail_thread_id": None})
    interaction = _interaction()
    asyncio.run(handle_discord_review_interaction(interaction, "approve", store=store, config=CFG))
    assert not store.decisions
    assert store.review["state"] == "pending"


def test_component_click_fails_closed_when_live_project_mapping_disagrees(monkeypatch):
    monkeypatch.setattr(
        "gateway.discord_project_mapping.resolve_discord_project_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            resolved=True, guild_id="100", channel_id="200", project_key="decoy"
        ),
    )
    store = _confirmed_store()
    interaction = _interaction()
    asyncio.run(handle_discord_review_interaction(interaction, "approve", store=store, config=CFG))
    assert not store.decisions


def _event(text: str, *, user_id="600", role=True):
    raw = SimpleNamespace(
        id=700,
        author=SimpleNamespace(roles=[SimpleNamespace(id=300)] if role else []),
    )
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="200",
        chat_type="group",
        user_id=user_id,
        scope_id="100",
        project_key="pid",
        project_channel_id="200",
        project_mapping_resolved=True,
        message_id="700",
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        raw_message=raw,
        message_id="700",
    )


def test_reject_collects_authorized_reason_without_slash_command(monkeypatch):
    monkeypatch.setattr(
        "gateway.discord_project_mapping.resolve_discord_project_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            resolved=True, guild_id="100", channel_id="200", project_key="pid"
        ),
    )
    store = _confirmed_store()
    interaction = _interaction()
    asyncio.run(handle_discord_review_interaction(interaction, "reject", store=store, config=CFG))
    assert store.review["capture_mode"] == "reject_reason"

    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.IntakeStore", lambda: store)
    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.load_config", lambda: CFG)
    adapter = SimpleNamespace(send=AsyncMock())
    gateway = SimpleNamespace(_adapter_for_source=lambda _source: adapter)
    result = asyncio.run(capture_review_text_hook(
        event=_event("The wording overstates the synthetic source."), gateway=gateway
    ))
    assert result == {"action": "skip", "reason": "client_knowledge_review_text_captured"}
    assert store.review["state"] == "rejected"
    assert store.decisions[0][1]["reason"] == "The wording overstates the synthetic source."


def test_mixed_free_text_instruction_is_durable_and_keeps_publication_blocked(monkeypatch):
    monkeypatch.setattr(
        "gateway.discord_project_mapping.resolve_discord_project_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            resolved=True, guild_id="100", channel_id="200", project_key="pid"
        ),
    )
    store = _confirmed_store()
    interaction = _interaction()
    asyncio.run(
        handle_discord_review_interaction(interaction, "instructions", store=store, config=CFG)
    )
    assert store.review["capture_mode"] == "instructions"
    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.IntakeStore", lambda: store)
    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.load_config", lambda: CFG)
    kicked = []
    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.review._kick_review_revision",
        lambda gateway: kicked.append(gateway),
    )
    adapter = SimpleNamespace(send=AsyncMock())
    gateway = SimpleNamespace(_adapter_for_source=lambda _source: adapter)
    instruction = "Accept 1 and 3, reject 2, and reword 1 to say weekly status report."
    result = asyncio.run(capture_review_text_hook(event=_event(instruction), gateway=gateway))
    assert result == {"action": "skip", "reason": "client_knowledge_review_text_captured"}
    assert store.review["state"] == "instructions_pending"
    assert store.instructions[0][1]["instruction"] == instruction
    assert not store.decisions
    assert adapter.send.await_args.args[1] == (
        "Instructions saved. Hermes will prepare a revised review; nothing will be "
        "published until you approve it."
    )
    assert kicked == [gateway]


def test_text_capture_reauthorizes_exact_project_mapping():
    store = _confirmed_store()
    store.begin_review_text_capture(
        "a" * 64,
        mode="instructions",
        reviewer_user_id="600",
        reviewer_role_id="300",
        channel_id="200",
    )
    from plugins.client_knowledge_gbrain.review import _authorize_event_for_capture, ProjectReviewConfig

    event = _event("instructions")
    assert _authorize_event_for_capture(
        event, store.review, ProjectReviewConfig.from_config(CFG, "pid")
    )[0] is True
    event.source.project_mapping_resolved = False
    assert _authorize_event_for_capture(
        event, store.review, ProjectReviewConfig.from_config(CFG, "pid")
    )[0] is False


def _durable_review_store(path):
    store = IntakeStore(path)
    artifact = _artifact()
    store.insert_artifact(artifact)
    now = time.time()
    review_id = "a" * 64
    with store._write() as conn:
        conn.execute(
            "INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "1" * 64, artifact.artifact_id, artifact.content_sha256, "2" * 64,
                "ev1", "lv1", "rv1", "extracted", "storage", "object",
                "3" * 64, 1, 1, "{}", now,
            ),
        )
        conn.execute(
            "INSERT INTO interpretation_envelopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "4" * 64, artifact.artifact_id, "pid", artifact.content_sha256,
                "1" * 64, "3" * 64, "ev1", "sv1", "pv1", "task",
                "storage", "object", "5" * 64, 1, now,
            ),
        )
        conn.execute(
            "INSERT INTO interpretations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "6" * 64, "4" * 64, artifact.artifact_id, "1" * 64,
                "sv1", "pv1", "storage", "object", "7" * 64, 1,
                "provider", "model", "provider", "model", "advanced", "route",
                1, 1, 2, 0, 0, now,
            ),
        )
        conn.execute(
            "INSERT INTO assimilation_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "8" * 64, artifact.artifact_id, "6" * 64, "av1", "sv1", "pv1",
                "policy", "pid", "9" * 64, "storage", "object", "b" * 64, 1,
                "provider", "model", "provider", "model", "advanced", "route",
                1, "finding_grounding_mismatch", "head", None, 0, now,
            ),
        )
        conn.execute(
            "INSERT INTO client_knowledge_reviews("
            "review_id, assimilation_id, artifact_id, project_key, proposal_sha256, "
            "assimilation_version, state, reason_code, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,'pending',?,?,?)",
            (
                review_id, "8" * 64, artifact.artifact_id, "pid", "9" * 64,
                "av1", "finding_grounding_mismatch", now, now,
            ),
        )
        for job_id, stage in (("c" * 32, "assimilated"), ("d" * 32, "needs_review")):
            conn.execute(
                "INSERT INTO jobs(job_id, artifact_id, stage, status, max_attempts, "
                "last_error_class, created_at, updated_at) VALUES(?,?,?,'operator_blocked',3,?,?,?)",
                (job_id, artifact.artifact_id, stage, "review_pending", now, now),
            )
        for index, stage, receipt_id in (
            ("e" * 32, "notion_archived", "notion:page:0123456789abcdef0123456789abcdef"),
            ("f" * 32, "extracted", "extraction:" + "1" * 64),
            ("0" * 32, "interpreted", "interpretation:" + "6" * 64),
        ):
            conn.execute(
                "INSERT INTO jobs(job_id, artifact_id, stage, status, max_attempts, "
                "created_at, updated_at) VALUES(?,?,?,'succeeded',3,?,?)",
                (index, artifact.artifact_id, stage, now, now),
            )
            conn.execute(
                "INSERT INTO stage_receipts(artifact_id, stage, receipt_id, recorded_at) "
                "VALUES(?,?,?,?)",
                (artifact.artifact_id, stage, receipt_id, now),
            )
    store.record_review_notification(
        review_id,
        state="confirmed",
        content_sha256="e" * 64,
        guild_id="100",
        channel_id="200",
        role_id="300",
        marker="[ck-review:synthetic:ux2]",
        message_id="400",
        detail_state="confirmed",
        detail_content_sha256="f" * 64,
        detail_thread_id="401",
    )
    return store, review_id, artifact.artifact_id


def test_real_store_reject_and_mixed_instruction_transitions_remain_fail_closed(tmp_path):
    rejected, review_id, artifact_id = _durable_review_store(
        tmp_path / "reject" / "intake.db"
    )
    assert rejected.get_publication("8" * 64) is None
    assert rejected.begin_review_text_capture(
        review_id,
        mode="reject_reason",
        reviewer_user_id="600",
        reviewer_role_id="300",
        channel_id="200",
    ) is True
    assert rejected.decide_review(
        review_id,
        decision="rejected",
        reviewer_user_id="600",
        reviewer_role_id="300",
        decision_message_id="700",
        reason="Synthetic source does not support this wording.",
    ) is True
    assert rejected.get_review(review_id)["state"] == "rejected"
    with rejected._connect() as conn:
        status = conn.execute(
            "SELECT status FROM jobs WHERE artifact_id=? AND stage='assimilated'",
            (artifact_id,),
        ).fetchone()[0]
    assert status == "quarantined"
    assert rejected.get_publication("8" * 64) is None

    instructed, review_id, artifact_id = _durable_review_store(
        tmp_path / "instructions" / "intake.db"
    )
    assert instructed.begin_review_text_capture(
        review_id,
        mode="instructions",
        reviewer_user_id="600",
        reviewer_role_id="300",
        channel_id="200",
    ) is True
    instruction = "Accept 1 and 3, reject 2, and use plainer wording for 1."
    assert instructed.record_review_instruction(
        review_id,
        reviewer_user_id="600",
        reviewer_role_id="300",
        decision_message_id="701",
        instruction=instruction,
    ) is True
    review = instructed.get_review(review_id)
    assert review["state"] == "instructions_pending"
    assert review["decision_reason"] == instruction
    revision = instructed.get_review_revision_for_source(review_id)
    assert revision["state"] == "queued"
    assert revision["instruction_text"] == instruction
    with instructed._connect() as conn:
        jobs = dict(conn.execute(
            "SELECT stage, status FROM jobs WHERE artifact_id=?", (artifact_id,)
        ).fetchall())
    assert jobs["assimilated"] == "operator_blocked"
    assert jobs["needs_review"] == "operator_blocked"
    assert instructed.get_publication("8" * 64) is None


def _revised_operation(index: int, *, ignored: bool = False):
    operation = _operation(index, ignored=ignored)
    if ignored:
        return operation
    operation["claim"] = f"Revised requirement {index}: send a concise status report every Monday."
    operation["final_markdown"] = _canonical_markdown(operation, project_key="pid")
    return operation


def test_revision_batch_rejects_outside_findings_drops_and_evidence_changes():
    original = _proposal(2)
    outside = {"operations": [dict(item) for item in original["operations"]]}
    outside["operations"][0] = {**outside["operations"][0], "finding_id": "outside"}
    try:
        _validate_revision_batch(original, outside)
    except ValueError as exc:
        assert str(exc) == "review_revision_outside_batch_finding"
    else:
        raise AssertionError("outside finding was accepted")

    dropped = {"operations": [dict(original["operations"][0])]}
    try:
        _validate_revision_batch(original, dropped)
    except ValueError as exc:
        assert str(exc) == "review_revision_implicit_finding_drop"
    else:
        raise AssertionError("implicit finding drop was accepted")

    changed = {"operations": [dict(item) for item in original["operations"]]}
    changed["operations"][0]["evidence_ids"] = ["evidence-999"]
    try:
        _validate_revision_batch(original, changed)
    except ValueError as exc:
        assert str(exc) == "review_revision_evidence_changed"
    else:
        raise AssertionError("evidence change was accepted")


def test_revision_worker_creates_linked_replacement_review_requiring_approval(
    tmp_path, monkeypatch
):
    store, review_id, _artifact_id = _durable_review_store(
        tmp_path / "revision" / "intake.db"
    )
    original_proposal = {
        "artifact_id": _artifact().artifact_id,
        "interpretation_id": "6" * 64,
        "project_key": "pid",
        "operations": [_operation(1), _operation(2, ignored=True)],
    }
    revised_proposal = {
        **original_proposal,
        "operations": [_revised_operation(1), _operation(2, ignored=True)],
    }
    original_sha = hashlib.sha256(
        canonical_json(original_proposal)
    ).hexdigest()
    with store._write() as conn:
        conn.execute(
            "UPDATE assimilation_proposals SET proposal_sha256=? WHERE assimilation_id=?",
            (original_sha, "8" * 64),
        )
        conn.execute(
            "UPDATE client_knowledge_reviews SET proposal_sha256=? WHERE review_id=?",
            (original_sha, review_id),
        )

    class Derived:
        def __init__(self):
            self.saved = {}

        def read_json(self, kind, *_args):
            if kind == "assimilations":
                return {"proposal": original_proposal}
            if kind == "interpretations":
                interpretation = _interpretation(2)
                return {"interpretation": interpretation}
            if kind == "extractions":
                return EXTRACTION
            raise AssertionError(kind)

        def put_json(self, kind, object_id, value):
            from plugins.client_knowledge_gbrain.derived import DerivedRecord, canonical_json

            data = canonical_json(value)
            self.saved[object_id] = value
            return DerivedRecord(
                object_id,
                kind,
                "storage",
                f"{kind}/{object_id}",
                hashlib.sha256(data).hexdigest(),
                len(data),
                tmp_path / "object.json",
            )

    class Llm:
        def complete_structured(self, **_kwargs):
            return PluginLlmStructuredResult(
                text="{}",
                parsed=revised_proposal,
                content_type="json",
                provider="provider",
                model="model",
                agent_id="agent",
                audit={
                    "selected_provider": "provider",
                    "selected_model": "model",
                    "model_tier": "advanced",
                    "route_fingerprint": "route",
                },
            )

    class Client:
        settings = SimpleNamespace(
            source_id="client-knowledge",
            source_branch="main",
            source_checkout=tmp_path,
        )

        def search(self, *_args, **_kwargs):
            return []

        def assert_source_checkout(self):
            return tmp_path

    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.review.GitSourcePublisher.head",
        lambda _self: "head",
    )
    assert store.begin_review_text_capture(
        review_id,
        mode="instructions",
        reviewer_user_id="600",
        reviewer_role_id="300",
        channel_id="200",
    ) is True
    instruction = "Keep item 1 but use plainer wording; do not publish item 2."
    assert store.record_review_instruction(
        review_id,
        reviewer_user_id="600",
        reviewer_role_id="300",
        decision_message_id="701",
        instruction=instruction,
    ) is True
    result = process_pending_review_revisions(
        store=store,
        derived=Derived(),
        config={
            "client_knowledge": {
                "assimilation": {
                    "enabled": True,
                    "max_jobs_per_run": 1,
                    "retry_delay_seconds": 60,
                }
            }
        },
        llm=Llm(),
        client=Client(),
    )
    revision_status = store.get_review_revision_for_source(review_id)
    assert result == {"processed": 1, "succeeded": 1, "failed": 0}, (
        revision_status["last_error_class"]
    )
    source = store.get_review(review_id)
    assert source["state"] == "superseded"
    replacement = store.get_review(source["superseded_by_review_id"])
    assert replacement["state"] == "pending"
    assert replacement["parent_review_id"] == review_id
    assert replacement["revision_number"] == 1
    assert replacement["reason_code"] == "human_instruction_revision"
    revision = store.get_review_revision_for_source(review_id)
    assert revision["state"] == "succeeded"
    assert revision["replacement_review_id"] == replacement["review_id"]
    assert store.get_publication(replacement["assimilation_id"]) is None
    store.record_review_notification(
        replacement["review_id"],
        state="confirmed",
        content_sha256="1" * 64,
        guild_id="100",
        channel_id="200",
        role_id="300",
        marker="[ck-review:replacement:ux2]",
        message_id="402",
        detail_state="confirmed",
        detail_content_sha256="2" * 64,
        detail_thread_id="403",
    )
    assert store.decide_review(
        replacement["review_id"],
        decision="approved",
        reviewer_user_id="600",
        reviewer_role_id="300",
        decision_message_id="704",
    ) is True
    active = store.get_active_review_for_assimilation("8" * 64)
    assert active["review_id"] == replacement["review_id"]
    assert active["state"] == "approved"


def test_original_controls_are_stale_after_replacement_exists(tmp_path, monkeypatch):
    store, review_id, _artifact_id = _durable_review_store(
        tmp_path / "stale-original" / "intake.db"
    )
    replacement_id = "b" * 64
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_reviews SET state='superseded', "
            "superseded_by_review_id=? WHERE review_id=?",
            (replacement_id, review_id),
        )
    monkeypatch.setattr(
        "gateway.discord_project_mapping.resolve_discord_project_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            resolved=True, guild_id="100", channel_id="200", project_key="pid"
        ),
    )
    interaction = _interaction()
    asyncio.run(
        handle_discord_review_interaction(
            interaction, "approve", store=store, config=CFG
        )
    )
    assert store.get_review(review_id)["state"] == "superseded"
    assert "already been resolved" in interaction.followup.send.await_args.args[0]


def test_revision_failure_stays_retryable_and_operator_visible(tmp_path):
    store, review_id, _artifact_id = _durable_review_store(
        tmp_path / "revision-failure" / "intake.db"
    )
    assert store.begin_review_text_capture(
        review_id,
        mode="instructions",
        reviewer_user_id="600",
        reviewer_role_id="300",
        channel_id="200",
    ) is True
    assert store.record_review_instruction(
        review_id,
        reviewer_user_id="600",
        reviewer_role_id="300",
        decision_message_id="701",
        instruction="Use plainer wording.",
    ) is True
    original = _proposal(2)
    original.update(
        {
            "artifact_id": _artifact().artifact_id,
            "interpretation_id": "6" * 64,
            "project_key": "pid",
        }
    )
    original_sha = hashlib.sha256(canonical_json(original)).hexdigest()
    with store._write() as conn:
        conn.execute(
            "UPDATE assimilation_proposals SET proposal_sha256=? WHERE assimilation_id=?",
            (original_sha, "8" * 64),
        )
        conn.execute(
            "UPDATE client_knowledge_reviews SET proposal_sha256=? WHERE review_id=?",
            (original_sha, review_id),
        )

    class FailureDerived(_Derived):
        def read_json(self, kind, *_args):
            if kind == "assimilations":
                return {"proposal": original}
            return super().read_json(kind, *_args)

    class UnavailableClient:
        settings = SimpleNamespace(source_id="client-knowledge")

        def search(self, *_args, **_kwargs):
            raise ConnectionError("temporarily unavailable")

    result = process_pending_review_revisions(
        store=store,
        derived=FailureDerived(2),
        config={
            "client_knowledge": {
                "assimilation": {
                    "enabled": True,
                    "max_jobs_per_run": 1,
                    "retry_delay_seconds": 60,
                }
            }
        },
        llm=SimpleNamespace(),
        client=UnavailableClient(),
    )
    assert result == {"processed": 1, "succeeded": 0, "failed": 1}
    review = store.get_review(review_id)
    assert review["state"] == "instructions_pending"
    visible = store.list_open_reviews()
    assert visible[0]["revision_state"] == "failed"
    assert visible[0]["revision_attempt_count"] == 1
    assert visible[0]["revision_error_class"] == "review_revision_internal_error"
    assert store.claim_next_review_revision(
        now=time.time() + 61
    ) is not None


def test_refresh_can_replace_previous_native_card(tmp_path):
    store, review_id, _artifact_id = _durable_review_store(
        tmp_path / "refresh" / "intake.db"
    )
    assert store.refresh_review_notification(review_id) is True
    review = store.get_review(review_id)
    assert review["state"] == "pending"
    assert review["notification_state"] == "pending"
    assert review["notification_message_id"] is None


def test_refresh_does_not_replace_healthy_compact_native_card(tmp_path):
    store, review_id, _artifact_id = _durable_review_store(
        tmp_path / "compact-refresh" / "intake.db"
    )
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_reviews SET notification_marker=? WHERE review_id=?",
            ("[ck-review:synthetic:ux4]", review_id),
        )
    assert store.refresh_review_notification(review_id) is False


def test_refresh_can_replace_incomplete_compact_native_card(tmp_path):
    store, review_id, _artifact_id = _durable_review_store(
        tmp_path / "compact-incomplete-refresh" / "intake.db"
    )
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_reviews SET notification_marker=?, detail_state='uncertain' "
            "WHERE review_id=?",
            ("[ck-review:synthetic:ux4]", review_id),
        )
    assert store.refresh_review_notification(review_id) is True
