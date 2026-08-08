from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time

import pytest

from plugins.client_knowledge_gbrain.cli import client_knowledge_command, register_cli
from plugins.client_knowledge_gbrain.derived import canonical_json
from plugins.client_knowledge_gbrain.models import IntakeArtifact
from plugins.client_knowledge_gbrain.store import IntakeStore


def _artifact() -> IntakeArtifact:
    return IntakeArtifact.from_bytes(
        project_key="pid", provider_id="gmail", provider_artifact_id="message-1",
        content=b"private body", received_at=10,
    )


def test_cli_parser_registers_operator_actions_including_narrow_legacy_migration():
    parser = argparse.ArgumentParser()
    register_cli(parser)
    actions = (
        "status", "list", "show", "retry", "quarantine", "reconcile", "reviews",
        "notify-reviews-once", "adopt-review-message", "requeue-review-notification",
        "migrate-legacy-review", "restore-item-revision", "regenerate-undecided-synthesis",
        "run-once", "gmail-poll-once", "notion-preflight",
    )
    for action in actions:
        if action in {"show", "retry", "quarantine"}:
            suffix = ["0" * 32]
        elif action == "notion-preflight":
            suffix = ["--project", "pid"]
        elif action == "adopt-review-message":
            suffix = ["--review-id", "a" * 64, "--message-id", "123"]
        elif action in {"requeue-review-notification", "migrate-legacy-review"}:
            suffix = ["--review-id", "a" * 64]
        elif action == "restore-item-revision":
            suffix = ["--item-id", "a" * 64]
        elif action == "regenerate-undecided-synthesis":
            suffix = ["--synthesis-id", "a" * 64]
        else:
            suffix = []
        args = parser.parse_args([action, *suffix])
        assert args.client_knowledge_action == action
        assert args.func is client_knowledge_command


def test_cli_output_is_redacted_and_run_once_uses_synthesis(tmp_path, capsys):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    artifact = _artifact()
    store.insert_artifact(artifact)
    store.add_job(artifact.artifact_id, "quarantined")
    args = argparse.Namespace(
        client_knowledge_action="list", status="", limit=20, db_path=str(store.path)
    )
    assert client_knowledge_command(args) == 0
    output = capsys.readouterr().out
    assert artifact.content_sha256 not in output
    assert "spool_key" not in output
    assert "private body" not in output
    args.client_knowledge_action = "run-once"
    assert client_knowledge_command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "synthesis" in payload
    assert "interpretation" not in payload
    assert "assimilation" not in payload
    assert "honcho_projection" not in payload


def test_run_once_dry_run_reports_synthesis_without_loading_stages(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"client_knowledge": {
            "notion": {"enabled": True}, "extraction": {"enabled": False},
            "synthesis": {"enabled": True}, "review_notifications": {"enabled": True},
        }},
    )
    blocked = {
        "plugins.client_knowledge_gbrain.notion_archive",
        "plugins.client_knowledge_gbrain.extraction",
        "plugins.client_knowledge_gbrain.synthesis",
        "plugins.client_knowledge_gbrain.review",
    }
    for name in blocked:
        sys.modules.pop(name, None)
    args = argparse.Namespace(
        client_knowledge_action="run-once", dry_run=True,
        db_path=str(tmp_path / "private" / "intake.db"),
    )
    assert client_knowledge_command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stage_enablement"] == {
        "extraction": False, "notion": True, "review_notifications": True,
        "synthesis": True,
    }
    assert blocked.isdisjoint(sys.modules)


def test_status_reports_cutover_and_dual_persistent_views(tmp_path, capsys, monkeypatch):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    monkeypatch.setattr(
        "hermes_cli.config.load_config",
        lambda: {"client_knowledge": {"synthesis": {"enabled": True}}},
    )
    args = argparse.Namespace(
        client_knowledge_action="status", db_path=str(store.path)
    )
    assert client_knowledge_command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pipeline"] == {
        "live_stage": "synthesized",
        "synthesis_enabled": True,
        "legacy_live_stages": [],
        "persistent_component_views": [
            "client-knowledge-review", "client-knowledge-review-item",
        ],
    }
    assert payload["cutover"]["legacy_reviews"] == {
        "migrated": 0, "pending_unmigrated": 0,
    }


def _legacy_fixture(store, derived, *, selected_count=3):
    artifact = _artifact()
    store.insert_artifact(artifact)
    now = time.time()
    extraction_id = "e" * 64
    interpretation_id = "1" * 64
    assimilation_id = "b" * 64
    review_id = "a" * 64
    quotes = [
        "Send a concise status report every Monday.",
        "Use the existing approval flow.",
        "Keep client-facing copy brief.",
        "Call tomorrow about launch timing.",
    ]
    body = " ".join(quotes)
    segments = [{
        "segment_id": "body-0001", "kind": "body_plain", "label": "Email body", "text": body,
    }]
    evidence = []
    findings = []
    offset = 0
    for index, quote in enumerate(quotes, start=1):
        start = body.index(quote, offset)
        end = start + len(quote)
        offset = end
        evidence.append({
            "id": f"evidence-{index:03d}", "segment_id": "body-0001",
            "start": start, "end": end, "quote": quote,
        })
        findings.append({
            "id": f"finding-{index}", "text": quote, "confidence": "high",
            "sensitivity": "internal", "evidence_ids": [f"evidence-{index:03d}"],
        })
    extraction_value = {
        "object_version": "client-knowledge-extractor/v1", "extraction_id": extraction_id,
        "artifact_id": artifact.artifact_id, "project_key": "pid",
        "source_sha256": artifact.content_sha256, "source_manifest_sha256": "m" * 64,
        "limits_version": "client-knowledge-extraction-limits/v1",
        "redaction_version": "client-knowledge-redaction/v1", "status": "extracted",
        "redaction_counts": {}, "segments": segments, "unsupported_attachments": [],
    }
    extraction_record = derived.put_json("extractions", extraction_id, extraction_value)
    interpretation = {
        "summary": "Persisted summary.", "candidate_learnings": findings,
        "decisions": [], "requirements": [], "preferences": [], "risks": [],
        "stakeholders": [], "deadlines": [], "open_questions": [], "suggested_actions": [],
        "evidence": evidence,
    }
    interpretation_value = {
        "object_version": "legacy", "interpretation_id": interpretation_id,
        "interpretation": interpretation,
    }
    interpretation_record = derived.put_json("interpretations", interpretation_id, interpretation_value)
    operations = []
    for index in range(1, 5):
        operations.append({
            "operation": "add" if index <= selected_count else "ignore_transient",
            "finding_id": f"finding-{index}", "evidence_ids": [f"evidence-{index:03d}"],
            "claim": quotes[index - 1],
        })
    proposal = {"operations": operations}
    proposal_sha = hashlib.sha256(canonical_json(proposal)).hexdigest()
    assimilation_value = {"proposal": proposal}
    assimilation_record = derived.put_json("assimilations", assimilation_id, assimilation_value)
    with store._write() as conn:
        conn.execute(
            "INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (extraction_id, artifact.artifact_id, artifact.content_sha256, "m" * 64,
             "client-knowledge-extractor/v1", "client-knowledge-extraction-limits/v1",
             "client-knowledge-redaction/v1", "extracted", extraction_record.storage_id,
             extraction_record.object_key, extraction_record.sha256, extraction_record.byte_size,
             len(body), "{}", now),
        )
        conn.execute(
            "INSERT INTO interpretation_envelopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2" * 64, artifact.artifact_id, "pid", artifact.content_sha256, extraction_id,
             extraction_record.sha256, "ev", "sv", "pv", "task", "storage", "object",
             "z" * 64, 1, now),
        )
        conn.execute(
            "INSERT INTO interpretations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (interpretation_id, "2" * 64, artifact.artifact_id, extraction_id, "sv", "pv",
             interpretation_record.storage_id, interpretation_record.object_key,
             interpretation_record.sha256, interpretation_record.byte_size,
             "provider", "model", "provider", "model", "advanced", "route",
             10, 5, 15, 0, 0, now),
        )
        conn.execute(
            "INSERT INTO assimilation_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (assimilation_id, artifact.artifact_id, interpretation_id, "av", "sv", "pv", "policy",
             "pid", proposal_sha, assimilation_record.storage_id, assimilation_record.object_key,
             assimilation_record.sha256, assimilation_record.byte_size,
             "provider", "model", "provider", "model", "advanced", "route", 1,
             "review", "head", None, 0, now),
        )
        conn.execute(
            "INSERT INTO client_knowledge_reviews("
            "review_id, assimilation_id, artifact_id, project_key, proposal_sha256, assimilation_version, "
            "state, reason_code, notification_state, notification_content_sha256, notification_message_id, "
            "notification_guild_id, notification_channel_id, notification_role_id, notification_marker, "
            "detail_state, detail_content_sha256, detail_thread_id, created_at, updated_at) "
            "VALUES(?,?,?,?,?,?,'pending',?,'confirmed',?,?,?,?,?,?, 'confirmed',?,?,?,?)",
            (review_id, assimilation_id, artifact.artifact_id, "pid", proposal_sha, "av", "review",
             "c" * 64, "400", "100", "200", "300", "[legacy]", "d" * 64, "401", now, now),
        )
        for stage, receipt in (
            ("notion_archived", "notion:page:source"),
            ("extracted", f"extraction:{extraction_id}"),
            ("interpreted", f"interpretation:{interpretation_id}"),
        ):
            conn.execute(
                "INSERT INTO jobs(job_id, artifact_id, stage, status, max_attempts, created_at, updated_at) "
                "VALUES(?,?,?,'succeeded',3,?,?)",
                (hashlib.sha256(stage.encode()).hexdigest()[:32], artifact.artifact_id, stage, now, now),
            )
            conn.execute(
                "INSERT INTO stage_receipts(artifact_id, stage, receipt_id, recorded_at) VALUES(?,?,?,?)",
                (artifact.artifact_id, stage, receipt, now),
            )
    return review_id


@pytest.mark.parametrize("selected_count", [1, 2, 3])
def test_legacy_migration_accepts_one_to_three_operations(
    tmp_path, monkeypatch, capsys, selected_count
):
    from plugins.client_knowledge_gbrain.derived import DerivedStore

    store = IntakeStore(tmp_path / f"private-{selected_count}" / "intake.db")
    derived = DerivedStore(tmp_path / f"private-{selected_count}" / "derived")
    review_id = _legacy_fixture(store, derived, selected_count=selected_count)
    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.derived.resolve_derived_path",
        lambda _config=None: derived.root,
    )
    args = argparse.Namespace(
        client_knowledge_action="migrate-legacy-review",
        review_id=review_id,
        db_path=str(store.path),
    )
    assert client_knowledge_command(args) == 0
    synthesis_id = json.loads(capsys.readouterr().out)["synthesis_id"]
    assert len(store.list_synthesis_items(synthesis_id, active_only=True)) == selected_count
    synthesis_job = next(
        job for job in store.list_jobs(limit=50) if job["stage"] == "synthesized"
    )
    assert synthesis_job["status"] == "operator_blocked"
    assert synthesis_job["last_error_class"] == "synthesis_items_pending"


def test_legacy_migration_uses_persisted_derived_objects_only(tmp_path, monkeypatch, capsys):
    from plugins.client_knowledge_gbrain.derived import DerivedStore

    store = IntakeStore(tmp_path / "private" / "intake.db")
    derived = DerivedStore(tmp_path / "private" / "derived")
    review_id = _legacy_fixture(store, derived)
    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.derived.resolve_derived_path",
        lambda _config=None: derived.root,
    )
    for name in (
        "plugins.client_knowledge_gbrain.gmail_api.GmailClient",
        "plugins.client_knowledge_gbrain.notion.NotionClient",
        "plugins.client_knowledge_gbrain.spool.RawSpool",
        "agent.plugin_llm.PluginLlm",
    ):
        module, attr = name.rsplit(".", 1)
        monkeypatch.setattr(f"{module}.{attr}", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError(name)))
    args = argparse.Namespace(
        client_knowledge_action="migrate-legacy-review", review_id=review_id, db_path=str(store.path)
    )
    assert client_knowledge_command(args) == 0
    payload = json.loads(capsys.readouterr().out)
    synthesis = store.get_synthesis(payload["synthesis_id"])
    assert synthesis["source_legacy_review_id"] == review_id
    assert len(store.list_synthesis_items(payload["synthesis_id"], active_only=True)) == 3
    assert store.get_review(review_id)["state"] == "superseded"
    assert store.get_publication("b" * 64) is None
    assert client_knowledge_command(args) == 1


def test_legacy_migration_mismatch_aborts_before_derived_mutation(tmp_path, monkeypatch, capsys):
    from plugins.client_knowledge_gbrain.derived import DerivedStore

    store = IntakeStore(tmp_path / "private" / "intake.db")
    derived = DerivedStore(tmp_path / "private" / "derived")
    review_id = _legacy_fixture(store, derived)
    with store._write() as conn:
        conn.execute(
            "INSERT INTO publication_transactions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("b" * 64, _artifact().artifact_id, "av", "p" * 64, "refs/heads/main",
             "head", None, "prepared", "[]", None, time.time()),
        )
    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.derived.resolve_derived_path",
        lambda _config=None: derived.root,
    )
    before = {path.relative_to(derived.root) for path in derived.root.rglob("*")}
    args = argparse.Namespace(
        client_knowledge_action="migrate-legacy-review", review_id=review_id,
        db_path=str(store.path),
    )
    assert client_knowledge_command(args) == 1
    assert json.loads(capsys.readouterr().out) == {"error_class": "ValueError"}
    after = {path.relative_to(derived.root) for path in derived.root.rglob("*")}
    assert after == before
    assert store.get_review(review_id)["state"] == "pending"


def test_legacy_migration_preflight_failure_does_not_create_derived_store(
    tmp_path, monkeypatch, capsys
):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    derived_root = tmp_path / "private" / "not-created"
    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.derived.resolve_derived_path",
        lambda _config=None: derived_root,
    )
    args = argparse.Namespace(
        client_knowledge_action="migrate-legacy-review", review_id="a" * 64,
        db_path=str(store.path),
    )
    assert client_knowledge_command(args) == 1
    assert json.loads(capsys.readouterr().out) == {"error_class": "ValueError"}
    assert not derived_root.exists()


def test_legacy_migration_rejects_active_revision_without_mutation(tmp_path, capsys):
    from plugins.client_knowledge_gbrain.derived import DerivedStore

    store = IntakeStore(tmp_path / "private" / "intake.db")
    derived = DerivedStore(tmp_path / "private" / "derived")
    review_id = _legacy_fixture(store, derived)
    with store._write() as conn:
        conn.execute(
            "INSERT INTO client_knowledge_review_revisions("
            "revision_id, source_review_id, root_assimilation_id, instruction_text, "
            "state, created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
            ("f" * 64, review_id, "b" * 64, "pending revision", "queued", time.time(), time.time()),
        )
    args = argparse.Namespace(
        client_knowledge_action="migrate-legacy-review", review_id=review_id,
        db_path=str(store.path),
    )
    assert client_knowledge_command(args) == 1
    assert json.loads(capsys.readouterr().out) == {"error_class": "ValueError"}
    assert store.get_review(review_id)["state"] == "pending"


def test_cli_rejects_noncanonical_ids_without_echoing_them(tmp_path, capsys):
    args = argparse.Namespace(
        client_knowledge_action="show", job_id="secret/path/customer.pdf",
        db_path=str(tmp_path / "private" / "intake.db"),
    )
    assert client_knowledge_command(args) == 1
    output = capsys.readouterr().out
    assert json.loads(output) == {"error_class": "ValueError"}
    assert "secret" not in output
