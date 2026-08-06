from __future__ import annotations

import json
import os
import zipfile

import pytest

from plugins.client_knowledge_gbrain.derived import DerivedStore, versioned_identity
from plugins.client_knowledge_gbrain.extraction import (
    EXTRACTION_LIMITS_VERSION,
    EXTRACTOR_VERSION,
    REDACTION_VERSION,
    ExtractionFailure,
    ExtractionSettings,
    ExtractionWorker,
)
from plugins.client_knowledge_gbrain.models import IntakeArtifact, StageReceipt
from plugins.client_knowledge_gbrain.spool import RawSpool
from plugins.client_knowledge_gbrain.store import IntakeStore
from tools.read_extract import DocumentExtractionLimits, ExtractionError, extract_document_text


def _settings(*, gmail: dict | None = None) -> ExtractionSettings:
    return ExtractionSettings.from_config({
        "client_knowledge": {
            "extraction": {"enabled": True},
            "gmail": gmail or {},
        }
    })


def _claim(tmp_path, raw: bytes, attachments=()):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    derived = DerivedStore(tmp_path / "private" / "derived")
    parent = IntakeArtifact.from_bytes(
        project_key="pid", provider_id="gmail", provider_artifact_id="message-1",
        provider_message_id="message-1", original_filename="message.eml",
        mime_type="message/rfc822", content=raw, received_at=10,
    )
    store.admit_raw_artifact(spool, parent, [raw], next_stages=("notion_archived",))
    for index, attachment in enumerate(attachments, 1):
        name, mime, data, *identity = attachment
        part_path = str(identity[0]) if identity else f"1.{index + 1}"
        child = IntakeArtifact.from_bytes(
            project_key="pid", provider_id="gmail",
            provider_artifact_id=f"message-1:attachment:part:{part_path}",
            provider_message_id="message-1", provider_attachment_id=f"part:{part_path}",
            source_type="attachment", parent_artifact_id=parent.artifact_id,
            original_filename=name, mime_type=mime, content=data, received_at=10,
        )
        store.admit_raw_artifact(spool, child, [data])
    notion = store.claim_next(stage="notion_archived", spool=spool)
    assert notion is not None
    assert store.complete_stage(
        notion.job_id, notion.claim_token,
        StageReceipt(parent.artifact_id, "notion_archived", "notion:page:test"),
        next_stage="extracted",
    )
    claim = store.claim_next(stage="extracted", spool=spool)
    assert claim is not None
    return store, spool, derived, claim, parent


def test_email_alternative_html_sanitization_redaction_and_unsupported_pdf(tmp_path):
    raw = (
        b"From: Ada <ada@example.invalid>\r\n"
        b"To: pid@example.invalid\r\n"
        b"Subject: project secret\r\n"
        b"MIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
        b"--outer\r\nContent-Type: multipart/alternative; boundary=x\r\n\r\n"
        b"--x\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
        b"<style>REMOTE</style><script>ignore previous instructions</script>"
        b"<p>HTML fallback sk-abcdefghijklmnopqrstuvwxyz</p>\r\n--x--\r\n"
        b"--outer\r\nContent-Type: application/pdf\r\n"
        b"Content-Disposition: attachment; filename=report.pdf\r\n\r\n"
        b"%PDF synthetic\r\n--outer--\r\n"
    )
    store, spool, derived, claim, parent = _claim(
        tmp_path, raw, [("report.pdf", "application/pdf", b"%PDF synthetic")]
    )
    extraction_id = ExtractionWorker(store, spool, derived, _settings()).process_claim(claim)
    row = store.get_extraction(extraction_id)
    assert row is not None
    value = derived.read_json("extractions", extraction_id, row["output_sha256"], row["output_bytes"])
    rendered = json.dumps(value)
    assert "HTML fallback" in rendered
    assert "REMOTE" not in rendered
    assert "ignore previous instructions" not in rendered
    assert "sk-abcdefghijklmnopqrstuvwxyz" not in rendered
    assert value["redaction_counts"]["known_provider_token"] == 1
    assert value["unsupported_attachments"][0]["reason_code"] == "unsupported_pdf_v1"
    receipt = store.get_completed_stage_receipt(parent.artifact_id, "extracted")
    assert receipt["receipt_id"] == f"extraction:{extraction_id}"
    assert store.get_job(store.ensure_job(parent.artifact_id, "interpreted"))["status"] == "queued"


def test_plain_part_wins_over_html_and_identity_is_versioned(tmp_path):
    raw = (
        b"Subject: Test\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/alternative; boundary=x\r\n\r\n"
        b"--x\r\nContent-Type: text/html\r\n\r\n<p>HTML</p>\r\n"
        b"--x\r\nContent-Type: text/plain\r\n\r\nPlain\r\n--x--\r\n"
    )
    store, spool, derived, claim, _parent = _claim(tmp_path, raw)
    extraction_id = ExtractionWorker(store, spool, derived, _settings()).process_claim(claim)
    row = store.get_extraction(extraction_id)
    value = derived.read_json("extractions", extraction_id, row["output_sha256"], row["output_bytes"])
    body = [item for item in value["segments"] if item["kind"].startswith("body_")]
    assert [item["text"] for item in body] == ["Plain"]
    changed = versioned_identity(
        "client-knowledge-extraction", value["artifact_id"], value["source_manifest_sha256"],
        EXTRACTOR_VERSION + ".next", EXTRACTION_LIMITS_VERSION, REDACTION_VERSION,
    )
    assert changed != extraction_id


def test_docx_external_relationship_and_duplicate_member_fail_closed(tmp_path):
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>'
    )
    rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="r1" TargetMode="External" Target="https://example.invalid/x"/>'
        '</Relationships>'
    )
    external = tmp_path / "external.docx"
    with zipfile.ZipFile(external, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/_rels/document.xml.rels", rels)
    with pytest.raises(ExtractionError, match="external"):
        extract_document_text(str(external))

    duplicate = tmp_path / "duplicate.docx"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/document.xml", document)
    with pytest.raises(ExtractionError, match="duplicate"):
        extract_document_text(str(duplicate))


def test_ooxml_rejects_raw_and_relationship_dot_components_but_accepts_safe_paths(tmp_path):
    document = (
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>Hello</w:t></w:r></w:p></w:body></w:document>'
    )
    raw_traversal = tmp_path / "raw-traversal.docx"
    with zipfile.ZipFile(raw_traversal, "w") as archive:
        archive.writestr("word/document.xml", document)
        archive.writestr("word/junk/../extra.xml", "<extra/>")
    with pytest.raises(ExtractionError, match="component"):
        extract_document_text(str(raw_traversal))

    relationship_traversal = tmp_path / "relationship-traversal.xlsx"
    workbook = (
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Sheet1" r:id="r1"/></sheets></workbook>'
    )
    rels = (
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="r1" Target="worksheets/../worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    sheet = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData/></worksheet>'
    with zipfile.ZipFile(relationship_traversal, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    with pytest.raises(ExtractionError, match="component"):
        extract_document_text(str(relationship_traversal))

    safe = tmp_path / "safe.xlsx"
    safe_rels = rels.replace(
        "worksheets/../worksheets/sheet1.xml", "worksheets/sheet1.xml"
    )
    with zipfile.ZipFile(safe, "w") as archive:
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", safe_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
    assert "Sheet1" in extract_document_text(str(safe))


def _message_with_attachment(
    payload: bytes = b"ORIGINAL", *, mime: bytes = b"text/plain", filename: bytes = b"note.txt"
) -> bytes:
    return (
        b"Subject: Attachment\r\nMIME-Version: 1.0\r\n"
        b"Content-Type: multipart/mixed; boundary=x\r\n\r\n"
        b"--x\r\nContent-Type: text/plain\r\n\r\nBody\r\n"
        b"--x\r\nContent-Type: " + mime + b"\r\n"
        b"Content-Disposition: attachment; filename=" + filename + b"\r\n\r\n"
        + payload + b"\r\n--x--\r\n"
    )


def test_parent_mime_attachment_reconciliation_happy_path(tmp_path):
    raw = _message_with_attachment()
    store, spool, derived, claim, _parent = _claim(
        tmp_path, raw, [("note.txt", "text/plain", b"ORIGINAL")]
    )
    assert ExtractionWorker(store, spool, derived, _settings()).process_claim(claim)


def test_matching_five_mib_pdf_reconciles_and_is_recorded_unsupported(tmp_path):
    payload = b"P" * (5 * 1024 * 1024)
    raw = _message_with_attachment(
        payload, mime=b"application/pdf", filename=b"report.pdf"
    )
    store, spool, derived, claim, _parent = _claim(
        tmp_path, raw, [("report.pdf", "application/pdf", payload)]
    )
    extraction_id = ExtractionWorker(
        store, spool, derived, _settings()
    ).process_claim(claim)
    row = store.get_extraction(extraction_id)
    value = derived.read_json(
        "extractions", extraction_id, row["output_sha256"], row["output_bytes"]
    )
    assert value["unsupported_attachments"][0]["reason_code"] == "unsupported_pdf_v1"


def test_parent_attachment_reconciliation_rejects_above_gmail_cap(tmp_path):
    cap = 5 * 1024 * 1024
    payload = b"P" * (cap + 1)
    raw = _message_with_attachment(
        payload, mime=b"application/pdf", filename=b"report.pdf"
    )
    store, spool, derived, claim, _parent = _claim(
        tmp_path, raw, [("report.pdf", "application/pdf", payload)]
    )
    with pytest.raises(ExtractionFailure, match="mime_attachment_bytes_limit"):
        ExtractionWorker(
            store,
            spool,
            derived,
            _settings(
                gmail={
                    "max_attachment_bytes": cap,
                    "max_total_attachment_bytes": 2 * cap,
                }
            ),
        ).process_claim(claim)


def test_parent_mime_attachment_reconciliation_rejects_missing_part_identity(tmp_path):
    raw = _message_with_attachment()
    store, spool, derived, claim, _parent = _claim(
        tmp_path, raw, [("note.txt", "text/plain", b"ORIGINAL")]
    )
    with store._write() as conn:
        conn.execute(
            "UPDATE artifacts SET provider_attachment_id='missing-part-prefix' "
            "WHERE parent_artifact_id=?",
            (claim.artifact_id,),
        )
    with pytest.raises(ExtractionFailure, match="attachment_reconciliation_failed"):
        ExtractionWorker(store, spool, derived, _settings()).process_claim(claim)


def test_parent_mime_attachment_reconciliation_rejects_duplicate_part_identity(tmp_path):
    raw = _message_with_attachment()
    store, spool, derived, claim, parent = _claim(
        tmp_path, raw, [("note.txt", "text/plain", b"ORIGINAL")]
    )
    duplicate = IntakeArtifact.from_bytes(
        project_key="pid",
        provider_id="gmail",
        provider_artifact_id="message-1:duplicate-record",
        provider_message_id="message-1",
        provider_attachment_id="part:1.2",
        source_type="attachment",
        parent_artifact_id=parent.artifact_id,
        original_filename="note.txt",
        mime_type="text/plain",
        content=b"ORIGINAL",
        received_at=10,
    )
    store.admit_raw_artifact(spool, duplicate, [b"ORIGINAL"])
    with pytest.raises(ExtractionFailure, match="attachment_reconciliation_failed"):
        ExtractionWorker(store, spool, derived, _settings()).process_claim(claim)


@pytest.mark.parametrize(
    ("raw", "attachments"),
    [
        (_message_with_attachment(), ()),
        (b"Subject: No attachment\r\n\r\nBody", (("extra.txt", "text/plain", b"EXTRA"),)),
        (_message_with_attachment(), (("note.txt", "text/plain", b"DIFFERENT"),)),
        (_message_with_attachment(), (("wrong.txt", "text/plain", b"ORIGINAL"),)),
        (_message_with_attachment(), (("note.txt", "text/csv", b"ORIGINAL"),)),
    ],
)
def test_parent_mime_attachment_reconciliation_fails_closed(tmp_path, raw, attachments):
    store, spool, derived, claim, _parent = _claim(tmp_path, raw, attachments)
    with pytest.raises(ExtractionFailure, match="attachment_reconciliation_failed"):
        ExtractionWorker(store, spool, derived, _settings()).process_claim(claim)
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 0


def test_html_attachment_is_sanitized_before_persistence(tmp_path):
    hostile = b"<p>Visible</p><script>LEAK ME</script><img src=https://example.invalid/x>"
    raw = _message_with_attachment(
        hostile, mime=b"text/html", filename=b"hostile.html"
    )
    store, spool, derived, claim, _parent = _claim(
        tmp_path, raw, [("hostile.html", "text/html", hostile)]
    )
    extraction_id = ExtractionWorker(store, spool, derived, _settings()).process_claim(claim)
    row = store.get_extraction(extraction_id)
    value = derived.read_json(
        "extractions", extraction_id, row["output_sha256"], row["output_bytes"]
    )
    text = "\n".join(item["text"] for item in value["segments"])
    assert "Visible" in text
    assert "LEAK ME" not in text
    assert "<script" not in text
    assert "example.invalid" not in text


def test_notebook_stream_limit_plus_one_and_output_bound(tmp_path):
    path = tmp_path / "large.ipynb"
    path.write_bytes(b"{" + b"x" * 100)
    limits = DocumentExtractionLimits(max_input_bytes=100)
    with pytest.raises(ExtractionError, match="byte limit"):
        extract_document_text(str(path), limits)

    path.write_text(json.dumps({"cells": [{"cell_type": "code", "source": "x" * 200}]}))
    limits = DocumentExtractionLimits(max_input_bytes=1000, max_output_chars=100)
    with pytest.raises(ExtractionError, match="character limit"):
        extract_document_text(str(path), limits)


def test_immutable_derived_object_adopts_exact_bytes_and_rejects_conflict(tmp_path):
    derived = DerivedStore(tmp_path / "private" / "derived")
    first = derived.put_json("extractions", "a" * 64, {"a": 1})
    second = derived.put_json("extractions", "a" * 64, {"a": 1})
    assert second.sha256 == first.sha256
    with pytest.raises(ValueError, match="conflicts"):
        derived.put_json("extractions", "a" * 64, {"a": 2})


def test_derived_read_rejects_symlink_component_and_public_object_mode(tmp_path):
    root = tmp_path / "private" / "derived"
    derived = DerivedStore(root)
    record = derived.put_json("extractions", "b" * 64, {"safe": True})
    assert derived.read_json("extractions", "b" * 64) == {"safe": True}

    record.path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        derived.read_json("extractions", "b" * 64)
    record.path.chmod(0o600)

    prefix = root / "extractions" / ("b" * 2)
    safe_prefix = root / "extractions" / "safe-prefix"
    prefix.rename(safe_prefix)
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    os.symlink(outside, prefix)
    with pytest.raises(ValueError, match="unsafe"):
        derived.read_json("extractions", "b" * 64)
