"""Durable intake queue owned by the client-knowledge plugin.

This store is intentionally independent from Hermes' generic ledgers.  It is a
small SQLite state machine with explicit migrations, lease ownership, and
transactional stage/receipt transitions.  Raw content remains in ``spool.py``;
the database contains only bounded metadata and classified errors.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import socket
import sqlite3
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Iterable, Mapping

from hermes_constants import get_hermes_home
from hermes_cli.config import cfg_get, load_config

from .models import (
    UNMAPPED_PROJECT_KEY,
    ExternalReceipt,
    IntakeArtifact,
    StageReceipt,
    validate_stage,
)

if TYPE_CHECKING:
    from .spool import RawSpool, SpoolRecord


DEFAULT_DB_RELATIVE_PATH = "client-knowledge/intake.db"
CURRENT_SCHEMA_VERSION = 8
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 300.0
DEFAULT_BUSY_TIMEOUT_MS = 10_000
DEFAULT_STALE_HEARTBEAT_GRACE_SECONDS = 300.0

_LINEAR_STAGES = (
    "discovered",
    "raw_preserved",
    "notion_archived",
    "extracted",
    "interpreted",
    "assimilated",
    "honcho_projected",
    "complete",
)
_STAGE_PREDECESSOR = {
    stage: _LINEAR_STAGES[index - 1]
    for index, stage in enumerate(_LINEAR_STAGES)
    if index > 0
}
_BRANCH_STAGES = frozenset({"needs_mapping", "needs_review", "quarantined"})
_INITIAL_RECEIPT_STAGES = frozenset({"discovered", "raw_preserved"})
_BLOCKING_BRANCH_STAGES = frozenset({"needs_mapping", "needs_review"})

_JOB_STATUSES = frozenset(
    {"queued", "running", "succeeded", "failed", "quarantined", "operator_blocked"}
)
_ERROR_CLASS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]{0,119}$")
_CURSOR_NAME_RE = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class JobClaim:
    job_id: str
    artifact_id: str
    stage: str
    claim_token: str
    owner_pid: int
    owner_host: str
    lease_expires_at: float
    attempt_count: int


def resolve_store_path(config: Mapping[str, object] | None = None) -> Path:
    cfg = dict(config or load_config() or {})
    raw = cfg_get(cfg, "client_knowledge", "intake", "db_path", default="")
    if raw is None or not str(raw).strip():
        return get_hermes_home() / DEFAULT_DB_RELATIVE_PATH
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        raise ValueError("client_knowledge.intake.db_path must be absolute")
    return path


def _secure_parent(path: Path) -> None:
    parent = path.parent
    current = Path(parent.anchor) if parent.anchor else Path()
    parts = parent.parts[1:] if parent.is_absolute() else parent.parts
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("client knowledge database path may not contain symlinks")
        if current.exists():
            if not current.is_dir():
                raise ValueError("client knowledge database parent is not a directory")
            continue
        current.mkdir(mode=0o700)
        current.chmod(0o700)
    if parent.stat().st_mode & 0o077:
        raise ValueError("client knowledge database parent must not be group/world accessible")
    if parent.is_symlink():
        raise ValueError("client knowledge database parent may not be a symlink")


def _ensure_private_database(path: Path) -> None:
    if path.is_symlink():
        raise ValueError("client knowledge database may not be a symlink")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("client knowledge database must be a regular file")
    finally:
        os.close(fd)


def _owner_stamp() -> tuple[int, str, int | None]:
    pid = os.getpid()
    host = socket.gethostname()
    try:
        from gateway.status import get_process_start_time

        started = get_process_start_time(pid)
    except Exception:
        started = None
    return pid, host, started


def _pid_alive(pid: Any) -> bool:
    try:
        from gateway.status import _pid_exists

        return _pid_exists(int(pid))
    except (Exception, TypeError, ValueError):
        return False


def _process_start(pid: int) -> int | None:
    try:
        from gateway.status import get_process_start_time

        value = get_process_start_time(pid)
        return int(value) if value is not None else None
    except (Exception, ValueError, TypeError):
        return None


def _owner_is_live(
    owner_host: Any,
    owner_pid: Any,
    owner_started_at: Any,
    heartbeat_at: Any,
    *,
    now: float,
    heartbeat_grace_seconds: float,
) -> bool:
    """Conservatively identify a live owner, especially on this host."""
    if not _pid_alive(owner_pid):
        return False
    if str(owner_host or "") != socket.gethostname():
        # A remote host cannot be inspected; lease expiration is the recovery
        # boundary for it.
        return False
    try:
        heartbeat = float(heartbeat_at)
    except (TypeError, ValueError):
        return False
    if now - heartbeat > heartbeat_grace_seconds:
        return False
    current_start = _process_start(int(owner_pid))
    if current_start is None:
        # A live same-host PID is never stolen when process-start data is
        # unavailable. This is safer than duplicate processing.
        return True
    if owner_started_at is None:
        return True
    try:
        return int(owner_started_at) == current_start
    except (TypeError, ValueError):
        return True


def _error_class(value: Any) -> str:
    result = str(value or "unknown").strip()
    if not _ERROR_CLASS_RE.fullmatch(result):
        raise ValueError("error_class must be a canonical class identifier")
    return result


class IntakeStore:
    """Small testable SQLite store for intake artifacts and stage jobs."""

    def __init__(self, path: Path | str | None = None, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS):
        self.path = Path(path).expanduser() if path is not None else resolve_store_path()
        if not self.path.is_absolute():
            raise ValueError("client knowledge database path must be absolute")
        self.busy_timeout_ms = max(100, int(busy_timeout_ms))
        _secure_parent(self.path)
        self._migrate()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _connect(self) -> sqlite3.Connection:
        _secure_parent(self.path)
        _ensure_private_database(self.path)
        conn = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        journal_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            conn.close()
            raise RuntimeError("client knowledge intake requires SQLite WAL mode")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    @contextlib.contextmanager
    def _write(self):
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _migrate(self) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                raw_version = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                version = int(raw_version[0]) if raw_version else 0
                if version < 1:
                    statements = (
                    """CREATE TABLE IF NOT EXISTS artifacts (
                        artifact_id TEXT PRIMARY KEY,
                        project_key TEXT NOT NULL,
                        provider_id TEXT NOT NULL,
                        provider_artifact_id TEXT NOT NULL,
                        source_type TEXT NOT NULL,
                        parent_artifact_id TEXT,
                        provider_message_id TEXT NOT NULL,
                        provider_attachment_id TEXT,
                        occurred_at REAL NOT NULL,
                        actor_display TEXT,
                        actor_id TEXT,
                        delivered_alias TEXT,
                        original_filename TEXT,
                        mime_type TEXT NOT NULL,
                        source_url TEXT,
                        text_context TEXT,
                        provenance_json TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        byte_size INTEGER NOT NULL,
                        spool_key TEXT NOT NULL,
                        spool_storage_id TEXT NOT NULL DEFAULT '',
                        admission_state TEXT NOT NULL DEFAULT '',
                        received_at REAL NOT NULL,
                        FOREIGN KEY(parent_artifact_id) REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                        UNIQUE(provider_id, provider_artifact_id)
                    )""",
                    """CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                        stage TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        max_attempts INTEGER NOT NULL DEFAULT 3,
                        claim_token TEXT,
                        owner_pid INTEGER,
                        owner_host TEXT,
                        owner_started_at INTEGER,
                        lease_expires_at REAL,
                        heartbeat_at REAL,
                        next_retry_at REAL,
                        last_error_class TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL,
                        UNIQUE(artifact_id, stage)
                    )""",
                    "CREATE INDEX IF NOT EXISTS jobs_pickup_idx ON jobs(status, next_retry_at, updated_at)",
                    """CREATE TABLE IF NOT EXISTS stage_receipts (
                        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                        stage TEXT NOT NULL,
                        receipt_id TEXT NOT NULL,
                        output_sha256 TEXT,
                        recorded_at REAL NOT NULL,
                        PRIMARY KEY(artifact_id, stage)
                    )""",
                    """CREATE TABLE IF NOT EXISTS external_receipts (
                        provider_id TEXT NOT NULL,
                        external_id TEXT NOT NULL,
                        artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                        receipt_kind TEXT NOT NULL DEFAULT '',
                        recorded_at REAL NOT NULL,
                        PRIMARY KEY(provider_id, external_id)
                    )""",
                    """CREATE TABLE IF NOT EXISTS cursors (
                        cursor_name TEXT PRIMARY KEY,
                        cursor_value TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    )""",
                    )
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '1')"
                    )
                    version = 1
                if version < 2:
                    columns = {
                        str(row[1])
                        for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()
                    }
                    if "spool_storage_id" not in columns:
                        conn.execute(
                            "ALTER TABLE artifacts ADD COLUMN "
                            "spool_storage_id TEXT NOT NULL DEFAULT ''"
                        )
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) "
                        "VALUES('schema_version', '2')"
                    )
                    version = 2
                if version < 3:
                    columns = {
                        str(row[1])
                        for row in conn.execute("PRAGMA table_info(artifacts)").fetchall()
                    }
                    if "admission_state" not in columns:
                        conn.execute(
                            "ALTER TABLE artifacts ADD COLUMN "
                            "admission_state TEXT NOT NULL DEFAULT ''"
                        )
                    conn.execute(
                        "DELETE FROM jobs WHERE stage IN ('discovered','raw_preserved')"
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) "
                        "VALUES('schema_version', '3')"
                    )
                    version = 3
                if version < 4:
                    statements = (
                        """CREATE TABLE IF NOT EXISTS notion_operations (
                            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                            operation_kind TEXT NOT NULL,
                            state TEXT NOT NULL,
                            page_id TEXT,
                            block_id TEXT,
                            active_upload_attempt_id TEXT,
                            expected_sha256 TEXT NOT NULL,
                            expected_size INTEGER NOT NULL,
                            expected_mime_type TEXT NOT NULL,
                            last_job_id TEXT NOT NULL,
                            updated_at REAL NOT NULL,
                            PRIMARY KEY(artifact_id, operation_kind)
                        )""",
                        """CREATE TABLE IF NOT EXISTS notion_upload_attempts (
                            attempt_id TEXT PRIMARY KEY,
                            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                            operation_kind TEXT NOT NULL,
                            ordinal INTEGER NOT NULL,
                            replaces_attempt_id TEXT REFERENCES notion_upload_attempts(attempt_id),
                            replacement_reason TEXT,
                            marker_version TEXT NOT NULL,
                            opaque_marker TEXT NOT NULL UNIQUE,
                            remote_filename TEXT NOT NULL,
                            upload_mode TEXT NOT NULL,
                            expected_sha256 TEXT NOT NULL,
                            expected_size INTEGER NOT NULL,
                            expected_mime_type TEXT NOT NULL,
                            expected_part_count INTEGER NOT NULL,
                            expected_part_size INTEGER NOT NULL,
                            baseline_scan_id TEXT,
                            remote_upload_id TEXT UNIQUE,
                            created_by_job_id TEXT NOT NULL,
                            created_at REAL NOT NULL,
                            UNIQUE(artifact_id, operation_kind, ordinal)
                        )""",
                        """CREATE TABLE IF NOT EXISTS notion_upload_attempt_events (
                            event_id TEXT PRIMARY KEY,
                            attempt_id TEXT NOT NULL REFERENCES notion_upload_attempts(attempt_id) ON DELETE CASCADE,
                            sequence INTEGER NOT NULL,
                            event_type TEXT NOT NULL,
                            job_id TEXT NOT NULL,
                            claim_fingerprint TEXT NOT NULL,
                            remote_upload_id TEXT,
                            remote_state TEXT,
                            remote_parts_sent INTEGER,
                            page_id TEXT,
                            block_id TEXT,
                            evidence_identity TEXT,
                            classified_reason TEXT,
                            observed_at REAL NOT NULL,
                            UNIQUE(attempt_id, sequence)
                        )""",
                        """CREATE TABLE IF NOT EXISTS notion_upload_scans (
                            scan_id TEXT PRIMARY KEY,
                            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                            attempt_id TEXT NOT NULL REFERENCES notion_upload_attempts(attempt_id) ON DELETE CASCADE,
                            scan_role TEXT NOT NULL,
                            request_status TEXT NOT NULL,
                            page_count INTEGER NOT NULL,
                            result_count INTEGER NOT NULL,
                            completed_at REAL NOT NULL,
                            created_by_job_id TEXT NOT NULL
                        )""",
                        """CREATE TABLE IF NOT EXISTS notion_upload_scan_items (
                            scan_id TEXT NOT NULL REFERENCES notion_upload_scans(scan_id) ON DELETE CASCADE,
                            remote_upload_id TEXT NOT NULL,
                            opaque_marker TEXT NOT NULL,
                            remote_filename TEXT NOT NULL,
                            content_type TEXT,
                            content_length INTEGER,
                            status TEXT NOT NULL,
                            created_time TEXT,
                            expiry_time TEXT,
                            part_count_total INTEGER,
                            part_count_sent INTEGER,
                            PRIMARY KEY(scan_id, remote_upload_id)
                        )""",
                    )
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) "
                        "VALUES('schema_version', '4')"
                    )
                    version = 4
                if version < 5:
                    columns = {
                        str(row[1])
                        for row in conn.execute(
                            "PRAGMA table_info(notion_upload_attempts)"
                        ).fetchall()
                    }
                    if "expected_part_size" not in columns:
                        conn.execute(
                            "ALTER TABLE notion_upload_attempts ADD COLUMN "
                            "expected_part_size INTEGER NOT NULL DEFAULT 0"
                        )
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) "
                        "VALUES('schema_version', '5')"
                    )
                    version = 5
                if version < 6:
                    statements = (
                        """CREATE TABLE IF NOT EXISTS gmail_mailboxes (
                            mailbox TEXT PRIMARY KEY,
                            cutover_history_id TEXT NOT NULL,
                            cursor_history_id TEXT NOT NULL,
                            bracket_start_server_ms INTEGER NOT NULL,
                            bracket_end_server_ms INTEGER NOT NULL,
                            admit_after_server_ms INTEGER NOT NULL,
                            initialized_at REAL NOT NULL,
                            updated_at REAL NOT NULL
                        )""",
                        """CREATE TRIGGER IF NOT EXISTS gmail_mailbox_cutover_immutable
                        BEFORE UPDATE OF mailbox, cutover_history_id,
                            bracket_start_server_ms, bracket_end_server_ms,
                            admit_after_server_ms ON gmail_mailboxes
                        BEGIN SELECT RAISE(ABORT, 'gmail cutover is immutable'); END""",
                        """CREATE TRIGGER IF NOT EXISTS gmail_mailbox_delete_forbidden
                        BEFORE DELETE ON gmail_mailboxes
                        BEGIN SELECT RAISE(ABORT, 'gmail cutover is immutable'); END""",
                        """CREATE TABLE IF NOT EXISTS gmail_batches (
                            batch_id TEXT PRIMARY KEY,
                            mailbox TEXT NOT NULL REFERENCES gmail_mailboxes(mailbox),
                            expected_cursor TEXT NOT NULL,
                            generation INTEGER NOT NULL,
                            target_cursor TEXT,
                            mode TEXT NOT NULL DEFAULT 'incremental',
                            config_hash TEXT NOT NULL,
                            alias_count INTEGER NOT NULL,
                            status TEXT NOT NULL DEFAULT 'active',
                            recovery_reason TEXT,
                            created_at REAL NOT NULL,
                            committed_at REAL,
                            UNIQUE(mailbox, expected_cursor, generation)
                        )""",
                        """CREATE UNIQUE INDEX IF NOT EXISTS gmail_active_batch_unique
                        ON gmail_batches(mailbox, expected_cursor) WHERE status='active'""",
                        """CREATE TABLE IF NOT EXISTS gmail_history_pages (
                            batch_id TEXT NOT NULL REFERENCES gmail_batches(batch_id) ON DELETE CASCADE,
                            page_ordinal INTEGER NOT NULL,
                            request_token TEXT NOT NULL,
                            next_token TEXT NOT NULL,
                            response_history_id TEXT NOT NULL,
                            manifest_hash TEXT NOT NULL,
                            candidate_count INTEGER NOT NULL,
                            recorded_at REAL NOT NULL,
                            PRIMARY KEY(batch_id, page_ordinal)
                        )""",
                        """CREATE TABLE IF NOT EXISTS gmail_history_candidates (
                            candidate_id TEXT PRIMARY KEY,
                            mailbox TEXT NOT NULL,
                            message_id TEXT NOT NULL,
                            addition_history_id TEXT NOT NULL,
                            disposition TEXT NOT NULL DEFAULT 'pending',
                            error_class TEXT,
                            raw_spool_key TEXT,
                            raw_storage_id TEXT,
                            raw_sha256 TEXT,
                            raw_byte_size INTEGER,
                            message_history_id TEXT,
                            internal_date_ms INTEGER,
                            artifact_id TEXT REFERENCES artifacts(artifact_id),
                            invalid_fingerprint TEXT,
                            invalid_error_class TEXT,
                            invalid_count INTEGER NOT NULL DEFAULT 0,
                            updated_at REAL NOT NULL,
                            UNIQUE(mailbox, message_id, addition_history_id)
                        )""",
                        """CREATE TABLE IF NOT EXISTS gmail_batch_candidates (
                            batch_id TEXT NOT NULL REFERENCES gmail_batches(batch_id) ON DELETE CASCADE,
                            candidate_id TEXT NOT NULL REFERENCES gmail_history_candidates(candidate_id),
                            page_ordinal INTEGER NOT NULL,
                            discovered_ordinal INTEGER NOT NULL,
                            adopted_at REAL NOT NULL,
                            PRIMARY KEY(batch_id, candidate_id)
                        )""",
                        """CREATE TABLE IF NOT EXISTS gmail_history_page_items (
                            batch_id TEXT NOT NULL REFERENCES gmail_batches(batch_id) ON DELETE CASCADE,
                            page_ordinal INTEGER NOT NULL,
                            discovered_ordinal INTEGER NOT NULL,
                            candidate_id TEXT NOT NULL REFERENCES gmail_history_candidates(candidate_id),
                            PRIMARY KEY(batch_id, page_ordinal, discovered_ordinal)
                        )""",
                        """CREATE TABLE IF NOT EXISTS gmail_history_completion (
                            batch_id TEXT PRIMARY KEY REFERENCES gmail_batches(batch_id) ON DELETE CASCADE,
                            final_history_id TEXT NOT NULL,
                            page_count INTEGER NOT NULL,
                            candidate_count INTEGER NOT NULL,
                            chain_hash TEXT NOT NULL,
                            recovery_mode INTEGER NOT NULL DEFAULT 0,
                            completed_at REAL NOT NULL
                        )""",
                        """CREATE TABLE IF NOT EXISTS gmail_reconciliation_pages (
                            batch_id TEXT NOT NULL REFERENCES gmail_batches(batch_id) ON DELETE CASCADE,
                            alias_ordinal INTEGER NOT NULL,
                            page_ordinal INTEGER NOT NULL,
                            request_token TEXT NOT NULL,
                            next_token TEXT NOT NULL,
                            manifest_hash TEXT NOT NULL,
                            observation_count INTEGER NOT NULL,
                            recorded_at REAL NOT NULL,
                            PRIMARY KEY(batch_id, alias_ordinal, page_ordinal)
                        )""",
                        """CREATE TABLE IF NOT EXISTS gmail_reconciliation_observations (
                            observation_id TEXT PRIMARY KEY,
                            mailbox TEXT NOT NULL,
                            message_id TEXT NOT NULL,
                            anchor_history_id TEXT NOT NULL,
                            disposition TEXT NOT NULL DEFAULT 'pending',
                            error_class TEXT,
                            raw_spool_key TEXT,
                            raw_storage_id TEXT,
                            raw_sha256 TEXT,
                            raw_byte_size INTEGER,
                            message_history_id TEXT,
                            internal_date_ms INTEGER,
                            artifact_id TEXT REFERENCES artifacts(artifact_id),
                            invalid_fingerprint TEXT,
                            invalid_error_class TEXT,
                            invalid_count INTEGER NOT NULL DEFAULT 0,
                            updated_at REAL NOT NULL,
                            UNIQUE(mailbox, message_id, anchor_history_id)
                        )""",
                        """CREATE TABLE IF NOT EXISTS gmail_batch_observations (
                            batch_id TEXT NOT NULL REFERENCES gmail_batches(batch_id) ON DELETE CASCADE,
                            observation_id TEXT NOT NULL REFERENCES gmail_reconciliation_observations(observation_id),
                            alias_ordinal INTEGER NOT NULL,
                            page_ordinal INTEGER NOT NULL,
                            discovered_ordinal INTEGER NOT NULL,
                            adopted_at REAL NOT NULL,
                            PRIMARY KEY(batch_id, observation_id)
                        )""",
                        """CREATE TABLE IF NOT EXISTS gmail_reconciliation_page_items (
                            batch_id TEXT NOT NULL REFERENCES gmail_batches(batch_id) ON DELETE CASCADE,
                            alias_ordinal INTEGER NOT NULL,
                            page_ordinal INTEGER NOT NULL,
                            discovered_ordinal INTEGER NOT NULL,
                            observation_id TEXT NOT NULL REFERENCES gmail_reconciliation_observations(observation_id),
                            PRIMARY KEY(batch_id, alias_ordinal, page_ordinal, discovered_ordinal)
                        )""",
                        """CREATE TABLE IF NOT EXISTS gmail_reconciliation_completion (
                            batch_id TEXT PRIMARY KEY REFERENCES gmail_batches(batch_id) ON DELETE CASCADE,
                            alias_count INTEGER NOT NULL,
                            page_count INTEGER NOT NULL,
                            observation_count INTEGER NOT NULL,
                            chain_hash TEXT NOT NULL,
                            completed_at REAL NOT NULL
                        )""",
                    )
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) "
                        "VALUES('schema_version', '6')"
                    )
                    version = 6
                if version < 7:
                    statements = (
                        """CREATE TABLE IF NOT EXISTS extractions (
                            extraction_id TEXT PRIMARY KEY,
                            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                            source_sha256 TEXT NOT NULL,
                            source_manifest_sha256 TEXT NOT NULL,
                            extractor_version TEXT NOT NULL,
                            limits_version TEXT NOT NULL,
                            redaction_version TEXT NOT NULL,
                            status TEXT NOT NULL,
                            derived_storage_id TEXT NOT NULL,
                            derived_object_key TEXT NOT NULL,
                            output_sha256 TEXT NOT NULL,
                            output_bytes INTEGER NOT NULL,
                            output_characters INTEGER NOT NULL,
                            redaction_counts_json TEXT NOT NULL,
                            created_at REAL NOT NULL,
                            UNIQUE(artifact_id, source_manifest_sha256, extractor_version,
                                   limits_version, redaction_version)
                        )""",
                        """CREATE TABLE IF NOT EXISTS interpretation_envelopes (
                            envelope_id TEXT PRIMARY KEY,
                            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                            project_key TEXT NOT NULL,
                            source_sha256 TEXT NOT NULL,
                            extraction_id TEXT NOT NULL REFERENCES extractions(extraction_id),
                            extraction_sha256 TEXT NOT NULL,
                            envelope_version TEXT NOT NULL,
                            schema_version TEXT NOT NULL,
                            prompt_version TEXT NOT NULL,
                            task TEXT NOT NULL,
                            derived_storage_id TEXT NOT NULL,
                            derived_object_key TEXT NOT NULL,
                            output_sha256 TEXT NOT NULL,
                            output_bytes INTEGER NOT NULL,
                            created_at REAL NOT NULL,
                            UNIQUE(extraction_id, envelope_version, schema_version, prompt_version, task)
                        )""",
                        """CREATE TABLE IF NOT EXISTS interpretations (
                            interpretation_id TEXT PRIMARY KEY,
                            envelope_id TEXT NOT NULL UNIQUE REFERENCES interpretation_envelopes(envelope_id),
                            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                            extraction_id TEXT NOT NULL REFERENCES extractions(extraction_id),
                            schema_version TEXT NOT NULL,
                            prompt_version TEXT NOT NULL,
                            derived_storage_id TEXT NOT NULL,
                            derived_object_key TEXT NOT NULL,
                            output_sha256 TEXT NOT NULL,
                            output_bytes INTEGER NOT NULL,
                            actual_provider TEXT NOT NULL,
                            actual_model TEXT NOT NULL,
                            selected_provider TEXT NOT NULL,
                            selected_model TEXT NOT NULL,
                            model_tier TEXT NOT NULL,
                            route_fingerprint TEXT NOT NULL,
                            input_tokens INTEGER NOT NULL,
                            output_tokens INTEGER NOT NULL,
                            total_tokens INTEGER NOT NULL,
                            cache_read_tokens INTEGER NOT NULL,
                            cache_write_tokens INTEGER NOT NULL,
                            created_at REAL NOT NULL
                        )""",
                    )
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) "
                        "VALUES('schema_version', '7')"
                    )
                    version = 7
                if version < 8:
                    statements = (
                        """CREATE TABLE IF NOT EXISTS assimilation_proposals (
                            assimilation_id TEXT PRIMARY KEY,
                            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                            interpretation_id TEXT NOT NULL REFERENCES interpretations(interpretation_id),
                            assimilation_version TEXT NOT NULL,
                            schema_version TEXT NOT NULL,
                            prompt_version TEXT NOT NULL,
                            policy_version TEXT NOT NULL,
                            project_key TEXT NOT NULL,
                            proposal_sha256 TEXT NOT NULL,
                            derived_storage_id TEXT NOT NULL,
                            derived_object_key TEXT NOT NULL,
                            output_sha256 TEXT NOT NULL,
                            output_bytes INTEGER NOT NULL,
                            actual_provider TEXT NOT NULL,
                            actual_model TEXT NOT NULL,
                            selected_provider TEXT NOT NULL,
                            selected_model TEXT NOT NULL,
                            model_tier TEXT NOT NULL,
                            route_fingerprint TEXT NOT NULL,
                            review_required INTEGER NOT NULL,
                            review_reason TEXT NOT NULL,
                            base_git_head TEXT NOT NULL,
                            git_commit_sha TEXT,
                            sync_verified INTEGER NOT NULL DEFAULT 0,
                            created_at REAL NOT NULL,
                            UNIQUE(artifact_id, assimilation_version)
                        )""",
                        """CREATE TABLE IF NOT EXISTS client_knowledge_reviews (
                            review_id TEXT PRIMARY KEY,
                            assimilation_id TEXT NOT NULL UNIQUE REFERENCES assimilation_proposals(assimilation_id) ON DELETE CASCADE,
                            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                            project_key TEXT NOT NULL,
                            proposal_sha256 TEXT NOT NULL,
                            assimilation_version TEXT NOT NULL,
                            state TEXT NOT NULL,
                            reason_code TEXT NOT NULL,
                            notification_state TEXT NOT NULL DEFAULT 'pending',
                            notification_content_sha256 TEXT,
                            notification_message_id TEXT,
                            notification_guild_id TEXT,
                            notification_channel_id TEXT,
                            notification_role_id TEXT,
                            notification_marker TEXT,
                            reviewer_user_id TEXT,
                            reviewer_role_id TEXT,
                            decision_message_id TEXT,
                            decision_reason TEXT,
                            decided_at REAL,
                            created_at REAL NOT NULL,
                            updated_at REAL NOT NULL
                        )""",
                        """CREATE TABLE IF NOT EXISTS publication_transactions (
                            assimilation_id TEXT PRIMARY KEY REFERENCES assimilation_proposals(assimilation_id) ON DELETE CASCADE,
                            artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id) ON DELETE CASCADE,
                            assimilation_version TEXT NOT NULL,
                            proposal_sha256 TEXT NOT NULL,
                            branch_ref TEXT NOT NULL,
                            expected_head TEXT NOT NULL,
                            commit_sha TEXT,
                            state TEXT NOT NULL,
                            manifest_json TEXT NOT NULL,
                            error_class TEXT,
                            updated_at REAL NOT NULL
                        )""",
                        """CREATE TABLE IF NOT EXISTS honcho_projections (
                            projection_key TEXT PRIMARY KEY,
                            project_key TEXT NOT NULL,
                            page_slug TEXT NOT NULL,
                            page_sha256 TEXT NOT NULL,
                            marker TEXT NOT NULL UNIQUE,
                            exact_content TEXT NOT NULL,
                            conclusion_id TEXT,
                            obsolete_conclusion_id TEXT,
                            state TEXT NOT NULL,
                            updated_at REAL NOT NULL,
                            UNIQUE(project_key, page_slug)
                        )""",
                    )
                    for statement in statements:
                        conn.execute(statement)
                    conn.execute(
                        "INSERT OR REPLACE INTO schema_meta(key, value) "
                        "VALUES('schema_version', '8')"
                    )
                    version = 8
                if version != CURRENT_SCHEMA_VERSION:
                    raise RuntimeError(f"unsupported client knowledge schema version {version}")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        try:
            self.path.chmod(0o600)
            for sidecar in (Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
                if sidecar.exists() and not sidecar.is_symlink():
                    sidecar.chmod(0o600)
        except OSError:
            pass

    @staticmethod
    def _insert_artifact_locked(
        conn: sqlite3.Connection,
        artifact: IntakeArtifact,
        *,
        stages: Iterable[str],
        max_attempts: int,
        timestamp: float,
        spool_storage_id: str = "",
    ) -> str:
        attempts = max(1, int(max_attempts))
        stage_values = tuple(
            dict.fromkeys(validate_stage(stage) for stage in stages if str(stage).strip())
        )
        if artifact.parent_artifact_id:
            parent = conn.execute(
                "SELECT project_key, provider_id, provider_message_id FROM artifacts "
                "WHERE artifact_id=?",
                (artifact.parent_artifact_id,),
            ).fetchone()
            if parent is None:
                raise sqlite3.IntegrityError("parent artifact does not exist")
            if (
                parent[0] != artifact.project_key
                or parent[1] != artifact.provider_id
                or parent[2] != artifact.provider_message_id
            ):
                raise ValueError("attachment parent crosses project/provider identity")
        existing = conn.execute(
            "SELECT artifact_id, project_key, source_type, parent_artifact_id, "
            "provider_message_id, provider_attachment_id, occurred_at, actor_display, "
            "actor_id, delivered_alias, original_filename, mime_type, source_url, "
            "text_context, provenance_json, content_sha256, byte_size, spool_key, "
            "spool_storage_id, admission_state FROM artifacts "
            "WHERE provider_id=? AND provider_artifact_id=?",
            (artifact.provider_id, artifact.provider_artifact_id),
        ).fetchone()
        if existing is not None:
            if (
                existing[1] != artifact.project_key
                or existing[2] != artifact.source_type
                or (existing[3] or "") != artifact.parent_artifact_id
                or existing[4] != artifact.provider_message_id
                or (existing[5] or "") != artifact.provider_attachment_id
                or float(existing[6]) != artifact.occurred_at
                or (existing[7] or "") != artifact.actor_display
                or (existing[8] or "") != artifact.actor_id
                or (existing[9] or "") != artifact.delivered_alias
                or (existing[10] or "") != artifact.original_filename
                or existing[11] != artifact.mime_type
                or (existing[12] or "") != artifact.source_url
                or (existing[13] or "") != artifact.text_context
                or existing[14] != artifact.provenance_json
                or existing[15] != artifact.content_sha256
                or int(existing[16]) != artifact.byte_size
                or existing[17] != artifact.spool_key
                or (
                    spool_storage_id
                    and existing[18]
                    and existing[18] != spool_storage_id
                )
            ):
                raise ValueError("provider identity was reused with different artifact metadata")
            artifact_id = str(existing[0])
            if existing[1] == UNMAPPED_PROJECT_KEY and artifact.project_key != UNMAPPED_PROJECT_KEY:
                conn.execute(
                    "UPDATE artifacts SET project_key=? WHERE artifact_id=?",
                    (artifact.project_key, artifact_id),
                )
            if spool_storage_id and not existing[18]:
                conn.execute(
                    "UPDATE artifacts SET spool_storage_id=? WHERE artifact_id=?",
                    (spool_storage_id, artifact_id),
                )
        else:
            conn.execute(
                "INSERT INTO artifacts(artifact_id, project_key, provider_id, provider_artifact_id, "
                "source_type, parent_artifact_id, provider_message_id, provider_attachment_id, "
                "occurred_at, actor_display, actor_id, delivered_alias, original_filename, "
                "mime_type, source_url, text_context, provenance_json, content_sha256, byte_size, "
                "spool_key, spool_storage_id, admission_state, received_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    artifact.artifact_id,
                    artifact.project_key,
                    artifact.provider_id,
                    artifact.provider_artifact_id,
                    artifact.source_type,
                    artifact.parent_artifact_id or None,
                    artifact.provider_message_id,
                    artifact.provider_attachment_id or None,
                    artifact.occurred_at,
                    artifact.actor_display or None,
                    artifact.actor_id or None,
                    artifact.delivered_alias or None,
                    artifact.original_filename or None,
                    artifact.mime_type,
                    artifact.source_url or None,
                    artifact.text_context or None,
                    artifact.provenance_json,
                    artifact.content_sha256,
                    artifact.byte_size,
                    artifact.spool_key,
                    spool_storage_id,
                    "",
                    artifact.received_at,
                ),
            )
            artifact_id = artifact.artifact_id
        for stage in stage_values:
            conn.execute(
                "INSERT OR IGNORE INTO jobs(job_id, artifact_id, stage, status, max_attempts, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    secrets.token_hex(16),
                    artifact_id,
                    stage,
                    "queued",
                    attempts,
                    timestamp,
                    timestamp,
                ),
            )
        return artifact_id

    def insert_artifact(
        self,
        artifact: IntakeArtifact,
        *,
        stages: Iterable[str] = (),
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        now: float | None = None,
    ) -> str:
        """Insert metadata and initial stage jobs in one transaction."""
        timestamp = time.time() if now is None else float(now)
        stage_values = tuple(
            dict.fromkeys(validate_stage(stage) for stage in stages if str(stage).strip())
        )
        if _INITIAL_RECEIPT_STAGES.intersection(stage_values):
            raise ValueError("initial receipt stages are created by raw admission")
        if stage_values:
            raise ValueError("metadata insertion cannot queue processing stages")
        with self._write() as conn:
            return self._insert_artifact_locked(
                conn,
                artifact,
                stages=stage_values,
                max_attempts=max_attempts,
                timestamp=timestamp,
            )

    insert = insert_artifact

    def admit_raw_artifact(
        self,
        spool: "RawSpool",
        artifact: IntakeArtifact,
        source: BinaryIO | Iterable[bytes],
        *,
        next_stages: Iterable[str] = (),
        defer_stages: bool = False,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        now: float | None = None,
    ) -> "SpoolRecord":
        """Durably preserve raw bytes, then publish initial DB receipts.

        The filesystem cannot share SQLite's transaction, so the verified raw
        object is written first. A crash may leave an orphaned object that a
        retry can safely adopt, but the database never points at missing bytes.
        """
        timestamp = time.time() if now is None else float(now)
        stages = tuple(
            dict.fromkeys(
                validate_stage(stage)
                for stage in next_stages
                if str(stage).strip()
            )
        )
        if _INITIAL_RECEIPT_STAGES.intersection(stages):
            raise ValueError("initial receipt stages cannot be queued as downstream work")
        if artifact.project_key == UNMAPPED_PROJECT_KEY:
            if not stages and (defer_stages or artifact.source_type == "attachment"):
                pass
            elif set(stages) != {"needs_mapping"}:
                raise ValueError("unmapped artifacts may only queue needs_mapping")
        elif "needs_mapping" in stages:
            raise ValueError("needs_mapping requires the unmapped project key")
        completed_storage_id = ""
        with self._write() as conn:
            self._insert_artifact_locked(
                conn,
                artifact,
                stages=(),
                max_attempts=max_attempts,
                timestamp=timestamp,
                spool_storage_id=spool.storage_id,
            )
            state = conn.execute(
                "SELECT admission_state, spool_storage_id FROM artifacts WHERE artifact_id=?",
                (artifact.artifact_id,),
            ).fetchone()
            if state is None:
                raise ValueError("artifact admission reservation was lost")
            if state[0] == "complete":
                completed_storage_id = str(state[1] or "")
                self._validate_initial_receipts_locked(conn, artifact)
            else:
                conn.execute(
                    "UPDATE artifacts SET admission_state='writing' WHERE artifact_id=?",
                    (artifact.artifact_id,),
                )
        if completed_storage_id:
            return spool.verify(
                artifact.spool_key,
                storage_id=completed_storage_id,
                expected_sha256=artifact.content_sha256,
                expected_size=artifact.byte_size,
            )
        receipt = spool.preserve_artifact(artifact, source)
        with self._write() as conn:
            state = conn.execute(
                "SELECT admission_state FROM artifacts WHERE artifact_id=?",
                (artifact.artifact_id,),
            ).fetchone()
            if state is None:
                raise ValueError("artifact admission reservation was lost")
            if state[0] == "complete":
                self._validate_initial_receipts_locked(conn, artifact)
                return receipt
            if state[0] != "writing":
                raise ValueError("artifact admission reservation was lost")
            conn.execute(
                "INSERT OR IGNORE INTO stage_receipts"
                "(artifact_id, stage, receipt_id, output_sha256, recorded_at) "
                "VALUES(?,?,?,?,?)",
                (
                    artifact.artifact_id,
                    "discovered",
                    f"discovered:{artifact.artifact_id}",
                    artifact.content_sha256,
                    timestamp,
                ),
            )
            existing = conn.execute(
                "SELECT receipt_id, output_sha256 FROM stage_receipts "
                "WHERE artifact_id=? AND stage='raw_preserved'",
                (artifact.artifact_id,),
            ).fetchone()
            raw_receipt_id = f"spool:{receipt.storage_key}"
            if existing is not None and (
                existing[0] != raw_receipt_id
                or (existing[1] or "") != receipt.sha256
            ):
                raise ValueError("raw preservation receipt conflicts with existing metadata")
            conn.execute(
                "INSERT OR IGNORE INTO stage_receipts"
                "(artifact_id, stage, receipt_id, output_sha256, recorded_at) "
                "VALUES(?,?,?,?,?)",
                (
                    artifact.artifact_id,
                    "raw_preserved",
                    raw_receipt_id,
                    receipt.sha256,
                    timestamp,
                ),
            )
            for stage in stages:
                conn.execute(
                    "INSERT OR IGNORE INTO jobs"
                    "(job_id, artifact_id, stage, status, max_attempts, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        secrets.token_hex(16),
                        artifact.artifact_id,
                        stage,
                        "queued",
                        max(1, int(max_attempts)),
                        timestamp,
                        timestamp,
                    ),
                )
            conn.execute(
                "UPDATE artifacts SET admission_state='complete' WHERE artifact_id=?",
                (artifact.artifact_id,),
            )
        return receipt

    @staticmethod
    def _validate_initial_receipts_locked(
        conn: sqlite3.Connection,
        artifact: IntakeArtifact,
    ) -> None:
        rows = {
            str(row[0]): (str(row[1]), str(row[2] or ""))
            for row in conn.execute(
                "SELECT stage, receipt_id, output_sha256 FROM stage_receipts "
                "WHERE artifact_id=? AND stage IN ('discovered','raw_preserved')",
                (artifact.artifact_id,),
            ).fetchall()
        }
        expected = {
            "discovered": (
                f"discovered:{artifact.artifact_id}",
                artifact.content_sha256,
            ),
            "raw_preserved": (
                f"spool:{artifact.spool_key}",
                artifact.content_sha256,
            ),
        }
        if rows != expected:
            raise ValueError("completed artifact has invalid initial receipts")

    def resolve_mapping(
        self,
        artifact_id: str,
        project_key: str,
        claim_token: str,
        *,
        next_stages: Iterable[str] = (),
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        now: float | None = None,
    ) -> str:
        """Resolve one previously unmapped identity without changing its bytes."""
        from .scope import validate_project_key

        project = validate_project_key(project_key)
        if project == UNMAPPED_PROJECT_KEY:
            raise ValueError("mapping resolution requires a concrete project key")
        timestamp = time.time() if now is None else float(now)
        stages = tuple(
            dict.fromkeys(validate_stage(stage) for stage in next_stages if str(stage).strip())
        )
        if _INITIAL_RECEIPT_STAGES.intersection(stages) or "needs_mapping" in stages:
            raise ValueError("mapping resolution may only queue downstream stages")
        with self._write() as conn:
            row = conn.execute(
                "SELECT artifacts.project_key, artifacts.content_sha256 "
                "FROM artifacts JOIN jobs ON jobs.artifact_id=artifacts.artifact_id "
                "WHERE artifacts.artifact_id=? AND jobs.stage='needs_mapping' "
                "AND jobs.status='running' AND jobs.claim_token=?",
                (artifact_id, claim_token),
            ).fetchone()
            if row is None or row[0] != UNMAPPED_PROJECT_KEY:
                raise ValueError("mapping claim is not active for an unmapped artifact")
            parent = conn.execute(
                "SELECT parent_artifact_id FROM artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if parent is not None and parent[0]:
                parent_project = conn.execute(
                    "SELECT project_key FROM artifacts WHERE artifact_id=?",
                    (parent[0],),
                ).fetchone()
                if parent_project is None or parent_project[0] != project:
                    raise ValueError("attachment mapping must match its parent project")
            conn.execute(
                "UPDATE artifacts SET project_key=? WHERE artifact_id=?",
                (project, artifact_id),
            )
            conn.execute(
                "UPDATE artifacts SET project_key=? WHERE parent_artifact_id=? "
                "AND project_key=?",
                (project, artifact_id, UNMAPPED_PROJECT_KEY),
            )
            conn.execute(
                "UPDATE jobs SET status='succeeded', claim_token=NULL, owner_pid=NULL, "
                "owner_host=NULL, owner_started_at=NULL, lease_expires_at=NULL, "
                "heartbeat_at=NULL, updated_at=? WHERE artifact_id=? "
                "AND stage='needs_mapping' AND status='running' AND claim_token=?",
                (timestamp, artifact_id, claim_token),
            )
            conn.execute(
                "INSERT OR IGNORE INTO stage_receipts"
                "(artifact_id, stage, receipt_id, output_sha256, recorded_at) "
                "VALUES(?,?,?,?,?)",
                (
                    artifact_id,
                    "needs_mapping",
                    f"mapping:{project}",
                    row[1],
                    timestamp,
                ),
            )
            for stage in stages:
                conn.execute(
                    "INSERT OR IGNORE INTO jobs"
                    "(job_id, artifact_id, stage, status, max_attempts, created_at, updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        secrets.token_hex(16), artifact_id, stage, "queued",
                        max(1, int(max_attempts)), timestamp, timestamp,
                    ),
                )
            return artifact_id

    def add_job(self, artifact_id: str, stage: str, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> str:
        stage = validate_stage(stage)
        if stage in _INITIAL_RECEIPT_STAGES:
            raise ValueError("initial receipt stages are created by raw admission")
        now = time.time()
        job_id = secrets.token_hex(16)
        with self._write() as conn:
            artifact = conn.execute(
                "SELECT project_key FROM artifacts WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
            if artifact is None:
                raise sqlite3.IntegrityError("artifact does not exist")
            if artifact[0] == UNMAPPED_PROJECT_KEY and stage != "needs_mapping":
                raise ValueError("unmapped artifacts may only queue needs_mapping")
            if artifact[0] != UNMAPPED_PROJECT_KEY and stage == "needs_mapping":
                raise ValueError("needs_mapping requires an unmapped artifact")
            conn.execute(
                "INSERT INTO jobs(job_id, artifact_id, stage, status, max_attempts, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (job_id, artifact_id, stage, "queued", max(1, int(max_attempts)), now, now),
            )
        return job_id

    def ensure_job(
        self, artifact_id: str, stage: str, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS
    ) -> str:
        """Return the one durable stage job, creating it when absent."""
        stage = validate_stage(stage)
        if stage in _INITIAL_RECEIPT_STAGES:
            raise ValueError("initial receipt stages are created by raw admission")
        now = time.time()
        with self._write() as conn:
            artifact = conn.execute(
                "SELECT project_key FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
            if artifact is None:
                raise sqlite3.IntegrityError("artifact does not exist")
            if artifact[0] == UNMAPPED_PROJECT_KEY and stage != "needs_mapping":
                raise ValueError("unmapped artifacts may only queue needs_mapping")
            if artifact[0] != UNMAPPED_PROJECT_KEY and stage == "needs_mapping":
                raise ValueError("needs_mapping requires an unmapped artifact")
            conn.execute(
                "INSERT OR IGNORE INTO jobs(job_id, artifact_id, stage, status, max_attempts, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (secrets.token_hex(16), artifact_id, stage, "queued", max(1, int(max_attempts)), now, now),
            )
            row = conn.execute(
                "SELECT job_id FROM jobs WHERE artifact_id=? AND stage=?", (artifact_id, stage)
            ).fetchone()
            if row is None:
                raise RuntimeError("intake stage job creation failed")
            return str(row[0])

    def claim_next(
        self,
        *,
        stage: str | None = None,
        spool: "RawSpool | None" = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        now: float | None = None,
    ) -> JobClaim | None:
        timestamp = time.time() if now is None else float(now)
        lease = max(1.0, float(lease_seconds))
        if stage is not None:
            stage = validate_stage(stage)
        pid, host, started = _owner_stamp()
        params: list[Any] = [timestamp]
        query = (
            "SELECT jobs.job_id, jobs.artifact_id, jobs.stage, jobs.attempt_count, "
            "jobs.max_attempts, artifacts.spool_key, artifacts.spool_storage_id, "
            "artifacts.content_sha256, artifacts.byte_size FROM jobs "
            "JOIN artifacts ON artifacts.artifact_id=jobs.artifact_id "
            "WHERE status IN ('queued','failed') AND attempt_count<max_attempts "
            "AND (next_retry_at IS NULL OR next_retry_at<=?) "
            "AND NOT EXISTS ("
            "SELECT 1 FROM jobs blocker WHERE blocker.artifact_id=jobs.artifact_id "
            "AND blocker.stage IN ('needs_mapping','needs_review') "
            "AND blocker.status!='succeeded' AND blocker.job_id!=jobs.job_id"
            ") "
            "AND ("
            "stage IN ('discovered','quarantined') "
            "OR EXISTS ("
            "SELECT 1 FROM stage_receipts predecessor "
            "WHERE predecessor.artifact_id=jobs.artifact_id "
            "AND predecessor.stage=CASE jobs.stage "
            "WHEN 'raw_preserved' THEN 'discovered' "
            "WHEN 'notion_archived' THEN 'raw_preserved' "
            "WHEN 'extracted' THEN 'notion_archived' "
            "WHEN 'interpreted' THEN 'extracted' "
            "WHEN 'assimilated' THEN 'interpreted' "
            "WHEN 'honcho_projected' THEN 'assimilated' "
            "WHEN 'complete' THEN 'honcho_projected' END"
            ")"
            "OR (stage='needs_mapping' AND EXISTS ("
            "SELECT 1 FROM stage_receipts predecessor "
            "WHERE predecessor.artifact_id=jobs.artifact_id "
            "AND predecessor.stage='discovered'))"
            "OR (stage='needs_review' AND EXISTS ("
            "SELECT 1 FROM stage_receipts predecessor "
            "WHERE predecessor.artifact_id=jobs.artifact_id "
            "AND predecessor.stage='interpreted'))"
            ")"
        )
        if stage:
            query += " AND stage=?"
            params.append(stage)
        query += " ORDER BY created_at, job_id"
        with self._connect() as conn:
            candidates = conn.execute(query, params).fetchall()
        verified_job_id = ""
        rejected_job_ids: list[str] = []
        adopted_storage_id = ""
        for candidate in candidates:
            if candidate[2] not in {"discovered", "raw_preserved", "needs_mapping", "quarantined"}:
                if spool is None:
                    raise ValueError("downstream claims require the configured raw spool")
                try:
                    spool.verify(
                        str(candidate[5]),
                        storage_id=str(candidate[6] or ""),
                        expected_sha256=str(candidate[7]),
                        expected_size=int(candidate[8]),
                    )
                except Exception as exc:
                    from .spool import RawSpoolRootMismatch

                    if isinstance(exc, RawSpoolRootMismatch):
                        raise
                    rejected_job_ids.append(str(candidate[0]))
                    continue
                if not candidate[6]:
                    adopted_storage_id = spool.storage_id
            verified_job_id = str(candidate[0])
            break
        with self._write() as conn:
            self._recover_stale_locked(
                conn,
                timestamp,
                heartbeat_grace_seconds=DEFAULT_STALE_HEARTBEAT_GRACE_SECONDS,
            )
            conn.execute(
                "UPDATE jobs SET status='quarantined', next_retry_at=NULL, "
                "last_error_class='retry_exhausted', updated_at=? "
                "WHERE status IN ('queued','failed') AND attempt_count>=max_attempts",
                (timestamp,),
            )
            for job_id in rejected_job_ids:
                conn.execute(
                    "UPDATE jobs SET status='quarantined', last_error_class='raw_verification_failed', "
                    "next_retry_at=NULL, updated_at=? WHERE job_id=? "
                    "AND status IN ('queued','failed')",
                    (timestamp, job_id),
                )
            row = None
            if verified_job_id:
                row = conn.execute(
                    query.replace(" ORDER BY created_at, job_id", "") + " AND jobs.job_id=?",
                    [*params, verified_job_id],
                ).fetchone()
            if row is None:
                return None
            if adopted_storage_id:
                conn.execute(
                    "UPDATE artifacts SET spool_storage_id=? "
                    "WHERE artifact_id=? AND spool_storage_id=''",
                    (adopted_storage_id, row[1]),
                )
            token = secrets.token_urlsafe(24)
            attempt = int(row[3]) + 1
            expires = timestamp + lease
            cursor = conn.execute(
                "UPDATE jobs SET status='running', attempt_count=?, claim_token=?, owner_pid=?, owner_host=?, "
                "owner_started_at=?, lease_expires_at=?, heartbeat_at=?, updated_at=?, next_retry_at=NULL "
                "WHERE job_id=? AND status IN ('queued','failed')",
                (attempt, token, pid, host, started, expires, timestamp, timestamp, row[0]),
            )
            if cursor.rowcount != 1:
                return None
            return JobClaim(
                job_id=str(row[0]),
                artifact_id=str(row[1]),
                stage=str(row[2]),
                claim_token=token,
                owner_pid=pid,
                owner_host=host,
                lease_expires_at=expires,
                attempt_count=attempt,
            )

    claim = claim_next

    def heartbeat(self, job_id: str, claim_token: str, *, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> bool:
        now = time.time()
        expires = now + max(1.0, float(lease_seconds))
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET heartbeat_at=?, lease_expires_at=?, updated_at=? "
                "WHERE job_id=? AND status='running' AND claim_token=?",
                (now, expires, now, job_id, claim_token),
            )
            return cursor.rowcount == 1

    heartbeat_job = heartbeat

    @staticmethod
    def _claim_fingerprint(job_id: str, claim_token: str) -> str:
        import hashlib

        return hashlib.sha256(f"{job_id}\0{claim_token}".encode()).hexdigest()

    @staticmethod
    def _active_notion_claim_locked(
        conn: sqlite3.Connection,
        job_id: str,
        claim_token: str,
        artifact_id: str,
        *,
        now: float,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT job_id, artifact_id, stage, lease_expires_at FROM jobs "
            "WHERE job_id=? AND artifact_id=? AND stage='notion_archived' "
            "AND status='running' AND claim_token=?",
            (job_id, artifact_id, claim_token),
        ).fetchone()
        if row is None or row[3] is None or float(row[3]) <= now:
            raise PermissionError("notion job claim is no longer active")
        return row

    def renew_notion_claim(
        self,
        job_id: str,
        claim_token: str,
        artifact_id: str,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> bool:
        now = time.time()
        expires = now + max(1.0, float(lease_seconds))
        with self._write() as conn:
            self._active_notion_claim_locked(
                conn, job_id, claim_token, artifact_id, now=now
            )
            cursor = conn.execute(
                "UPDATE jobs SET heartbeat_at=?, lease_expires_at=?, updated_at=? "
                "WHERE job_id=? AND artifact_id=? AND stage='notion_archived' "
                "AND status='running' AND claim_token=?",
                (now, expires, now, job_id, artifact_id, claim_token),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _artifact_from_row(row: sqlite3.Row) -> IntakeArtifact:
        return IntakeArtifact(
            artifact_id=row["artifact_id"],
            project_key=row["project_key"],
            provider_id=row["provider_id"],
            provider_artifact_id=row["provider_artifact_id"],
            source_type=row["source_type"],
            parent_artifact_id=row["parent_artifact_id"] or "",
            provider_message_id=row["provider_message_id"],
            provider_attachment_id=row["provider_attachment_id"] or "",
            occurred_at=float(row["occurred_at"]),
            actor_display=row["actor_display"] or "",
            actor_id=row["actor_id"] or "",
            delivered_alias=row["delivered_alias"] or "",
            original_filename=row["original_filename"] or "",
            mime_type=row["mime_type"],
            source_url=row["source_url"] or "",
            text_context=row["text_context"] or "",
            provenance_json=row["provenance_json"],
            content_sha256=row["content_sha256"],
            byte_size=int(row["byte_size"]),
            spool_key=row["spool_key"],
            received_at=float(row["received_at"]),
        )

    def get_artifact_for_notion_claim(
        self, job_id: str, claim_token: str, artifact_id: str
    ) -> IntakeArtifact:
        now = time.time()
        with self._connect() as conn:
            self._active_notion_claim_locked(
                conn, job_id, claim_token, artifact_id, now=now
            )
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise ValueError("artifact does not exist")
            return self._artifact_from_row(row)

    @staticmethod
    def _active_stage_claim_locked(
        conn: sqlite3.Connection, claim: JobClaim, stage: str, *, now: float
    ) -> None:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE job_id=? AND artifact_id=? AND stage=? "
            "AND status='running' AND claim_token=? AND lease_expires_at>?",
            (claim.job_id, claim.artifact_id, stage, claim.claim_token, now),
        ).fetchone()
        if row is None:
            raise PermissionError(f"{stage} job claim is no longer active")

    def get_artifact_family_for_claim(
        self, claim: JobClaim
    ) -> tuple[IntakeArtifact, list[IntakeArtifact]]:
        now = time.time()
        with self._connect() as conn:
            self._active_stage_claim_locked(conn, claim, claim.stage, now=now)
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (claim.artifact_id,)
            ).fetchone()
            if row is None:
                raise ValueError("artifact does not exist")
            children = conn.execute(
                "SELECT * FROM artifacts WHERE parent_artifact_id=? "
                "ORDER BY provider_attachment_id, artifact_id",
                (claim.artifact_id,),
            ).fetchall()
            return self._artifact_from_row(row), [self._artifact_from_row(item) for item in children]

    def get_extraction(self, extraction_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM extractions WHERE extraction_id=?", (extraction_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_extraction_for_interpretation_claim(
        self, claim: JobClaim
    ) -> tuple[IntakeArtifact, dict[str, Any]]:
        now = time.time()
        with self._connect() as conn:
            self._active_stage_claim_locked(conn, claim, "interpreted", now=now)
            artifact = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (claim.artifact_id,)
            ).fetchone()
            receipt = conn.execute(
                "SELECT receipt_id FROM stage_receipts WHERE artifact_id=? AND stage='extracted'",
                (claim.artifact_id,),
            ).fetchone()
            if artifact is None or receipt is None or not str(receipt[0]).startswith("extraction:"):
                raise ValueError("interpretation claim lacks a versioned extraction receipt")
            extraction_id = str(receipt[0]).split(":", 1)[1]
            extraction = conn.execute(
                "SELECT * FROM extractions WHERE extraction_id=?", (extraction_id,)
            ).fetchone()
            if extraction is None:
                raise ValueError("versioned extraction metadata is missing")
            return self._artifact_from_row(artifact), dict(extraction)

    @staticmethod
    def _complete_claim_locked(
        conn: sqlite3.Connection,
        claim: JobClaim,
        receipt: StageReceipt,
        *,
        now: float,
        next_stage: str = "",
    ) -> None:
        IntakeStore._active_stage_claim_locked(conn, claim, receipt.stage, now=now)
        existing = conn.execute(
            "SELECT receipt_id, output_sha256 FROM stage_receipts WHERE artifact_id=? AND stage=?",
            (receipt.artifact_id, receipt.stage),
        ).fetchone()
        if existing and (existing[0], existing[1] or "") != (receipt.receipt_id, receipt.output_sha256):
            raise ValueError("stage receipt conflicts with immutable metadata")
        conn.execute(
            "INSERT OR IGNORE INTO stage_receipts"
            "(artifact_id, stage, receipt_id, output_sha256, recorded_at) VALUES(?,?,?,?,?)",
            (receipt.artifact_id, receipt.stage, receipt.receipt_id, receipt.output_sha256, receipt.recorded_at),
        )
        conn.execute(
            "UPDATE jobs SET status='succeeded', claim_token=NULL, owner_pid=NULL, owner_host=NULL, "
            "owner_started_at=NULL, lease_expires_at=NULL, heartbeat_at=NULL, next_retry_at=NULL, "
            "last_error_class=NULL, updated_at=? WHERE job_id=? AND claim_token=?",
            (now, claim.job_id, claim.claim_token),
        )
        if next_stage:
            conn.execute(
                "INSERT OR IGNORE INTO jobs(job_id, artifact_id, stage, status, max_attempts, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (secrets.token_hex(16), claim.artifact_id, next_stage, "queued", DEFAULT_MAX_ATTEMPTS, now, now),
            )

    def complete_extraction(self, claim: JobClaim, row: Mapping[str, Any]) -> None:
        now = time.time()
        receipt = StageReceipt(
            claim.artifact_id,
            "extracted",
            f"extraction:{row['extraction_id']}",
            str(row["output_sha256"]),
            recorded_at=now,
        )
        with self._write() as conn:
            self._active_stage_claim_locked(conn, claim, "extracted", now=now)
            existing = conn.execute(
                "SELECT * FROM extractions WHERE extraction_id=?", (row["extraction_id"],)
            ).fetchone()
            values = (
                row["extraction_id"], row["artifact_id"], row["source_sha256"],
                row["source_manifest_sha256"], row["extractor_version"], row["limits_version"],
                row["redaction_version"], row["status"], row["derived_storage_id"],
                row["derived_object_key"], row["output_sha256"], int(row["output_bytes"]),
                int(row["output_characters"]), row["redaction_counts_json"], now,
            )
            if existing is None:
                conn.execute(
                    "INSERT INTO extractions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", values
                )
            else:
                columns = tuple(existing.keys())[:-1]
                if tuple(existing[name] for name in columns) != values[:-1]:
                    raise ValueError("extraction identity conflicts with immutable metadata")
            self._complete_claim_locked(conn, claim, receipt, now=now, next_stage="interpreted")

    def complete_interpretation(
        self,
        claim: JobClaim,
        envelope: Mapping[str, Any],
        interpretation: Mapping[str, Any],
    ) -> None:
        now = time.time()
        receipt = StageReceipt(
            claim.artifact_id,
            "interpreted",
            f"interpretation:{interpretation['interpretation_id']}",
            str(interpretation["output_sha256"]),
            recorded_at=now,
        )
        with self._write() as conn:
            self._active_stage_claim_locked(conn, claim, "interpreted", now=now)
            envelope_values = (
                envelope["envelope_id"], envelope["artifact_id"], envelope["project_key"],
                envelope["source_sha256"], envelope["extraction_id"], envelope["extraction_sha256"],
                envelope["envelope_version"], envelope["schema_version"], envelope["prompt_version"],
                envelope["task"], envelope["derived_storage_id"], envelope["derived_object_key"],
                envelope["output_sha256"], int(envelope["output_bytes"]), now,
            )
            existing = conn.execute(
                "SELECT * FROM interpretation_envelopes WHERE envelope_id=?", (envelope["envelope_id"],)
            ).fetchone()
            if existing is None:
                conn.execute("INSERT INTO interpretation_envelopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", envelope_values)
            elif tuple(existing[name] for name in tuple(existing.keys())[:-1]) != envelope_values[:-1]:
                raise ValueError("interpretation envelope conflicts with immutable metadata")
            interpretation_values = (
                interpretation["interpretation_id"], interpretation["envelope_id"],
                interpretation["artifact_id"], interpretation["extraction_id"],
                interpretation["schema_version"], interpretation["prompt_version"],
                interpretation["derived_storage_id"], interpretation["derived_object_key"],
                interpretation["output_sha256"], int(interpretation["output_bytes"]),
                interpretation["actual_provider"], interpretation["actual_model"],
                interpretation["selected_provider"], interpretation["selected_model"],
                interpretation["model_tier"], interpretation["route_fingerprint"],
                int(interpretation["input_tokens"]), int(interpretation["output_tokens"]),
                int(interpretation["total_tokens"]), int(interpretation["cache_read_tokens"]),
                int(interpretation["cache_write_tokens"]), now,
            )
            existing = conn.execute(
                "SELECT * FROM interpretations WHERE interpretation_id=?",
                (interpretation["interpretation_id"],),
            ).fetchone()
            if existing is None:
                conn.execute("INSERT INTO interpretations VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", interpretation_values)
            elif tuple(existing[name] for name in tuple(existing.keys())[:-1]) != interpretation_values[:-1]:
                raise ValueError("interpretation identity conflicts with immutable metadata")
            self._complete_claim_locked(
                conn, claim, receipt, now=now, next_stage="assimilated"
            )

    def persist_interpretation_envelope(
        self, claim: JobClaim, envelope: Mapping[str, Any]
    ) -> None:
        now = time.time()
        values = (
            envelope["envelope_id"], envelope["artifact_id"], envelope["project_key"],
            envelope["source_sha256"], envelope["extraction_id"], envelope["extraction_sha256"],
            envelope["envelope_version"], envelope["schema_version"], envelope["prompt_version"],
            envelope["task"], envelope["derived_storage_id"], envelope["derived_object_key"],
            envelope["output_sha256"], int(envelope["output_bytes"]), now,
        )
        with self._write() as conn:
            self._active_stage_claim_locked(conn, claim, "interpreted", now=now)
            existing = conn.execute(
                "SELECT * FROM interpretation_envelopes WHERE envelope_id=?",
                (envelope["envelope_id"],),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO interpretation_envelopes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
            elif tuple(existing[name] for name in tuple(existing.keys())[:-1]) != values[:-1]:
                raise ValueError("interpretation envelope conflicts with immutable metadata")

    def get_interpretation(self, interpretation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM interpretations WHERE interpretation_id=?", (interpretation_id,)
            ).fetchone()
            return dict(row) if row else None

    def get_interpretation_for_assimilation_claim(
        self, claim: JobClaim
    ) -> tuple[IntakeArtifact, dict[str, Any], str]:
        now = time.time()
        with self._connect() as conn:
            self._active_stage_claim_locked(conn, claim, "assimilated", now=now)
            artifact = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (claim.artifact_id,)
            ).fetchone()
            receipt = conn.execute(
                "SELECT receipt_id FROM stage_receipts WHERE artifact_id=? AND stage='interpreted'",
                (claim.artifact_id,),
            ).fetchone()
            notion = conn.execute(
                "SELECT receipt_id FROM stage_receipts WHERE artifact_id=? AND stage='notion_archived'",
                (claim.artifact_id,),
            ).fetchone()
            if artifact is None or receipt is None or not str(receipt[0]).startswith(
                "interpretation:"
            ):
                raise ValueError("assimilation claim lacks a versioned interpretation")
            if notion is None or not str(notion[0]).startswith("notion:page:"):
                raise ValueError("assimilation claim lacks its Notion source citation")
            interpretation_id = str(receipt[0]).split(":", 1)[1]
            interpretation = conn.execute(
                "SELECT * FROM interpretations WHERE interpretation_id=?",
                (interpretation_id,),
            ).fetchone()
            if interpretation is None:
                raise ValueError("versioned interpretation metadata is missing")
            return (
                self._artifact_from_row(artifact),
                dict(interpretation),
                str(notion[0]),
            )

    def get_assimilation(self, assimilation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM assimilation_proposals WHERE assimilation_id=?",
                (assimilation_id,),
            ).fetchone()
            return dict(row) if row else None

    def get_assimilation_for_artifact(self, artifact_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM assimilation_proposals WHERE artifact_id=? "
                "ORDER BY created_at DESC LIMIT 1",
                (artifact_id,),
            ).fetchone()
            return dict(row) if row else None

    def persist_assimilation_proposal(
        self, claim: JobClaim, row: Mapping[str, Any]
    ) -> None:
        now = time.time()
        values = (
            row["assimilation_id"], row["artifact_id"], row["interpretation_id"],
            row["assimilation_version"], row["schema_version"], row["prompt_version"],
            row["policy_version"], row["project_key"], row["proposal_sha256"],
            row["derived_storage_id"], row["derived_object_key"], row["output_sha256"],
            int(row["output_bytes"]), row["actual_provider"], row["actual_model"],
            row["selected_provider"], row["selected_model"], row["model_tier"],
            row["route_fingerprint"], int(bool(row["review_required"])),
            row["review_reason"], row["base_git_head"], row.get("git_commit_sha") or None,
            int(bool(row.get("sync_verified"))), now,
        )
        with self._write() as conn:
            self._active_stage_claim_locked(conn, claim, "assimilated", now=now)
            existing = conn.execute(
                "SELECT * FROM assimilation_proposals WHERE assimilation_id=?",
                (row["assimilation_id"],),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO assimilation_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
            else:
                immutable = tuple(existing[name] for name in tuple(existing.keys())[:22])
                if immutable != values[:22]:
                    raise ValueError("assimilation identity conflicts with immutable metadata")

    def require_assimilation_review(
        self,
        claim: JobClaim,
        *,
        assimilation_id: str,
        review_id: str,
        proposal_sha256: str,
        assimilation_version: str,
        project_key: str,
        reason_code: str,
    ) -> None:
        now = time.time()
        with self._write() as conn:
            self._active_stage_claim_locked(conn, claim, "assimilated", now=now)
            proposal = conn.execute(
                "SELECT artifact_id FROM assimilation_proposals WHERE assimilation_id=?",
                (assimilation_id,),
            ).fetchone()
            if proposal is None or proposal[0] != claim.artifact_id:
                raise ValueError("review does not belong to the active assimilation")
            conn.execute(
                "INSERT OR IGNORE INTO client_knowledge_reviews("
                "review_id, assimilation_id, artifact_id, project_key, proposal_sha256, "
                "assimilation_version, state, reason_code, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?,'pending',?,?,?)",
                (
                    review_id, assimilation_id, claim.artifact_id, project_key,
                    proposal_sha256, assimilation_version, reason_code, now, now,
                ),
            )
            review = conn.execute(
                "SELECT assimilation_id, proposal_sha256, state FROM client_knowledge_reviews "
                "WHERE review_id=?",
                (review_id,),
            ).fetchone()
            if review is None or review[0] != assimilation_id or review[1] != proposal_sha256:
                raise ValueError("review identity conflicts with immutable proposal")
            conn.execute(
                "INSERT OR IGNORE INTO jobs(job_id, artifact_id, stage, status, max_attempts, "
                "last_error_class, created_at, updated_at) VALUES(?,?,?,'operator_blocked',?,?,?,?)",
                (
                    secrets.token_hex(16), claim.artifact_id, "needs_review",
                    DEFAULT_MAX_ATTEMPTS, "review_pending", now, now,
                ),
            )
            conn.execute(
                "UPDATE jobs SET status='queued', claim_token=NULL, owner_pid=NULL, owner_host=NULL, "
                "owner_started_at=NULL, lease_expires_at=NULL, heartbeat_at=NULL, next_retry_at=NULL, "
                "last_error_class='review_pending', updated_at=? WHERE job_id=? AND claim_token=?",
                (now, claim.job_id, claim.claim_token),
            )

    def get_review(self, review_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM client_knowledge_reviews WHERE review_id=?", (review_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_pending_reviews(self, *, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM client_knowledge_reviews WHERE state='pending' "
                    "ORDER BY created_at LIMIT ?", (max(1, min(limit, 500)),)
                ).fetchall()
            ]

    def record_review_notification(
        self,
        review_id: str,
        *,
        state: str,
        content_sha256: str,
        guild_id: str,
        channel_id: str,
        role_id: str,
        marker: str,
        message_id: str = "",
    ) -> None:
        if state not in {"pending", "confirmed", "proven_none", "uncertain"}:
            raise ValueError("invalid review notification state")
        now = time.time()
        with self._write() as conn:
            row = conn.execute(
                "SELECT notification_content_sha256, notification_guild_id, "
                "notification_channel_id, notification_role_id, notification_marker, "
                "notification_message_id FROM client_knowledge_reviews WHERE review_id=?",
                (review_id,),
            ).fetchone()
            if row is None:
                raise ValueError("review does not exist")
            expected = (content_sha256, guild_id, channel_id, role_id, marker)
            actual = tuple(str(value or "") for value in row[:5])
            if any(actual) and actual != expected:
                raise ValueError("notification identity conflicts with existing review")
            if row[5] and message_id and row[5] != message_id:
                raise ValueError("review already has a different Discord message id")
            conn.execute(
                "UPDATE client_knowledge_reviews SET notification_state=?, "
                "notification_content_sha256=?, notification_message_id=COALESCE(notification_message_id, ?), "
                "notification_guild_id=?, notification_channel_id=?, notification_role_id=?, "
                "notification_marker=?, updated_at=? WHERE review_id=?",
                (
                    state, content_sha256, message_id or None, guild_id, channel_id,
                    role_id, marker, now, review_id,
                ),
            )

    def claim_review_notification(
        self,
        review_id: str,
        *,
        content_sha256: str,
        guild_id: str,
        channel_id: str,
        role_id: str,
        marker: str,
    ) -> bool:
        """Reserve the one Discord POST by durably entering uncertainty first."""
        now = time.time()
        with self._write() as conn:
            row = conn.execute(
                "SELECT state, notification_state, notification_content_sha256, "
                "notification_guild_id, notification_channel_id, notification_role_id, "
                "notification_marker FROM client_knowledge_reviews WHERE review_id=?",
                (review_id,),
            ).fetchone()
            if row is None or row[0] != "pending" or row[1] not in {
                "pending",
                "proven_none",
            }:
                return False
            expected = (content_sha256, guild_id, channel_id, role_id, marker)
            actual = tuple(str(value or "") for value in row[2:])
            if any(actual) and actual != expected:
                raise ValueError("notification identity conflicts with existing review")
            updated = conn.execute(
                "UPDATE client_knowledge_reviews SET notification_state='uncertain', "
                "notification_content_sha256=?, notification_guild_id=?, "
                "notification_channel_id=?, notification_role_id=?, notification_marker=?, "
                "updated_at=? WHERE review_id=? AND state='pending' "
                "AND notification_state IN ('pending','proven_none')",
                (
                    content_sha256,
                    guild_id,
                    channel_id,
                    role_id,
                    marker,
                    now,
                    review_id,
                ),
            ).rowcount
            return updated == 1

    def decide_review(
        self,
        review_id: str,
        *,
        decision: str,
        reviewer_user_id: str,
        reviewer_role_id: str,
        decision_message_id: str,
        reason: str = "",
    ) -> bool:
        if decision not in {"approved", "rejected"}:
            raise ValueError("review decision must be approved or rejected")
        now = time.time()
        with self._write() as conn:
            review = conn.execute(
                "SELECT artifact_id, state, notification_state, notification_message_id "
                "FROM client_knowledge_reviews WHERE review_id=?", (review_id,)
            ).fetchone()
            if review is None or review[1] != "pending":
                return False
            if review[2] != "confirmed" or not review[3]:
                raise ValueError("review notification is not confirmed")
            conn.execute(
                "UPDATE client_knowledge_reviews SET state=?, reviewer_user_id=?, reviewer_role_id=?, "
                "decision_message_id=?, decision_reason=?, decided_at=?, updated_at=? "
                "WHERE review_id=? AND state='pending'",
                (
                    decision, reviewer_user_id, reviewer_role_id or None,
                    decision_message_id, reason or None, now, now, review_id,
                ),
            )
            if decision == "approved":
                conn.execute(
                    "UPDATE jobs SET status='succeeded', last_error_class=NULL, updated_at=? "
                    "WHERE artifact_id=? AND stage='needs_review' AND status!='succeeded'",
                    (now, review[0]),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO stage_receipts(artifact_id, stage, receipt_id, recorded_at) "
                    "VALUES(?,?,?,?)",
                    (review[0], "needs_review", f"review:{review_id}:approved", now),
                )
                conn.execute(
                    "UPDATE jobs SET status='queued', attempt_count=0, next_retry_at=NULL, "
                    "last_error_class=NULL, updated_at=? WHERE artifact_id=? AND stage='assimilated' "
                    "AND status IN ('queued','failed','operator_blocked')",
                    (now, review[0]),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status='succeeded', last_error_class=NULL, updated_at=? "
                    "WHERE artifact_id=? AND stage='needs_review'",
                    (now, review[0]),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO stage_receipts(artifact_id, stage, receipt_id, recorded_at) "
                    "VALUES(?,?,?,?)",
                    (review[0], "needs_review", f"review:{review_id}:rejected", now),
                )
                conn.execute(
                    "UPDATE jobs SET status='quarantined', next_retry_at=NULL, "
                    "last_error_class='review_rejected', updated_at=? "
                    "WHERE artifact_id=? AND stage='assimilated'",
                    (now, review[0]),
                )
            return True

    def record_publication(
        self,
        *,
        assimilation_id: str,
        artifact_id: str,
        assimilation_version: str,
        proposal_sha256: str,
        branch_ref: str,
        expected_head: str,
        manifest_json: str,
        state: str,
        commit_sha: str = "",
        error_class: str = "",
    ) -> None:
        now = time.time()
        with self._write() as conn:
            existing = conn.execute(
                "SELECT artifact_id, assimilation_version, proposal_sha256, branch_ref, "
                "expected_head, manifest_json, commit_sha FROM publication_transactions "
                "WHERE assimilation_id=?", (assimilation_id,)
            ).fetchone()
            identity = (
                artifact_id, assimilation_version, proposal_sha256, branch_ref,
                expected_head, manifest_json,
            )
            if existing is not None and tuple(existing[:6]) != identity:
                raise ValueError("publication identity conflicts with immutable proposal")
            if existing is not None and existing[6] and commit_sha and existing[6] != commit_sha:
                raise ValueError("publication commit identity conflicts")
            conn.execute(
                "INSERT INTO publication_transactions(assimilation_id, artifact_id, assimilation_version, "
                "proposal_sha256, branch_ref, expected_head, commit_sha, state, manifest_json, error_class, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(assimilation_id) DO UPDATE SET "
                "commit_sha=COALESCE(publication_transactions.commit_sha, excluded.commit_sha), "
                "state=excluded.state, error_class=excluded.error_class, updated_at=excluded.updated_at",
                (
                    assimilation_id, artifact_id, assimilation_version, proposal_sha256,
                    branch_ref, expected_head, commit_sha or None, state, manifest_json,
                    error_class or None, now,
                ),
            )

    def get_publication(self, assimilation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM publication_transactions WHERE assimilation_id=?",
                (assimilation_id,),
            ).fetchone()
            return dict(row) if row else None

    def complete_assimilation(
        self,
        claim: JobClaim,
        *,
        assimilation_id: str,
        commit_sha: str,
        output_sha256: str,
        next_stage: str = "honcho_projected",
        sync_verified: bool = True,
    ) -> None:
        now = time.time()
        receipt = StageReceipt(
            claim.artifact_id, "assimilated", f"assimilation:{assimilation_id}:{commit_sha}",
            output_sha256, recorded_at=now,
        )
        with self._write() as conn:
            self._active_stage_claim_locked(conn, claim, "assimilated", now=now)
            conn.execute(
                "UPDATE assimilation_proposals SET git_commit_sha=?, sync_verified=? "
                "WHERE assimilation_id=?",
                (commit_sha or None, int(sync_verified), assimilation_id),
            )
            if next_stage != "complete":
                self._complete_claim_locked(
                    conn, claim, receipt, now=now, next_stage=next_stage
                )
                return
            self._complete_claim_locked(conn, claim, receipt, now=now)
            conn.execute(
                "INSERT OR IGNORE INTO stage_receipts"
                "(artifact_id, stage, receipt_id, output_sha256, recorded_at) VALUES(?,?,?,?,?)",
                (
                    claim.artifact_id,
                    "honcho_projected",
                    f"honcho:none:{assimilation_id}",
                    output_sha256,
                    now,
                ),
            )
            conn.execute(
                "INSERT OR IGNORE INTO jobs(job_id, artifact_id, stage, status, max_attempts, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                (
                    secrets.token_hex(16),
                    claim.artifact_id,
                    "complete",
                    "queued",
                    DEFAULT_MAX_ATTEMPTS,
                    now,
                    now,
                ),
            )

    def requeue_review_notification(self, review_id: str) -> bool:
        """Operator-confirmed reset after proving an uncertain POST did not land."""
        now = time.time()
        with self._write() as conn:
            return conn.execute(
                "UPDATE client_knowledge_reviews SET notification_state='proven_none', "
                "notification_message_id=NULL, updated_at=? WHERE review_id=? "
                "AND state='pending' AND notification_state='uncertain'",
                (now, review_id),
            ).rowcount == 1

    def upsert_honcho_projection(
        self,
        *,
        projection_key: str,
        project_key: str,
        page_slug: str,
        page_sha256: str,
        marker: str,
        exact_content: str,
        state: str,
        conclusion_id: str = "",
        obsolete_conclusion_id: str = "",
    ) -> None:
        now = time.time()
        with self._write() as conn:
            row = conn.execute(
                "SELECT project_key, page_slug, marker FROM honcho_projections "
                "WHERE projection_key=?", (projection_key,)
            ).fetchone()
            if row is not None and tuple(row) != (project_key, page_slug, marker):
                raise ValueError("Honcho projection identity conflicts")
            conn.execute(
                "INSERT INTO honcho_projections(projection_key, project_key, page_slug, page_sha256, "
                "marker, exact_content, conclusion_id, obsolete_conclusion_id, state, updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(projection_key) DO UPDATE SET "
                "page_sha256=excluded.page_sha256, exact_content=excluded.exact_content, "
                "conclusion_id=excluded.conclusion_id, "
                "obsolete_conclusion_id=excluded.obsolete_conclusion_id, "
                "state=excluded.state, updated_at=excluded.updated_at",
                (
                    projection_key, project_key, page_slug, page_sha256, marker,
                    exact_content, conclusion_id or None, obsolete_conclusion_id or None,
                    state, now,
                ),
            )

    def get_honcho_projection(self, project_key: str, page_slug: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM honcho_projections WHERE project_key=? AND page_slug=?",
                (project_key, page_slug),
            ).fetchone()
            return dict(row) if row else None

    def list_honcho_projections(self, project_key: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM honcho_projections WHERE project_key=? ORDER BY page_slug",
                (project_key,),
            ).fetchall()]

    def get_assimilation_for_projection_claim(
        self, claim: JobClaim
    ) -> tuple[IntakeArtifact, dict[str, Any]]:
        now = time.time()
        with self._connect() as conn:
            self._active_stage_claim_locked(conn, claim, "honcho_projected", now=now)
            artifact = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id=?", (claim.artifact_id,)
            ).fetchone()
            receipt = conn.execute(
                "SELECT receipt_id FROM stage_receipts WHERE artifact_id=? AND stage='assimilated'",
                (claim.artifact_id,),
            ).fetchone()
            if artifact is None or receipt is None or not str(receipt[0]).startswith(
                "assimilation:"
            ):
                raise ValueError("projection claim lacks a verified assimilation")
            parts = str(receipt[0]).split(":")
            if len(parts) < 3:
                raise ValueError("assimilation receipt is invalid")
            assimilation = conn.execute(
                "SELECT * FROM assimilation_proposals WHERE assimilation_id=?", (parts[1],)
            ).fetchone()
            if assimilation is None or not int(assimilation["sync_verified"]):
                raise ValueError("projection claim lacks verified GBrain sync")
            return self._artifact_from_row(artifact), dict(assimilation)

    def complete_honcho_projection(
        self, claim: JobClaim, *, receipt_id: str, output_sha256: str
    ) -> None:
        now = time.time()
        receipt = StageReceipt(
            claim.artifact_id, "honcho_projected", receipt_id, output_sha256,
            recorded_at=now,
        )
        with self._write() as conn:
            self._complete_claim_locked(
                conn, claim, receipt, now=now, next_stage="complete"
            )

    def list_child_artifacts_for_notion_claim(
        self, job_id: str, claim_token: str, artifact_id: str
    ) -> list[IntakeArtifact]:
        now = time.time()
        with self._connect() as conn:
            self._active_notion_claim_locked(
                conn, job_id, claim_token, artifact_id, now=now
            )
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE parent_artifact_id=? "
                "ORDER BY provider_attachment_id, artifact_id",
                (artifact_id,),
            ).fetchall()
            return [self._artifact_from_row(row) for row in rows]

    def get_notion_operation(
        self, artifact_id: str, operation_kind: str
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM notion_operations WHERE artifact_id=? AND operation_kind=?",
                (artifact_id, operation_kind),
            ).fetchone()
            return dict(row) if row else None

    def get_completed_stage_receipt(
        self, artifact_id: str, stage: str
    ) -> dict[str, Any] | None:
        stage = validate_stage(stage)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT stage_receipts.artifact_id, stage_receipts.stage, "
                "stage_receipts.receipt_id, stage_receipts.output_sha256, "
                "stage_receipts.recorded_at FROM stage_receipts "
                "JOIN jobs ON jobs.artifact_id=stage_receipts.artifact_id "
                "AND jobs.stage=stage_receipts.stage "
                "WHERE stage_receipts.artifact_id=? AND stage_receipts.stage=? "
                "AND jobs.status='succeeded'",
                (artifact_id, stage),
            ).fetchone()
            return dict(row) if row else None

    def advance_notion_operation(
        self,
        job_id: str,
        claim_token: str,
        artifact_id: str,
        operation_kind: str,
        state: str,
        *,
        expected_sha256: str,
        expected_size: int,
        expected_mime_type: str,
        page_id: str | None = None,
        block_id: str | None = None,
        active_upload_attempt_id: str | None = None,
        claimed_artifact_id: str | None = None,
    ) -> None:
        now = time.time()
        with self._write() as conn:
            self._active_notion_claim_locked(
                conn, job_id, claim_token, claimed_artifact_id or artifact_id, now=now
            )
            existing = conn.execute(
                "SELECT expected_sha256, expected_size, expected_mime_type, page_id, block_id, "
                "state, "
                "active_upload_attempt_id FROM notion_operations "
                "WHERE artifact_id=? AND operation_kind=?",
                (artifact_id, operation_kind),
            ).fetchone()
            if existing is not None and (
                existing[0] != expected_sha256
                or int(existing[1]) != int(expected_size)
                or existing[2] != expected_mime_type
            ):
                raise ValueError("notion operation identity conflicts with immutable artifact")
            durable_state = state
            state_order = {
                "attempt-selected": 0,
                "upload-created": 1,
                "bytes-sent": 2,
                "multipart-completed": 3,
                "page-attached": 4,
                "receipt-verified": 5,
            }
            if (
                existing is not None
                and operation_kind == "file"
                and state_order.get(str(existing[5]), -1)
                > state_order.get(state, -1)
            ):
                durable_state = str(existing[5])
            conn.execute(
                "INSERT INTO notion_operations(artifact_id, operation_kind, state, page_id, block_id, "
                "active_upload_attempt_id, expected_sha256, expected_size, expected_mime_type, "
                "last_job_id, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(artifact_id, operation_kind) DO UPDATE SET "
                "state=excluded.state, page_id=COALESCE(excluded.page_id, notion_operations.page_id), "
                "block_id=COALESCE(excluded.block_id, notion_operations.block_id), "
                "active_upload_attempt_id=COALESCE(excluded.active_upload_attempt_id, notion_operations.active_upload_attempt_id), "
                "last_job_id=excluded.last_job_id, updated_at=excluded.updated_at",
                (
                    artifact_id, operation_kind, durable_state, page_id, block_id,
                    active_upload_attempt_id, expected_sha256, int(expected_size),
                    expected_mime_type, job_id, now,
                ),
            )

    def reserve_upload_attempt(
        self,
        job_id: str,
        claim_token: str,
        artifact: IntakeArtifact,
        *,
        attempt_id: str,
        opaque_marker: str,
        remote_filename: str,
        upload_mode: str,
        expected_part_count: int,
        expected_part_size: int = 0,
        replaces_attempt_id: str | None = None,
        replacement_reason: str | None = None,
        claimed_artifact_id: str | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        with self._write() as conn:
            self._active_notion_claim_locked(
                conn, job_id, claim_token, claimed_artifact_id or artifact.artifact_id, now=now
            )
            existing = conn.execute(
                "SELECT * FROM notion_upload_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if existing is not None:
                return dict(existing)
            ordinal = int(conn.execute(
                "SELECT COUNT(*) FROM notion_upload_attempts WHERE artifact_id=? AND operation_kind='file'",
                (artifact.artifact_id,),
            ).fetchone()[0]) + 1
            conn.execute(
                "INSERT INTO notion_upload_attempts(attempt_id, artifact_id, operation_kind, ordinal, "
                "replaces_attempt_id, replacement_reason, marker_version, opaque_marker, remote_filename, "
                "upload_mode, expected_sha256, expected_size, expected_mime_type, expected_part_count, "
                "expected_part_size, created_by_job_id, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id, artifact.artifact_id, "file", ordinal,
                    replaces_attempt_id, replacement_reason, "ckfu-v1", opaque_marker,
                    remote_filename, upload_mode, artifact.content_sha256,
                    artifact.byte_size, artifact.mime_type, int(expected_part_count),
                    int(expected_part_size), job_id, now,
                ),
            )
            self._append_upload_event_locked(
                conn, job_id, claim_token, attempt_id, "attempt_reserved", now=now
            )
            return dict(conn.execute(
                "SELECT * FROM notion_upload_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone())

    def _append_upload_event_locked(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        claim_token: str,
        attempt_id: str,
        event_type: str,
        *,
        now: float,
        remote_upload_id: str | None = None,
        remote_state: str | None = None,
        remote_parts_sent: int | None = None,
        page_id: str | None = None,
        block_id: str | None = None,
        evidence_identity: str | None = None,
        classified_reason: str | None = None,
    ) -> None:
        sequence = int(conn.execute(
            "SELECT COUNT(*) FROM notion_upload_attempt_events WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()[0]) + 1
        conn.execute(
            "INSERT INTO notion_upload_attempt_events(event_id, attempt_id, sequence, event_type, "
            "job_id, claim_fingerprint, remote_upload_id, remote_state, remote_parts_sent, page_id, "
            "block_id, evidence_identity, classified_reason, observed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                secrets.token_hex(16), attempt_id, sequence, event_type, job_id,
                self._claim_fingerprint(job_id, claim_token), remote_upload_id, remote_state,
                remote_parts_sent, page_id, block_id, evidence_identity, classified_reason, now,
            ),
        )

    def append_upload_attempt_event(
        self,
        job_id: str,
        claim_token: str,
        artifact_id: str,
        attempt_id: str,
        event_type: str,
        claimed_artifact_id: str | None = None,
        **evidence: Any,
    ) -> None:
        now = time.time()
        with self._write() as conn:
            self._active_notion_claim_locked(
                conn, job_id, claim_token, claimed_artifact_id or artifact_id, now=now
            )
            attempt = conn.execute(
                "SELECT artifact_id FROM notion_upload_attempts WHERE attempt_id=?", (attempt_id,)
            ).fetchone()
            if attempt is None or attempt[0] != artifact_id:
                raise ValueError("upload attempt does not belong to claimed artifact")
            self._append_upload_event_locked(
                conn, job_id, claim_token, attempt_id, event_type, now=now, **evidence
            )

    def publish_upload_scan(
        self,
        job_id: str,
        claim_token: str,
        artifact_id: str,
        attempt_id: str,
        *,
        scan_role: str,
        page_count: int,
        items: list[dict[str, Any]],
        claimed_artifact_id: str | None = None,
    ) -> str:
        now = time.time()
        scan_id = secrets.token_hex(16)
        with self._write() as conn:
            self._active_notion_claim_locked(
                conn, job_id, claim_token, claimed_artifact_id or artifact_id, now=now
            )
            attempt = conn.execute(
                "SELECT artifact_id, baseline_scan_id FROM notion_upload_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if attempt is None or attempt[0] != artifact_id:
                raise ValueError("upload attempt does not belong to claimed artifact")
            conn.execute(
                "INSERT INTO notion_upload_scans(scan_id, artifact_id, attempt_id, scan_role, "
                "request_status, page_count, result_count, completed_at, created_by_job_id) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (scan_id, artifact_id, attempt_id, scan_role, "complete", int(page_count), len(items), now, job_id),
            )
            for item in items:
                parts = item.get("number_of_parts") or {}
                filename = str(item.get("filename") or "")
                conn.execute(
                    "INSERT INTO notion_upload_scan_items(scan_id, remote_upload_id, opaque_marker, "
                    "remote_filename, content_type, content_length, status, created_time, expiry_time, "
                    "part_count_total, part_count_sent) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        scan_id, str(item["id"]), filename.rsplit(".", 1)[0], filename,
                        item.get("content_type"), item.get("content_length"), str(item.get("status") or ""),
                        item.get("created_time"), item.get("expiry_time"), parts.get("total"), parts.get("sent"),
                    ),
                )
            if scan_role == "baseline":
                if attempt[1] and attempt[1] != scan_id:
                    raise ValueError("upload attempt already has a baseline scan")
                conn.execute(
                    "UPDATE notion_upload_attempts SET baseline_scan_id=? WHERE attempt_id=?",
                    (scan_id, attempt_id),
                )
            self._append_upload_event_locked(
                conn, job_id, claim_token, attempt_id,
                "baseline_scan_completed" if scan_role == "baseline" else "reconciliation_scan_completed",
                now=now, evidence_identity=scan_id,
            )
        return scan_id

    def record_upload_remote_identity(
        self,
        job_id: str,
        claim_token: str,
        artifact_id: str,
        attempt_id: str,
        remote_upload_id: str,
        *,
        evidence_identity: str,
        claimed_artifact_id: str | None = None,
    ) -> None:
        now = time.time()
        conflict = False
        with self._write() as conn:
            self._active_notion_claim_locked(
                conn, job_id, claim_token, claimed_artifact_id or artifact_id, now=now
            )
            row = conn.execute(
                "SELECT artifact_id, remote_upload_id FROM notion_upload_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if row is None or row[0] != artifact_id:
                raise ValueError("upload attempt does not belong to claimed artifact")
            owner = conn.execute(
                "SELECT attempt_id FROM notion_upload_attempts "
                "WHERE remote_upload_id=? AND attempt_id!=?",
                (remote_upload_id, attempt_id),
            ).fetchone()
            if (row[1] and row[1] != remote_upload_id) or owner is not None:
                self._append_upload_event_locked(
                    conn, job_id, claim_token, attempt_id, "remote_identity_conflict",
                    now=now, remote_upload_id=remote_upload_id,
                    evidence_identity=evidence_identity,
                )
                conflict = True
            else:
                conn.execute(
                    "UPDATE notion_upload_attempts SET remote_upload_id=COALESCE(remote_upload_id, ?) "
                    "WHERE attempt_id=?",
                    (remote_upload_id, attempt_id),
                )
                self._append_upload_event_locked(
                    conn, job_id, claim_token, attempt_id, "remote_id_recorded", now=now,
                    remote_upload_id=remote_upload_id, evidence_identity=evidence_identity,
                )
        if conflict:
            raise ValueError("upload attempt has conflicting remote identity")

    def get_upload_attempts(self, artifact_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM notion_upload_attempts WHERE artifact_id=? ORDER BY ordinal",
                (artifact_id,),
            ).fetchall()]

    def get_upload_attempt_events(self, attempt_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM notion_upload_attempt_events WHERE attempt_id=? ORDER BY sequence",
                (attempt_id,),
            ).fetchall()]

    def select_active_upload_attempt(
        self,
        job_id: str,
        claim_token: str,
        claimed_artifact_id: str,
        artifact: IntakeArtifact,
        attempt_id: str,
        state: str = "attempt-selected",
    ) -> None:
        now = time.time()
        with self._write() as conn:
            self._active_notion_claim_locked(
                conn, job_id, claim_token, claimed_artifact_id, now=now
            )
            attempt = conn.execute(
                "SELECT artifact_id FROM notion_upload_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if attempt is None or attempt[0] != artifact.artifact_id:
                raise ValueError("upload attempt does not belong to artifact")
            conn.execute(
                "INSERT INTO notion_operations(artifact_id, operation_kind, state, "
                "active_upload_attempt_id, expected_sha256, expected_size, expected_mime_type, "
                "last_job_id, updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(artifact_id, operation_kind) DO UPDATE SET "
                "active_upload_attempt_id=excluded.active_upload_attempt_id, "
                "last_job_id=excluded.last_job_id, updated_at=excluded.updated_at",
                (
                    artifact.artifact_id, "file", state, attempt_id,
                    artifact.content_sha256, artifact.byte_size, artifact.mime_type,
                    job_id, now,
                ),
            )

    def set_upload_attempt_disposition(
        self,
        job_id: str,
        claim_token: str,
        claimed_artifact_id: str,
        artifact_id: str,
        attempt_id: str,
        disposition: str,
        *,
        remote_upload_id: str | None = None,
    ) -> None:
        event_type = {
            "expired": "attempt_expired",
            "failed": "attempt_failed",
            "superseded": "attempt_superseded",
            "verified": "receipt_verified",
        }.get(disposition)
        if event_type is None:
            raise ValueError("unknown upload attempt disposition")
        self.append_upload_attempt_event(
            job_id, claim_token, artifact_id, attempt_id, event_type,
            claimed_artifact_id=claimed_artifact_id,
            remote_upload_id=remote_upload_id,
            remote_state=disposition,
        )

    def get_upload_scan_items(self, scan_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM notion_upload_scan_items WHERE scan_id=? ORDER BY remote_upload_id",
                (scan_id,),
            ).fetchall()]

    def complete_stage(
        self,
        job_id: str,
        claim_token: str,
        receipt: StageReceipt,
        *,
        now: float | None = None,
        next_stage: str = "",
    ) -> bool:
        now = time.time() if now is None else float(now)
        with self._write() as conn:
            row = conn.execute(
                "SELECT artifact_id, stage FROM jobs WHERE job_id=? AND status='running' "
                "AND claim_token=? AND lease_expires_at>?",
                (job_id, claim_token, now),
            ).fetchone()
            if row is None or row[0] != receipt.artifact_id or row[1] != receipt.stage:
                return False
            if receipt.stage in _INITIAL_RECEIPT_STAGES or receipt.stage == "needs_mapping":
                raise ValueError("stage must be completed through its dedicated transition")
            existing = conn.execute(
                "SELECT receipt_id, output_sha256 FROM stage_receipts "
                "WHERE artifact_id=? AND stage=?",
                (receipt.artifact_id, receipt.stage),
            ).fetchone()
            if existing is not None:
                if (
                    existing[0] != receipt.receipt_id
                    or (existing[1] or "") != receipt.output_sha256
                ):
                    raise ValueError("stage receipt conflicts with immutable metadata")
            else:
                conn.execute(
                    "INSERT INTO stage_receipts(artifact_id, stage, receipt_id, output_sha256, recorded_at) "
                    "VALUES(?,?,?,?,?)",
                    (receipt.artifact_id, receipt.stage, receipt.receipt_id, receipt.output_sha256 or None, receipt.recorded_at),
                )
            cursor = conn.execute(
                "UPDATE jobs SET status='succeeded', claim_token=NULL, owner_pid=NULL, owner_host=NULL, "
                "owner_started_at=NULL, lease_expires_at=NULL, heartbeat_at=NULL, updated_at=?, last_error_class=NULL "
                "WHERE job_id=? AND status='running' AND claim_token=? AND lease_expires_at>?",
                (now, job_id, claim_token, now),
            )
            if cursor.rowcount == 1 and next_stage:
                next_stage = validate_stage(next_stage)
                conn.execute(
                    "INSERT OR IGNORE INTO jobs(job_id, artifact_id, stage, status, max_attempts, "
                    "created_at, updated_at) VALUES(?,?,?,?,?,?,?)",
                    (secrets.token_hex(16), receipt.artifact_id, next_stage, "queued", DEFAULT_MAX_ATTEMPTS, now, now),
                )
            return cursor.rowcount == 1

    record_stage_receipt = complete_stage

    def fail_stage(
        self,
        job_id: str,
        claim_token: str,
        *,
        error_class: str,
        retry_delay: float = 0,
        quarantine: bool = False,
        now: float | None = None,
    ) -> bool:
        now = time.time() if now is None else float(now)
        status = "quarantined" if quarantine else "failed"
        next_retry = None if quarantine else now + max(0.0, float(retry_delay))
        with self._write() as conn:
            row = conn.execute(
                "SELECT attempt_count, max_attempts FROM jobs WHERE job_id=? AND status='running' "
                "AND claim_token=? AND lease_expires_at>?",
                (job_id, claim_token, now),
            ).fetchone()
            if row is None:
                return False
            if not quarantine and int(row[0]) >= int(row[1]):
                status = "quarantined"
                next_retry = None
                error_class = "retry_exhausted"
            error_class = _error_class(error_class)
            cursor = conn.execute(
                "UPDATE jobs SET status=?, claim_token=NULL, owner_pid=NULL, owner_host=NULL, owner_started_at=NULL, "
                "lease_expires_at=NULL, heartbeat_at=NULL, next_retry_at=?, last_error_class=?, updated_at=? "
                "WHERE job_id=? AND status='running' AND claim_token=? AND lease_expires_at>?",
                (status, next_retry, error_class, now, job_id, claim_token, now),
            )
            return cursor.rowcount == 1

    def block_stage(
        self,
        job_id: str,
        claim_token: str,
        *,
        error_class: str,
        now: float | None = None,
    ) -> bool:
        now = time.time() if now is None else float(now)
        error_class = _error_class(error_class)
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status='operator_blocked', claim_token=NULL, owner_pid=NULL, "
                "owner_host=NULL, owner_started_at=NULL, lease_expires_at=NULL, heartbeat_at=NULL, "
                "next_retry_at=NULL, last_error_class=?, updated_at=? WHERE job_id=? "
                "AND status='running' AND claim_token=? AND lease_expires_at>?",
                (error_class, now, job_id, claim_token, now),
            )
            return cursor.rowcount == 1

    def reconcile(
        self,
        *,
        now: float | None = None,
        heartbeat_grace_seconds: float = DEFAULT_STALE_HEARTBEAT_GRACE_SECONDS,
    ) -> int:
        timestamp = time.time() if now is None else float(now)
        with self._write() as conn:
            return self._recover_stale_locked(
                conn,
                timestamp,
                heartbeat_grace_seconds=heartbeat_grace_seconds,
            )

    def _recover_stale_locked(
        self,
        conn: sqlite3.Connection,
        now: float,
        *,
        heartbeat_grace_seconds: float = DEFAULT_STALE_HEARTBEAT_GRACE_SECONDS,
    ) -> int:
        rows = conn.execute(
            "SELECT job_id, owner_host, owner_pid, owner_started_at, lease_expires_at, heartbeat_at "
            "FROM jobs WHERE status='running' AND lease_expires_at IS NOT NULL AND lease_expires_at<=?",
            (now,),
        ).fetchall()
        recovered = 0
        for row in rows:
            if _owner_is_live(
                row[1],
                row[2],
                row[3],
                row[5],
                now=now,
                heartbeat_grace_seconds=max(1.0, float(heartbeat_grace_seconds)),
            ):
                continue
            conn.execute(
                "UPDATE jobs SET status='queued', claim_token=NULL, owner_pid=NULL, owner_host=NULL, "
                "owner_started_at=NULL, lease_expires_at=NULL, heartbeat_at=NULL, next_retry_at=?, updated_at=?, "
                "last_error_class='stale_lease' WHERE job_id=? AND status='running'",
                (now, now, row[0]),
            )
            recovered += 1
        return recovered

    def retry(self, job_id: str) -> bool:
        now = time.time()
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status='queued', attempt_count=0, next_retry_at=NULL, "
                "last_error_class=NULL, updated_at=? "
                "WHERE job_id=? AND status IN ('failed','quarantined','operator_blocked')",
                (now, job_id),
            )
            return cursor.rowcount == 1

    def quarantine(self, job_id: str) -> bool:
        now = time.time()
        with self._write() as conn:
            cursor = conn.execute(
                "UPDATE jobs SET status='quarantined', next_retry_at=NULL, updated_at=? WHERE job_id=? "
                "AND status IN ('queued','failed')",
                (now, job_id),
            )
            return cursor.rowcount == 1

    def record_external_receipt(self, receipt: ExternalReceipt) -> bool:
        with self._write() as conn:
            existing = conn.execute(
                "SELECT artifact_id, receipt_kind FROM external_receipts "
                "WHERE provider_id=? AND external_id=?",
                (receipt.provider_id, receipt.external_id),
            ).fetchone()
            if existing is not None:
                if existing[0] != receipt.artifact_id or existing[1] != receipt.receipt_kind:
                    raise ValueError("external receipt identity was reused with different metadata")
                return False
            cursor = conn.execute(
                "INSERT OR IGNORE INTO external_receipts(provider_id, external_id, artifact_id, receipt_kind, recorded_at) "
                "VALUES(?,?,?,?,?)",
                (receipt.provider_id, receipt.external_id, receipt.artifact_id, receipt.receipt_kind, receipt.recorded_at),
            )
            return cursor.rowcount == 1

    def get_cursor(self, name: str) -> str | None:
        name = self._cursor_name(name)
        with self._connect() as conn:
            row = conn.execute("SELECT cursor_value FROM cursors WHERE cursor_name=?", (name,)).fetchone()
            return str(row[0]) if row else None

    def set_cursor(self, name: str, value: str) -> None:
        name = self._cursor_name(name)
        value = str(value)
        if not value or len(value) > 4096 or any(ord(ch) < 32 for ch in value):
            raise ValueError("cursor value must be bounded printable text")
        now = time.time()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO cursors(cursor_name, cursor_value, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(cursor_name) DO UPDATE SET cursor_value=excluded.cursor_value, updated_at=excluded.updated_at",
                (name, str(value), now),
            )

    @staticmethod
    def _cursor_name(value: Any) -> str:
        name = str(value or "").strip().lower()
        if not _CURSOR_NAME_RE.fullmatch(name):
            raise ValueError("cursor name is not canonical")
        return name

    def list_jobs(self, *, status: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        if status and status not in _JOB_STATUSES:
            raise ValueError("unknown job status")
        query = (
            "SELECT job_id, artifact_id, stage, status, attempt_count, max_attempts, last_error_class, "
            "created_at, updated_at, next_retry_at, lease_expires_at FROM jobs"
        )
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY updated_at DESC, job_id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(query, params).fetchall()]

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT job_id, artifact_id, stage, status, attempt_count, max_attempts, last_error_class, "
                "created_at, updated_at, next_retry_at, lease_expires_at FROM jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            return dict(row) if row else None

    def stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
        result = {status: 0 for status in _JOB_STATUSES}
        result.update({str(row[0]): int(row[1]) for row in rows})
        result["total"] = sum(result.values())
        return result


# Compact aliases make future stage workers less coupled to the class name.
ClientKnowledgeStore = IntakeStore
resolve_intake_store_path = resolve_store_path


__all__ = [
    "ClientKnowledgeStore",
    "DEFAULT_DB_RELATIVE_PATH",
    "DEFAULT_LEASE_SECONDS",
    "IntakeStore",
    "JobClaim",
    "resolve_intake_store_path",
    "resolve_store_path",
]
