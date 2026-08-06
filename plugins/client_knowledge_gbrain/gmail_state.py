"""Durable Gmail discovery manifests and cursor publication."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .store import IntakeStore

if TYPE_CHECKING:
    from .spool import RawSpool


TERMINAL_DISPOSITIONS = frozenset(
    {
        "admitted",
        "needs_mapping",
        "quarantined",
        "gone",
        "rejected_pre_cutover",
        "rejected_cutover_precision",
        "rejected_oversize_actual",
        "rejected_invalid_provider_raw_consistent",
    }
)
_ARTIFACT_DISPOSITIONS = frozenset({"admitted", "needs_mapping", "quarantined"})


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _opaque_id(kind: str, *parts: str) -> str:
    return hashlib.sha256((kind + "\0" + "\0".join(parts)).encode()).hexdigest()


def _history_id(value: Any) -> str:
    result = str(value or "").strip()
    if not result.isdigit() or len(result) > 32:
        raise ValueError("Gmail history ID is invalid")
    return result


def _mailbox(value: Any) -> str:
    result = str(value or "").strip().lower()
    if not result or len(result) > 320 or "@" not in result or any(ord(ch) < 33 for ch in result):
        raise ValueError("Gmail mailbox is invalid")
    return result


@dataclass(frozen=True, slots=True)
class GmailMailboxState:
    mailbox: str
    cutover_history_id: str
    cursor_history_id: str
    bracket_start_server_ms: int
    bracket_end_server_ms: int
    admit_after_server_ms: int


@dataclass(frozen=True, slots=True)
class InvalidRawResult:
    """Outcome of disposition-fenced malformed-provider accounting."""

    stop: bool
    terminal: bool
    adopted: bool
    disposition: str
    count: int


class GmailState:
    """Gmail-specific state layered onto the shared intake database."""

    def __init__(self, store: IntakeStore) -> None:
        self.store = store

    def get_mailbox(self, mailbox: str) -> GmailMailboxState | None:
        mailbox = _mailbox(mailbox)
        with self.store._connect() as conn:
            row = conn.execute(
                "SELECT mailbox, cutover_history_id, cursor_history_id, "
                "bracket_start_server_ms, bracket_end_server_ms, admit_after_server_ms "
                "FROM gmail_mailboxes WHERE mailbox=?",
                (mailbox,),
            ).fetchone()
        return GmailMailboxState(*row) if row else None

    def initialize_mailbox(
        self,
        mailbox: str,
        *,
        cutover_history_id: str,
        bracket_start_server_ms: int,
        bracket_end_server_ms: int,
        admit_after_server_ms: int,
        now: float | None = None,
    ) -> GmailMailboxState:
        mailbox = _mailbox(mailbox)
        cutover = _history_id(cutover_history_id)
        start = int(bracket_start_server_ms)
        end = int(bracket_end_server_ms)
        admit = int(admit_after_server_ms)
        if start < 0 or end < start or admit <= end:
            raise ValueError("Gmail cutover bracket is invalid")
        timestamp = time.time() if now is None else float(now)
        with self.store._write() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO gmail_mailboxes(mailbox, cutover_history_id, "
                "cursor_history_id, bracket_start_server_ms, bracket_end_server_ms, "
                "admit_after_server_ms, initialized_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (mailbox, cutover, cutover, start, end, admit, timestamp, timestamp),
            )
            row = conn.execute(
                "SELECT mailbox, cutover_history_id, cursor_history_id, "
                "bracket_start_server_ms, bracket_end_server_ms, admit_after_server_ms "
                "FROM gmail_mailboxes WHERE mailbox=?",
                (mailbox,),
            ).fetchone()
            if row is None or tuple(row) != (mailbox, cutover, cutover, start, end, admit):
                raise ValueError("Gmail mailbox cutover is already initialized differently")
        return GmailMailboxState(*row)

    def get_or_create_batch(
        self,
        mailbox: str,
        expected_cursor: str,
        *,
        config_hash: str,
        alias_count: int,
        now: float | None = None,
    ) -> dict[str, Any]:
        mailbox = _mailbox(mailbox)
        expected = _history_id(expected_cursor)
        if len(config_hash) != 64 or any(ch not in "0123456789abcdef" for ch in config_hash):
            raise ValueError("Gmail batch config hash is invalid")
        aliases = int(alias_count)
        if aliases < 0:
            raise ValueError("Gmail batch alias count is invalid")
        timestamp = time.time() if now is None else float(now)
        with self.store._write() as conn:
            state = conn.execute(
                "SELECT cursor_history_id FROM gmail_mailboxes WHERE mailbox=?", (mailbox,)
            ).fetchone()
            if state is None or str(state[0]) != expected:
                raise ValueError("Gmail expected cursor is not current")
            row = conn.execute(
                "SELECT * FROM gmail_batches WHERE mailbox=? AND expected_cursor=? "
                "AND status='active'",
                (mailbox, expected),
            ).fetchone()
            if row is None:
                generation = int(conn.execute(
                    "SELECT COALESCE(MAX(generation), 0) + 1 FROM gmail_batches "
                    "WHERE mailbox=? AND expected_cursor=?",
                    (mailbox, expected),
                ).fetchone()[0])
                batch_id = _opaque_id("gmail-batch", mailbox, expected, str(generation))
                conn.execute(
                    "INSERT INTO gmail_batches(batch_id, mailbox, expected_cursor, generation, "
                    "config_hash, alias_count, created_at) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, mailbox, expected, generation, config_hash, aliases, timestamp),
                )
                row = conn.execute(
                    "SELECT * FROM gmail_batches WHERE batch_id=?", (batch_id,)
                ).fetchone()
            batch_id = _opaque_id(
                "gmail-batch", mailbox, expected, str(int(row["generation"]))
            )
            if (
                row["batch_id"] != batch_id
                or row["config_hash"] != config_hash
                or int(row["alias_count"]) != aliases
            ):
                raise ValueError("Gmail active batch configuration changed")
            return dict(row)

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        with self.store._connect() as conn:
            row = conn.execute("SELECT * FROM gmail_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise ValueError("Gmail batch does not exist")
        return dict(row)

    def next_history_page(self, batch_id: str) -> tuple[int, str] | None:
        with self.store._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM gmail_history_completion WHERE batch_id=?", (batch_id,)
            ).fetchone():
                return None
            row = conn.execute(
                "SELECT page_ordinal, next_token FROM gmail_history_pages WHERE batch_id=? "
                "ORDER BY page_ordinal DESC LIMIT 1",
                (batch_id,),
            ).fetchone()
        if row is None:
            return 0, ""
        return (int(row[0]) + 1, str(row[1])) if row[1] else None

    def register_history_page(
        self,
        batch_id: str,
        *,
        page_ordinal: int,
        request_token: str,
        next_token: str,
        response_history_id: str,
        candidates: Iterable[tuple[str, str]],
        now: float | None = None,
    ) -> None:
        ordinal = int(page_ordinal)
        if ordinal < 0:
            raise ValueError("Gmail history page ordinal is invalid")
        response_history = _history_id(response_history_id)
        ordered = []
        for message_id, addition_history_id in candidates:
            message = str(message_id or "").strip()
            addition = _history_id(addition_history_id)
            if not message or len(message) > 500:
                raise ValueError("Gmail message ID is invalid")
            ordered.append((message, addition))
        manifest_hash = _digest(ordered)
        timestamp = time.time() if now is None else float(now)
        with self.store._write() as conn:
            batch = conn.execute(
                "SELECT mailbox, expected_cursor FROM gmail_batches WHERE batch_id=? AND status='active'", (batch_id,)
            ).fetchone()
            if batch is None:
                raise ValueError("Gmail active batch does not exist")
            if int(response_history) < int(batch[1]):
                raise ValueError("Gmail history response moved the cursor backward")
            previous = conn.execute(
                "SELECT next_token FROM gmail_history_pages WHERE batch_id=? AND page_ordinal=?",
                (batch_id, ordinal - 1),
            ).fetchone()
            if (ordinal == 0 and request_token) or (
                ordinal > 0 and (previous is None or str(previous[0]) != str(request_token))
            ):
                raise ValueError("Gmail history page token chain is incomplete")
            existing = conn.execute(
                "SELECT request_token, next_token, response_history_id, manifest_hash, candidate_count "
                "FROM gmail_history_pages WHERE batch_id=? AND page_ordinal=?",
                (batch_id, ordinal),
            ).fetchone()
            expected_page = (
                str(request_token), str(next_token), response_history, manifest_hash, len(ordered)
            )
            if existing is not None:
                if tuple(existing) != expected_page:
                    raise ValueError("Gmail history page receipt changed")
                return
            for discovered_ordinal, (message_id, addition_id) in enumerate(ordered):
                candidate_id = _opaque_id("gmail-candidate", str(batch[0]), message_id, addition_id)
                conn.execute(
                    "INSERT OR IGNORE INTO gmail_history_candidates(candidate_id, mailbox, message_id, "
                    "addition_history_id, updated_at) VALUES(?,?,?,?,?)",
                    (candidate_id, batch[0], message_id, addition_id, timestamp),
                )
                row = conn.execute(
                    "SELECT mailbox, message_id, addition_history_id FROM gmail_history_candidates "
                    "WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                if row is None or tuple(row) != (batch[0], message_id, addition_id):
                    raise ValueError("Gmail candidate identity changed")
                conn.execute(
                    "INSERT OR IGNORE INTO gmail_batch_candidates(batch_id, candidate_id, page_ordinal, "
                    "discovered_ordinal, adopted_at) VALUES(?,?,?,?,?)",
                    (batch_id, candidate_id, ordinal, discovered_ordinal, timestamp),
                )
                conn.execute(
                    "INSERT INTO gmail_history_page_items(batch_id, page_ordinal, discovered_ordinal, "
                    "candidate_id) VALUES(?,?,?,?)",
                    (batch_id, ordinal, discovered_ordinal, candidate_id),
                )
            conn.execute(
                "INSERT INTO gmail_history_pages(batch_id, page_ordinal, request_token, next_token, "
                "response_history_id, manifest_hash, candidate_count, recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                (batch_id, ordinal, str(request_token), str(next_token), response_history, manifest_hash, len(ordered), timestamp),
            )

    @staticmethod
    def _history_manifest_locked(conn: sqlite3.Connection, batch_id: str) -> tuple[str, int, int, str]:
        pages = conn.execute(
            "SELECT page_ordinal, request_token, next_token, response_history_id, manifest_hash, "
            "candidate_count FROM gmail_history_pages WHERE batch_id=? ORDER BY page_ordinal",
            (batch_id,),
        ).fetchall()
        if not pages:
            raise ValueError("Gmail history manifest is empty")
        expected_token = ""
        chain = []
        total = 0
        for expected_ordinal, page in enumerate(pages):
            if int(page[0]) != expected_ordinal or str(page[1]) != expected_token:
                raise ValueError("Gmail history manifest has a gap")
            associations = conn.execute(
                "SELECT c.message_id, c.addition_history_id FROM gmail_history_page_items pi "
                "JOIN gmail_history_candidates c ON c.candidate_id=pi.candidate_id "
                "WHERE pi.batch_id=? AND pi.page_ordinal=? ORDER BY pi.discovered_ordinal",
                (batch_id, expected_ordinal),
            ).fetchall()
            values = [(str(row[0]), str(row[1])) for row in associations]
            if len(values) != int(page[5]) or _digest(values) != str(page[4]):
                raise ValueError("Gmail history page manifest is incomplete")
            total += len(values)
            chain.append(tuple(page))
            expected_token = str(page[2])
        if expected_token:
            raise ValueError("Gmail history paging is not complete")
        unique_associations = conn.execute(
            "SELECT COUNT(*) FROM gmail_batch_candidates WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        manifested_unique = conn.execute(
            "SELECT COUNT(DISTINCT candidate_id) FROM gmail_history_page_items WHERE batch_id=?",
            (batch_id,),
        ).fetchone()[0]
        if int(unique_associations) != int(manifested_unique):
            raise ValueError("Gmail history associations are outside the manifest")
        return str(pages[-1][3]), len(pages), int(unique_associations), _digest(chain)

    def complete_history(self, batch_id: str, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else float(now)
        with self.store._write() as conn:
            final_id, page_count, candidate_count, chain_hash = self._history_manifest_locked(conn, batch_id)
            conn.execute(
                "INSERT OR IGNORE INTO gmail_history_completion(batch_id, final_history_id, page_count, "
                "candidate_count, chain_hash, completed_at) VALUES(?,?,?,?,?,?)",
                (batch_id, final_id, page_count, candidate_count, chain_hash, timestamp),
            )
            row = conn.execute(
                "SELECT final_history_id, page_count, candidate_count, chain_hash, recovery_mode "
                "FROM gmail_history_completion WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if tuple(row) != (final_id, page_count, candidate_count, chain_hash, 0):
                raise ValueError("Gmail history completion receipt changed")
            conn.execute("UPDATE gmail_batches SET target_cursor=? WHERE batch_id=?", (final_id, batch_id))

    def complete_history_recovery(
        self, batch_id: str, recovery_anchor: str, *, now: float | None = None
    ) -> None:
        anchor = _history_id(recovery_anchor)
        timestamp = time.time() if now is None else float(now)
        chain_hash = _digest(["history_404", anchor])
        with self.store._write() as conn:
            row = conn.execute(
                "SELECT 1 FROM gmail_history_pages WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if row is not None:
                raise ValueError("Gmail recovery cannot replace registered history pages")
            batch = conn.execute(
                "SELECT expected_cursor FROM gmail_batches WHERE batch_id=? AND status='active'",
                (batch_id,),
            ).fetchone()
            if batch is None or int(anchor) < int(batch[0]):
                raise ValueError("Gmail recovery anchor moved the cursor backward")
            conn.execute(
                "UPDATE gmail_batches SET mode='history_404_recovery', target_cursor=?, "
                "recovery_reason='history_cursor_expired' WHERE batch_id=? AND status='active'",
                (anchor, batch_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO gmail_history_completion(batch_id, final_history_id, page_count, "
                "candidate_count, chain_hash, recovery_mode, completed_at) VALUES(?,?,?,?,?,?,?)",
                (batch_id, anchor, 0, 0, chain_hash, 1, timestamp),
            )
            receipt = conn.execute(
                "SELECT final_history_id, page_count, candidate_count, chain_hash, recovery_mode "
                "FROM gmail_history_completion WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if tuple(receipt) != (anchor, 0, 0, chain_hash, 1):
                raise ValueError("Gmail recovery anchor changed")

    def next_reconciliation_page(self, batch_id: str, alias_ordinal: int) -> tuple[int, str] | None:
        with self.store._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM gmail_reconciliation_completion WHERE batch_id=?", (batch_id,)
            ).fetchone():
                return None
            row = conn.execute(
                "SELECT page_ordinal, next_token FROM gmail_reconciliation_pages "
                "WHERE batch_id=? AND alias_ordinal=? ORDER BY page_ordinal DESC LIMIT 1",
                (batch_id, int(alias_ordinal)),
            ).fetchone()
        return (0, "") if row is None else ((int(row[0]) + 1, str(row[1])) if row[1] else None)

    def register_reconciliation_page(
        self,
        batch_id: str,
        *,
        alias_ordinal: int,
        page_ordinal: int,
        request_token: str,
        next_token: str,
        message_ids: Iterable[str],
        now: float | None = None,
    ) -> None:
        alias_index = int(alias_ordinal)
        page_index = int(page_ordinal)
        if alias_index < 0 or page_index < 0:
            raise ValueError("Gmail reconciliation page ordinal is invalid")
        ordered = [str(value or "").strip() for value in message_ids]
        if any(not value or len(value) > 500 for value in ordered):
            raise ValueError("Gmail reconciliation message ID is invalid")
        manifest_hash = _digest(ordered)
        timestamp = time.time() if now is None else float(now)
        with self.store._write() as conn:
            batch = conn.execute(
                "SELECT mailbox, target_cursor, alias_count FROM gmail_batches "
                "WHERE batch_id=? AND status='active'", (batch_id,)
            ).fetchone()
            if batch is None or not batch[1] or alias_index >= int(batch[2]):
                raise ValueError("Gmail reconciliation batch is incomplete")
            previous = conn.execute(
                "SELECT next_token FROM gmail_reconciliation_pages WHERE batch_id=? "
                "AND alias_ordinal=? AND page_ordinal=?",
                (batch_id, alias_index, page_index - 1),
            ).fetchone()
            if (page_index == 0 and request_token) or (
                page_index > 0 and (previous is None or str(previous[0]) != str(request_token))
            ):
                raise ValueError("Gmail reconciliation token chain is incomplete")
            existing = conn.execute(
                "SELECT request_token, next_token, manifest_hash, observation_count "
                "FROM gmail_reconciliation_pages WHERE batch_id=? AND alias_ordinal=? AND page_ordinal=?",
                (batch_id, alias_index, page_index),
            ).fetchone()
            expected_page = (str(request_token), str(next_token), manifest_hash, len(ordered))
            if existing is not None:
                if tuple(existing) != expected_page:
                    raise ValueError("Gmail reconciliation page receipt changed")
                return
            for discovered_ordinal, message_id in enumerate(ordered):
                observation_id = _opaque_id(
                    "gmail-observation", str(batch[0]), message_id, str(batch[1])
                )
                conn.execute(
                    "INSERT OR IGNORE INTO gmail_reconciliation_observations(observation_id, mailbox, "
                    "message_id, anchor_history_id, updated_at) VALUES(?,?,?,?,?)",
                    (observation_id, batch[0], message_id, batch[1], timestamp),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO gmail_batch_observations(batch_id, observation_id, alias_ordinal, "
                    "page_ordinal, discovered_ordinal, adopted_at) VALUES(?,?,?,?,?,?)",
                    (batch_id, observation_id, alias_index, page_index, discovered_ordinal, timestamp),
                )
                conn.execute(
                    "INSERT INTO gmail_reconciliation_page_items(batch_id, alias_ordinal, page_ordinal, "
                    "discovered_ordinal, observation_id) VALUES(?,?,?,?,?)",
                    (batch_id, alias_index, page_index, discovered_ordinal, observation_id),
                )
            conn.execute(
                "INSERT INTO gmail_reconciliation_pages(batch_id, alias_ordinal, page_ordinal, "
                "request_token, next_token, manifest_hash, observation_count, recorded_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (batch_id, alias_index, page_index, str(request_token), str(next_token), manifest_hash, len(ordered), timestamp),
            )

    @staticmethod
    def _reconciliation_manifest_locked(
        conn: sqlite3.Connection, batch_id: str
    ) -> tuple[int, int, int, str]:
        batch = conn.execute(
            "SELECT alias_count FROM gmail_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise ValueError("Gmail batch does not exist")
        alias_count = int(batch[0])
        chain = []
        total = 0
        page_count = 0
        for alias_index in range(alias_count):
            pages = conn.execute(
                "SELECT page_ordinal, request_token, next_token, manifest_hash, observation_count "
                "FROM gmail_reconciliation_pages WHERE batch_id=? AND alias_ordinal=? "
                "ORDER BY page_ordinal",
                (batch_id, alias_index),
            ).fetchall()
            if not pages:
                raise ValueError("Gmail reconciliation alias is incomplete")
            expected_token = ""
            for expected_page, page in enumerate(pages):
                if int(page[0]) != expected_page or str(page[1]) != expected_token:
                    raise ValueError("Gmail reconciliation manifest has a gap")
                rows = conn.execute(
                    "SELECT o.message_id FROM gmail_reconciliation_page_items pi "
                    "JOIN gmail_reconciliation_observations o ON o.observation_id=pi.observation_id "
                    "WHERE pi.batch_id=? AND pi.alias_ordinal=? AND pi.page_ordinal=? "
                    "ORDER BY pi.discovered_ordinal",
                    (batch_id, alias_index, expected_page),
                ).fetchall()
                values = [str(row[0]) for row in rows]
                if len(values) != int(page[4]) or _digest(values) != str(page[3]):
                    raise ValueError("Gmail reconciliation page manifest is incomplete")
                total += len(values)
                page_count += 1
                chain.append((alias_index, *tuple(page)))
                expected_token = str(page[2])
            if expected_token:
                raise ValueError("Gmail reconciliation paging is not complete")
        unique_associations = conn.execute(
            "SELECT COUNT(*) FROM gmail_batch_observations WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        manifested_unique = conn.execute(
            "SELECT COUNT(DISTINCT observation_id) FROM gmail_reconciliation_page_items "
            "WHERE batch_id=?", (batch_id,)
        ).fetchone()[0]
        if int(unique_associations) != int(manifested_unique):
            raise ValueError("Gmail reconciliation associations are outside the manifest")
        return alias_count, page_count, int(unique_associations), _digest(chain)

    def complete_reconciliation(
        self, batch_id: str, *, max_observations: int, now: float | None = None
    ) -> None:
        timestamp = time.time() if now is None else float(now)
        with self.store._write() as conn:
            alias_count, page_count, observation_count, chain_hash = (
                self._reconciliation_manifest_locked(conn, batch_id)
            )
            result_count = int(conn.execute(
                "SELECT COALESCE(SUM(observation_count), 0) FROM gmail_reconciliation_pages "
                "WHERE batch_id=?", (batch_id,)
            ).fetchone()[0])
            if result_count > int(max_observations):
                raise ValueError("Gmail reconciliation exceeded its configured bound")
            conn.execute(
                "INSERT OR IGNORE INTO gmail_reconciliation_completion(batch_id, alias_count, page_count, "
                "observation_count, chain_hash, completed_at) VALUES(?,?,?,?,?,?)",
                (batch_id, alias_count, page_count, observation_count, chain_hash, timestamp),
            )
            row = conn.execute(
                "SELECT alias_count, page_count, observation_count, chain_hash "
                "FROM gmail_reconciliation_completion WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if tuple(row) != (alias_count, page_count, observation_count, chain_hash):
                raise ValueError("Gmail reconciliation completion receipt changed")

    def pending_items(self, batch_id: str) -> list[dict[str, Any]]:
        with self.store._connect() as conn:
            history = conn.execute(
                "SELECT 'candidate' AS item_kind, c.* FROM gmail_batch_candidates bc "
                "JOIN gmail_history_candidates c ON c.candidate_id=bc.candidate_id "
                "WHERE bc.batch_id=? AND c.disposition NOT IN (%s) ORDER BY bc.page_ordinal, bc.discovered_ordinal"
                % ",".join("?" for _ in TERMINAL_DISPOSITIONS),
                (batch_id, *sorted(TERMINAL_DISPOSITIONS)),
            ).fetchall()
            observations = conn.execute(
                "SELECT 'observation' AS item_kind, o.* FROM gmail_batch_observations bo "
                "JOIN gmail_reconciliation_observations o ON o.observation_id=bo.observation_id "
                "WHERE bo.batch_id=? AND o.disposition NOT IN (%s) "
                "ORDER BY bo.alias_ordinal, bo.page_ordinal, bo.discovered_ordinal"
                % ",".join("?" for _ in TERMINAL_DISPOSITIONS),
                (batch_id, *sorted(TERMINAL_DISPOSITIONS)),
            ).fetchall()
        return [dict(row) for row in (*history, *observations)]

    @staticmethod
    def _item_table(kind: str) -> tuple[str, str]:
        if kind == "candidate":
            return "gmail_history_candidates", "candidate_id"
        if kind == "observation":
            return "gmail_reconciliation_observations", "observation_id"
        raise ValueError("unknown Gmail item kind")

    def record_raw(
        self,
        kind: str,
        item_id: str,
        *,
        spool_key: str,
        storage_id: str,
        sha256: str,
        byte_size: int,
        message_history_id: str,
        internal_date_ms: int,
        now: float | None = None,
    ) -> None:
        table, key = self._item_table(kind)
        timestamp = time.time() if now is None else float(now)
        with self.store._write() as conn:
            conn.execute(
                f"UPDATE {table} SET disposition='raw_preserved', raw_spool_key=?, raw_storage_id=?, "
                f"raw_sha256=?, raw_byte_size=?, message_history_id=?, internal_date_ms=?, "
                f"invalid_fingerprint=NULL, invalid_error_class=NULL, "
                f"invalid_count=0, updated_at=? WHERE {key}=? AND disposition IN ('pending','raw_preserved')",
                (
                    spool_key, storage_id, sha256, int(byte_size),
                    _history_id(message_history_id),
                    int(internal_date_ms), timestamp, item_id,
                ),
            )

    def terminal_disposition(
        self,
        kind: str,
        item_id: str,
        disposition: str,
        *,
        error_class: str = "",
        artifact_id: str = "",
        now: float | None = None,
    ) -> None:
        if disposition not in TERMINAL_DISPOSITIONS:
            raise ValueError("Gmail disposition is not terminal")
        if disposition in _ARTIFACT_DISPOSITIONS and not artifact_id:
            raise ValueError("Gmail artifact disposition requires an artifact")
        if disposition not in _ARTIFACT_DISPOSITIONS and artifact_id:
            raise ValueError("Gmail non-artifact disposition cannot reference an artifact")
        table, key = self._item_table(kind)
        timestamp = time.time() if now is None else float(now)
        with self.store._write() as conn:
            cursor = conn.execute(
                f"UPDATE {table} SET disposition=?, error_class=?, artifact_id=?, updated_at=? "
                f"WHERE {key}=? AND disposition NOT IN ({','.join('?' for _ in TERMINAL_DISPOSITIONS)})",
                (
                    disposition,
                    error_class or None,
                    artifact_id or None,
                    timestamp,
                    item_id,
                    *sorted(TERMINAL_DISPOSITIONS),
                ),
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    f"SELECT disposition, COALESCE(error_class,''), COALESCE(artifact_id,'') "
                    f"FROM {table} WHERE {key}=?", (item_id,)
                ).fetchone()
                if row is None or tuple(row) != (disposition, error_class, artifact_id):
                    raise ValueError("Gmail terminal disposition changed")

    def record_invalid_raw(
        self,
        kind: str,
        item_id: str,
        *,
        fingerprint: str,
        error_class: str,
        threshold: int,
        now: float | None = None,
    ) -> InvalidRawResult:
        table, key = self._item_table(kind)
        if len(fingerprint) != 64:
            raise ValueError("Gmail invalid response fingerprint is invalid")
        timestamp = time.time() if now is None else float(now)
        with self.store._write() as conn:
            row = conn.execute(
                f"SELECT disposition, invalid_fingerprint, invalid_error_class, invalid_count "
                f"FROM {table} "
                f"WHERE {key}=?", (item_id,)
            ).fetchone()
            if row is None:
                raise ValueError("Gmail item does not exist")
            disposition = str(row[0])
            if disposition != "pending":
                return InvalidRawResult(
                    stop=True,
                    terminal=disposition in TERMINAL_DISPOSITIONS,
                    adopted=True,
                    disposition=disposition,
                    count=int(row[3]),
                )
            count = int(row[3]) + 1 if row[1] == fingerprint and row[2] == error_class else 1
            terminal = count >= max(1, int(threshold))
            next_disposition = (
                "rejected_invalid_provider_raw_consistent" if terminal else "pending"
            )
            cursor = conn.execute(
                f"UPDATE {table} SET invalid_fingerprint=?, invalid_error_class=?, invalid_count=?, "
                f"disposition=?, error_class=?, updated_at=? WHERE {key}=? "
                f"AND disposition='pending' AND invalid_count=?",
                (
                    fingerprint,
                    error_class,
                    count,
                    next_disposition,
                    error_class,
                    timestamp,
                    item_id,
                    int(row[3]),
                ),
            )
            if cursor.rowcount != 1:
                adopted = conn.execute(
                    f"SELECT disposition, invalid_count FROM {table} WHERE {key}=?",
                    (item_id,),
                ).fetchone()
                if adopted is None:
                    raise ValueError("Gmail item does not exist")
                adopted_disposition = str(adopted[0])
                return InvalidRawResult(
                    stop=adopted_disposition != "pending",
                    terminal=adopted_disposition in TERMINAL_DISPOSITIONS,
                    adopted=True,
                    disposition=adopted_disposition,
                    count=int(adopted[1]),
                )
            return InvalidRawResult(
                stop=terminal,
                terminal=terminal,
                adopted=False,
                disposition=next_disposition,
                count=count,
            )

    def finalize(
        self,
        batch_id: str,
        *,
        spool: "RawSpool | None" = None,
        now: float | None = None,
    ) -> str:
        timestamp = time.time() if now is None else float(now)
        with self.store._write() as conn:
            batch = conn.execute("SELECT * FROM gmail_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None or batch["status"] != "active":
                raise ValueError("Gmail batch is not active and complete")
            history = conn.execute(
                "SELECT * FROM gmail_history_completion WHERE batch_id=?", (batch_id,)
            ).fetchone()
            reconciliation = conn.execute(
                "SELECT * FROM gmail_reconciliation_completion WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if history is None or reconciliation is None:
                raise ValueError("Gmail discovery completion receipts are missing")
            if not batch["target_cursor"]:
                raise ValueError("Gmail batch target cursor is missing")
            if int(batch["target_cursor"]) < int(batch["expected_cursor"]):
                raise ValueError("Gmail target cursor moved backward")
            if int(history["recovery_mode"]):
                expected_history = (
                    str(batch["target_cursor"]), 0, 0,
                    _digest(["history_404", str(batch["target_cursor"])]), 1,
                )
            else:
                final_id, pages, candidates, chain = self._history_manifest_locked(conn, batch_id)
                expected_history = (final_id, pages, candidates, chain, 0)
            actual_history = (
                str(history["final_history_id"]), int(history["page_count"]),
                int(history["candidate_count"]), str(history["chain_hash"]),
                int(history["recovery_mode"]),
            )
            if actual_history != expected_history:
                raise ValueError("Gmail history completion manifest is invalid")
            aliases, pages, observations, chain = self._reconciliation_manifest_locked(conn, batch_id)
            if (
                int(reconciliation["alias_count"]), int(reconciliation["page_count"]),
                int(reconciliation["observation_count"]), str(reconciliation["chain_hash"]),
            ) != (aliases, pages, observations, chain):
                raise ValueError("Gmail reconciliation completion manifest is invalid")
            for table, association, key in (
                ("gmail_history_candidates", "gmail_batch_candidates", "candidate_id"),
                ("gmail_reconciliation_observations", "gmail_batch_observations", "observation_id"),
            ):
                rows = conn.execute(
                    f"SELECT item.disposition, item.artifact_id, item.raw_spool_key, item.raw_sha256, "
                    f"item.raw_byte_size FROM {association} assoc JOIN {table} item "
                    f"ON item.{key}=assoc.{key} WHERE assoc.batch_id=?", (batch_id,)
                ).fetchall()
                for row in rows:
                    if row[0] not in TERMINAL_DISPOSITIONS:
                        raise ValueError("Gmail batch still has pending candidates")
                    if row[0] in _ARTIFACT_DISPOSITIONS:
                        artifact = conn.execute(
                            "SELECT spool_key, spool_storage_id, content_sha256, byte_size, admission_state "
                            "FROM artifacts "
                            "WHERE artifact_id=?", (row[1],)
                        ).fetchone()
                        if artifact is None or (
                            artifact[0], artifact[2], artifact[3], artifact[4]
                        ) != (
                            row[2], row[3], row[4], "complete"
                        ):
                            raise ValueError("Gmail artifact receipt is incomplete")
                        if spool is None:
                            raise ValueError("Gmail spool verification is required")
                        family = conn.execute(
                            "SELECT spool_key, spool_storage_id, content_sha256, byte_size "
                            "FROM artifacts WHERE artifact_id=? OR parent_artifact_id=? "
                            "ORDER BY artifact_id",
                            (row[1], row[1]),
                        ).fetchall()
                        for member in family:
                            spool.verify(
                                str(member[0]),
                                storage_id=str(member[1] or ""),
                                expected_sha256=str(member[2]),
                                expected_size=int(member[3]),
                            )
                        expected_stage = {
                            "admitted": "notion_archived",
                            "needs_mapping": "needs_mapping",
                            "quarantined": "quarantined",
                        }[str(row[0])]
                        if conn.execute(
                            "SELECT 1 FROM jobs WHERE artifact_id=? AND stage=?",
                            (row[1], expected_stage),
                        ).fetchone() is None:
                            raise ValueError("Gmail downstream job receipt is incomplete")
            cursor = conn.execute(
                "UPDATE gmail_mailboxes SET cursor_history_id=?, updated_at=? "
                "WHERE mailbox=? AND cursor_history_id=?",
                (batch["target_cursor"], timestamp, batch["mailbox"], batch["expected_cursor"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("Gmail cursor compare-and-swap failed")
            conn.execute(
                "UPDATE gmail_batches SET status='committed', committed_at=? WHERE batch_id=?",
                (timestamp, batch_id),
            )
            return str(batch["target_cursor"])


__all__ = [
    "GmailMailboxState",
    "GmailState",
    "InvalidRawResult",
    "TERMINAL_DISPOSITIONS",
]
