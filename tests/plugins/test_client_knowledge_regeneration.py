from __future__ import annotations

import hashlib
import json
import time

import pytest

from agent.plugin_llm import PluginLlmStructuredResult, PluginLlmUsage
from plugins.client_knowledge_gbrain.derived import DerivedStore, canonical_json
from plugins.client_knowledge_gbrain.extraction import (
    EXTRACTOR_VERSION,
    EXTRACTION_LIMITS_VERSION,
    REDACTION_VERSION,
)
from plugins.client_knowledge_gbrain.models import IntakeArtifact
from plugins.client_knowledge_gbrain.regeneration import (
    SynthesisRegenerationFailure,
    regenerate_undecided_synthesis,
)
from plugins.client_knowledge_gbrain.review import retire_superseded_synthesis_review
from plugins.client_knowledge_gbrain.store import IntakeStore
from plugins.client_knowledge_gbrain.synthesis import SynthesisFailure


CFG = {
    "client_knowledge": {"synthesis": {"enabled": True}},
    "projects": {"pid": {
        "display_name": "PID",
        "client_knowledge_review": {
            "guild_id": "100",
            "channel_id": "200",
            "reviewer_role_id": "300",
            "reviewer_user_ids": ["600"],
        },
    }},
}


def _evidence(segment: str, quote: str, start: int) -> dict[str, object]:
    return {
        "segment_id": segment,
        "start": start,
        "end": start + len(quote),
        "quote": quote,
    }


def _fixture(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    derived = DerivedStore(tmp_path / "private" / "derived")
    artifact = IntakeArtifact.from_bytes(
        project_key="pid",
        provider_id="gmail",
        provider_artifact_id="message-1",
        content=b"persisted source bytes",
    )
    store.insert_artifact(artifact)
    quotes = [
        "Send the project status report every Monday.",
        "Use the existing approval workflow for client-facing changes.",
        "Keep executive dashboard copy concise and direct.",
        "Record launch blockers in the delivery checklist.",
    ]
    body = " ".join(quotes)
    segments = [
        {"kind": "header", "label": "From", "text": "Alex <alex@example.test>"},
        {"kind": "header", "label": "Subject", "text": "PID weekly reporting"},
        {"kind": "header", "label": "Date", "text": "Fri, 7 Aug 2026 09:00:00 +0000"},
        {
            "segment_id": "body-0001",
            "kind": "body_plain",
            "label": "Email body",
            "text": body,
        },
    ]
    extraction_id = "e" * 64
    extraction = {
        "object_version": EXTRACTOR_VERSION,
        "limits_version": EXTRACTION_LIMITS_VERSION,
        "redaction_version": REDACTION_VERSION,
        "extraction_id": extraction_id,
        "artifact_id": artifact.artifact_id,
        "project_key": "pid",
        "source_sha256": artifact.content_sha256,
        "source_manifest_sha256": "m" * 64,
        "status": "extracted",
        "redaction_counts": {},
        "segments": segments,
        "unsupported_attachments": [],
    }
    extraction_record = derived.put_json("extractions", extraction_id, extraction)
    synthesis_id = "a" * 64
    old_learnings = []
    offset = 0
    for statement, quote in zip(
        (
            "Provide a regular project status report.",
            "Use the established approval workflow.",
            "Keep executive copy concise.",
        ),
        quotes,
    ):
        start = body.index(quote, offset)
        offset = start + len(quote)
        old_learnings.append({
            "statement": statement,
            "evidence": [_evidence("body-0001", quote, start)],
        })
    old_value = {
        "object_version": "client-knowledge-synthesis/v1",
        "synthesis_id": synthesis_id,
        "extraction_id": extraction_id,
        "synthesis": {"learnings": old_learnings},
    }
    old_record = derived.put_json("syntheses", synthesis_id, old_value)
    now = time.time()
    synthesis = {
        "synthesis_id": synthesis_id,
        "artifact_id": artifact.artifact_id,
        "extraction_id": extraction_id,
        "project_key": "pid",
        "notion_ref": "notion:page:source",
        "synthesis_version": "client-knowledge-synthesis/v1",
        "schema_version": "client-knowledge-synthesis-schema/v1",
        "prompt_version": "client-knowledge-synthesis-prompt/v1",
        "derived_storage_id": old_record.storage_id,
        "derived_object_key": old_record.object_key,
        "output_sha256": old_record.sha256,
        "output_bytes": old_record.byte_size,
        "actual_provider": "provider",
        "actual_model": "model",
        "selected_provider": "provider",
        "selected_model": "model",
        "model_tier": "advanced",
        "route_fingerprint": "route",
        "base_git_head": "publication-time",
    }
    items = []
    for position, learning in enumerate(old_learnings, start=1):
        digest = hashlib.sha256(canonical_json(learning)).hexdigest()
        items.append({
            "item_id": str(position) * 64,
            "position": position,
            "statement": learning["statement"],
            "evidence_json": canonical_json(learning["evidence"]).decode(),
            "item_sha256": digest,
        })
    with store._write() as conn:
        conn.execute(
            "INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                extraction_id,
                artifact.artifact_id,
                artifact.content_sha256,
                "m" * 64,
                EXTRACTOR_VERSION,
                EXTRACTION_LIMITS_VERSION,
                REDACTION_VERSION,
                "extracted",
                extraction_record.storage_id,
                extraction_record.object_key,
                extraction_record.sha256,
                extraction_record.byte_size,
                len(body),
                "{}",
                now,
            ),
        )
        store._insert_synthesis_locked(conn, synthesis, items, now=now)
        conn.execute(
            "UPDATE client_knowledge_synthesis_notifications SET state='confirmed', "
            "content_sha256=?, message_id='400', guild_id='100', channel_id='200', "
            "role_id='300', marker='marker', thread_id='401', items_sha256=? "
            "WHERE synthesis_id=?",
            ("c" * 64, "d" * 64, synthesis_id),
        )
        for position, item in enumerate(items, start=2):
            conn.execute(
                "UPDATE client_knowledge_synthesis_items SET notification_state='confirmed', "
                "notification_message_id=? WHERE item_id=?",
                (str(400 + position), item["item_id"]),
            )
        for index, (stage, receipt) in enumerate((
            ("notion_archived", "notion:page:source"),
            ("extracted", f"extraction:{extraction_id}"),
        ), start=1):
            conn.execute(
                "INSERT INTO jobs(job_id, artifact_id, stage, status, max_attempts, "
                "created_at, updated_at) VALUES(?,?,?,'succeeded',3,?,?)",
                (str(index) * 32, artifact.artifact_id, stage, now, now),
            )
            conn.execute(
                "INSERT INTO stage_receipts(artifact_id, stage, receipt_id, recorded_at) "
                "VALUES(?,?,?,?)",
                (artifact.artifact_id, stage, receipt, now),
            )
        conn.execute(
            "INSERT INTO jobs(job_id, artifact_id, stage, status, max_attempts, "
            "last_error_class, created_at, updated_at) "
            "VALUES(?,?,?,'operator_blocked',3,'synthesis_items_pending',?,?)",
            ("9" * 32, artifact.artifact_id, "synthesized", now, now),
        )
    starts = [body.index(quote) for quote in quotes]
    replacement = {"learnings": [
        {
            "statement": "Deliver the project status report every Monday.",
            "evidence": [_evidence("body-0001", quotes[0], starts[0])],
        },
        {
            "statement": "Route client-facing changes through the existing approval workflow.",
            "evidence": [_evidence("body-0001", quotes[1], starts[1])],
        },
        {
            "statement": "Use concise, direct copy in the executive dashboard.",
            "evidence": [_evidence("body-0001", quotes[2], starts[2])],
        },
        {
            "statement": "Track launch blockers in the delivery checklist.",
            "evidence": [_evidence("body-0001", quotes[3], starts[3])],
        },
    ]}
    return store, derived, synthesis_id, replacement


class _Llm:
    def __init__(self, parsed):
        self.parsed = parsed
        self.calls = 0

    def complete_structured(self, **_kwargs):
        self.calls += 1
        return PluginLlmStructuredResult(
            text="{}",
            parsed=self.parsed,
            content_type="json",
            provider="provider",
            model="model-v2",
            agent_id="agent",
            audit={
                "selected_provider": "provider",
                "selected_model": "model-v2",
                "model_tier": "advanced",
                "route_fingerprint": "route-v2",
            },
            usage=PluginLlmUsage(input_tokens=20, output_tokens=10, total_tokens=30),
        )


def test_regeneration_uses_persisted_provenance_once_and_supersedes_atomically(
    tmp_path, monkeypatch
):
    store, derived, synthesis_id, replacement = _fixture(tmp_path)
    for target in (
        "plugins.client_knowledge_gbrain.gmail_api.GmailClient",
        "plugins.client_knowledge_gbrain.notion.NotionClient",
        "plugins.client_knowledge_gbrain.spool.RawSpool",
    ):
        monkeypatch.setattr(
            target,
            lambda *_args, _target=target, **_kwargs: (_ for _ in ()).throw(
                AssertionError(_target)
            ),
        )
    llm = _Llm(replacement)
    replacement_id = regenerate_undecided_synthesis(
        synthesis_id, store=store, derived=derived, llm=llm, config=CFG
    )
    assert llm.calls == 1
    source = store.get_synthesis(synthesis_id)
    current = store.get_synthesis(replacement_id)
    assert source["state"] == "superseded"
    assert source["superseded_by_synthesis_id"] == replacement_id
    assert current["state"] == "review_pending"
    assert current["parent_synthesis_id"] == synthesis_id
    assert len(store.list_synthesis_items(replacement_id, active_only=True)) == 4
    assert {item["state"] for item in store.list_synthesis_items(synthesis_id)} == {"superseded"}
    assert store.get_synthesis_notification(synthesis_id)["retirement_state"] == "pending"
    assert store.get_synthesis_notification(replacement_id)["state"] == "pending"
    assert store.get_synthesis_publication(synthesis_id) is None
    assert store.get_synthesis_publication(replacement_id) is None
    retry_llm = _Llm(replacement)
    assert regenerate_undecided_synthesis(
        synthesis_id,
        store=store,
        derived=derived,
        llm=retry_llm,
        config=CFG,
    ) == replacement_id
    assert retry_llm.calls == 0


def test_regeneration_validation_failure_has_no_store_or_derived_mutation(tmp_path):
    store, derived, synthesis_id, replacement = _fixture(tmp_path)
    duplicate = json.loads(json.dumps(replacement))
    duplicate["learnings"][1]["statement"] = (
        "Deliver the Monday project status report using the regular project reporting process."
    )
    duplicate["learnings"][1]["evidence"] = duplicate["learnings"][0]["evidence"]
    before_paths = {path.relative_to(derived.root) for path in derived.root.rglob("*")}
    with pytest.raises(SynthesisFailure, match="synthesis_statements_overlap"):
        regenerate_undecided_synthesis(
            synthesis_id,
            store=store,
            derived=derived,
            llm=_Llm(duplicate),
            config=CFG,
        )
    assert store.get_synthesis(synthesis_id)["state"] == "review_pending"
    assert store.get_synthesis(synthesis_id)["superseded_by_synthesis_id"] is None
    assert {item["state"] for item in store.list_synthesis_items(synthesis_id)} == {"pending"}
    assert {path.relative_to(derived.root) for path in derived.root.rglob("*")} == before_paths


@pytest.mark.parametrize("mutation", ["decision", "capture", "revision", "publication"])
def test_regeneration_preflight_rejects_non_pristine_review_state(tmp_path, mutation):
    store, _derived, synthesis_id, _replacement = _fixture(tmp_path)
    now = time.time()
    with store._write() as conn:
        if mutation == "decision":
            conn.execute(
                "UPDATE client_knowledge_synthesis_items SET state='approved', "
                "reviewer_user_id='600', decision_message_id='500', decided_at=? "
                "WHERE item_id=?",
                (now, "1" * 64),
            )
        elif mutation == "capture":
            conn.execute(
                "UPDATE client_knowledge_synthesis_items SET capture_user_id='600', "
                "capture_thread_id='401', capture_started_at=? WHERE item_id=?",
                (now, "1" * 64),
            )
        elif mutation == "revision":
            conn.execute(
                "INSERT INTO client_knowledge_synthesis_item_revisions("
                "revision_id, source_item_id, synthesis_id, instruction_text, state, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                ("r" * 64, "1" * 64, synthesis_id, "change", "queued", now, now),
            )
        else:
            conn.execute(
                "INSERT INTO client_knowledge_synthesis_publications VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    synthesis_id,
                    store.get_synthesis(synthesis_id)["artifact_id"],
                    "v1",
                    "c" * 64,
                    "refs/heads/main",
                    "head",
                    None,
                    "prepared",
                    "[]",
                    None,
                    now,
                ),
            )
    with pytest.raises(ValueError):
        store.preflight_synthesis_regeneration(synthesis_id)


def test_superseded_discord_controls_retire_idempotently_before_new_delivery(tmp_path):
    store, derived, synthesis_id, replacement = _fixture(tmp_path)
    regenerate_undecided_synthesis(
        synthesis_id, store=store, derived=derived, llm=_Llm(replacement), config=CFG
    )
    calls = []

    def request(method, path, token, body=None, **_kwargs):
        calls.append((method, path, token, body))
        return {"id": path.rsplit("/", 1)[-1]}

    assert retire_superseded_synthesis_review(
        store, synthesis_id, request=request, token="token"
    ) == "confirmed"
    assert [call[1] for call in calls] == [
        "/channels/401/messages/402",
        "/channels/401/messages/403",
        "/channels/401/messages/404",
        "/channels/200/messages/400",
        "/channels/401",
    ]
    assert all(call[3] == {"components": []} for call in calls[:3])
    assert calls[-1][3] == {"archived": True, "locked": True}
    assert store.get_synthesis_notification(synthesis_id)["retirement_state"] == "confirmed"
    calls.clear()
    assert retire_superseded_synthesis_review(
        store, synthesis_id, request=request, token="token"
    ) == "confirmed"
    assert calls == []


def test_uncertain_discord_retirement_is_recoverable(tmp_path):
    store, derived, synthesis_id, replacement = _fixture(tmp_path)
    regenerate_undecided_synthesis(
        synthesis_id, store=store, derived=derived, llm=_Llm(replacement), config=CFG
    )
    failed = False

    def request(method, path, token, body=None, **_kwargs):
        nonlocal failed
        if not failed and path.endswith("/messages/403"):
            failed = True
            raise TimeoutError
        return {"id": path.rsplit("/", 1)[-1]}

    assert retire_superseded_synthesis_review(
        store, synthesis_id, request=request, token="token"
    ) == "uncertain"
    assert store.get_synthesis_notification(synthesis_id)["retirement_state"] == "uncertain"
    assert retire_superseded_synthesis_review(
        store, synthesis_id, request=request, token="token"
    ) == "confirmed"
