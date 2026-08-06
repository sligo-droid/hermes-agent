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
CURRENT_SCHEMA_VERSION = 3
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

_JOB_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "quarantined"})
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
            if set(stages) != {"needs_mapping"}:
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

    def complete_stage(self, job_id: str, claim_token: str, receipt: StageReceipt) -> bool:
        now = time.time()
        with self._write() as conn:
            row = conn.execute(
                "SELECT artifact_id, stage FROM jobs WHERE job_id=? AND status='running' AND claim_token=?",
                (job_id, claim_token),
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
            conn.execute(
                "UPDATE jobs SET status='succeeded', claim_token=NULL, owner_pid=NULL, owner_host=NULL, "
                "owner_started_at=NULL, lease_expires_at=NULL, heartbeat_at=NULL, updated_at=?, last_error_class=NULL "
                "WHERE job_id=? AND status='running' AND claim_token=?",
                (now, job_id, claim_token),
            )
            return True

    record_stage_receipt = complete_stage

    def fail_stage(
        self,
        job_id: str,
        claim_token: str,
        *,
        error_class: str,
        retry_delay: float = 0,
        quarantine: bool = False,
    ) -> bool:
        now = time.time()
        status = "quarantined" if quarantine else "failed"
        next_retry = None if quarantine else now + max(0.0, float(retry_delay))
        with self._write() as conn:
            row = conn.execute(
                "SELECT attempt_count, max_attempts FROM jobs WHERE job_id=? AND status='running' AND claim_token=?",
                (job_id, claim_token),
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
                "WHERE job_id=? AND status='running' AND claim_token=?",
                (status, next_retry, error_class, now, job_id, claim_token),
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
                "WHERE job_id=? AND status IN ('failed','quarantined')",
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
