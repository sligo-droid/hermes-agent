from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from hermes_cli.fork_defaults import FORK_DEFAULT_ADDITIONS
from plugins.client_knowledge_gbrain.gmail_api import (
    GmailClient,
    GmailHistoryExpired,
    GmailInvalidResponse,
    GmailMessageGone,
    GmailResponse,
)
from plugins.client_knowledge_gbrain.gmail_auth import (
    GMAIL_READONLY_SCOPE,
    GmailAuthError,
    GmailOAuth,
)
from plugins.client_knowledge_gbrain.gmail_mime import cleanup_attachments, parse_attachments, resolve_alias
from plugins.client_knowledge_gbrain.gmail_poller import GmailPoller, GmailSettings
from plugins.client_knowledge_gbrain.gmail_state import GmailState
from plugins.client_knowledge_gbrain.spool import RawSpool
from plugins.client_knowledge_gbrain.store import IntakeStore


FIXTURE = Path(__file__).parents[1] / "fixtures" / "client_knowledge_gmail" / "synthetic-message.eml"


def _config(tmp_path, *, enabled=True):
    return {
        "client_knowledge": {
            "gmail": {
                **FORK_DEFAULT_ADDITIONS["client_knowledge"]["gmail"],
                "enabled": enabled,
                "mailbox": "collector@example.invalid",
                "token_path": str(tmp_path / "private" / "gmail-token.json"),
                "max_message_bytes": 1_000_000,
                "max_http_response_bytes": 2_000_000,
                "mime_parser_memory_bytes": 512 * 1024 * 1024,
            }
        },
        "projects": {
            "pid": {"aliases": ["pid@example.invalid", "same-project@example.invalid"]},
            "other": {"aliases": ["conflicting@example.invalid"]},
        },
    }


def _settings(tmp_path):
    return GmailSettings.from_config(_config(tmp_path))


def _private_token(tmp_path, *, scopes=None):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "gmail-token.json"
    path.write_text(json.dumps({
        "client_id": "synthetic-client",
        "client_secret": "synthetic-secret",
        "refresh_token": "synthetic-refresh",
        "token_uri": "https://oauth2.googleapis.com/token",
        "token": "synthetic-access",
        "expiry": "2099-01-01T00:00:00+00:00",
        "scopes": scopes if scopes is not None else [GMAIL_READONLY_SCOPE],
    }), encoding="utf-8")
    path.chmod(0o600)
    return path


def _json_response(payload, *, date="Thu, 06 Aug 2026 12:00:00 GMT", status=200):
    return httpx.Response(status, json=payload, headers={"Date": date})


def test_defaults_are_disabled_present_only_and_bounded():
    defaults = FORK_DEFAULT_ADDITIONS["client_knowledge"]["gmail"]
    assert defaults["enabled"] is False
    assert defaults["historical_processing"] == "disabled"
    assert defaults["poll_interval_seconds"] == 120
    assert "cutover_history_id" not in defaults


def test_exact_readonly_oauth_refresh_and_introspection_use_httpx(tmp_path):
    path = _private_token(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["expiry"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(0o600)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/token":
            return httpx.Response(200, json={
                "access_token": "refreshed",
                "expires_in": 3600,
                "scope": GMAIL_READONLY_SCOPE,
            })
        return httpx.Response(200, json={"scope": GMAIL_READONLY_SCOPE})

    token = GmailOAuth(path, transport=httpx.MockTransport(handler)).access_token(now=2_000_000_000)
    assert token.value == "refreshed"
    assert [request.method for request in requests] == ["POST", "GET"]
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("scopes", [
    [],
    [GMAIL_READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.send"],
    ["https://www.googleapis.com/auth/gmail.modify"],
    ["https://mail.google.com/"],
])
def test_oauth_rejects_missing_or_broader_scope(tmp_path, scopes):
    path = _private_token(tmp_path, scopes=scopes)
    with pytest.raises(GmailAuthError, match="exactly"):
        GmailOAuth(path, transport=httpx.MockTransport(lambda _request: None)).access_token(now=10)


def test_oauth_rejects_introspected_audience_mismatch(tmp_path):
    path = _private_token(tmp_path)
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={
            "scope": GMAIL_READONLY_SCOPE,
            "aud": "different-client",
        })
    )
    with pytest.raises(GmailAuthError, match="audience"):
        GmailOAuth(path, transport=transport).access_token(now=10)


def test_failed_refreshed_scope_proof_does_not_persist_new_token(tmp_path):
    path = _private_token(tmp_path)
    original = json.loads(path.read_text(encoding="utf-8"))
    original["expiry"] = "2020-01-01T00:00:00+00:00"
    path.write_text(json.dumps(original), encoding="utf-8")
    path.chmod(0o600)

    def handler(request):
        if request.url.path == "/token":
            return httpx.Response(200, json={
                "access_token": "untrusted-new-token",
                "expires_in": 3600,
                "scope": GMAIL_READONLY_SCOPE,
            })
        return httpx.Response(200, json={
            "scope": "https://www.googleapis.com/auth/gmail.modify"
        })

    with pytest.raises(GmailAuthError, match="scope"):
        GmailOAuth(path, transport=httpx.MockTransport(handler)).access_token(now=2_000_000_000)
    assert json.loads(path.read_text(encoding="utf-8"))["token"] == "synthetic-access"


def test_alias_uses_first_matching_tier_and_conflicts_only_inside_that_tier():
    aliases = {
        "pid@example.invalid": "pid",
        "same-project@example.invalid": "pid",
        "conflicting@example.invalid": "other",
    }
    raw = FIXTURE.read_bytes()
    resolution = resolve_alias(raw, aliases, max_header_bytes=100_000)
    assert (resolution.project_key, resolution.tier, resolution.conflict) == (
        "pid", "Delivered-To", False,
    )

    conflict = (
        b"Delivered-To: pid@example.invalid\r\n"
        b"Delivered-To: conflicting@example.invalid\r\n"
        b"To: pid@example.invalid\r\n\r\nbody"
    )
    resolution = resolve_alias(conflict, aliases, max_header_bytes=1000)
    assert resolution.project_key == "unmapped"
    assert resolution.tier == "Delivered-To"
    assert resolution.conflict


def test_alias_case_exact_plus_and_unknown_behavior():
    aliases = {"pid@example.invalid": "pid"}
    assert resolve_alias(
        b"To: PID@EXAMPLE.INVALID\r\n\r\nbody", aliases, max_header_bytes=1000
    ).project_key == "pid"
    assert resolve_alias(
        b"To: pid+tag@example.invalid\r\n\r\nbody", aliases, max_header_bytes=1000
    ).project_key == "unmapped"
    assert resolve_alias(
        b"To: unknown@example.invalid\r\n\r\nbody", aliases, max_header_bytes=1000
    ).project_key == "unmapped"


def test_invalid_routing_headers_become_inspectable_needs_mapping(tmp_path):
    settings = _settings(tmp_path)
    raw = b"Delivered-To: pid@example.invalid\r\n" + b"x" * 400_000
    fake = _FakeGmail(
        history_pages=[_history_response(("bad-headers", "101"), final="102")],
        lists=_empty_reconciliation(settings),
        raw={"bad-headers": _raw_response("bad-headers", raw)},
    )
    poller, store, _spool = _initialized_poller(tmp_path, fake)
    assert poller.run_once()["cursor"] == "102"
    with store._connect() as conn:
        candidate = conn.execute(
            "SELECT disposition, error_class FROM gmail_history_candidates"
        ).fetchone()
        artifact = conn.execute("SELECT project_key FROM artifacts").fetchone()
    assert tuple(candidate) == ("needs_mapping", "gmail_routing_headers_invalid")
    assert artifact[0] == "unmapped"


def test_mime_worker_runs_after_preserved_fixture_and_extracts_attachment(tmp_path):
    spool = RawSpool(tmp_path / "private" / "raw")
    record = spool.put(
        provider_id="gmail",
        provider_artifact_id="collector@example.invalid:message:synthetic-1",
        source=[FIXTURE.read_bytes()],
    )
    attachments, error = parse_attachments(record.path, _settings(tmp_path).mime)
    try:
        assert error == ""
        assert len(attachments) == 1
        assert Path(attachments[0]["path"]).read_bytes() == b"synthetic attachment\n"
        assert attachments[0]["part_path"] == "1.2"
    finally:
        cleanup_attachments(attachments)


def test_actual_decoded_bytes_not_size_estimate_enforce_limit():
    raw = b"small valid message"
    encoded = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    assert GmailPoller._decode_raw(encoded, len(raw)) == raw
    with pytest.raises(OverflowError):
        GmailPoller._decode_raw(encoded, len(raw) - 1)


def test_first_start_uses_two_profile_bracket_and_processes_no_mail(tmp_path):
    calls = []
    responses = iter([
        _json_response({"emailAddress": "collector@example.invalid", "historyId": "100"}),
        _json_response(
            {"emailAddress": "collector@example.invalid", "historyId": "101"},
            date="Thu, 06 Aug 2026 12:00:01 GMT",
        ),
    ])

    def handler(request):
        calls.append(request.url.path)
        return next(responses)

    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    settings = _settings(tmp_path)
    with GmailClient("token", transport=httpx.MockTransport(handler)) as client:
        result = GmailPoller(store, spool, client, settings, now=lambda: 1786017601).run_once()
    assert result == {"mode": "initialized", "cursor": "101", "processed": 0}
    assert calls == ["/gmail/v1/users/me/profile", "/gmail/v1/users/me/profile"]
    state = GmailState(store).get_mailbox(settings.mailbox)
    assert state.cutover_history_id == state.cursor_history_id == "101"
    assert state.admit_after_server_ms == state.bracket_end_server_ms + 1000
    with store._write() as conn:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE gmail_mailboxes SET cutover_history_id='1' WHERE mailbox=?",
                (settings.mailbox,),
            )


def test_existing_poll_rejects_authenticated_mailbox_change(tmp_path):
    settings = _settings(tmp_path)
    fake = _FakeGmail(profile=GmailResponse(
        {"emailAddress": "other@example.invalid", "historyId": "200"},
        3_000,
        "7" * 64,
    ))
    poller, store, _spool = _initialized_poller(tmp_path, fake)
    with pytest.raises(ValueError, match="mailbox"):
        poller.run_once()
    assert GmailState(store).get_mailbox(settings.mailbox).cursor_history_id == "100"


def test_deterministic_concurrent_batch_creation_and_incomplete_manifest_rejected(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    state = GmailState(store)
    state.initialize_mailbox(
        "collector@example.invalid",
        cutover_history_id="100",
        bracket_start_server_ms=1000,
        bracket_end_server_ms=1000,
        admit_after_server_ms=2000,
    )

    def create(_):
        return state.get_or_create_batch(
            "collector@example.invalid", "100", config_hash="a" * 64, alias_count=1
        )["batch_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        batch_ids = list(pool.map(create, range(2)))
    assert batch_ids[0] == batch_ids[1]
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM gmail_batches").fetchone()[0] == 1
    with pytest.raises(ValueError, match="completion receipts"):
        state.finalize(batch_ids[0])


def test_page_registration_is_atomic_and_completion_rejects_incomplete_chain(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    state = GmailState(store)
    state.initialize_mailbox(
        "collector@example.invalid",
        cutover_history_id="100",
        bracket_start_server_ms=1000,
        bracket_end_server_ms=1000,
        admit_after_server_ms=2000,
    )
    batch = state.get_or_create_batch(
        "collector@example.invalid", "100", config_hash="b" * 64, alias_count=1
    )
    state.register_history_page(
        batch["batch_id"], page_ordinal=0, request_token="", next_token="next",
        response_history_id="101", candidates=[("message-1", "101")],
    )
    with pytest.raises(ValueError, match="not complete"):
        state.complete_history(batch["batch_id"])
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM gmail_history_completion").fetchone()[0] == 0


def test_history_and_recovery_targets_cannot_move_cursor_backward(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    state = GmailState(store)
    state.initialize_mailbox(
        "collector@example.invalid", cutover_history_id="100",
        bracket_start_server_ms=1000, bracket_end_server_ms=1000,
        admit_after_server_ms=2000,
    )
    batch = state.get_or_create_batch(
        "collector@example.invalid", "100", config_hash="5" * 64, alias_count=0
    )
    with pytest.raises(ValueError, match="backward"):
        state.register_history_page(
            batch["batch_id"], page_ordinal=0, request_token="", next_token="",
            response_history_id="99", candidates=[],
        )
    with pytest.raises(ValueError, match="backward"):
        state.complete_history_recovery(batch["batch_id"], "99")


def test_raw_response_message_identity_mismatch_is_retryable(tmp_path):
    settings = _settings(tmp_path)
    raw = b"Delivered-To: pid@example.invalid\r\n\r\nvalid"
    mismatched = _raw_response("other-message", raw)
    fake = _FakeGmail(
        history_pages=[_history_response(("message-1", "101"), final="102")],
        lists=_empty_reconciliation(settings),
        raw={"message-1": mismatched},
    )
    poller, store, _spool = _initialized_poller(tmp_path, fake)
    with pytest.raises(GmailInvalidResponse):
        poller.run_once()
    with store._connect() as conn:
        assert tuple(conn.execute(
            "SELECT disposition, error_class, invalid_count FROM gmail_history_candidates"
        ).fetchone()) == ("pending", "gmail_message_identity_mismatch", 1)


def test_reconciliation_registration_rolls_back_with_page_receipt(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    state = GmailState(store)
    state.initialize_mailbox(
        "collector@example.invalid",
        cutover_history_id="100",
        bracket_start_server_ms=1000,
        bracket_end_server_ms=1000,
        admit_after_server_ms=2000,
    )
    batch = state.get_or_create_batch(
        "collector@example.invalid", "100", config_hash="c" * 64, alias_count=1
    )
    state.register_history_page(
        batch["batch_id"], page_ordinal=0, request_token="", next_token="",
        response_history_id="101", candidates=[],
    )
    state.complete_history(batch["batch_id"])
    with store._write() as conn:
        conn.execute(
            "CREATE TRIGGER fail_reconcile_page BEFORE INSERT ON gmail_reconciliation_pages "
            "BEGIN SELECT RAISE(ABORT, 'crash'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        state.register_reconciliation_page(
            batch["batch_id"], alias_ordinal=0, page_ordinal=0, request_token="",
            next_token="", message_ids=["message-1"],
        )
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM gmail_reconciliation_observations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM gmail_batch_observations").fetchone()[0] == 0


def test_malformed_provider_raw_is_bounded_retry_then_valid_clears_state(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    state = GmailState(store)
    state.initialize_mailbox(
        "collector@example.invalid",
        cutover_history_id="100", bracket_start_server_ms=1000,
        bracket_end_server_ms=1000, admit_after_server_ms=2000,
    )
    batch = state.get_or_create_batch(
        "collector@example.invalid", "100", config_hash="d" * 64, alias_count=0
    )
    state.register_history_page(
        batch["batch_id"], page_ordinal=0, request_token="", next_token="",
        response_history_id="101", candidates=[("message-1", "101")],
    )
    state.complete_history(batch["batch_id"])
    item = state.pending_items(batch["batch_id"])[0]
    assert not state.record_invalid_raw(
        "candidate", item["candidate_id"], fingerprint="1" * 64,
        error_class="gmail_invalid_provider_raw", threshold=3,
    )
    state.record_raw(
        "candidate", item["candidate_id"], spool_key="a" * 64,
        storage_id="b" * 64, sha256="c" * 64, byte_size=1,
        message_history_id="101", internal_date_ms=3000,
    )
    with store._connect() as conn:
        row = conn.execute(
            "SELECT disposition, invalid_count FROM gmail_history_candidates"
        ).fetchone()
    assert tuple(row) == ("raw_preserved", 0)


def test_consistent_malformed_provider_raw_becomes_terminal_at_bound(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    state = GmailState(store)
    state.initialize_mailbox(
        "collector@example.invalid", cutover_history_id="100",
        bracket_start_server_ms=1000, bracket_end_server_ms=1000,
        admit_after_server_ms=2000,
    )
    batch = state.get_or_create_batch(
        "collector@example.invalid", "100", config_hash="9" * 64, alias_count=0
    )
    state.register_history_page(
        batch["batch_id"], page_ordinal=0, request_token="", next_token="",
        response_history_id="101", candidates=[("message-1", "101")],
    )
    state.complete_history(batch["batch_id"])
    item = state.pending_items(batch["batch_id"])[0]
    for expected_terminal in (False, False, True):
        assert state.record_invalid_raw(
            "candidate", item["candidate_id"], fingerprint="8" * 64,
            error_class="gmail_invalid_provider_raw", threshold=3,
        ) is expected_terminal
    assert state.pending_items(batch["batch_id"]) == []


def test_client_uses_only_get_and_never_format_minimal():
    requests = []

    def handler(request):
        requests.append(request)
        return _json_response({"id": "m", "historyId": "1", "internalDate": "1", "raw": "YQ"})

    with GmailClient("token", transport=httpx.MockTransport(handler)) as client:
        client.raw_message("m")
    assert requests[0].method == "GET"
    assert "format=raw" in str(requests[0].url)
    assert "minimal" not in str(requests[0].url)


class _FakeGmail:
    def __init__(self, *, history_pages=None, lists=None, raw=None, profile=None):
        self.history_pages = list(history_pages or [])
        self.lists = list(lists or [])
        self.raw = dict(raw or {})
        self.profile_response = profile or GmailResponse(
            {"emailAddress": "collector@example.invalid", "historyId": "200"},
            3_000,
            "f" * 64,
        )

    def profile(self):
        return self.profile_response

    def history(self, _cursor, _page_token=""):
        value = self.history_pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def list_messages(self, _query, _page_token=""):
        return self.lists.pop(0) if self.lists else GmailResponse({}, 3_000, "e" * 64)

    def raw_message(self, message_id):
        value = self.raw[message_id]
        if isinstance(value, list):
            value = value.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _initialized_poller(tmp_path, fake, *, cutover="100", admit_after=2_000):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    spool = RawSpool(tmp_path / "private" / "raw")
    settings = _settings(tmp_path)
    GmailState(store).initialize_mailbox(
        settings.mailbox,
        cutover_history_id=cutover,
        bracket_start_server_ms=1_000,
        bracket_end_server_ms=1_000,
        admit_after_server_ms=admit_after,
    )
    return GmailPoller(store, spool, fake, settings, now=lambda: 10), store, spool


def _raw_response(message_id, raw, *, history="101", internal_date=3_000, estimate=999_999_999):
    return GmailResponse(
        {
            "id": message_id,
            "historyId": history,
            "internalDate": str(internal_date),
            "sizeEstimate": estimate,
            "raw": base64.urlsafe_b64encode(raw).decode().rstrip("="),
        },
        3_000,
        hashlib.sha256(raw).hexdigest(),
    )


def _history_response(*messages, final="102"):
    history = [
        {"id": addition, "messagesAdded": [{"message": {"id": message}}]}
        for message, addition in messages
    ]
    return GmailResponse(
        {"history": history, "historyId": final}, 3_000, "a" * 64
    )


def _empty_reconciliation(settings):
    return [GmailResponse({}, 3_000, f"{index:064x}") for index in range(len(settings.aliases))]


def test_full_incremental_poll_preserves_parent_and_attachment_before_cursor(tmp_path):
    settings = _settings(tmp_path)
    raw = FIXTURE.read_bytes()
    fake = _FakeGmail(
        history_pages=[_history_response(("message-1", "101"), final="102")],
        lists=_empty_reconciliation(settings),
        raw={"message-1": _raw_response("message-1", raw)},
    )
    poller, store, spool = _initialized_poller(tmp_path, fake)

    result = poller.run_once()

    assert result["cursor"] == "102"
    state = GmailState(store).get_mailbox(settings.mailbox)
    assert state.cursor_history_id == "102"
    with store._connect() as conn:
        artifacts = conn.execute(
            "SELECT source_type, project_key, admission_state FROM artifacts ORDER BY source_type DESC"
        ).fetchall()
        jobs = conn.execute("SELECT stage, status FROM jobs").fetchall()
    assert [tuple(row) for row in artifacts] == [
        ("email", "pid", "complete"),
        ("attachment", "pid", "complete"),
    ]
    assert [tuple(row) for row in jobs] == [("notion_archived", "queued")]
    assert len(list(spool.root.glob("*/raw"))) == 2


def test_finalization_reverifies_parent_and_child_spool_before_cursor(tmp_path):
    settings = _settings(tmp_path)
    raw = FIXTURE.read_bytes()
    fake = _FakeGmail(
        history_pages=[_history_response(("message-1", "101"), final="102")],
        lists=_empty_reconciliation(settings),
        raw={"message-1": _raw_response("message-1", raw)},
    )
    poller, store, spool = _initialized_poller(tmp_path, fake)
    original_finalize = poller.state.finalize

    def tamper_then_finalize(batch_id, **kwargs):
        with store._connect() as conn:
            key = conn.execute(
                "SELECT spool_key FROM artifacts WHERE source_type='attachment'"
            ).fetchone()[0]
        spool.path_for_key(key).write_bytes(b"tampered")
        return original_finalize(batch_id, **kwargs)

    poller.state.finalize = tamper_then_finalize
    with pytest.raises(Exception, match="verification"):
        poller.run_once()
    assert GmailState(store).get_mailbox(settings.mailbox).cursor_history_id == "100"


def test_deleted_poison_candidate_is_terminal_and_does_not_deadlock_cursor(tmp_path):
    settings = _settings(tmp_path)
    fake = _FakeGmail(
        history_pages=[_history_response(("deleted", "101"), final="102")],
        lists=_empty_reconciliation(settings),
        raw={"deleted": GmailMessageGone("gone")},
    )
    poller, store, _spool = _initialized_poller(tmp_path, fake)

    assert poller.run_once()["cursor"] == "102"
    with store._connect() as conn:
        row = conn.execute(
            "SELECT disposition, error_class FROM gmail_history_candidates"
        ).fetchone()
        assert tuple(row) == ("gone", "gmail_message_gone")
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_history_404_recovery_rejects_pre_cutover_observation_and_advances_anchor(tmp_path):
    settings = _settings(tmp_path)
    old = b"Delivered-To: pid@example.invalid\r\n\r\nold"
    lists = [
        GmailResponse({"messages": [{"id": "old-message"}]}, 3_000, "b" * 64),
        *_empty_reconciliation(settings)[1:],
    ]
    fake = _FakeGmail(
        history_pages=[GmailHistoryExpired("expired")],
        lists=lists,
        raw={"old-message": _raw_response(
            "old-message", old, history="99", internal_date=500
        )},
        profile=GmailResponse(
            {"emailAddress": settings.mailbox, "historyId": "200"}, 3_000, "c" * 64
        ),
    )
    poller, store, _spool = _initialized_poller(tmp_path, fake)

    result = poller.run_once()

    assert result["mode"] == "history_404_recovery"
    assert result["cursor"] == "200"
    with store._connect() as conn:
        assert conn.execute(
            "SELECT disposition FROM gmail_reconciliation_observations"
        ).fetchone()[0] == "rejected_pre_cutover"
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_cutover_precision_window_is_terminal_without_admission(tmp_path):
    settings = _settings(tmp_path)
    raw = b"Delivered-To: pid@example.invalid\r\n\r\nprecision"
    fake = _FakeGmail(
        history_pages=[_history_response(("precision", "101"), final="102")],
        lists=_empty_reconciliation(settings),
        raw={"precision": _raw_response("precision", raw, internal_date=1_500)},
    )
    poller, store, _spool = _initialized_poller(tmp_path, fake, admit_after=2_000)

    assert poller.run_once()["cursor"] == "102"
    with store._connect() as conn:
        assert conn.execute(
            "SELECT disposition FROM gmail_history_candidates"
        ).fetchone()[0] == "rejected_cutover_precision"
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0


def test_transient_malformed_then_valid_reuses_batch_and_preserves_source(tmp_path):
    settings = _settings(tmp_path)
    raw = b"Delivered-To: pid@example.invalid\r\n\r\nvalid"
    malformed = GmailResponse(
        {"id": "message-1", "historyId": "101", "internalDate": "3000", "raw": "%%%"},
        3_000,
        "d" * 64,
    )
    fake = _FakeGmail(
        history_pages=[_history_response(("message-1", "101"), final="102")],
        lists=_empty_reconciliation(settings),
        raw={"message-1": [malformed, _raw_response("message-1", raw)]},
    )
    poller, store, _spool = _initialized_poller(tmp_path, fake)

    with pytest.raises(GmailInvalidResponse):
        poller.run_once()
    assert GmailState(store).get_mailbox(settings.mailbox).cursor_history_id == "100"
    with store._connect() as conn:
        first_batch = conn.execute("SELECT batch_id FROM gmail_batches").fetchone()[0]
        assert tuple(conn.execute(
            "SELECT invalid_count, disposition FROM gmail_history_candidates"
        ).fetchone()) == (1, "pending")

    result = poller.run_once()

    assert result["batch_id"] == first_batch
    assert result["cursor"] == "102"
    with store._connect() as conn:
        assert tuple(conn.execute(
            "SELECT invalid_count, disposition FROM gmail_history_candidates"
        ).fetchone()) == (0, "admitted")


def test_duplicate_candidate_occurrences_have_complete_page_receipts_and_one_adoption(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    state = GmailState(store)
    state.initialize_mailbox(
        "collector@example.invalid", cutover_history_id="100",
        bracket_start_server_ms=1000, bracket_end_server_ms=1000,
        admit_after_server_ms=2000,
    )
    batch = state.get_or_create_batch(
        "collector@example.invalid", "100", config_hash="e" * 64, alias_count=0
    )
    state.register_history_page(
        batch["batch_id"], page_ordinal=0, request_token="", next_token="next",
        response_history_id="101", candidates=[("message-1", "101")],
    )
    state.register_history_page(
        batch["batch_id"], page_ordinal=1, request_token="next", next_token="",
        response_history_id="102", candidates=[("message-1", "101")],
    )
    state.complete_history(batch["batch_id"])
    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM gmail_history_page_items").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM gmail_batch_candidates").fetchone()[0] == 1


def test_completed_empty_same_cursor_range_allows_next_deterministic_generation(tmp_path):
    store = IntakeStore(tmp_path / "private" / "intake.db")
    state = GmailState(store)
    state.initialize_mailbox(
        "collector@example.invalid", cutover_history_id="100",
        bracket_start_server_ms=1000, bracket_end_server_ms=1000,
        admit_after_server_ms=2000,
    )
    first = state.get_or_create_batch(
        "collector@example.invalid", "100", config_hash="6" * 64, alias_count=0
    )
    state.register_history_page(
        first["batch_id"], page_ordinal=0, request_token="", next_token="",
        response_history_id="100", candidates=[],
    )
    state.complete_history(first["batch_id"])
    state.complete_reconciliation(first["batch_id"], max_observations=1)
    assert state.finalize(first["batch_id"]) == "100"

    second = state.get_or_create_batch(
        "collector@example.invalid", "100", config_hash="6" * 64, alias_count=0
    )
    assert second["batch_id"] != first["batch_id"]
    assert second["generation"] == first["generation"] + 1
    with store._connect() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM gmail_batches WHERE mailbox='collector@example.invalid'"
        ).fetchone()[0] == 2
