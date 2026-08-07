from __future__ import annotations

import email.message
import io
import json
import sqlite3
from collections import Counter

import httpx
import pytest

from plugins.client_knowledge_gbrain.models import IntakeArtifact, StageReceipt
from plugins.client_knowledge_gbrain.notion import (
    NOTION_API_VERSION,
    NotionClient,
    NotionIncompleteEvidenceError,
    NotionRetryableError,
    NotionSchemaError,
    NotionStaticConfigError,
)
from hermes_cli.fork_defaults import FORK_DEFAULT_ADDITIONS
from plugins.client_knowledge_gbrain.notion_archive import (
    NotionArchiveSettings,
    NotionArchiveWorker,
    ProjectNotionConfig,
    run_fixed_sandbox_fixtures,
    run_notion_once,
    _HeartbeatReader,
    _assert_sandbox_target,
    _marker,
    _multipart_plan,
    MAX_BODY_CHARS,
    TRUNCATION_NOTICE,
)
from plugins.client_knowledge_gbrain.spool import RawSpool
from plugins.client_knowledge_gbrain.store import CURRENT_SCHEMA_VERSION, IntakeStore


def _schema(*, machine: bool = True):
    properties = {
        "Feedback": {"id": "title", "type": "title", "title": {}},
        "Status": {
            "id": "status",
            "type": "status",
            "status": {"options": [{"name": value} for value in ("New", "In Progress", "Resolved")]},
        },
        "Date Received": {"id": "date", "type": "date", "date": {}},
        "Client / Source": {"id": "source", "type": "rich_text", "rich_text": {}},
        "Category": {"id": "category", "type": "select", "select": {"options": [{"name": "Other"}]}},
        "Priority": {"id": "priority", "type": "select", "select": {"options": []}},
    }
    if machine:
        properties.update({
            "Source ID": {"id": "source-id", "type": "rich_text", "rich_text": {}},
            "Source Type": {"id": "source-type", "type": "rich_text", "rich_text": {}},
            "Source Hash": {"id": "source-hash", "type": "rich_text", "rich_text": {}},
            "Source URL": {"id": "source-url", "type": "url", "url": {}},
        })
    return {"object": "data_source", "id": "source-1", "properties": properties}


def _config(*, max_file_bytes=100_000_000):
    return {
        "client_knowledge": {
            "notion": {
                "enabled": True,
                "max_file_bytes": max_file_bytes,
                "multipart_part_bytes": 5 * 1024 * 1024,
                "max_jobs_per_run": 10,
                "lease_seconds": 300,
                "claim_heartbeat_seconds": 1,
            }
        },
        "projects": {
            "pid": {
                "notion": {
                    "database_id": "database-1",
                    "data_source_id": "source-1",
                    "properties": {},
                }
            }
        },
    }


def _settings(config):
    raw = config["client_knowledge"]["notion"]
    return NotionArchiveSettings(
        enabled=True,
        api_key="token",
        timeout=30,
        max_file_bytes=raw["max_file_bytes"],
        multipart_part_bytes=raw["multipart_part_bytes"],
        max_jobs_per_run=10,
        lease_seconds=300,
        heartbeat_seconds=1,
    )


def _artifact(content=b"raw", *, external="message-1", source_type="email", parent="", attachment_id=""):
    return IntakeArtifact.from_bytes(
        project_key="pid",
        provider_id="gmail",
        provider_artifact_id=external,
        provider_message_id="message-1",
        provider_attachment_id=attachment_id,
        source_type=source_type,
        parent_artifact_id=parent,
        original_filename="../../user-controlled.pdf" if source_type == "attachment" else "message.eml",
        mime_type="application/pdf" if source_type == "attachment" else "message/rfc822",
        actor_display="Ada",
        source_url="https://mail.example.invalid/message-1",
        content=content,
        received_at=10,
    )


def _claim(tmp_path, artifact=None, content=None):
    artifact = artifact or _artifact()
    payload = content if content is not None else b"raw"
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    store.admit_raw_artifact(spool, artifact, [payload], next_stages=("notion_archived",))
    claim = store.claim_next(stage="notion_archived", spool=spool)
    assert claim is not None
    return store, spool, claim, artifact


def _list(results, *, status="complete", has_more=False, cursor=None):
    return {
        "object": "list",
        "results": results,
        "has_more": has_more,
        "next_cursor": cursor,
        "request_status": {"type": status},
    }


def _upload(upload_id, filename, *, size=None, content_type="message/rfc822", status="pending", total=1, sent=0):
    return {
        "object": "file_upload",
        "id": upload_id,
        "filename": filename,
        "content_type": content_type,
        "content_length": size,
        "status": status,
        "created_time": "2026-01-01T00:00:00Z",
        "expiry_time": "2026-01-01T01:00:00Z",
        "number_of_parts": {"total": total, "sent": sent},
    }


def _page(artifact, page_id="page-1"):
    def rich(value):
        return {"rich_text": [{"plain_text": value}]}
    return {
        "object": "page",
        "id": page_id,
        "in_trash": False,
        "properties": {
            "Source ID": rich(artifact.artifact_id),
            "Source Hash": rich(artifact.content_sha256),
            "Source Type": rich(artifact.source_type),
        },
    }


def test_client_pins_api_version_and_classifies_retryable_failures():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(503, json={"object": "error"})
    client = NotionClient("token", transport=httpx.MockTransport(handler))
    with pytest.raises(NotionRetryableError):
        client.retrieve_data_source("source-1")
    assert requests[0].headers["Notion-Version"] == NOTION_API_VERSION == "2026-03-11"


def test_universal_defaults_do_not_ship_pid_notion_ids():
    assert FORK_DEFAULT_ADDITIONS["projects"] == {}


@pytest.mark.parametrize("path", ["/file_uploads/u/send", "/file_uploads/u/complete"])
def test_conflict_error_is_retryable_for_upload_storage_paths(path):
    client = NotionClient("token", transport=httpx.MockTransport(
        lambda _request: httpx.Response(409, json={"object": "error", "code": "conflict_error"})
    ))
    with pytest.raises(NotionRetryableError):
        client._request("POST", path, json={})


def test_terminal_409_variant_fails_closed():
    client = NotionClient("token", transport=httpx.MockTransport(
        lambda _request: httpx.Response(409, json={"object": "error", "code": "agent_deleted"})
    ))
    with pytest.raises(NotionSchemaError):
        client._request("POST", "/pages", json={})


def test_upload_listing_requires_complete_paginated_evidence():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        if len(calls) == 1:
            return httpx.Response(200, json=_list([_upload("u1", "a.bin")], has_more=True, cursor="opaque"))
        return httpx.Response(200, json=_list([_upload("u2", "b.bin")], status="incomplete"))
    client = NotionClient("token", transport=httpx.MockTransport(handler))
    with pytest.raises(NotionIncompleteEvidenceError):
        client.list_file_uploads()
    assert "start_cursor=opaque" in calls[1]


def test_upload_listing_accepts_optional_request_status_when_pagination_is_complete():
    client = NotionClient("token", transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json={
            "object": "list", "results": [], "has_more": False, "next_cursor": None,
        })
    ))
    evidence = client.list_file_uploads()
    assert evidence.items == ()
    assert evidence.page_count == 1


def test_upload_listing_rejects_invalid_request_status():
    client = NotionClient("token", transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json={
            "object": "list", "results": [], "has_more": False, "next_cursor": None,
            "request_status": "complete",
        })
    ))
    with pytest.raises(NotionIncompleteEvidenceError, match="status is invalid"):
        client.list_file_uploads()


@pytest.mark.parametrize("payload", [
    {"object": "list", "results": [], "has_more": False, "next_cursor": None,
     "request_status": None},
    {"object": "list", "results": [], "has_more": False},
    {"object": "list", "results": [], "has_more": False, "next_cursor": "stale"},
])
def test_upload_listing_rejects_malformed_optional_evidence(payload):
    client = NotionClient("token", transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json=payload)
    ))
    with pytest.raises(NotionIncompleteEvidenceError):
        client.list_file_uploads()


def test_raw_email_uses_supported_notion_upload_transport(tmp_path, monkeypatch):
    store, spool, claim, artifact = _claim(tmp_path)
    config = _config()
    client = type("Client", (), {})()
    worker = NotionArchiveWorker(store, spool, client, _settings(config), config)
    monkeypatch.setattr(worker, "_complete_scan", lambda *_args: (
        "baseline", type("Evidence", (), {"items": (), "page_count": 1})()
    ))
    created = {}
    client.create_file_upload = lambda **kwargs: created.update(kwargs) or {"id": "upload-1"}
    attempt_id = "a" * 32
    marker = _marker(artifact, attempt_id)
    attempt = store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=attempt_id, opaque_marker=marker,
        remote_filename=f"{marker}.txt", upload_mode="single_part",
        expected_part_count=1,
    )
    assert worker._create_or_recover_upload(
        claim, artifact.artifact_id, artifact, attempt
    ) == "upload-1"
    assert created["filename"] == f"{marker}.txt"
    assert created["content_type"] == "text/plain"


def test_legacy_email_attempt_without_create_is_replaced_before_retry(tmp_path, monkeypatch):
    store, spool, claim, artifact = _claim(tmp_path)
    config = _config()
    legacy_id = "b" * 32
    legacy_marker = _marker(artifact, legacy_id)
    store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=legacy_id, opaque_marker=legacy_marker,
        remote_filename=f"{legacy_marker}.eml", upload_mode="single_part",
        expected_part_count=1,
    )
    store.select_active_upload_attempt(
        claim.job_id, claim.claim_token, artifact.artifact_id, artifact, legacy_id
    )
    worker = NotionArchiveWorker(store, spool, object(), _settings(config), config)
    monkeypatch.setattr(worker, "_create_or_recover_upload", lambda *_args: "upload-1")
    monkeypatch.setattr(worker, "_send_bytes", lambda *_args: None)
    monkeypatch.setattr(worker, "_ensure_attachment_block", lambda *_args: "block-1")
    worker.client = type("Client", (), {
        "retrieve_file_upload": staticmethod(lambda _upload_id: _upload(
            "upload-1", store.get_upload_attempts(artifact.artifact_id)[-1]["remote_filename"],
            size=artifact.byte_size, content_type="text/plain", status="uploaded",
        )),
    })()
    worker._ensure_file(claim, artifact.artifact_id, artifact, "page-1")
    attempts = store.get_upload_attempts(artifact.artifact_id)
    assert len(attempts) == 2
    assert attempts[1]["remote_filename"].endswith(".txt")
    assert attempts[1]["replaces_attempt_id"] == legacy_id
    assert attempts[1]["replacement_reason"] == "notion_eml_transport_unsupported"


def test_legacy_email_create_with_no_reconciled_candidate_is_replaced(tmp_path, monkeypatch):
    store, spool, claim, artifact = _claim(tmp_path)
    config = _config()
    legacy_id = "c" * 32
    legacy_marker = _marker(artifact, legacy_id)
    store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=legacy_id, opaque_marker=legacy_marker,
        remote_filename=f"{legacy_marker}.eml", upload_mode="single_part",
        expected_part_count=1,
    )
    baseline_id = store.publish_upload_scan(
        claim.job_id, claim.claim_token, artifact.artifact_id, legacy_id,
        scan_role="baseline", page_count=1, items=[],
    )
    store.append_upload_attempt_event(
        claim.job_id, claim.claim_token, artifact.artifact_id, legacy_id,
        "create_started",
    )
    store.select_active_upload_attempt(
        claim.job_id, claim.claim_token, artifact.artifact_id, artifact, legacy_id
    )
    worker = NotionArchiveWorker(store, spool, object(), _settings(config), config)
    monkeypatch.setattr(worker, "_complete_scan", lambda *_args: (
        "reconciliation", type("Evidence", (), {"items": (), "page_count": 1})()
    ))
    monkeypatch.setattr(worker, "_create_or_recover_upload", lambda *_args: "upload-1")
    monkeypatch.setattr(worker, "_send_bytes", lambda *_args: None)
    monkeypatch.setattr(worker, "_ensure_attachment_block", lambda *_args: "block-1")
    worker.client = type("Client", (), {
        "retrieve_file_upload": staticmethod(lambda _upload_id: _upload(
            "upload-1", store.get_upload_attempts(artifact.artifact_id)[-1]["remote_filename"],
            size=artifact.byte_size, content_type="text/plain", status="uploaded",
        )),
    })()
    assert baseline_id
    worker._ensure_file(claim, artifact.artifact_id, artifact, "page-1")
    attempts = store.get_upload_attempts(artifact.artifact_id)
    assert len(attempts) == 2
    assert attempts[1]["remote_filename"].endswith(".txt")


def test_legacy_email_marker_conflict_does_not_create_replacement(tmp_path, monkeypatch):
    store, spool, claim, artifact = _claim(tmp_path)
    config = _config()
    legacy_id = "d" * 32
    legacy_marker = _marker(artifact, legacy_id)
    filename = f"{legacy_marker}.eml"
    store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=legacy_id, opaque_marker=legacy_marker,
        remote_filename=filename, upload_mode="single_part",
        expected_part_count=1,
    )
    store.publish_upload_scan(
        claim.job_id, claim.claim_token, artifact.artifact_id, legacy_id,
        scan_role="baseline", page_count=1, items=[],
    )
    store.append_upload_attempt_event(
        claim.job_id, claim.claim_token, artifact.artifact_id, legacy_id,
        "create_started",
    )
    store.select_active_upload_attempt(
        claim.job_id, claim.claim_token, artifact.artifact_id, artifact, legacy_id
    )
    worker = NotionArchiveWorker(store, spool, object(), _settings(config), config)
    monkeypatch.setattr(worker, "_complete_scan", lambda *_args: (
        "reconciliation", type("Evidence", (), {
            "items": (_upload(
                "conflict", filename, size=artifact.byte_size,
                content_type="text/plain",
            ),),
            "page_count": 1,
        })()
    ))
    with pytest.raises(Exception, match="conflicting metadata"):
        worker._ensure_file(claim, artifact.artifact_id, artifact, "page-1")
    assert len(store.get_upload_attempts(artifact.artifact_id)) == 1


def test_mixed_valid_and_conflicting_marker_candidates_fail_closed(tmp_path, monkeypatch):
    store, spool, claim, artifact = _claim(tmp_path)
    config = _config()
    attempt_id = "f" * 32
    marker = _marker(artifact, attempt_id)
    filename = f"{marker}.eml"
    store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=attempt_id, opaque_marker=marker,
        remote_filename=filename, upload_mode="single_part",
        expected_part_count=1,
    )
    baseline_id = store.publish_upload_scan(
        claim.job_id, claim.claim_token, artifact.artifact_id, attempt_id,
        scan_role="baseline", page_count=1, items=[],
    )
    attempt = store.get_upload_attempts(artifact.artifact_id)[0]
    worker = NotionArchiveWorker(store, spool, object(), _settings(config), config)
    monkeypatch.setattr(worker, "_complete_scan", lambda *_args: (
        "reconciliation", type("Evidence", (), {
            "items": (
                _upload("valid", filename, size=artifact.byte_size),
                _upload(
                    "conflict", filename, size=artifact.byte_size,
                    content_type="text/plain",
                ),
            ),
            "page_count": 1,
        })()
    ))
    with pytest.raises(Exception, match="conflicting metadata"):
        worker._recover_uncertain_creation(
            claim, artifact.artifact_id, artifact, attempt, baseline_id,
            allow_absent=True,
        )
    assert store.get_upload_attempts(artifact.artifact_id)[0]["remote_upload_id"] is None


def test_legacy_multipart_replacement_computes_new_partition(tmp_path, monkeypatch):
    payload = b"x" * (21 * 1024 * 1024)
    artifact = _artifact(content=payload)
    store, spool, claim, artifact = _claim(
        tmp_path, artifact=artifact, content=payload
    )
    config = _config(max_file_bytes=30 * 1024 * 1024)
    legacy_id = "e" * 32
    legacy_marker = _marker(artifact, legacy_id)
    store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=legacy_id, opaque_marker=legacy_marker,
        remote_filename=f"{legacy_marker}.eml", upload_mode="multi_part",
        expected_part_count=2, expected_part_size=0,
    )
    store.select_active_upload_attempt(
        claim.job_id, claim.claim_token, artifact.artifact_id, artifact, legacy_id
    )
    worker = NotionArchiveWorker(store, spool, object(), _settings(config), config)
    monkeypatch.setattr(worker, "_create_or_recover_upload", lambda *_args: "upload-1")
    worker.client = type("Client", (), {
        "retrieve_file_upload": staticmethod(lambda _upload_id: _upload(
            "upload-1", store.get_upload_attempts(artifact.artifact_id)[-1]["remote_filename"],
            size=artifact.byte_size, content_type="text/plain", status="uploaded",
            total=5, sent=5,
        )),
    })()
    monkeypatch.setattr(worker, "_ensure_attachment_block", lambda *_args: "block-1")
    worker._ensure_file(claim, artifact.artifact_id, artifact, "page-1")
    replacement = store.get_upload_attempts(artifact.artifact_id)[-1]
    assert replacement["upload_mode"] == "multi_part"
    assert replacement["expected_part_size"] == 5 * 1024 * 1024
    assert replacement["expected_part_count"] == 5


def test_preflight_adds_only_missing_machine_properties_and_preserves_schema(tmp_path):
    before = _schema(machine=False)
    after = _schema(machine=True)
    patches = []
    gets = 0
    def handler(request):
        nonlocal gets
        if request.method == "GET":
            gets += 1
            return httpx.Response(200, json=before if gets == 1 else after)
        if request.url.path.endswith("/query"):
            return httpx.Response(200, json=_list([]))
        patches.append(json.loads(request.content))
        return httpx.Response(200, json=after)
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    config = _config()
    client = NotionClient("token", transport=httpx.MockTransport(handler))
    worker = NotionArchiveWorker(store, spool, client, _settings(config), config)
    result = worker.preflight(ProjectNotionConfig.from_config(config, "pid"), apply_schema=True)
    assert result["applied"] is True
    assert set(patches[0]["properties"]) == {"Source ID", "Source Type", "Source Hash", "Source URL"}
    assert "Status" not in patches[0]["properties"]
    assert before["properties"]["Status"] == after["properties"]["Status"]


def test_read_only_preflight_succeeds_when_all_machine_properties_are_absent(tmp_path):
    methods = []
    def handler(request):
        methods.append((request.method, request.url.path))
        return httpx.Response(200, json=_schema(machine=False))
    config = _config()
    worker = NotionArchiveWorker(
        IntakeStore(tmp_path / "private" / "intake.db"),
        RawSpool(tmp_path / "private" / "raw"),
        NotionClient("token", transport=httpx.MockTransport(handler)),
        _settings(config), config,
    )
    result = worker.preflight(ProjectNotionConfig.from_config(config, "pid"))
    assert result["applied"] is False
    assert set(result["missing_properties"]) == {
        "Source ID", "Source Type", "Source Hash", "Source URL",
    }
    assert methods == [("GET", "/v1/data_sources/source-1")]


def test_preflight_never_imports_or_mutates_historical_rows(tmp_path):
    historical_rows = [
        {"id": f"historical-{index}", "properties": {"Feedback": f"Legacy {index}"}}
        for index in range(4)
    ]
    original = json.loads(json.dumps(historical_rows))
    methods = []
    def handler(request):
        methods.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json=_list([]) if request.url.path.endswith("/query") else _schema(),
        )
    config = _config()
    worker = NotionArchiveWorker(
        IntakeStore(tmp_path / "private" / "intake.db"),
        RawSpool(tmp_path / "private" / "raw"),
        NotionClient("token", transport=httpx.MockTransport(handler)),
        _settings(config), config,
    )
    worker.preflight(ProjectNotionConfig.from_config(config, "pid"))
    assert historical_rows == original
    assert methods == [
        ("GET", "/v1/data_sources/source-1"),
        ("POST", "/v1/data_sources/source-1/query"),
    ]


def test_preflight_rejects_static_schema_conflict_without_patch(tmp_path):
    schema = _schema()
    schema["properties"]["Source ID"] = {"id": "bad", "type": "number", "number": {}}
    methods = []
    def handler(request):
        methods.append(request.method)
        return httpx.Response(200, json=schema)
    config = _config()
    worker = NotionArchiveWorker(
        IntakeStore(tmp_path / "private" / "intake.db"),
        RawSpool(tmp_path / "private" / "raw"),
        NotionClient("token", transport=httpx.MockTransport(handler)),
        _settings(config), config,
    )
    with pytest.raises(NotionStaticConfigError):
        worker.preflight(ProjectNotionConfig.from_config(config, "pid"), apply_schema=True)
    assert methods == ["GET"]


def test_upload_marker_is_opaque_and_never_uses_original_name():
    artifact = _artifact(b"attachment", external="message-1:a", source_type="attachment", parent="0" * 64, attachment_id="a")
    marker = _marker(artifact, "a" * 32)
    assert marker.startswith("ckfu-v1-")
    assert "user-controlled" not in marker
    assert "pid" not in marker


def test_multipart_part_size_config_is_bounded():
    config = _config()
    config["client_knowledge"]["notion"]["multipart_part_bytes"] = 4 * 1024 * 1024
    with pytest.raises(NotionStaticConfigError, match="between 5 and 20"):
        NotionArchiveSettings.from_config(config)


def test_multipart_plan_enforces_1000_part_limit_with_larger_compliant_parts():
    part_size, count = _multipart_plan(5 * 1024 * 1024 * 1001, 5 * 1024 * 1024)
    assert count == 1000
    assert 5 * 1024 * 1024 < part_size <= 20 * 1024 * 1024
    assert _multipart_plan(20 * 1024 * 1024 * 1000, 20 * 1024 * 1024) == (
        20 * 1024 * 1024, 1000,
    )
    with pytest.raises(NotionStaticConfigError, match="part limit"):
        _multipart_plan(20 * 1024 * 1024 * 1000 + 1, 20 * 1024 * 1024)


def test_send_file_upload_rejects_part_numbers_outside_api_limit():
    client = NotionClient("token", transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json={})
    ))
    for part_number in (0, 1001):
        with pytest.raises(NotionSchemaError, match="part number"):
            client.send_file_upload(
                "upload", filename="fixture.bin",
                content_type="application/octet-stream",
                content=io.BytesIO(b"x"), part_number=part_number,
            )


def test_uncertain_creation_adopts_only_marker_bound_candidate_among_concurrent_uploads(tmp_path):
    store, spool, claim, artifact = _claim(tmp_path)
    config = _config()
    settings = _settings(config)
    attempt_id = "a" * 32
    marker = _marker(artifact, attempt_id)
    filename = f"{marker}.eml"
    attempt = store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=attempt_id, opaque_marker=marker, remote_filename=filename,
        upload_mode="single_part", expected_part_count=1,
    )
    baseline = [_upload("u1", "other.bin"), _upload("u2", "another.bin")]
    scan_id = store.publish_upload_scan(
        claim.job_id, claim.claim_token, artifact.artifact_id, attempt_id,
        scan_role="baseline", page_count=1, items=baseline,
    )
    attempt = store.get_upload_attempts(artifact.artifact_id)[0]
    reconciliation = baseline + [
        _upload("u3", "concurrent.bin", size=artifact.byte_size),
        _upload("u4", "same-size.eml", size=artifact.byte_size),
        _upload("u5", filename, size=artifact.byte_size),
    ]
    def handler(request):
        assert request.method == "GET"
        return httpx.Response(200, json=_list(reconciliation))
    worker = NotionArchiveWorker(
        store, spool, NotionClient("token", transport=httpx.MockTransport(handler)),
        settings, config,
    )
    adopted = worker._recover_uncertain_creation(claim, artifact.artifact_id, artifact, attempt, scan_id)
    assert adopted == "u5"
    assert store.get_upload_attempts(artifact.artifact_id)[0]["remote_upload_id"] == "u5"


def test_uncertain_creation_quarantines_two_fully_matching_candidates(tmp_path):
    store, spool, claim, artifact = _claim(tmp_path)
    config = _config()
    attempt_id = "b" * 32
    marker = _marker(artifact, attempt_id)
    filename = f"{marker}.eml"
    store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=attempt_id, opaque_marker=marker, remote_filename=filename,
        upload_mode="single_part", expected_part_count=1,
    )
    scan_id = store.publish_upload_scan(
        claim.job_id, claim.claim_token, artifact.artifact_id, attempt_id,
        scan_role="baseline", page_count=1, items=[],
    )
    attempt = store.get_upload_attempts(artifact.artifact_id)[0]
    client = NotionClient("token", transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json=_list([
            _upload("u1", filename, size=artifact.byte_size),
            _upload("u2", filename, size=artifact.byte_size),
        ]))
    ))
    worker = NotionArchiveWorker(store, spool, client, _settings(config), config)
    with pytest.raises(Exception, match="conflicting metadata"):
        worker._recover_uncertain_creation(claim, artifact.artifact_id, artifact, attempt, scan_id)


def test_uncertain_creation_with_no_marker_candidate_remains_retryable(tmp_path):
    store, spool, claim, artifact = _claim(tmp_path)
    config = _config()
    attempt_id = "d" * 32
    marker = _marker(artifact, attempt_id)
    store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=attempt_id, opaque_marker=marker,
        remote_filename=f"{marker}.eml", upload_mode="single_part",
        expected_part_count=1,
    )
    scan_id = store.publish_upload_scan(
        claim.job_id, claim.claim_token, artifact.artifact_id, attempt_id,
        scan_role="baseline", page_count=1, items=[],
    )
    attempt = store.get_upload_attempts(artifact.artifact_id)[0]
    client = NotionClient("token", transport=httpx.MockTransport(
        lambda _request: httpx.Response(200, json=_list([_upload("u1", "unrelated.bin")]))
    ))
    worker = NotionArchiveWorker(store, spool, client, _settings(config), config)
    with pytest.raises(NotionIncompleteEvidenceError):
        worker._recover_uncertain_creation(claim, artifact.artifact_id, artifact, attempt, scan_id)


def test_missing_create_id_records_uncertain_event_before_successful_adoption(tmp_path, monkeypatch):
    store, spool, claim, artifact = _claim(tmp_path)
    config = _config()
    attempt_id = "f" * 32
    marker = _marker(artifact, attempt_id)
    attempt = store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=attempt_id, opaque_marker=marker,
        remote_filename=f"{marker}.eml", upload_mode="single_part",
        expected_part_count=1,
    )
    worker = NotionArchiveWorker(
        store, spool,
        NotionClient("token", transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        _settings(config), config,
    )
    monkeypatch.setattr(worker, "_complete_scan", lambda *_args: ("baseline", type("E", (), {"items": (), "page_count": 1})()))
    monkeypatch.setattr(worker.client, "create_file_upload", lambda **_kwargs: {"object": "file_upload"})
    def adopt(*_args):
        events = store.get_upload_attempt_events(attempt_id)
        assert events[-1]["event_type"] == "create_result_uncertain"
        return "adopted-upload"
    monkeypatch.setattr(worker, "_recover_uncertain_creation", adopt)
    assert worker._create_or_recover_upload(
        claim, artifact.artifact_id, artifact, attempt
    ) == "adopted-upload"
    assert "create_result_uncertain" in {
        event["event_type"] for event in store.get_upload_attempt_events(attempt_id)
    }


def test_upload_attempt_history_is_append_only_and_replacements_retain_prior_identity(tmp_path):
    store, _spool, claim, artifact = _claim(tmp_path)
    first = store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id="1" * 32, opaque_marker=_marker(artifact, "1" * 32),
        remote_filename=f"{_marker(artifact, '1' * 32)}.eml",
        upload_mode="single_part", expected_part_count=1,
    )
    store.record_upload_remote_identity(
        claim.job_id, claim.claim_token, artifact.artifact_id,
        first["attempt_id"], "remote-1", evidence_identity="direct",
    )
    store.append_upload_attempt_event(
        claim.job_id, claim.claim_token, artifact.artifact_id,
        first["attempt_id"], "attempt_expired", remote_upload_id="remote-1",
    )
    second = store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id="2" * 32, opaque_marker=_marker(artifact, "2" * 32),
        remote_filename=f"{_marker(artifact, '2' * 32)}.eml",
        upload_mode="single_part", expected_part_count=1,
        replaces_attempt_id=first["attempt_id"], replacement_reason="expired_before_attachment",
    )
    store.record_upload_remote_identity(
        claim.job_id, claim.claim_token, artifact.artifact_id,
        second["attempt_id"], "remote-2", evidence_identity="direct",
    )
    attempts = store.get_upload_attempts(artifact.artifact_id)
    assert [row["remote_upload_id"] for row in attempts] == ["remote-1", "remote-2"]
    assert attempts[1]["replaces_attempt_id"] == attempts[0]["attempt_id"]
    with store._connect() as conn:
        events = conn.execute(
            "SELECT event_type FROM notion_upload_attempt_events ORDER BY observed_at, sequence"
        ).fetchall()
    assert "attempt_expired" in {row[0] for row in events}


def test_conflicting_remote_identity_persists_append_only_conflict_event(tmp_path):
    store, _spool, claim, artifact = _claim(tmp_path)
    attempt_id = "e" * 32
    marker = _marker(artifact, attempt_id)
    store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=attempt_id, opaque_marker=marker,
        remote_filename=f"{marker}.eml", upload_mode="single_part",
        expected_part_count=1,
    )
    store.record_upload_remote_identity(
        claim.job_id, claim.claim_token, artifact.artifact_id,
        attempt_id, "remote-1", evidence_identity="direct",
    )
    with pytest.raises(ValueError, match="conflicting"):
        store.record_upload_remote_identity(
            claim.job_id, claim.claim_token, artifact.artifact_id,
            attempt_id, "remote-2", evidence_identity="reconciliation",
        )
    assert store.get_upload_attempts(artifact.artifact_id)[0]["remote_upload_id"] == "remote-1"
    assert store.get_upload_attempt_events(attempt_id)[-1]["event_type"] == "remote_identity_conflict"


def test_stale_worker_cannot_advance_or_append_after_reclaimer(tmp_path, monkeypatch):
    store, spool, first, artifact = _claim(tmp_path)
    attempt = store.reserve_upload_attempt(
        first.job_id, first.claim_token, artifact,
        attempt_id="3" * 32, opaque_marker=_marker(artifact, "3" * 32),
        remote_filename=f"{_marker(artifact, '3' * 32)}.eml",
        upload_mode="single_part", expected_part_count=1,
    )
    monkeypatch.setattr("plugins.client_knowledge_gbrain.store._pid_alive", lambda _pid: False)
    with store._write() as conn:
        conn.execute("UPDATE jobs SET lease_expires_at=1 WHERE job_id=?", (first.job_id,))
    assert store.reconcile(now=20) == 1
    second = store.claim_next(stage="notion_archived", spool=spool, now=21)
    assert second is not None and second.claim_token != first.claim_token
    with pytest.raises(PermissionError):
        store.append_upload_attempt_event(
            first.job_id, first.claim_token, artifact.artifact_id,
            attempt["attempt_id"], "bytes_send_started",
        )
    with pytest.raises(PermissionError):
        store.advance_notion_operation(
            first.job_id, first.claim_token, artifact.artifact_id, "file", "bytes-sent",
            expected_sha256=artifact.content_sha256, expected_size=artifact.byte_size,
            expected_mime_type=artifact.mime_type,
        )


def test_heartbeat_reader_aborts_transfer_when_ownership_is_lost():
    def lost():
        raise PermissionError("lost")
    reader = _HeartbeatReader(io.BytesIO(b"payload"), lost, 0)
    with pytest.raises(PermissionError, match="lost"):
        reader.read(1)


def test_process_claim_renews_immediately_before_final_completion(tmp_path, monkeypatch):
    store, spool, claim, artifact = _claim(tmp_path)
    config = _config()
    worker = NotionArchiveWorker(
        store, spool,
        NotionClient("token", transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        _settings(config), config,
    )
    monkeypatch.setattr(worker, "preflight", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(worker, "_ensure_page", lambda *_args: "page-1")
    monkeypatch.setattr(worker, "_ensure_file", lambda *_args: "attempt-1")
    renewals = []
    original_renew = worker._renew
    def renew(current_claim):
        renewals.append(current_claim.claim_token)
        original_renew(current_claim)
    monkeypatch.setattr(worker, "_renew", renew)
    original_complete = store.complete_stage
    def complete(job_id, claim_token, receipt, **kwargs):
        assert renewals[-1] == claim_token
        return original_complete(job_id, claim_token, receipt, **kwargs)
    monkeypatch.setattr(store, "complete_stage", complete)
    assert worker.process_claim(claim) == "page-1"


def test_page_recovery_and_archive_fields_exclude_priority_and_synthesis(tmp_path):
    message = email.message.EmailMessage()
    message["Subject"] = "Feedback"
    message["From"] = "ada@example.invalid"
    message.set_content("Faithful body")
    raw = message.as_bytes()
    artifact = _artifact(raw)
    store, spool, claim, _ = _claim(tmp_path, artifact, raw)
    config = _config()
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/data_sources/source-1"):
            return httpx.Response(200, json=_schema())
        if request.url.path.endswith("/data_sources/source-1/query"):
            return httpx.Response(200, json=_list([]))
        if request.url.path.endswith("/pages"):
            body = json.loads(request.content)
            assert body["parent"] == {"data_source_id": "source-1"}
            assert "Priority" not in body["properties"]
            serialized = json.dumps(body)
            assert "Key Decisions" not in serialized
            assert "Action Items" not in serialized
            return httpx.Response(200, json={"id": "page-1"})
        if request.url.path.endswith("/pages/page-1"):
            return httpx.Response(200, json=_page(artifact))
        if request.url.path.endswith("/blocks/page-1/children") and request.method == "GET":
            return httpx.Response(200, json=_list([]))
        if request.url.path.endswith("/blocks/page-1/children") and request.method == "PATCH":
            return httpx.Response(200, json={"results": [{"id": "content-block"}]})
        raise AssertionError(request.url)
    worker = NotionArchiveWorker(
        store, spool, NotionClient("token", transport=httpx.MockTransport(handler)),
        _settings(config), config,
    )
    assert worker._ensure_page(claim, artifact, ProjectNotionConfig.from_config(config, "pid")) == "page-1"
    assert store.get_notion_operation(artifact.artifact_id, "page")["page_id"] == "page-1"


def test_page_recovery_rejects_source_type_mismatch(tmp_path):
    artifact = _artifact()
    page = _page(artifact)
    page["properties"]["Source Type"] = {"rich_text": [{"plain_text": "attachment"}]}
    config = _config()
    worker = NotionArchiveWorker(
        IntakeStore(tmp_path / "private" / "intake.db"),
        RawSpool(tmp_path / "private" / "raw"),
        NotionClient("token", transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        _settings(config), config,
    )
    with pytest.raises(Exception, match="type conflicts"):
        worker._verify_page(page, artifact, ProjectNotionConfig.from_config(config, "pid"))


def test_oversized_email_render_is_bounded_with_deterministic_notice(tmp_path):
    body = "x" * (MAX_BODY_CHARS + 10_000)
    message = email.message.EmailMessage()
    message["Subject"] = "Large"
    message.set_content(body)
    raw = message.as_bytes()
    artifact = _artifact(raw)
    store, spool, claim, _ = _claim(tmp_path, artifact, raw)
    worker = NotionArchiveWorker(
        store, spool,
        NotionClient("token", transport=httpx.MockTransport(lambda _request: httpx.Response(200))),
        _settings(_config()), _config(),
    )
    _subject, rendered, _headers = worker._source_content(artifact)
    assert len(rendered) <= MAX_BODY_CHARS + len(TRUNCATION_NOTICE)
    assert rendered.endswith(TRUNCATION_NOTICE)


def test_oversized_render_emits_no_more_than_fixed_block_limit(tmp_path, monkeypatch):
    artifact = _artifact()
    store, spool, claim, _ = _claim(tmp_path)
    batches = []
    class Client:
        def list_block_children(self, *_args, **_kwargs):
            return type("Evidence", (), {"items": ()})()
        def append_block_children(self, _page_id, children):
            batches.append(list(children))
            return {"results": [{"id": "block"}]}
    worker = NotionArchiveWorker(store, spool, Client(), _settings(_config()), _config())
    monkeypatch.setattr(
        worker, "_source_content",
        lambda _artifact: ("Large", "x" * (MAX_BODY_CHARS + 1), {"Subject": "Large"}),
    )
    worker._ensure_source_blocks(claim, artifact, "page-1")
    assert sum(len(batch) - 1 for batch in batches) <= 80
    assert len(batches) == 1


def test_select_status_schema_uses_select_page_property(tmp_path):
    schema = _schema()
    schema["properties"]["Status"] = {
        "id": "status", "type": "select",
        "select": {"options": [{"name": value} for value in ("New", "In Progress", "Resolved")]},
    }
    config = _config()
    worker = NotionArchiveWorker(
        IntakeStore(tmp_path / "private" / "intake.db"),
        RawSpool(tmp_path / "private" / "raw"),
        NotionClient("token", transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=_list([]) if request.url.path.endswith("/query") else schema
            )
        )),
        _settings(config), config,
    )
    project = ProjectNotionConfig.from_config(config, "pid")
    worker.preflight(project)
    properties = worker._page_properties(_artifact(), project, "Title")
    assert properties["Status"] == {"select": {"name": "New"}}


def test_existing_attachment_marker_prevents_duplicate_append(tmp_path):
    store, spool, claim, artifact = _claim(tmp_path)
    config = _config()
    attempt_id = "c" * 32
    marker = _marker(artifact, attempt_id)
    store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=attempt_id, opaque_marker=marker,
        remote_filename=f"{marker}.eml", upload_mode="single_part",
        expected_part_count=1,
    )
    methods = Counter()
    def handler(request):
        methods[request.method] += 1
        return httpx.Response(200, json=_list([{
            "id": "block-1",
            "type": "file",
            "file": {
                "file_upload": {"id": "upload-1"},
                "caption": [{"plain_text": marker}],
            },
        }]))
    worker = NotionArchiveWorker(
        store, spool, NotionClient("token", transport=httpx.MockTransport(handler)),
        _settings(config), config,
    )
    block_id = worker._ensure_attachment_block(
        claim, artifact.artifact_id, artifact, "page-1", "upload-1", marker, attempt_id
    )
    assert block_id == "block-1"
    assert methods["PATCH"] == 0


def test_single_part_flow_streams_verified_spool_bytes(tmp_path):
    payload = b"raw"
    store, spool, claim, artifact = _claim(tmp_path, content=payload)
    config = _config()
    attempt_id = "6" * 32
    marker = _marker(artifact, attempt_id)
    attempt = store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=attempt_id, opaque_marker=marker,
        remote_filename=f"{marker}.eml", upload_mode="single_part",
        expected_part_count=1,
    )
    sent = []
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json=_upload(
                "upload-1", attempt["remote_filename"], size=None, status="pending",
            ))
        sent.append(request.read())
        return httpx.Response(200, json=_upload(
            "upload-1", attempt["remote_filename"], size=len(payload), status="uploaded",
        ))
    worker = NotionArchiveWorker(
        store, spool, NotionClient("token", transport=httpx.MockTransport(handler)),
        _settings(config), config,
    )
    worker._send_bytes(claim, artifact.artifact_id, artifact, attempt, "upload-1")
    assert len(sent) == 1
    assert payload in sent[0]


def test_multipart_flow_sends_parts_then_completes_with_heartbeats(tmp_path):
    payload = b"abcdefghijk"
    artifact = _artifact(payload)
    store, spool, claim, _ = _claim(tmp_path, artifact, payload)
    config = _config()
    settings = NotionArchiveSettings(
        enabled=True, api_key="token", timeout=30,
        max_file_bytes=100, multipart_part_bytes=5,
        max_jobs_per_run=10, lease_seconds=300, heartbeat_seconds=1,
    )
    attempt_id = "7" * 32
    marker = _marker(artifact, attempt_id)
    attempt = store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=attempt_id, opaque_marker=marker,
        remote_filename=f"{marker}.eml", upload_mode="multi_part",
        expected_part_count=3, expected_part_size=5,
    )
    calls = []
    def handler(request):
        calls.append((request.method, request.url.path, request.read()))
        if request.method == "GET":
            return httpx.Response(200, json=_upload(
                "upload-1", attempt["remote_filename"], size=None,
                status="pending", total=3, sent=0,
            ))
        return httpx.Response(200, json=_upload(
            "upload-1", attempt["remote_filename"], size=None,
            status="pending", total=3, sent=0,
        ))
    worker = NotionArchiveWorker(
        store, spool, NotionClient("token", transport=httpx.MockTransport(handler)),
        settings, config,
    )
    heartbeats = 0
    original_renew = worker._renew
    def renew(current_claim):
        nonlocal heartbeats
        heartbeats += 1
        original_renew(current_claim)
    worker._renew = renew
    worker._send_bytes(claim, artifact.artifact_id, artifact, attempt, "upload-1")
    sends = [call for call in calls if call[1].endswith("/send")]
    completes = [call for call in calls if call[1].endswith("/complete")]
    assert len(sends) == 3
    assert len(completes) == 1
    assert heartbeats >= 8
    events = store.get_upload_attempt_events(attempt_id)
    assert [event["remote_parts_sent"] for event in events if event["event_type"] == "part_acknowledged"] == [1, 2, 3]


def test_store_migration_creates_append_only_upload_tables(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    with store._connect() as conn:
        assert conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0] == str(CURRENT_SCHEMA_VERSION)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"notion_upload_attempts", "notion_upload_attempt_events", "notion_upload_scans"}.issubset(tables)


def test_schema_v4_migrates_upload_attempt_part_size_additively(tmp_path):
    db = tmp_path / "private" / "intake.db"
    store = IntakeStore(db)
    with store._write() as conn:
        conn.execute("UPDATE schema_meta SET value='4' WHERE key='schema_version'")
        conn.execute(
            "CREATE TABLE replacement AS SELECT attempt_id, artifact_id, operation_kind, ordinal, "
            "replaces_attempt_id, replacement_reason, marker_version, opaque_marker, remote_filename, "
            "upload_mode, expected_sha256, expected_size, expected_mime_type, expected_part_count, "
            "baseline_scan_id, remote_upload_id, created_by_job_id, created_at "
            "FROM notion_upload_attempts"
        )
        conn.execute("DROP TABLE notion_upload_attempts")
        conn.execute("ALTER TABLE replacement RENAME TO notion_upload_attempts")
    migrated = IntakeStore(db)
    with migrated._connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(notion_upload_attempts)")}
        version = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
    assert version == str(CURRENT_SCHEMA_VERSION)
    assert "expected_part_size" in columns


@pytest.mark.parametrize("sent_parts", [0, 1])
def test_migrated_legacy_multipart_attempt_blocks_unknown_partition_resume(
    tmp_path, monkeypatch, sent_parts
):
    payload = b"x" * (20 * 1024 * 1024 + 1)
    artifact = _artifact(payload)
    db = tmp_path / "private" / "intake.db"
    store = IntakeStore(db)
    spool = RawSpool(tmp_path / "private" / "raw")
    store.admit_raw_artifact(
        spool, artifact, [payload], next_stages=("notion_archived",)
    )
    claim = store.claim_next(stage="notion_archived", spool=spool)
    assert claim is not None
    attempt_id = "8" * 32
    marker = _marker(artifact, attempt_id)
    store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=attempt_id, opaque_marker=marker,
        remote_filename=f"{marker}.eml", upload_mode="multi_part",
        expected_part_count=5, expected_part_size=5 * 1024 * 1024,
    )
    store.record_upload_remote_identity(
        claim.job_id, claim.claim_token, artifact.artifact_id,
        attempt_id, "legacy-upload", evidence_identity="direct",
    )
    store.select_active_upload_attempt(
        claim.job_id, claim.claim_token, artifact.artifact_id, artifact, attempt_id
    )
    if sent_parts:
        store.append_upload_attempt_event(
            claim.job_id, claim.claim_token, artifact.artifact_id, attempt_id,
            "part_acknowledged", remote_upload_id="legacy-upload",
            remote_parts_sent=sent_parts,
        )
    assert store.fail_stage(
        claim.job_id, claim.claim_token, error_class="notion_retryable", retry_delay=0
    )
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE schema_meta SET value='4' WHERE key='schema_version'"
        )
        conn.execute(
            "ALTER TABLE notion_upload_attempts DROP COLUMN expected_part_size"
        )
    migrated = IntakeStore(db)
    assert migrated.get_upload_attempts(artifact.artifact_id)[0]["expected_part_size"] == 0

    sends = []

    class Client:
        def retrieve_file_upload(self, _upload_id):
            return _upload(
                "legacy-upload", f"{marker}.eml", size=artifact.byte_size,
                status="pending", total=5, sent=sent_parts,
            )

        def send_file_upload(self, *_args, **_kwargs):
            sends.append(True)

    monkeypatch.setattr(NotionArchiveWorker, "preflight", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(NotionArchiveWorker, "_ensure_page", lambda *_args: "page-1")
    monkeypatch.setattr(
        spool, "read_verified",
        lambda *_args, **_kwargs: pytest.fail("legacy partition opened the spool"),
    )
    config = _config(max_file_bytes=30 * 1024 * 1024)
    config["client_knowledge"]["notion"]["multipart_part_bytes"] = 10 * 1024 * 1024
    result = run_notion_once(
        store=migrated, spool=spool, client=Client(), config=config
    )
    assert result == {"processed": 1, "succeeded": 0, "failed": 0, "quarantined": 1}
    assert migrated.get_job(claim.job_id)["status"] == "quarantined"
    assert sends == []


def test_synthetic_fixture_guard_never_allows_pid_or_production_id_collision():
    config = _config()
    pid = ProjectNotionConfig.from_config(config, "pid")
    with pytest.raises(NotionStaticConfigError):
        _assert_sandbox_target(config, pid)
    config["projects"]["sandbox"] = {
        "notion": {
            "database_id": "database-1",
            "data_source_id": "sandbox-source",
            "sandbox": True,
            "allow_synthetic_fixture_writes": True,
        }
    }
    sandbox = ProjectNotionConfig.from_config(config, "sandbox")
    with pytest.raises(NotionStaticConfigError, match="overlaps"):
        _assert_sandbox_target(config, sandbox)


@pytest.mark.parametrize("pid_value", [{}, {"notion": {}}, {"notion": {"database_id": "db"}}])
def test_synthetic_fixture_guard_requires_valid_pid_notion_ids(pid_value):
    config = _config()
    config["projects"]["pid"] = pid_value
    config["projects"]["sandbox"] = {
        "notion": {
            "database_id": "sandbox-db",
            "data_source_id": "sandbox-source",
            "sandbox": True,
            "allow_synthetic_fixture_writes": True,
        }
    }
    sandbox = ProjectNotionConfig.from_config(config, "sandbox")
    with pytest.raises(NotionStaticConfigError, match="valid PID"):
        _assert_sandbox_target(config, sandbox)


def test_fixed_sandbox_fixture_entry_point_is_repeatable_without_duplicates(tmp_path, monkeypatch):
    config = _config(max_file_bytes=30 * 1024 * 1024)
    config["projects"]["sandbox"] = {
        "notion": {
            "database_id": "sandbox-db",
            "data_source_id": "sandbox-source",
            "sandbox": True,
            "allow_synthetic_fixture_writes": True,
        }
    }
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    remote_uploads = {}
    markers = []
    page_artifact = None

    class FakeClient:
        def retrieve_data_source(self, _source_id):
            return _schema()

        def query_data_source(self, *_args, **_kwargs):
            return []

        def retrieve_page(self, _page_id):
            return _page(page_artifact, "sandbox-page")

        def list_block_children(self, _page_id, **_kwargs):
            return type("Evidence", (), {
                "items": tuple({
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"plain_text": marker}]},
                } for marker in markers)
            })()

        def retrieve_file_upload(self, upload_id):
            return remote_uploads[upload_id]

    def complete_fixture(self, claim):
        nonlocal page_artifact
        parent = store.get_artifact_for_notion_claim(
            claim.job_id, claim.claim_token, claim.artifact_id
        )
        page_artifact = parent
        family = [parent, *store.list_child_artifacts_for_notion_claim(
            claim.job_id, claim.claim_token, claim.artifact_id
        )]
        store.advance_notion_operation(
            claim.job_id, claim.claim_token, parent.artifact_id, "page", "page-verified",
            expected_sha256=parent.content_sha256, expected_size=parent.byte_size,
            expected_mime_type=parent.mime_type, page_id="sandbox-page",
        )
        for index, artifact in enumerate(family):
            attempt_id = f"{index + 1:032x}"
            marker = _marker(artifact, attempt_id)
            markers.append(marker)
            upload_id = f"upload-{index}"
            attempt = store.reserve_upload_attempt(
                claim.job_id, claim.claim_token, artifact,
                attempt_id=attempt_id, opaque_marker=marker,
                remote_filename=f"{marker}.bin", upload_mode="single_part",
                expected_part_count=1, expected_part_size=artifact.byte_size,
                claimed_artifact_id=parent.artifact_id,
            )
            store.record_upload_remote_identity(
                claim.job_id, claim.claim_token, artifact.artifact_id,
                attempt_id, upload_id, evidence_identity="fixture",
                claimed_artifact_id=parent.artifact_id,
            )
            store.advance_notion_operation(
                claim.job_id, claim.claim_token, artifact.artifact_id, "file", "receipt-verified",
                expected_sha256=artifact.content_sha256, expected_size=artifact.byte_size,
                expected_mime_type=artifact.mime_type, page_id="sandbox-page",
                block_id=f"block-{index}", active_upload_attempt_id=attempt_id,
                claimed_artifact_id=parent.artifact_id,
            )
            remote_uploads[upload_id] = _upload(
                upload_id, attempt["remote_filename"], size=artifact.byte_size,
                content_type=artifact.mime_type, status="uploaded",
            )
        self._renew(claim)
        assert store.complete_stage(
            claim.job_id, claim.claim_token,
            StageReceipt(parent.artifact_id, "notion_archived", "notion:page:sandbox-page"),
        )
        return "sandbox-page"

    monkeypatch.setattr(NotionArchiveWorker, "process_claim", complete_fixture)
    first = run_fixed_sandbox_fixtures(
        store=store, spool=spool, client=FakeClient(), config=config,
        project_key="sandbox",
    )
    second = run_fixed_sandbox_fixtures(
        store=store, spool=spool, client=FakeClient(), config=config,
        project_key="sandbox",
    )
    assert first["page_id"] == second["page_id"] == "sandbox-page"
    assert first["recovered"] is False
    assert second["recovered"] is True
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM notion_upload_attempts").fetchone()[0] == 3
    assert len(remote_uploads) == 3


def test_fixed_sandbox_fixture_resumes_after_page_persistence_without_duplicates(
    tmp_path, monkeypatch
):
    config = _config(max_file_bytes=30 * 1024 * 1024)
    config["projects"]["sandbox"] = {
        "notion": {
            "database_id": "sandbox-db",
            "data_source_id": "sandbox-source",
            "sandbox": True,
            "allow_synthetic_fixture_writes": True,
        }
    }
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    page_artifact = None
    page_writes = []
    upload_writes = []
    block_writes = []
    remote_uploads = {}

    class FakeClient:
        def retrieve_data_source(self, _source_id):
            return _schema()

        def query_data_source(self, *_args, **_kwargs):
            return []

        def retrieve_page(self, _page_id):
            return _page(page_artifact, "sandbox-page")

        def list_block_children(self, _page_id, **_kwargs):
            return type("Evidence", (), {
                "items": tuple({
                    "type": "paragraph",
                    "paragraph": {"rich_text": [{"plain_text": marker}]},
                } for marker in block_writes)
            })()

        def retrieve_file_upload(self, upload_id):
            return remote_uploads[upload_id]

    calls = 0

    def interrupted_then_complete(self, claim):
        nonlocal calls, page_artifact
        calls += 1
        parent = store.get_artifact_for_notion_claim(
            claim.job_id, claim.claim_token, claim.artifact_id
        )
        page_artifact = parent
        if calls == 1:
            page_writes.append("sandbox-page")
            store.advance_notion_operation(
                claim.job_id, claim.claim_token, parent.artifact_id,
                "page", "page-created",
                expected_sha256=parent.content_sha256,
                expected_size=parent.byte_size,
                expected_mime_type=parent.mime_type,
                page_id="sandbox-page",
            )
            with store._write() as conn:
                conn.execute(
                    "UPDATE jobs SET lease_expires_at=1, heartbeat_at=1, "
                    "owner_pid=2147483647 WHERE job_id=?",
                    (claim.job_id,),
                )
            raise NotionRetryableError("simulated interruption")
        assert store.get_notion_operation(parent.artifact_id, "page")["page_id"] == "sandbox-page"
        family = [parent, *store.list_child_artifacts_for_notion_claim(
            claim.job_id, claim.claim_token, claim.artifact_id
        )]
        for index, artifact in enumerate(family):
            attempt_id = f"{index + 1:032x}"
            marker = _marker(artifact, attempt_id)
            upload_id = f"upload-{index}"
            attempt = store.reserve_upload_attempt(
                claim.job_id, claim.claim_token, artifact,
                attempt_id=attempt_id, opaque_marker=marker,
                remote_filename=f"{marker}.bin", upload_mode="single_part",
                expected_part_count=1, expected_part_size=artifact.byte_size,
                claimed_artifact_id=parent.artifact_id,
            )
            upload_writes.append(upload_id)
            block_writes.append(marker)
            store.record_upload_remote_identity(
                claim.job_id, claim.claim_token, artifact.artifact_id,
                attempt_id, upload_id, evidence_identity="fixture",
                claimed_artifact_id=parent.artifact_id,
            )
            store.advance_notion_operation(
                claim.job_id, claim.claim_token, artifact.artifact_id,
                "file", "receipt-verified",
                expected_sha256=artifact.content_sha256,
                expected_size=artifact.byte_size,
                expected_mime_type=artifact.mime_type,
                page_id="sandbox-page", block_id=f"block-{index}",
                active_upload_attempt_id=attempt_id,
                claimed_artifact_id=parent.artifact_id,
            )
            remote_uploads[upload_id] = _upload(
                upload_id, attempt["remote_filename"], size=artifact.byte_size,
                content_type=artifact.mime_type, status="uploaded",
            )
        self._renew(claim)
        assert store.complete_stage(
            claim.job_id, claim.claim_token,
            StageReceipt(
                parent.artifact_id, "notion_archived",
                "notion:page:sandbox-page",
            ),
        )
        return "sandbox-page"

    monkeypatch.setattr(NotionArchiveWorker, "process_claim", interrupted_then_complete)
    monkeypatch.setattr(
        "plugins.client_knowledge_gbrain.store._pid_alive", lambda _pid: False
    )
    with pytest.raises(NotionRetryableError, match="simulated interruption"):
        run_fixed_sandbox_fixtures(
            store=store, spool=spool, client=FakeClient(), config=config,
            project_key="sandbox",
        )
    resumed = run_fixed_sandbox_fixtures(
        store=store, spool=spool, client=FakeClient(), config=config,
        project_key="sandbox",
    )
    repeated = run_fixed_sandbox_fixtures(
        store=store, spool=spool, client=FakeClient(), config=config,
        project_key="sandbox",
    )
    assert resumed["page_id"] == repeated["page_id"] == "sandbox-page"
    assert resumed["recovered"] is False
    assert repeated["recovered"] is True
    assert page_writes == ["sandbox-page"]
    assert len(upload_writes) == len(set(upload_writes)) == 3
    assert len(block_writes) == len(set(block_writes)) == 3


def test_expired_upload_creates_linked_replacement_attempt(tmp_path, monkeypatch):
    store, spool, claim, artifact = _claim(tmp_path)
    config = _config()
    settings = _settings(config)
    first_id = "4" * 32
    first_marker = _marker(artifact, first_id)
    first = store.reserve_upload_attempt(
        claim.job_id, claim.claim_token, artifact,
        attempt_id=first_id, opaque_marker=first_marker,
        remote_filename=f"{first_marker}.eml", upload_mode="single_part",
        expected_part_count=1,
    )
    store.record_upload_remote_identity(
        claim.job_id, claim.claim_token, artifact.artifact_id,
        first_id, "expired-upload", evidence_identity="direct",
    )
    store.select_active_upload_attempt(
        claim.job_id, claim.claim_token, artifact.artifact_id, artifact, first_id
    )
    calls = 0
    current_attempt = first
    def retrieve(upload_id):
        nonlocal calls, current_attempt
        calls += 1
        if upload_id == "replacement-upload":
            current_attempt = store.get_upload_attempts(artifact.artifact_id)[-1]
        return _upload(
            upload_id,
            current_attempt["remote_filename"],
            size=artifact.byte_size,
            content_type=(
                "text/plain"
                if current_attempt["remote_filename"].endswith(".txt")
                else artifact.mime_type
            ),
            status="expired" if calls == 1 else "uploaded",
        )
    worker = NotionArchiveWorker(
        store, spool,
        NotionClient("token", transport=httpx.MockTransport(lambda _request: httpx.Response(500))),
        settings, config,
    )
    monkeypatch.setattr(worker.client, "retrieve_file_upload", retrieve)
    monkeypatch.setattr(worker, "_create_or_recover_upload", lambda *_args: "replacement-upload")
    monkeypatch.setattr(worker, "_send_bytes", lambda *_args: None)
    monkeypatch.setattr(worker, "_ensure_attachment_block", lambda *_args: "block-1")
    assert worker._ensure_file(claim, artifact.artifact_id, artifact, "page-1")
    attempts = store.get_upload_attempts(artifact.artifact_id)
    assert len(attempts) == 2
    assert attempts[1]["replaces_attempt_id"] == first_id
    assert attempts[0]["remote_upload_id"] == "expired-upload"
