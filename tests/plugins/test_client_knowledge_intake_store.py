from __future__ import annotations

import io
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from plugins.client_knowledge_gbrain.models import (
    UNMAPPED_PROJECT_KEY,
    ExternalReceipt,
    IntakeArtifact,
    StageReceipt,
    artifact_identity,
    storage_key,
)
from plugins.client_knowledge_gbrain.spool import RawSpool
from plugins.client_knowledge_gbrain.store import IntakeStore


def _artifact(provider: str = "gmail", external: str = "item-1", content: bytes = b"raw"):
    return IntakeArtifact.from_bytes(
        project_key="pid",
        provider_id=provider,
        provider_artifact_id=external,
        content=content,
        received_at=10,
    )


def test_provider_identity_is_unique_even_when_bytes_match(tmp_path):
    store = IntakeStore(tmp_path / "intake.db")
    first = _artifact("gmail")
    second = _artifact("discord")

    store.insert_artifact(first)
    store.insert_artifact(second)

    assert first.content_sha256 == second.content_sha256
    assert first.artifact_id != second.artifact_id
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 2


def test_same_provider_identity_cannot_be_remapped_to_another_project(tmp_path):
    store = IntakeStore(tmp_path / "intake.db")
    first = _artifact()
    second = IntakeArtifact.from_bytes(
        project_key="other", provider_id=first.provider_id,
        provider_artifact_id=first.provider_artifact_id, content=b"raw", received_at=10,
    )
    store.insert_artifact(first)
    with pytest.raises(ValueError, match="different artifact metadata"):
        store.insert_artifact(second)
    assert first.artifact_id == second.artifact_id


def test_same_provider_identity_is_idempotent_but_byte_change_fails(tmp_path):
    store = IntakeStore(tmp_path / "intake.db")
    first = _artifact()
    assert store.insert_artifact(first) == first.artifact_id
    assert store.insert_artifact(first) == first.artifact_id
    changed = IntakeArtifact(
        project_key=first.project_key,
        provider_id=first.provider_id,
        provider_artifact_id=first.provider_artifact_id,
        content_sha256="0" * 64,
        byte_size=first.byte_size,
        spool_key=first.spool_key,
        received_at=first.received_at,
    )
    with pytest.raises(ValueError, match="different artifact metadata"):
        store.insert_artifact(changed)


def test_artifact_contract_rejects_non_integer_sizes_and_invalid_stage_receipts():
    with pytest.raises(Exception):
        IntakeArtifact(
            project_key="pid",
            provider_id="gmail",
            provider_artifact_id="item-1",
            content_sha256="0" * 64,
            byte_size=1.5,
        )
    with pytest.raises(Exception):
        StageReceipt("0" * 64, "Not A Stage", "receipt")
    with pytest.raises(Exception, match="provider_id"):
        _artifact("provider-a")


def test_artifact_contract_normalizes_bounded_provenance_metadata():
    artifact = IntakeArtifact.from_bytes(
        project_key="pid",
        provider_id="gmail",
        provider_artifact_id="message-1",
        provider_message_id="message-1",
        actor_display="Ada",
        actor_id="ada@example.com",
        delivered_alias="pid@sligolabs.com",
        original_filename="message.eml",
        mime_type="message/rfc822",
        source_url="https://mail.google.com/mail/u/0/#inbox/message-1",
        text_context="bounded preview",
        provenance_json={"history_id": "123", "mailbox": "sligolabs@gmail.com"},
        content=b"raw",
        received_at=10,
    )

    assert artifact.occurred_at == 10
    assert artifact.provenance_json == (
        '{"history_id":"123","mailbox":"sligolabs@gmail.com"}'
    )
    assert artifact.to_dict()["delivered_alias"] == "pid@sligolabs.com"


def test_spool_streams_hashes_and_never_uses_original_names(tmp_path):
    spool = RawSpool(tmp_path / "spool")
    record = spool.put(
        provider_id="gmail",
        provider_artifact_id="../../original-name.pdf",
        source=io.BytesIO(b"a" * 100_001),
    )

    assert record.path.parent.parent == spool.root
    assert record.path.parent.name == record.storage_key
    assert record.path.name == "raw"
    assert record.path.name not in {"original-name.pdf", "provider"}
    assert record.path.stat().st_mode & 0o777 == 0o600
    assert record.path.parent.stat().st_mode & 0o777 == 0o700
    assert spool.root.stat().st_mode & 0o777 == 0o700
    assert record.byte_size == 100_001
    assert record.sha256 == _artifact(content=b"a" * 100_001).content_sha256

    with pytest.raises(Exception):
        spool.path_for_key("../escape")


def test_spool_receipt_must_match_artifact_metadata(tmp_path):
    spool = RawSpool(tmp_path / "spool")
    artifact = _artifact(content=b"expected")

    with pytest.raises(Exception, match="does not match"):
        spool.preserve_artifact(artifact, [b"different"])

    receipt = spool.preserve_artifact(artifact, [b"expected"])
    assert receipt.storage_key == artifact.spool_key


def test_admit_raw_artifact_publishes_verified_initial_receipts(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    artifact = _artifact(content=b"raw source")

    receipt = store.admit_raw_artifact(
        spool,
        artifact,
        [b"raw source"],
        next_stages=("notion_archived",),
        now=20,
    )

    with store._connect() as conn:
        stages = {
            row[0]: (row[1], row[2])
            for row in conn.execute(
                "SELECT stage, receipt_id, output_sha256 FROM stage_receipts "
                "WHERE artifact_id=?",
                (artifact.artifact_id,),
            ).fetchall()
        }
    assert stages["discovered"][1] == artifact.content_sha256
    assert stages["raw_preserved"] == (
        f"spool:{receipt.storage_key}",
        artifact.content_sha256,
    )
    assert store.claim_next(stage="notion_archived", spool=spool, now=21) is not None


def test_completed_admission_retry_is_idempotent(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    artifact = _artifact()

    first = store.admit_raw_artifact(
        spool,
        artifact,
        [b"raw"],
        next_stages=("notion_archived",),
    )
    second = store.admit_raw_artifact(
        spool,
        artifact,
        [b"ignored because durable admission is already complete"],
        next_stages=("notion_archived",),
    )

    assert second == first
    assert len(store.list_jobs()) == 1


def test_concurrent_identical_admissions_both_adopt_success(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    artifact = _artifact()

    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(
                lambda _: store.admit_raw_artifact(
                    spool,
                    artifact,
                    [b"raw"],
                    next_stages=("notion_archived",),
                ),
                range(2),
            )
        )

    assert receipts[0] == receipts[1]
    assert len(store.list_jobs()) == 1


def test_admission_conflict_does_not_publish_different_raw_bytes(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    original = _artifact(content=b"original")
    store.admit_raw_artifact(spool, original, [b"original"])
    spool.path_for_key(original.spool_key).unlink()
    changed = _artifact(content=b"changed")

    with pytest.raises(ValueError, match="different artifact metadata"):
        store.admit_raw_artifact(spool, changed, [b"changed"])

    assert not spool.path_for_key(original.spool_key).exists()


def test_initial_receipt_stages_cannot_be_queued_or_overwritten(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    artifact = _artifact()

    with pytest.raises(ValueError, match="initial receipt"):
        store.admit_raw_artifact(spool, artifact, [b"raw"], next_stages=("raw_preserved",))
    with pytest.raises(ValueError, match="initial receipt"):
        store.insert_artifact(artifact, stages=("discovered",))
    store.admit_raw_artifact(spool, artifact, [b"raw"])
    with pytest.raises(ValueError, match="initial receipt"):
        store.add_job(artifact.artifact_id, "raw_preserved")


def test_blocking_branch_prevents_linear_claim_until_resolved(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    artifact = _artifact()
    store.admit_raw_artifact(
        spool,
        artifact,
        [b"raw"],
        next_stages=("needs_review", "notion_archived"),
    )

    assert store.claim_next(stage="notion_archived", spool=spool) is None


def test_unmapped_artifact_can_be_preserved_then_resolved(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    unmapped = IntakeArtifact.from_bytes(
        project_key=UNMAPPED_PROJECT_KEY,
        provider_id="gmail",
        provider_artifact_id="message-1",
        content=b"raw",
        received_at=10,
    )
    store.admit_raw_artifact(
        spool,
        unmapped,
        [b"raw"],
        next_stages=("needs_mapping",),
    )
    with pytest.raises(ValueError, match="only queue needs_mapping"):
        store.add_job(unmapped.artifact_id, "notion_archived")
    claim = store.claim_next(stage="needs_mapping")
    assert claim is not None
    assert store.resolve_mapping(
        unmapped.artifact_id,
        "pid",
        claim.claim_token,
        next_stages=("notion_archived",),
    ) == unmapped.artifact_id
    assert store.claim_next(stage="notion_archived", spool=spool) is not None


def test_schema_migrates_existing_store_to_spool_storage_identity(tmp_path):
    db = tmp_path / "private" / "intake.db"
    store = IntakeStore(db)
    with store._write() as conn:
        conn.execute("UPDATE schema_meta SET value='1' WHERE key='schema_version'")
        conn.execute("ALTER TABLE artifacts DROP COLUMN spool_storage_id")

    migrated = IntakeStore(db)
    with migrated._connect() as conn:
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == "7"
        assert "spool_storage_id" in {
            row[1] for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()
        }


def test_downstream_claim_reverifies_spool_root_and_bytes(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    artifact = _artifact()
    store.admit_raw_artifact(
        spool,
        artifact,
        [b"raw"],
        next_stages=("notion_archived",),
    )

    with pytest.raises(Exception, match="root"):
        store.claim_next(
            stage="notion_archived",
            spool=RawSpool(tmp_path / "private" / "other-raw"),
        )
    spool.path_for_key(artifact.spool_key).write_bytes(b"tampered")
    assert store.claim_next(stage="notion_archived", spool=spool) is None


def test_spool_identity_survives_restore_to_new_path(tmp_path):
    import shutil

    original = RawSpool(tmp_path / "original")
    artifact = _artifact()
    original.preserve_artifact(artifact, [b"raw"])
    shutil.copytree(original.root, tmp_path / "restored")
    restored = RawSpool(tmp_path / "restored")

    assert restored.storage_id == original.storage_id
    restored.verify(
        artifact.spool_key,
        storage_id=original.storage_id,
        expected_sha256=artifact.content_sha256,
        expected_size=artifact.byte_size,
    )


def test_spool_rejects_symlink_destination(tmp_path):
    root = tmp_path / "spool"
    root.mkdir(mode=0o700)
    target = tmp_path / "target"
    target.write_bytes(b"secret")
    (root / storage_key("gmail", "x")).symlink_to(target)
    spool = RawSpool(root)
    with pytest.raises(Exception):
        spool.put(provider_id="gmail", provider_artifact_id="x", source=[b"new"])


def test_spool_concurrent_writers_never_replace_different_bytes(tmp_path, monkeypatch):
    spool = RawSpool(tmp_path / "spool")
    barrier = threading.Barrier(2)
    original_chunks = __import__(
        "plugins.client_knowledge_gbrain.spool", fromlist=["_source_chunks"]
    )._source_chunks

    def synchronized_chunks(source):
        for chunk in original_chunks(source):
            yield chunk
        barrier.wait(timeout=5)

    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.spool._source_chunks", synchronized_chunks
    )
    outcomes = []

    def writer(payload):
        try:
            outcomes.append(
                spool.put(
                    provider_id="gmail",
                    provider_artifact_id="message-1",
                    source=[payload],
                )
            )
        except Exception as exc:
            outcomes.append(exc)

    threads = [threading.Thread(target=writer, args=(payload,)) for payload in (b"a", b"b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert sum(not isinstance(item, Exception) for item in outcomes) == 1
    assert sum(isinstance(item, Exception) for item in outcomes) == 1
    key = storage_key("gmail", "message-1")
    with spool.read(key) as handle:
        assert handle.read() in {b"a", b"b"}


def test_spool_and_store_reject_insecure_existing_directories(tmp_path):
    insecure_spool = tmp_path / "public-spool"
    insecure_spool.mkdir(mode=0o755)
    with pytest.raises(Exception, match="group/world"):
        RawSpool(insecure_spool)

    insecure_store = tmp_path / "public-store"
    insecure_store.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="group/world"):
        IntakeStore(insecure_store / "intake.db")


def test_attachment_requires_existing_parent_artifact(tmp_path):
    parent = _artifact("gmail", "message-1")
    child = IntakeArtifact.from_bytes(
        project_key="pid",
        provider_id="gmail",
        provider_artifact_id="message-1:attachment:1",
        source_type="attachment",
        parent_artifact_id=parent.artifact_id,
        provider_message_id="message-1",
        provider_attachment_id="attachment-1",
        content=b"attachment",
        received_at=10,
    )
    store = IntakeStore(tmp_path / "intake.db")
    with pytest.raises(sqlite3.IntegrityError):
        store.insert_artifact(child)
    store.insert_artifact(parent)
    store.insert_artifact(child)


def test_attachment_parent_cannot_cross_project_or_provider(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    parent = _artifact("gmail", "message-1")
    store.insert_artifact(parent)
    child = IntakeArtifact.from_bytes(
        project_key="other",
        provider_id="gmail",
        provider_artifact_id="message-1:attachment:1",
        source_type="attachment",
        parent_artifact_id=parent.artifact_id,
        provider_message_id="message-1",
        provider_attachment_id="attachment-1",
        content=b"attachment",
        received_at=10,
    )

    with pytest.raises(ValueError, match="crosses"):
        store.insert_artifact(child)


def test_store_uses_wal_foreign_keys_and_transactional_receipt(tmp_path):
    db = tmp_path / "intake.db"
    store = IntakeStore(db)
    artifact = _artifact()
    spool = RawSpool(tmp_path / "raw")
    store.admit_raw_artifact(spool, artifact, [b"raw"], next_stages=("notion_archived",))
    claim = store.claim_next(stage="notion_archived", spool=spool, now=20)
    assert claim is not None
    assert store.complete_stage(
        claim.job_id,
        claim.claim_token,
        StageReceipt(artifact.artifact_id, "notion_archived", "receipt-1", recorded_at=21),
        now=21,
    )
    assert store.stats()["succeeded"] == 1
    with store._connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_stage_receipt_and_job_update_roll_back_together(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    artifact = _artifact()
    spool = RawSpool(tmp_path / "private" / "raw")
    store.admit_raw_artifact(spool, artifact, [b"raw"], next_stages=("notion_archived",))
    claim = store.claim_next(stage="notion_archived", spool=spool, now=20)
    assert claim is not None
    with store._write() as conn:
        conn.execute(
            "CREATE TRIGGER fail_job_completion BEFORE UPDATE OF status ON jobs "
            "WHEN NEW.status='succeeded' BEGIN SELECT RAISE(ABORT, 'fail completion'); END"
        )

    with pytest.raises(sqlite3.IntegrityError):
        store.complete_stage(
            claim.job_id,
            claim.claim_token,
            StageReceipt(artifact.artifact_id, "notion_archived", "receipt-1"),
            now=21,
        )

    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM stage_receipts WHERE stage='notion_archived'"
        ).fetchone()[0] == 0
    assert store.get_job(claim.job_id)["status"] == "running"


def test_live_same_host_owner_is_not_stolen_after_lease_expiry(tmp_path, monkeypatch):
    store = IntakeStore(tmp_path / "intake.db")
    artifact = _artifact()
    store.insert_artifact(artifact)
    store.add_job(artifact.artifact_id, "quarantined")
    claim = store.claim_next(stage="quarantined", lease_seconds=1, now=10)
    assert claim is not None
    monkeypatch.setattr("plugins.client_knowledge_gbrain.store._process_start", lambda _pid: 42)
    with store._write() as conn:
        conn.execute(
            "UPDATE jobs SET owner_started_at=42, heartbeat_at=19, lease_expires_at=1 WHERE job_id=?",
            (claim.job_id,),
        )
    assert store.reconcile(now=20) == 0
    assert store.get_job(claim.job_id)["status"] == "running"


def test_stale_dead_owner_is_requeued_and_retry_quarantine_are_bounded(tmp_path, monkeypatch):
    store = IntakeStore(tmp_path / "intake.db")
    artifact = _artifact()
    store.insert_artifact(artifact)
    store.add_job(artifact.artifact_id, "quarantined")
    claim = store.claim_next(stage="quarantined", lease_seconds=1, now=10)
    assert claim is not None
    monkeypatch.setattr("plugins.client_knowledge_gbrain.store._pid_alive", lambda _pid: False)
    with store._write() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at=1 WHERE job_id=?", (claim.job_id,))
    assert store.reconcile(now=20) == 1
    assert store.get_job(claim.job_id)["status"] == "queued"
    claim = store.claim_next(stage="quarantined", now=21)
    assert claim is not None
    assert store.fail_stage(
        claim.job_id, claim.claim_token, error_class="provider_error",
        quarantine=True, now=22,
    )
    assert store.retry(claim.job_id)
    assert store.get_job(claim.job_id)["attempt_count"] == 0
    assert store.quarantine(claim.job_id)
    assert store.get_job(claim.job_id)["status"] == "quarantined"


def test_expired_claim_cannot_complete_or_fail_before_or_after_reclamation(tmp_path, monkeypatch):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    artifact = _artifact()
    store.admit_raw_artifact(
        spool, artifact, [b"raw"], next_stages=("notion_archived",)
    )
    first = store.claim_next(
        stage="notion_archived", spool=spool, lease_seconds=1, now=10
    )
    assert first is not None
    with store._write() as conn:
        conn.execute(
            "UPDATE jobs SET lease_expires_at=1 WHERE job_id=?", (first.job_id,)
        )
    receipt = StageReceipt(
        artifact.artifact_id, "notion_archived", "notion:page:one"
    )
    assert not store.complete_stage(first.job_id, first.claim_token, receipt, now=20)
    assert not store.fail_stage(
        first.job_id, first.claim_token, error_class="notion_retryable", now=20
    )
    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.store._pid_alive", lambda _pid: False
    )
    assert store.reconcile(now=20) == 1
    second = store.claim_next(stage="notion_archived", spool=spool, now=21)
    assert second is not None and second.claim_token != first.claim_token
    assert not store.complete_stage(first.job_id, first.claim_token, receipt, now=22)
    assert not store.fail_stage(
        first.job_id, first.claim_token, error_class="notion_retryable", now=22
    )
    assert store.fail_stage(
        second.job_id, second.claim_token, error_class="notion_retryable", now=22
    )


def test_exhausted_job_does_not_starve_later_runnable_job(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    first = _artifact(external="message-1")
    second = _artifact(external="message-2")
    store.insert_artifact(first, now=1)
    store.insert_artifact(second, now=2)
    store.add_job(first.artifact_id, "quarantined", max_attempts=1)
    store.add_job(second.artifact_id, "quarantined", max_attempts=2)
    first_claim = store.claim_next(stage="quarantined", now=3)
    assert first_claim is not None and first_claim.artifact_id == first.artifact_id
    with store._write() as conn:
        conn.execute(
            "UPDATE jobs SET status='failed', claim_token=NULL, owner_pid=NULL, "
            "owner_host=NULL, owner_started_at=NULL, lease_expires_at=NULL, "
            "heartbeat_at=NULL WHERE job_id=?",
            (first_claim.job_id,),
        )

    next_claim = store.claim_next(stage="quarantined", now=4)

    assert next_claim is not None
    assert next_claim.artifact_id == second.artifact_id
    assert store.get_job(first_claim.job_id)["status"] == "quarantined"


def test_two_workers_cannot_claim_the_same_job(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    artifact = _artifact()
    store.insert_artifact(artifact)
    store.add_job(artifact.artifact_id, "quarantined")

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: store.claim_next(stage="quarantined"), range(2)))

    assert sum(claim is not None for claim in claims) == 1


def test_initial_stages_are_reserved_for_raw_admission(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    artifact = _artifact()
    with pytest.raises(ValueError, match="initial receipt"):
        store.insert_artifact(artifact, stages=("discovered", "raw_preserved"))


def test_generic_completion_cannot_resolve_mapping(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    artifact = IntakeArtifact.from_bytes(
        project_key=UNMAPPED_PROJECT_KEY,
        provider_id="gmail",
        provider_artifact_id="message-1",
        content=b"raw",
        received_at=10,
    )
    store.admit_raw_artifact(
        spool,
        artifact,
        [b"raw"],
        next_stages=("needs_mapping",),
    )
    claim = store.claim_next(stage="needs_mapping")
    assert claim is not None

    with pytest.raises(ValueError, match="dedicated transition"):
        store.complete_stage(
            claim.job_id,
            claim.claim_token,
            StageReceipt(artifact.artifact_id, "needs_mapping", "forged"),
        )


def test_mapped_artifact_cannot_queue_needs_mapping(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    artifact = _artifact()
    store.insert_artifact(artifact)

    with pytest.raises(ValueError, match="requires an unmapped"):
        store.add_job(artifact.artifact_id, "needs_mapping")


def test_live_process_with_stale_heartbeat_is_eventually_recovered(tmp_path, monkeypatch):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    artifact = _artifact()
    store.insert_artifact(artifact)
    store.add_job(artifact.artifact_id, "quarantined")
    claim = store.claim_next(stage="quarantined", lease_seconds=1, now=10)
    assert claim is not None
    monkeypatch.setattr("plugins.client_knowledge_gbrain.store._pid_alive", lambda _pid: True)
    monkeypatch.setattr("plugins.client_knowledge_gbrain.store._process_start", lambda _pid: 42)
    with store._write() as conn:
        conn.execute(
            "UPDATE jobs SET owner_started_at=42, heartbeat_at=10, lease_expires_at=11 "
            "WHERE job_id=?",
            (claim.job_id,),
        )

    assert store.reconcile(now=20, heartbeat_grace_seconds=30) == 0
    assert store.reconcile(now=50, heartbeat_grace_seconds=30) == 1


def test_external_receipts_and_cursors_are_idempotent(tmp_path):
    store = IntakeStore(tmp_path / "intake.db")
    artifact = _artifact()
    store.insert_artifact(artifact)
    receipt = ExternalReceipt("gmail", "external-1", artifact.artifact_id, "fetch", recorded_at=1)
    assert store.record_external_receipt(receipt)
    assert not store.record_external_receipt(receipt)
    store.set_cursor("gmail.history", "cursor-1")
    assert store.get_cursor("gmail.history") == "cursor-1"


def test_plugin_registers_only_existing_tools_plus_operator_surfaces(monkeypatch):
    import plugins.client_knowledge_gbrain as plugin

    calls = {"tools": [], "cli": [], "aux": []}

    class Context:
        def register_tool(self, **kwargs):
            calls["tools"].append(kwargs["name"])

        def register_cli_command(self, **kwargs):
            calls["cli"].append(kwargs["name"])

        def register_auxiliary_task(self, **kwargs):
            calls["aux"].append((kwargs["key"], kwargs["defaults"]))

    plugin.register(Context())
    assert calls["tools"] == ["client_knowledge_search", "client_knowledge_get"]
    assert calls["cli"] == ["client-knowledge"]
    assert calls["aux"] == [
        (
            "client_knowledge_interpret",
            {
                "model_tier": "advanced",
                "required_model_tier": "advanced",
                "configurable": False,
            },
        ),
        (
            "client_knowledge_assimilate",
            {
                "model_tier": "advanced",
                "required_model_tier": "advanced",
                "configurable": False,
            },
        ),
    ]


def test_enabled_plugin_registers_cli_and_named_auxiliary_tasks(monkeypatch, tmp_path):
    from hermes_cli.plugins import PluginManager

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - client-knowledge-gbrain\n",
        encoding="utf-8",
    )
    manager = PluginManager()
    manager.discover_and_load(force=True)

    assert "client-knowledge" in manager._cli_commands
    assert manager._aux_tasks["client_knowledge_interpret"]["defaults"]["model_tier"] == "advanced"
    assert manager._aux_tasks["client_knowledge_assimilate"]["defaults"]["model_tier"] == "advanced"
