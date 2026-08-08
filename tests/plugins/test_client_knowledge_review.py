from __future__ import annotations

import asyncio
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from plugins.client_knowledge_gbrain.derived import canonical_json
from plugins.client_knowledge_gbrain.models import IntakeArtifact
from plugins.client_knowledge_gbrain.review import (
    ProjectReviewConfig,
    ReviewFailure,
    _render_notification,
    capture_review_text_hook,
    fetch_and_reconcile_replacement_notification,
    handle_discord_review_interaction,
    item_review_components,
    send_pending_replacement_notifications,
    send_pending_review_notifications,
)
from plugins.client_knowledge_gbrain.synthesis import SynthesisFailure, SynthesisSettings
from plugins.client_knowledge_gbrain.store import IntakeStore


CFG = {
    "client_knowledge": {"review_notifications": {"enabled": True}},
    "projects": {"pid": {
        "display_name": "PID",
        "client_knowledge_review": {
            "guild_id": "100", "channel_id": "200", "reviewer_role_id": "300",
            "reviewer_user_ids": ["601"],
        },
    }},
}
EXTRACTION = {"segments": [
    {"kind": "header", "label": "From", "text": "Alex <alex@example.test>"},
    {"kind": "header", "label": "Subject", "text": "PID weekly reporting"},
    {"kind": "header", "label": "Date", "text": "Fri, 7 Aug 2026 09:00:00 +0000"},
]}


def _evidence(quote):
    return json.dumps([{
        "segment_id": "body-0001", "start": 0, "end": len(quote), "quote": quote,
    }], sort_keys=True, separators=(",", ":"))


def _items():
    return [
        {"item_id": "1" * 64, "position": 1, "revision_number": 0,
         "statement": "Send a concise status report every Monday.",
         "evidence_json": _evidence("Send a concise status report every Monday."),
         "state": "pending"},
        {"item_id": "2" * 64, "position": 2, "revision_number": 0,
         "statement": "Use the existing approval flow for client-facing changes.",
         "evidence_json": _evidence("Use the existing approval flow for client-facing changes."),
         "state": "pending"},
    ]


def _synthesis():
    return {
        "synthesis_id": "s" * 64, "artifact_id": "a" * 64, "project_key": "pid",
        "notion_ref": "notion:page:source", "output_sha256": "o" * 64,
        "output_bytes": 1, "extraction_id": "e" * 64, "state": "review_pending",
    }


def test_parent_has_no_controls_and_each_item_has_exactly_three_static_controls():
    content, _digest, _marker, embed, details, _items_digest = _render_notification(
        _synthesis(), _items(), EXTRACTION, ProjectReviewConfig.from_config(CFG, "pid")
    )
    assert content == "<@&300>"
    assert embed["title"] == "Self-Education"
    assert embed["description"].startswith("PID weekly reporting\n\n")
    assert "2 publication candidates" in embed["description"]
    assert "Source in Notion" in embed["description"]
    assert "Alex" in str(embed)
    assert "Email subject" not in str(embed.get("fields"))
    assert "Email sender" in str(embed.get("fields"))
    assert "Email date" in str(embed.get("fields"))
    assert all("components" in item for item in details)
    assert [button["label"] for button in details[0]["components"][0]["components"]] == [
        "Approve", "Reject", "✍️ Other",
    ]
    assert [button["custom_id"] for button in item_review_components()[0]["components"]] == [
        "client-knowledge-review-item:approve",
        "client-knowledge-review-item:reject",
        "client-knowledge-review-item:instructions",
    ]
    assert details[0]["embeds"][0]["fields"][0]["value"].endswith(
        r"Send a concise status report every Monday\."
    )
    assert details[0]["embeds"][0]["footer"]["text"] == "Learning 1 · revision 1"
    assert "ignore_transient" not in str(details)


def test_exact_evidence_is_split_without_utf16_truncation():
    quote = "😀" * 800
    item = {
        "item_id": "1" * 64, "position": 1, "revision_number": 0,
        "statement": "Durable statement.", "state": "pending",
        "evidence_json": _evidence(quote),
    }
    _content, _digest, _marker, _embed, details, _items_digest = _render_notification(
        _synthesis(), [item], EXTRACTION, ProjectReviewConfig.from_config(CFG, "pid")
    )
    fields = details[0]["embeds"][0]["fields"]
    from gateway.platforms.base import utf16_len

    assert len(fields) == 2
    assert all(utf16_len(field["value"]) <= 1024 for field in fields)
    rendered = fields[0]["value"].split("\n> ", 1)[1] + "".join(
        field["value"][2:] for field in fields[1:]
    )
    assert rendered == quote


def test_multiline_exact_evidence_keeps_quote_prefix_across_continuation_fields():
    quote = ("first line\n" + "second line " * 120 + "\nthird line")
    item = {
        "item_id": "1" * 64, "position": 1, "revision_number": 0,
        "statement": "Durable statement.", "state": "pending",
        "evidence_json": _evidence(quote),
    }
    details = _render_notification(
        _synthesis(), [item], EXTRACTION, ProjectReviewConfig.from_config(CFG, "pid")
    )[4]
    fields = details[0]["embeds"][0]["fields"]
    assert len(fields) >= 2
    chunks = []
    for index, field in enumerate(fields):
        value = field["value"]
        if index == 0:
            value = value.split("\n", 1)[1]
        assert value.startswith("> ")
        assert all(line.startswith("> ") for line in value.splitlines())
        chunks.append(value[2:].replace("\n> ", "\n"))
    assert "".join(chunks) == quote


def test_exact_evidence_split_never_breaks_markdown_escape_pair():
    quote = "*" * 600
    item = {
        "item_id": "1" * 64, "position": 1, "revision_number": 0,
        "statement": "Durable statement.", "state": "pending",
        "evidence_json": _evidence(quote),
    }
    details = _render_notification(
        _synthesis(), [item], EXTRACTION, ProjectReviewConfig.from_config(CFG, "pid")
    )[4]
    values = [field["value"] for field in details[0]["embeds"][0]["fields"]]
    assert all(not value.endswith("\\") for value in values[:-1])


def test_candidate_rejects_aggregate_embed_overflow_without_evidence_truncation():
    evidence = [
        {
            "segment_id": f"body-{index:04d}",
            "start": 0,
            "end": 800,
            "quote": "*" * 800,
        }
        for index in range(1, 4)
    ]
    item = {
        "item_id": "1" * 64,
        "position": 1,
        "revision_number": 0,
        "statement": "x" * 2000,
        "state": "pending",
        "evidence_json": json.dumps(evidence, sort_keys=True, separators=(",", ":")),
    }
    with pytest.raises(ReviewFailure, match="aggregate embed"):
        _render_notification(
            _synthesis(), [item], EXTRACTION, ProjectReviewConfig.from_config(CFG, "pid")
        )


class _Store:
    def __init__(self):
        self.synthesis = _synthesis()
        self.items = _items()
        self.parent = {"state": "pending"}
        self.decisions = []

    def list_pending_synthesis_notifications(self, limit=50):
        return [dict(self.synthesis)] if self.parent["state"] == "pending" else []

    def get_extraction(self, _):
        return {"extraction_id": "e" * 64, "output_sha256": "x" * 64, "output_bytes": 1}

    def list_synthesis_items(self, _id, active_only=False):
        return [dict(item) for item in self.items if not active_only or item["state"] != "superseded"]

    def claim_synthesis_notification(self, *_args, **_kwargs):
        self.parent["state"] = "uncertain"
        return True

    def record_synthesis_notification(self, _id, *, state, item_message_ids=(), **kwargs):
        self.parent.update({"state": state, "thread_id": kwargs.get("thread_id"),
                            "guild_id": kwargs["guild_id"], "channel_id": kwargs["channel_id"],
                            "role_id": kwargs["role_id"]})
        for item, message_id in zip(self.items, item_message_ids):
            item["notification_message_id"] = message_id
            item["notification_state"] = "confirmed"

    def get_synthesis_item_by_message(self, message_id):
        for item in self.items:
            if item.get("notification_message_id") == message_id:
                return {**item, "guild_id": self.parent.get("guild_id"),
                        "channel_id": self.parent.get("channel_id"),
                        "role_id": self.parent.get("role_id"),
                        "thread_id": self.parent.get("thread_id"),
                        "parent_notification_state": self.parent["state"],
                        "project_key": "pid", "synthesis_state": "review_pending"}
        return None

    def decide_synthesis_item(self, item_id, **kwargs):
        item = next(value for value in self.items if value["item_id"] == item_id)
        if item["state"] != "pending":
            return False
        item["state"] = kwargs["decision"]
        self.decisions.append((item_id, kwargs["decision"]))
        return True

    def begin_synthesis_item_instruction(self, item_id, **kwargs):
        item = next(value for value in self.items if value["item_id"] == item_id)
        if item.get("capture_user_id"):
            return False
        item.update(kwargs)
        item["capture_user_id"] = kwargs["reviewer_user_id"]
        return True


class _Derived:
    def read_json(self, kind, *_args):
        if kind == "syntheses":
            return {"synthesis_id": "s" * 64}
        if kind == "extractions":
            return EXTRACTION
        raise AssertionError(kind)


def test_delivery_returns_ordered_message_ids_and_click_changes_only_one_item(monkeypatch):
    store = _Store()

    async def sender(**kwargs):
        assert "components" not in kwargs["embed"]
        return {
            "success": True, "message_id": "400", "thread_id": "401",
            "side_effect_state": "confirmed", "detail_state": "confirmed",
            "detail_message_ids": ["402", "403"],
        }

    result = asyncio.run(send_pending_review_notifications(
        store=store, derived=_Derived(), config=CFG, sender=sender
    ))
    assert result["confirmed"] == 1
    monkeypatch.setattr(
        "gateway.discord_project_mapping.resolve_discord_project_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            resolved=True, guild_id="100", channel_id="200", project_key="pid"
        ),
    )
    interaction = _interaction(message_id="402")
    asyncio.run(handle_discord_review_interaction(
        interaction, "approve", store=store, config=CFG
    ))
    assert [item["state"] for item in store.items] == ["approved", "pending"]
    assert store.decisions == [("1" * 64, "approved")]


def _interaction(*, message_id="402", thread_id="401", user_id="600", roles=(300,), guild="100"):
    guild_obj = SimpleNamespace(id=int(guild), name="Guild")
    parent = SimpleNamespace(id=200, guild=guild_obj)
    channel = SimpleNamespace(id=int(thread_id), guild=guild_obj, parent=parent, name="review")
    state = {"done": False}

    async def defer(**_kwargs):
        state["done"] = True

    return SimpleNamespace(
        id="500", guild_id=guild, channel_id=thread_id,
        user=SimpleNamespace(id=user_id, roles=[SimpleNamespace(id=value) for value in roles]),
        message=SimpleNamespace(id=message_id), guild=guild_obj, channel=channel,
        response=SimpleNamespace(defer=AsyncMock(side_effect=defer), send_message=AsyncMock(),
                                 is_done=lambda: state["done"]),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def test_cross_thread_stale_duplicate_and_unauthorized_interactions_fail_closed(monkeypatch):
    monkeypatch.setattr(
        "gateway.discord_project_mapping.resolve_discord_project_context",
        lambda *_args, **_kwargs: SimpleNamespace(
            resolved=True, guild_id="100", channel_id="200", project_key="pid"
        ),
    )
    for interaction in (
        _interaction(thread_id="999"),
        _interaction(message_id="999"),
        _interaction(user_id="602", roles=()),
    ):
        store = _Store()
        store.parent.update({"state": "confirmed", "thread_id": "401", "guild_id": "100",
                             "channel_id": "200", "role_id": "300"})
        store.items[0].update({"notification_message_id": "402", "notification_state": "confirmed"})
        asyncio.run(handle_discord_review_interaction(
            interaction, "approve", store=store, config=CFG
        ))
        assert not store.decisions
    duplicate = _Store()
    duplicate.parent.update({"state": "confirmed", "thread_id": "401", "guild_id": "100",
                             "channel_id": "200", "role_id": "300"})
    duplicate.items[0].update({"notification_message_id": "402", "notification_state": "confirmed"})
    interaction = _interaction()
    asyncio.run(handle_discord_review_interaction(interaction, "approve", store=duplicate, config=CFG))
    asyncio.run(handle_discord_review_interaction(interaction, "approve", store=duplicate, config=CFG))
    assert len(duplicate.decisions) == 1


def _durable_store(tmp_path):
    store = IntakeStore(tmp_path / "intake.db")
    artifact = IntakeArtifact.from_bytes(
        project_key="pid", provider_id="gmail", provider_artifact_id="message-1", content=b"source"
    )
    store.insert_artifact(artifact)
    now = time.time()
    synthesis = {
        "synthesis_id": "s" * 64, "artifact_id": artifact.artifact_id,
        "extraction_id": "e" * 64, "project_key": "pid", "notion_ref": "notion:page:source",
        "synthesis_version": "v1", "schema_version": "sv1", "prompt_version": "pv1",
        "derived_storage_id": "storage", "derived_object_key": "object",
        "output_sha256": "o" * 64, "output_bytes": 1, "actual_provider": "provider",
        "actual_model": "model", "selected_provider": "provider", "selected_model": "model",
        "model_tier": "advanced", "route_fingerprint": "route", "base_git_head": "head",
    }
    with store._write() as conn:
        conn.execute(
            "INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("e" * 64, artifact.artifact_id, artifact.content_sha256, "m" * 64, "ev", "lv", "rv",
             "extracted", "storage", "object", "x" * 64, 1, 1, "{}", now),
        )
        store._insert_synthesis_locked(conn, synthesis, [
            {"item_id": "1" * 64, "position": 1, "statement": "First.",
             "evidence_json": _evidence("First."), "item_sha256": "a" * 64},
            {"item_id": "2" * 64, "position": 2, "statement": "Second.",
             "evidence_json": _evidence("Second."), "item_sha256": "b" * 64},
        ], now=now)
        conn.execute(
            "UPDATE client_knowledge_synthesis_notifications SET state='confirmed', message_id='400', "
            "guild_id='100', channel_id='200', role_id='300', thread_id='401' WHERE synthesis_id=?",
            ("s" * 64,),
        )
        conn.execute(
            "UPDATE client_knowledge_synthesis_items SET notification_state='confirmed', "
            "notification_message_id=CASE item_id WHEN ? THEN '402' ELSE '403' END",
            ("1" * 64,),
        )
        for job_id, stage in (("a" * 32, "synthesized"), ("b" * 32, "needs_review")):
            conn.execute(
                "INSERT INTO jobs(job_id, artifact_id, stage, status, max_attempts, created_at, updated_at) "
                "VALUES(?,?,?,'operator_blocked',3,?,?)",
                (job_id, artifact.artifact_id, stage, now, now),
            )
    return store, artifact.artifact_id


def _record_instruction(store, item_id, **kwargs):
    item = store.get_synthesis_item(item_id)
    capture_started_at = float(item["capture_started_at"])
    return store.record_synthesis_item_instruction(
        item_id,
        expected_capture_started_at=capture_started_at,
        source_created_at=capture_started_at + 1,
        **kwargs,
    )


def test_partial_resolution_never_releases_and_final_resolution_releases(tmp_path):
    store, artifact_id = _durable_store(tmp_path)
    assert store.decide_synthesis_item(
        "1" * 64, decision="approved", reviewer_user_id="600",
        reviewer_role_id="300", decision_message_id="500",
    ) is True
    assert store.get_synthesis("s" * 64)["state"] == "review_pending"
    assert store.get_job("a" * 32)["status"] == "operator_blocked"
    assert store.decide_synthesis_item(
        "2" * 64, decision="rejected", reviewer_user_id="600",
        reviewer_role_id="300", decision_message_id="501",
    ) is True
    assert store.get_synthesis("s" * 64)["state"] == "ready"
    assert store.get_job("a" * 32)["status"] == "queued"
    assert store.get_synthesis_publication("s" * 64) is None


def test_prepared_publication_reset_is_durable_compare_and_swap(tmp_path):
    store, artifact_id = _durable_store(tmp_path)
    old_manifest = '[{"path":"old"}]'
    new_manifest = '[{"path":"new"}]'
    store.record_synthesis_publication(
        synthesis_id="s" * 64,
        artifact_id=artifact_id,
        synthesis_version="v1",
        content_sha256="c" * 64,
        branch_ref="refs/heads/main",
        expected_head="old-head",
        manifest_json=old_manifest,
        state="prepared",
    )
    assert store.reset_prepared_synthesis_publication(
        synthesis_id="s" * 64,
        old_expected_head="old-head",
        old_manifest_json=old_manifest,
        new_expected_head="new-head",
        new_manifest_json=new_manifest,
    ) is True
    row = store.get_synthesis_publication("s" * 64)
    assert row["expected_head"] == "new-head"
    assert row["manifest_json"] == new_manifest
    assert store.reset_prepared_synthesis_publication(
        synthesis_id="s" * 64,
        old_expected_head="old-head",
        old_manifest_json=old_manifest,
        new_expected_head="other-head",
        new_manifest_json="[]",
    ) is False
    store.record_synthesis_publication(
        synthesis_id="s" * 64,
        artifact_id=artifact_id,
        synthesis_version="v1",
        content_sha256="c" * 64,
        branch_ref="refs/heads/main",
        expected_head="new-head",
        manifest_json=new_manifest,
        state="committed",
        commit_sha="d" * 40,
    )
    assert store.reset_prepared_synthesis_publication(
        synthesis_id="s" * 64,
        old_expected_head="new-head",
        old_manifest_json=new_manifest,
        new_expected_head="latest-head",
        new_manifest_json="[]",
    ) is False


def test_uncertain_parent_and_items_can_be_adopted_by_exact_payload(tmp_path, monkeypatch):
    store, _artifact_id = _durable_store(tmp_path)
    extraction = {**EXTRACTION, "segments": [
        *EXTRACTION["segments"],
        {"segment_id": "body-0001", "kind": "body_plain", "label": "Email body",
         "text": "First.Second."},
    ]}
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_synthesis_notifications SET state='uncertain', "
            "message_id=NULL, thread_id=NULL, content_sha256=NULL, marker=NULL, items_sha256=NULL "
            "WHERE synthesis_id=?",
            ("s" * 64,),
        )
        conn.execute(
            "UPDATE client_knowledge_synthesis_items SET notification_state='pending', "
            "notification_message_id=NULL WHERE synthesis_id=?",
            ("s" * 64,),
        )
    synthesis = store.get_synthesis("s" * 64)
    items = store.list_synthesis_items("s" * 64, active_only=True)
    content, digest, marker, embed, details, items_digest = _render_notification(
        synthesis, items, extraction, ProjectReviewConfig.from_config(CFG, "pid")
    )
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_synthesis_notifications SET content_sha256=?, guild_id='100', "
            "channel_id='200', role_id='300', marker=?, items_sha256=? WHERE synthesis_id=?",
            (digest, marker, items_digest, "s" * 64),
        )
    class Derived:
        def read_json(self, kind, *_args):
            if kind == "syntheses":
                return {"synthesis_id": "s" * 64}
            if kind == "extractions":
                return extraction
            raise AssertionError(kind)

    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.DerivedStore", Derived)
    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.load_config", lambda: CFG)
    monkeypatch.setattr("tools.discord_tool._get_bot_token", lambda: "token")

    def discord_request(method, path, _token, params=None):
        if path == "/channels/200/messages/400":
            return {
                "id": "400", "guild_id": "100", "channel_id": "200", "content": content,
                "author": {"bot": True}, "mention_roles": ["300"], "thread": {"id": "401"},
                "embeds": [embed], "components": [],
            }
        assert method == "GET" and path == "/channels/401/messages"
        return [
            {"id": message_id, "content": payload["content"], "embeds": payload["embeds"],
             "components": payload["components"], "author": {"bot": True}}
            for message_id, payload in zip(("402", "403"), details)
        ]

    monkeypatch.setattr("tools.discord_tool._discord_request", discord_request)
    from plugins.client_knowledge_gbrain.review import fetch_and_reconcile_notification

    assert fetch_and_reconcile_notification(store, "s" * 64, "400") is True
    assert [item["notification_message_id"] for item in store.list_synthesis_items("s" * 64)] == [
        "402", "403",
    ]


def test_parent_reconciliation_paginates_beyond_one_hundred_messages(tmp_path, monkeypatch):
    store, _artifact_id = _durable_store(tmp_path)
    extraction = {**EXTRACTION, "segments": [
        *EXTRACTION["segments"],
        {"segment_id": "body-0001", "kind": "body_plain", "label": "Email body",
         "text": "First.Second."},
    ]}
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_synthesis_notifications SET state='uncertain', "
            "message_id=NULL, thread_id=NULL, content_sha256=NULL, marker=NULL, items_sha256=NULL "
            "WHERE synthesis_id=?",
            ("s" * 64,),
        )
        conn.execute(
            "UPDATE client_knowledge_synthesis_items SET notification_state='pending', "
            "notification_message_id=NULL WHERE synthesis_id=?",
            ("s" * 64,),
        )
    synthesis = store.get_synthesis("s" * 64)
    items = store.list_synthesis_items("s" * 64, active_only=True)
    content, digest, marker, embed, details, items_digest = _render_notification(
        synthesis, items, extraction, ProjectReviewConfig.from_config(CFG, "pid")
    )
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_synthesis_notifications SET content_sha256=?, guild_id='100', "
            "channel_id='200', role_id='300', marker=?, items_sha256=? WHERE synthesis_id=?",
            (digest, marker, items_digest, "s" * 64),
        )

    class Derived:
        def read_json(self, kind, *_args):
            return {"synthesis_id": "s" * 64} if kind == "syntheses" else extraction

    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.DerivedStore", Derived)
    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.load_config", lambda: CFG)
    monkeypatch.setattr("tools.discord_tool._get_bot_token", lambda: "token")
    page_calls = []

    def discord_request(_method, path, _token, params=None):
        if path == "/channels/200/messages/400":
            return {
                "id": "400", "guild_id": "100", "channel_id": "200", "content": content,
                "author": {"bot": True}, "mention_roles": ["300"], "thread": {"id": "401"},
                "embeds": [embed], "components": [],
            }
        page_calls.append(dict(params or {}))
        if len(page_calls) == 1:
            return [
                {"id": str(1000 - index), "content": f"unrelated-{index}",
                 "embeds": [], "components": [], "author": {"bot": True}}
                for index in range(100)
            ]
        return [
            {"id": message_id, "content": payload["content"], "embeds": payload["embeds"],
             "components": payload["components"], "author": {"bot": True}}
            for message_id, payload in zip(("402", "403"), details)
        ]

    monkeypatch.setattr("tools.discord_tool._discord_request", discord_request)
    from plugins.client_knowledge_gbrain.review import fetch_and_reconcile_notification

    assert fetch_and_reconcile_notification(store, "s" * 64, "400") is True
    assert page_calls == [{"limit": "100"}, {"limit": "100", "before": "901"}]
    assert store.get_synthesis_notification("s" * 64)["state"] == "confirmed"


def test_parent_reconciliation_remains_uncertain_when_history_cap_is_full(tmp_path, monkeypatch):
    store, _artifact_id = _durable_store(tmp_path)
    extraction = {**EXTRACTION, "segments": [
        *EXTRACTION["segments"],
        {"segment_id": "body-0001", "kind": "body_plain", "label": "Email body",
         "text": "First.Second."},
    ]}
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_synthesis_notifications SET state='uncertain', "
            "message_id=NULL, thread_id=NULL, content_sha256=NULL, marker=NULL, items_sha256=NULL "
            "WHERE synthesis_id=?",
            ("s" * 64,),
        )
        conn.execute(
            "UPDATE client_knowledge_synthesis_items SET notification_state='pending', "
            "notification_message_id=NULL WHERE synthesis_id=?",
            ("s" * 64,),
        )
    synthesis = store.get_synthesis("s" * 64)
    items = store.list_synthesis_items("s" * 64, active_only=True)
    content, digest, marker, embed, _details, items_digest = _render_notification(
        synthesis, items, extraction, ProjectReviewConfig.from_config(CFG, "pid")
    )
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_synthesis_notifications SET content_sha256=?, guild_id='100', "
            "channel_id='200', role_id='300', marker=?, items_sha256=? WHERE synthesis_id=?",
            (digest, marker, items_digest, "s" * 64),
        )

    class Derived:
        def read_json(self, kind, *_args):
            return {"synthesis_id": "s" * 64} if kind == "syntheses" else extraction

    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.DerivedStore", Derived)
    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.load_config", lambda: CFG)
    monkeypatch.setattr("tools.discord_tool._get_bot_token", lambda: "token")
    page = 0

    def discord_request(_method, path, _token, params=None):
        nonlocal page
        if path == "/channels/200/messages/400":
            return {
                "id": "400", "guild_id": "100", "channel_id": "200", "content": content,
                "author": {"bot": True}, "mention_roles": ["300"], "thread": {"id": "401"},
                "embeds": [embed], "components": [],
            }
        page += 1
        start = 100_000 - (page - 1) * 100
        return [
            {"id": str(start - index), "content": "unrelated", "embeds": [],
             "components": [], "author": {"bot": True}}
            for index in range(100)
        ]

    monkeypatch.setattr("tools.discord_tool._discord_request", discord_request)
    from plugins.client_knowledge_gbrain.review import fetch_and_reconcile_notification

    assert fetch_and_reconcile_notification(store, "s" * 64, "400") is False
    assert page == 10
    assert store.get_synthesis_notification("s" * 64)["state"] == "uncertain"


def test_uncertain_parent_adoption_persists_partial_prefix_for_repair(tmp_path, monkeypatch):
    store, _artifact_id = _durable_store(tmp_path)
    extraction = {**EXTRACTION, "segments": [
        *EXTRACTION["segments"],
        {"segment_id": "body-0001", "kind": "body_plain", "label": "Email body",
         "text": "First.Second."},
    ]}
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_synthesis_notifications SET state='uncertain', "
            "message_id=NULL, thread_id=NULL, content_sha256=NULL, marker=NULL, items_sha256=NULL "
            "WHERE synthesis_id=?",
            ("s" * 64,),
        )
        conn.execute(
            "UPDATE client_knowledge_synthesis_items SET notification_state='pending', "
            "notification_message_id=NULL WHERE synthesis_id=?",
            ("s" * 64,),
        )
    synthesis = store.get_synthesis("s" * 64)
    items = store.list_synthesis_items("s" * 64, active_only=True)
    content, digest, marker, embed, details, items_digest = _render_notification(
        synthesis, items, extraction, ProjectReviewConfig.from_config(CFG, "pid")
    )
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_synthesis_notifications SET content_sha256=?, guild_id='100', "
            "channel_id='200', role_id='300', marker=?, items_sha256=? WHERE synthesis_id=?",
            (digest, marker, items_digest, "s" * 64),
        )
    class Derived:
        def read_json(self, kind, *_args):
            return {"synthesis_id": "s" * 64} if kind == "syntheses" else extraction

    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.DerivedStore", Derived)
    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.load_config", lambda: CFG)
    monkeypatch.setattr("tools.discord_tool._get_bot_token", lambda: "token")

    def discord_request(_method, path, _token, params=None):
        if path == "/channels/200/messages/400":
            return {
                "id": "400", "guild_id": "100", "channel_id": "200", "content": content,
                "author": {"bot": True}, "mention_roles": ["300"], "thread": {"id": "401"},
                "embeds": [embed], "components": [],
            }
        return [{
            "id": "402", "content": details[0]["content"], "embeds": details[0]["embeds"],
            "components": details[0]["components"], "author": {"bot": True},
        }]

    monkeypatch.setattr("tools.discord_tool._discord_request", discord_request)
    from plugins.client_knowledge_gbrain.review import fetch_and_reconcile_notification

    assert fetch_and_reconcile_notification(store, "s" * 64, "400")
    assert store.get_synthesis_notification("s" * 64)["state"] == "uncertain"
    assert store.get_synthesis_item("1" * 64)["notification_message_id"] == "402"
    assert store.get_synthesis_item("2" * 64)["notification_message_id"] is None


def _prepare_partial_notification(store, extraction, *, thread_id="401", first_id="402"):
    synthesis = store.get_synthesis("s" * 64)
    items = store.list_synthesis_items("s" * 64, active_only=True)
    _content, digest, marker, _embed, _details, items_digest = _render_notification(
        synthesis, items, extraction, ProjectReviewConfig.from_config(CFG, "pid")
    )
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_synthesis_notifications SET state='uncertain', "
            "content_sha256=?, guild_id='100', channel_id='200', role_id='300', marker=?, "
            "items_sha256=?, message_id='400', thread_id=? WHERE synthesis_id=?",
            (digest, marker, items_digest, thread_id, "s" * 64),
        )
        conn.execute(
            "UPDATE client_knowledge_synthesis_items SET notification_state='pending', "
            "notification_message_id=NULL WHERE synthesis_id=?",
            ("s" * 64,),
        )
        if first_id:
            conn.execute(
                "UPDATE client_knowledge_synthesis_items SET notification_state='confirmed', "
                "notification_message_id=? WHERE item_id=?",
                (first_id, "1" * 64),
            )


def test_partial_initial_delivery_resumes_missing_item_without_duplicate_parent(tmp_path):
    store, _artifact_id = _durable_store(tmp_path)
    _prepare_partial_notification(store, EXTRACTION)
    parent_sender = AsyncMock(side_effect=AssertionError("parent must not be resent"))
    detail_sender = AsyncMock(return_value={
        "success": True, "message_id": "403", "side_effect_state": "confirmed",
    })
    result = asyncio.run(send_pending_review_notifications(
        store=store, derived=_Derived(), config=CFG,
        sender=parent_sender, detail_sender=detail_sender,
    ))
    assert result["confirmed"] == 1
    parent_sender.assert_not_awaited()
    assert [item["notification_message_id"] for item in store.list_synthesis_items("s" * 64)] == [
        "402", "403",
    ]


def test_crash_after_detail_send_adopts_exact_existing_message_before_retry(tmp_path):
    store, _artifact_id = _durable_store(tmp_path)
    _prepare_partial_notification(store, EXTRACTION)
    detail_sender = AsyncMock(side_effect=AssertionError("detail must not be duplicated"))
    detail_resolver = AsyncMock(return_value={
        "success": True, "message_id": "403", "side_effect_state": "confirmed",
    })
    result = asyncio.run(send_pending_review_notifications(
        store=store, derived=_Derived(), config=CFG,
        sender=AsyncMock(side_effect=AssertionError("parent must not be resent")),
        detail_sender=detail_sender, detail_resolver=detail_resolver,
    ))
    assert result["confirmed"] == 1
    detail_sender.assert_not_awaited()
    assert store.get_synthesis_item("2" * 64)["notification_message_id"] == "403"


def test_parent_only_delivery_recovers_thread_and_all_items(tmp_path):
    store, _artifact_id = _durable_store(tmp_path)
    _prepare_partial_notification(store, EXTRACTION, thread_id=None, first_id=None)
    ids = iter(("402", "403"))
    detail_sender = AsyncMock(side_effect=lambda **_kwargs: {
        "success": True, "message_id": next(ids), "side_effect_state": "confirmed",
    })
    result = asyncio.run(send_pending_review_notifications(
        store=store, derived=_Derived(), config=CFG,
        sender=AsyncMock(side_effect=AssertionError("parent must not be resent")),
        detail_sender=detail_sender,
        thread_creator=AsyncMock(return_value={
            "success": True, "thread_id": "401", "side_effect_state": "confirmed",
        }),
    ))
    assert result["confirmed"] == 1
    assert store.get_synthesis_notification("s" * 64)["thread_id"] == "401"


def test_uncertain_replacement_can_be_adopted_by_exact_payload(tmp_path, monkeypatch):
    store, _artifact_id = _durable_store(tmp_path)
    assert store.begin_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    )
    assert _record_instruction(store,
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300",
        decision_message_id="700", instruction="Use plainer wording.",
    )
    claim = store.claim_next_synthesis_item_revision()
    replacement_id = "3" * 64
    assert store.complete_synthesis_item_revision(claim, replacement={
        "item_id": replacement_id, "statement": "Revised first.",
        "evidence_json": _evidence("First."), "item_sha256": "c" * 64,
        "derived_storage_id": "storage", "derived_object_key": "object",
        "output_sha256": "d" * 64, "output_bytes": 1, "actual_provider": "provider",
        "actual_model": "model", "selected_provider": "provider", "selected_model": "model",
        "model_tier": "advanced", "route_fingerprint": "route",
    })
    assert store.record_replacement_item_notification(
        replacement_id, state="uncertain"
    )
    payload = __import__(
        "plugins.client_knowledge_gbrain.review", fromlist=["_item_payload"]
    )._item_payload(store.get_synthesis_item(replacement_id))
    monkeypatch.setattr("tools.discord_tool._get_bot_token", lambda: "token")
    monkeypatch.setattr("tools.discord_tool._discord_request", lambda *_a, **_k: {
        "id": "404", "channel_id": "401", "author": {"bot": True}, **payload,
    })
    assert fetch_and_reconcile_replacement_notification(
        store, replacement_id, "404"
    )
    assert store.get_synthesis_item(replacement_id)["notification_message_id"] == "404"


def test_replacement_reconciliation_includes_revision_footer_identity(tmp_path, monkeypatch):
    store, _artifact_id = _durable_store(tmp_path)
    assert store.begin_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    )
    assert _record_instruction(store,
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300",
        decision_message_id="700", instruction="Use plainer wording.",
    )
    claim = store.claim_next_synthesis_item_revision()
    replacement_id = "3" * 64
    assert store.complete_synthesis_item_revision(claim, replacement={
        "item_id": replacement_id, "statement": "Revised first.",
        "evidence_json": _evidence("First."), "item_sha256": "c" * 64,
        "derived_storage_id": "storage", "derived_object_key": "object",
        "output_sha256": "d" * 64, "output_bytes": 1, "actual_provider": "provider",
        "actual_model": "model", "selected_provider": "provider", "selected_model": "model",
        "model_tier": "advanced", "route_fingerprint": "route",
    })
    assert store.record_replacement_item_notification(replacement_id, state="uncertain")
    payload = __import__(
        "plugins.client_knowledge_gbrain.review", fromlist=["_item_payload"]
    )._item_payload(store.get_synthesis_item(replacement_id))
    payload["embeds"][0]["footer"]["text"] = "Learning 1 · revision 1"
    monkeypatch.setattr("tools.discord_tool._get_bot_token", lambda: "token")
    monkeypatch.setattr("tools.discord_tool._discord_request", lambda *_a, **_k: {
        "id": "404", "channel_id": "401", "author": {"bot": True}, **payload,
    })
    assert fetch_and_reconcile_replacement_notification(
        store, replacement_id, "404"
    ) is False
    assert store.get_synthesis_item(replacement_id)["notification_state"] == "uncertain"


def test_uncertain_replacement_worker_adopts_existing_before_retry(tmp_path):
    store, _artifact_id = _durable_store(tmp_path)
    assert store.begin_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    )
    assert _record_instruction(store,
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300",
        decision_message_id="700", instruction="Use plainer wording.",
    )
    claim = store.claim_next_synthesis_item_revision()
    replacement_id = "3" * 64
    assert store.complete_synthesis_item_revision(claim, replacement={
        "item_id": replacement_id, "statement": "Revised first.",
        "evidence_json": _evidence("First."), "item_sha256": "c" * 64,
        "derived_storage_id": "storage", "derived_object_key": "object",
        "output_sha256": "d" * 64, "output_bytes": 1, "actual_provider": "provider",
        "actual_model": "model", "selected_provider": "provider", "selected_model": "model",
        "model_tier": "advanced", "route_fingerprint": "route",
    })
    assert store.record_replacement_item_notification(replacement_id, state="uncertain")
    sender = AsyncMock(side_effect=AssertionError("replacement must not be duplicated"))
    result = asyncio.run(send_pending_replacement_notifications(
        store=store, config=CFG, sender=sender,
        resolver=AsyncMock(return_value={
            "success": True, "message_id": "404", "side_effect_state": "confirmed",
        }),
    ))
    assert result["confirmed"] == 1
    sender.assert_not_awaited()
    assert store.get_synthesis_item(replacement_id)["notification_message_id"] == "404"


def _event(
    text,
    *,
    thread_id="401",
    user_id="600",
    message_id="700",
    created_at=None,
):
    raw = SimpleNamespace(
        id=int(message_id),
        author=SimpleNamespace(roles=[SimpleNamespace(id=300)]),
    )
    source = SessionSource(
        platform=Platform.DISCORD, chat_id=thread_id, chat_type="thread", user_id=user_id,
        scope_id="100", project_key="pid", project_channel_id="200",
        project_mapping_resolved=True, thread_id=thread_id, message_id=message_id,
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        raw_message=raw,
        message_id=message_id,
        timestamp=created_at or datetime.now(timezone.utc),
    )


def test_other_revises_only_one_item_and_replacement_still_needs_decision(tmp_path, monkeypatch):
    store, _artifact_id = _durable_store(tmp_path)
    assert store.begin_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    ) is True
    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.IntakeStore", lambda: store)
    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.load_config", lambda: CFG)
    monkeypatch.setattr("plugins.client_knowledge_gbrain.review._kick_review_revision", lambda _gateway: None)
    gateway = SimpleNamespace(_adapter_for_source=lambda _source: SimpleNamespace(send=AsyncMock()))
    result = asyncio.run(capture_review_text_hook(
        event=_event("Use plainer wording."), gateway=gateway
    ))
    assert result == {"action": "skip", "reason": "client_knowledge_review_text_captured"}
    assert store.get_synthesis_item("1" * 64)["state"] == "instructions_pending"
    assert store.get_synthesis_item("2" * 64)["state"] == "pending"
    claim = store.claim_next_synthesis_item_revision()
    replacement_id = "3" * 64
    assert store.complete_synthesis_item_revision(claim, replacement={
        "item_id": replacement_id, "statement": "Revised first.",
        "evidence_json": _evidence("First."), "item_sha256": "c" * 64,
        "derived_storage_id": "storage", "derived_object_key": "object",
        "output_sha256": "d" * 64, "output_bytes": 1, "actual_provider": "provider",
        "actual_model": "model", "selected_provider": "provider", "selected_model": "model",
        "model_tier": "advanced", "route_fingerprint": "route",
    }) is True
    assert store.get_synthesis_item("1" * 64)["state"] == "superseded"
    assert store.get_synthesis_item(replacement_id)["state"] == "pending"
    assert store.get_synthesis("s" * 64)["state"] == "review_pending"


def test_explicit_decision_cancels_abandoned_other_capture(tmp_path):
    store, _artifact_id = _durable_store(tmp_path)
    assert store.begin_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    )
    assert store.decide_synthesis_item(
        "1" * 64, decision="approved", reviewer_user_id="600",
        reviewer_role_id="300", decision_message_id="701",
    )
    assert store.get_synthesis_item_text_capture(
        guild_id="100", thread_id="401", user_id="600"
    ) is None


def test_stale_other_capture_expires_transactionally_before_text_can_apply(tmp_path):
    store, _artifact_id = _durable_store(tmp_path)
    assert store.begin_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    )
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_synthesis_items SET capture_started_at=? WHERE item_id=?",
            (time.time() - 901, "1" * 64),
        )
    assert store.get_synthesis_item_text_capture(
        guild_id="100", thread_id="401", user_id="600"
    ) is None
    expired = store.get_synthesis_item("1" * 64)
    assert expired["capture_user_id"] is None
    assert expired["capture_started_at"] is None
    assert store.record_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300",
        decision_message_id="700", instruction="Too late.",
        expected_capture_started_at=time.time() - 901,
        source_created_at=time.time() - 900,
    ) is False
    assert store.begin_synthesis_item_instruction(
        "2" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    ) is True


def test_delayed_old_capture_text_falls_through_and_new_capture_text_is_accepted(
    tmp_path, monkeypatch
):
    store, _artifact_id = _durable_store(tmp_path)
    assert store.begin_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    )
    old_capture_started_at = time.time() - 901
    with store._write() as conn:
        conn.execute(
            "UPDATE client_knowledge_synthesis_items SET capture_started_at=? WHERE item_id=?",
            (old_capture_started_at, "1" * 64),
        )
    assert store.begin_synthesis_item_instruction(
        "2" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    )
    new_capture_started_at = float(
        store.get_synthesis_item("2" * 64)["capture_started_at"]
    )
    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.IntakeStore", lambda: store)
    monkeypatch.setattr("plugins.client_knowledge_gbrain.review.load_config", lambda: CFG)
    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.review._kick_review_revision", lambda _gateway: None
    )
    adapter = SimpleNamespace(send=AsyncMock())
    gateway = SimpleNamespace(_adapter_for_source=lambda _source: adapter)

    delayed = asyncio.run(capture_review_text_hook(
        event=_event(
            "Instructions intended for the old candidate.",
            message_id="701",
            created_at=datetime.fromtimestamp(
                old_capture_started_at + 1, tz=timezone.utc
            ),
        ),
        gateway=gateway,
    ))
    assert delayed is None
    adapter.send.assert_not_awaited()
    assert store.get_synthesis_item("2" * 64)["state"] == "pending"
    assert store.get_synthesis_item("2" * 64)["capture_started_at"] == new_capture_started_at
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM client_knowledge_synthesis_item_revisions"
        ).fetchone()[0] == 0

    accepted = asyncio.run(capture_review_text_hook(
        event=_event(
            "Use plainer wording for the new candidate.",
            message_id="702",
            created_at=datetime.fromtimestamp(
                new_capture_started_at + 1, tz=timezone.utc
            ),
        ),
        gateway=gateway,
    ))
    assert accepted == {
        "action": "skip",
        "reason": "client_knowledge_review_text_captured",
    }
    adapter.send.assert_awaited_once()
    assert store.get_synthesis_item("2" * 64)["state"] == "instructions_pending"
    with store._connect() as conn:
        revision = conn.execute(
            "SELECT source_item_id, instruction_text FROM "
            "client_knowledge_synthesis_item_revisions"
        ).fetchone()
    assert tuple(revision) == (
        "2" * 64,
        "Use plainer wording for the new candidate.",
    )


def test_decision_vs_capture_text_race_has_one_transactional_winner(tmp_path):
    store, _artifact_id = _durable_store(tmp_path)
    assert store.begin_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    )
    capture_started_at = float(
        store.get_synthesis_item("1" * 64)["capture_started_at"]
    )
    barrier = Barrier(2)

    def decide():
        barrier.wait()
        return store.decide_synthesis_item(
            "1" * 64,
            decision="approved",
            reviewer_user_id="600",
            reviewer_role_id="300",
            decision_message_id="710",
        )

    def capture():
        barrier.wait()
        return store.record_synthesis_item_instruction(
            "1" * 64,
            reviewer_user_id="600",
            reviewer_role_id="300",
            decision_message_id="711",
            instruction="Use plainer wording.",
            expected_capture_started_at=capture_started_at,
            source_created_at=capture_started_at + 1,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(decide), executor.submit(capture)]
        outcomes = [future.result() for future in results]
    assert sorted(outcomes) == [False, True]
    item = store.get_synthesis_item("1" * 64)
    with store._connect() as conn:
        revision_count = conn.execute(
            "SELECT COUNT(*) FROM client_knowledge_synthesis_item_revisions "
            "WHERE source_item_id=?",
            ("1" * 64,),
        ).fetchone()[0]
    assert (item["state"], revision_count) in {
        ("approved", 0),
        ("instructions_pending", 1),
    }


def test_duplicate_capture_processing_creates_only_one_revision(tmp_path):
    store, _artifact_id = _durable_store(tmp_path)
    assert store.begin_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    )
    capture_started_at = float(
        store.get_synthesis_item("1" * 64)["capture_started_at"]
    )
    barrier = Barrier(2)

    def capture():
        barrier.wait()
        return store.record_synthesis_item_instruction(
            "1" * 64,
            reviewer_user_id="600",
            reviewer_role_id="300",
            decision_message_id="720",
            instruction="Use plainer wording.",
            expected_capture_started_at=capture_started_at,
            source_created_at=capture_started_at + 1,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [executor.submit(capture), executor.submit(capture)]
        outcomes = [future.result() for future in results]
    assert sorted(outcomes) == [False, True]
    with store._connect() as conn:
        revisions = conn.execute(
            "SELECT revision_id, source_item_id, instruction_text FROM "
            "client_knowledge_synthesis_item_revisions"
        ).fetchall()
    assert len(revisions) == 1
    assert tuple(revisions[0][1:]) == ("1" * 64, "Use plainer wording.")


def test_bounded_revision_failure_can_be_restored_for_operator_review(tmp_path):
    store, _artifact_id = _durable_store(tmp_path)
    assert store.begin_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    )
    assert _record_instruction(store,
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300",
        decision_message_id="700", instruction="Use plainer wording.",
    )
    claim = store.claim_next_synthesis_item_revision()
    assert store.block_synthesis_item_revision(claim, error_class="provider_failed")
    assert store.restore_synthesis_item_revision("1" * 64)
    assert store.get_synthesis_item("1" * 64)["state"] == "pending"
    assert store.begin_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    )
    assert _record_instruction(store,
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300",
        decision_message_id="701", instruction="Use simpler wording instead.",
    )
    replacement_claim = store.claim_next_synthesis_item_revision()
    assert replacement_claim is not None
    assert replacement_claim.instruction_text == "Use simpler wording instead."
    assert replacement_claim.attempt_count == 1


def test_revision_claim_exhaustion_becomes_operator_blocked_without_reclaim(tmp_path):
    store, _artifact_id = _durable_store(tmp_path)
    assert store.begin_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    )
    assert _record_instruction(store,
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300",
        decision_message_id="700", instruction="Use plainer wording.",
    )
    claim = store.claim_next_synthesis_item_revision(max_attempts=1)
    assert store.fail_synthesis_item_revision(
        claim, error_class="worker_crashed", retry_delay=0
    )
    assert store.claim_next_synthesis_item_revision(max_attempts=1) is None
    with store._connect() as conn:
        state = conn.execute(
            "SELECT state FROM client_knowledge_synthesis_item_revisions "
            "WHERE source_item_id=?",
            ("1" * 64,),
        ).fetchone()[0]
    assert state == "operator_blocked"


def test_revision_worker_rejects_unchanged_statement(tmp_path):
    from plugins.client_knowledge_gbrain.review import _process_item_revision
    from plugins.client_knowledge_gbrain.derived import DerivedStore

    store, _artifact_id = _durable_store(tmp_path)
    assert store.begin_synthesis_item_instruction(
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300", thread_id="401"
    )
    assert _record_instruction(store,
        "1" * 64, reviewer_user_id="600", reviewer_role_id="300",
        decision_message_id="700", instruction="Keep the wording.",
    )
    claim = store.claim_next_synthesis_item_revision()
    derived = DerivedStore(tmp_path / "derived")
    extraction = {
        "segments": [{
            "segment_id": "body-0001", "text": "First.",
        }],
    }
    record = derived.put_json("extractions", "e" * 64, extraction)
    with store._write() as conn:
        conn.execute(
            "UPDATE extractions SET derived_storage_id=?, derived_object_key=?, "
            "output_sha256=?, output_bytes=? WHERE extraction_id=?",
            (record.storage_id, record.object_key, record.sha256, record.byte_size, "e" * 64),
        )
    llm = SimpleNamespace(complete_structured=lambda **_kwargs: SimpleNamespace(
        parsed={
            "statement": "First.",
            "evidence": [{
                "segment_id": "body-0001", "start": 0, "end": 6, "quote": "First.",
            }],
        },
        provider="provider", model="model",
        audit={"selected_provider": "provider", "selected_model": "model",
               "model_tier": "advanced", "route_fingerprint": "route"},
        usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2,
                              cache_read_tokens=0, cache_write_tokens=0),
    ))
    with pytest.raises(SynthesisFailure, match="unchanged"):
        _process_item_revision(
            claim, store=store, derived=derived, llm=llm,
            settings=SynthesisSettings(True, 1, 300, 60, 180, 4096, 600_000, 100_000),
        )
