"""SQLite storage and read-only service for self-improvement proposals."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

from self_improvement.proposals import ProposalValidationError, validate_proposal_run

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.IGNORECASE | re.DOTALL)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def proposals_db_path() -> Path:
    """Return the profile-scoped proposal DB path."""

    return get_hermes_home() / "self_improvement" / "proposals.db"


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or proposals_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path | None = None) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS proposal_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_key TEXT NOT NULL UNIQUE,
                contract_version TEXT,
                project TEXT,
                prong TEXT,
                run_id TEXT,
                cron_job_id TEXT,
                cron_job_name TEXT,
                cron_output_path TEXT,
                source_url TEXT,
                generated_at TEXT,
                created_at TEXT,
                completed_at TEXT,
                status TEXT NOT NULL,
                card_count INTEGER NOT NULL DEFAULT 0,
                parse_error TEXT,
                payload_json TEXT,
                human_markdown TEXT NOT NULL DEFAULT '',
                source_markdown TEXT NOT NULL DEFAULT '',
                source_ref_json TEXT NOT NULL DEFAULT '{}',
                ingested_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS proposal_cards (
                proposal_id TEXT PRIMARY KEY,
                run_db_id INTEGER NOT NULL REFERENCES proposal_runs(id) ON DELETE CASCADE,
                project TEXT NOT NULL,
                prong TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL,
                body TEXT NOT NULL,
                rationale TEXT NOT NULL,
                priority TEXT NOT NULL,
                severity TEXT,
                status TEXT NOT NULL,
                idempotency_key TEXT,
                created_at TEXT NOT NULL,
                source_excerpts_json TEXT NOT NULL DEFAULT '[]',
                kanban_task_json TEXT NOT NULL DEFAULT '{}',
                payload_json TEXT NOT NULL,
                kanban_task_id TEXT,
                worker_url TEXT,
                rejected_reason TEXT,
                archived_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_proposal_cards_group
                ON proposal_cards(project, prong, priority, created_at);
            CREATE INDEX IF NOT EXISTS idx_proposal_cards_run
                ON proposal_cards(run_db_id);

            CREATE TABLE IF NOT EXISTS proposal_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                body TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS proposal_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL,
                action TEXT NOT NULL,
                actor TEXT,
                kanban_task_id TEXT,
                reason TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            INSERT OR IGNORE INTO schema_migrations(version, applied_at)
            VALUES (1, CURRENT_TIMESTAMP);
            """
        )
        _ensure_card_action_columns(conn)
        conn.commit()
    finally:
        conn.close()


def _json_dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _ensure_card_action_columns(conn: sqlite3.Connection) -> None:
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(proposal_cards)")}
    for name, ddl in {
        "kanban_task_id": "kanban_task_id TEXT",
        "worker_url": "worker_url TEXT",
        "rejected_reason": "rejected_reason TEXT",
        "archived_at": "archived_at TEXT",
    }.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE proposal_cards ADD COLUMN {ddl}")


def _parse_payload(source_markdown: str) -> dict[str, Any]:
    match = _JSON_FENCE_RE.search(source_markdown)
    raw = match.group(1) if match else source_markdown.strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProposalValidationError(f"proposal JSON parse error at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ProposalValidationError("proposal JSON root must be an object")
    return payload


def _source_key(payload: dict[str, Any] | None, source_markdown: str, source: dict[str, Any]) -> str:
    explicit = source.get("source_key")
    if explicit:
        return str(explicit)
    run = payload.get("run") if isinstance(payload, dict) else None
    if isinstance(run, dict):
        parts = [run.get("cron_job_id"), run.get("run_id"), run.get("cron_output_path")]
        key = ":".join(str(part) for part in parts if part)
        if key:
            return key
    for key_name in ("cron_job_id", "run_id", "cron_output_path"):
        if source.get(key_name):
            return str(source[key_name])
    return "sha256:" + hashlib.sha256(source_markdown.encode("utf-8")).hexdigest()


def ingest_proposal_output(
    source_markdown: str,
    *,
    source: dict[str, Any] | None = None,
    db_path: Path | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse and persist a cron proposal output idempotently.

    Re-ingesting the same cron run replaces that run's cards instead of creating
    duplicates. Malformed output is still recorded with enough diagnostic detail
    for operators to inspect the source markdown/reference.
    """

    init_db(db_path)
    source = dict(source or {})
    parsed_payload: dict[str, Any] | None = None
    normalized: dict[str, Any] | None = None
    parse_error: str | None = None
    try:
        parsed_payload = _parse_payload(source_markdown)
        normalized = validate_proposal_run(parsed_payload, config=config)
    except ProposalValidationError as exc:
        parse_error = str(exc)

    key = _source_key(parsed_payload, source_markdown, source)
    now = utc_now()
    run = normalized.get("run", {}) if normalized else {}
    status = "malformed" if parse_error else ("empty" if not normalized.get("cards") else "valid")
    card_count = len(normalized.get("cards", [])) if normalized else 0

    conn = connect(db_path)
    try:
        with conn:
            existing = conn.execute("SELECT id, ingested_at FROM proposal_runs WHERE source_key = ?", (key,)).fetchone()
            ingested_at = existing["ingested_at"] if existing else now
            conn.execute(
                """
                INSERT INTO proposal_runs(
                    source_key, contract_version, project, prong, run_id, cron_job_id,
                    cron_job_name, cron_output_path, source_url, generated_at, created_at,
                    completed_at, status, card_count, parse_error, payload_json,
                    human_markdown, source_markdown, source_ref_json, ingested_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    contract_version=excluded.contract_version,
                    project=excluded.project,
                    prong=excluded.prong,
                    run_id=excluded.run_id,
                    cron_job_id=excluded.cron_job_id,
                    cron_job_name=excluded.cron_job_name,
                    cron_output_path=excluded.cron_output_path,
                    source_url=excluded.source_url,
                    generated_at=excluded.generated_at,
                    created_at=excluded.created_at,
                    completed_at=excluded.completed_at,
                    status=excluded.status,
                    card_count=excluded.card_count,
                    parse_error=excluded.parse_error,
                    payload_json=excluded.payload_json,
                    human_markdown=excluded.human_markdown,
                    source_markdown=excluded.source_markdown,
                    source_ref_json=excluded.source_ref_json,
                    updated_at=excluded.updated_at
                """,
                (
                    key,
                    normalized.get("contract_version") if normalized else parsed_payload.get("contract_version") if parsed_payload else None,
                    normalized.get("project") if normalized else parsed_payload.get("project") if parsed_payload else None,
                    normalized.get("prong") if normalized else parsed_payload.get("prong") if parsed_payload else None,
                    run.get("run_id") or source.get("run_id"),
                    run.get("cron_job_id") or source.get("cron_job_id"),
                    run.get("cron_job_name") or source.get("cron_job_name"),
                    run.get("cron_output_path") or source.get("cron_output_path"),
                    run.get("source_url") or source.get("source_url"),
                    normalized.get("generated_at") if normalized else parsed_payload.get("generated_at") if parsed_payload else None,
                    run.get("created_at"),
                    run.get("completed_at"),
                    status,
                    card_count,
                    parse_error,
                    _json_dumps(normalized or parsed_payload or {}),
                    normalized.get("human_markdown", "") if normalized else "",
                    source_markdown,
                    _json_dumps(source),
                    ingested_at,
                    now,
                ),
            )
            row = conn.execute("SELECT id FROM proposal_runs WHERE source_key = ?", (key,)).fetchone()
            run_db_id = int(row["id"])
            conn.execute("DELETE FROM proposal_cards WHERE run_db_id = ?", (run_db_id,))
            if normalized:
                for card in normalized["cards"]:
                    existing_card = conn.execute(
                        "SELECT kanban_task_id, worker_url, rejected_reason, archived_at FROM proposal_cards WHERE proposal_id = ?",
                        (card["proposal_id"],),
                    ).fetchone()
                    action_state = dict(existing_card) if existing_card else {}
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO proposal_cards(
                            proposal_id, run_db_id, project, prong, title, summary, body,
                            rationale, priority, severity, status, idempotency_key,
                            created_at, source_excerpts_json, kanban_task_json,
                            payload_json, kanban_task_id, worker_url, rejected_reason,
                            archived_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            card["proposal_id"],
                            run_db_id,
                            normalized["project"],
                            normalized["prong"],
                            card["title"],
                            card["summary"],
                            card["body"],
                            card["rationale"],
                            card["priority"],
                            card.get("severity"),
                            card["status"],
                            card.get("idempotency_key"),
                            card["created_at"],
                            _json_dumps(card.get("source_excerpts", [])),
                            _json_dumps(card.get("kanban_task", {})),
                            _json_dumps(card),
                            action_state.get("kanban_task_id"),
                            action_state.get("worker_url"),
                            action_state.get("rejected_reason"),
                            action_state.get("archived_at"),
                            now,
                        ),
                    )
        return {"run_id": run_db_id, "source_key": key, "status": status, "card_count": card_count, "parse_error": parse_error}
    finally:
        conn.close()


def _row_to_run(row: sqlite3.Row, *, include_source: bool = False) -> dict[str, Any]:
    data = dict(row)
    data["source_ref"] = json.loads(data.pop("source_ref_json") or "{}")
    data["payload"] = json.loads(data.pop("payload_json") or "{}")
    if not include_source:
        data.pop("source_markdown", None)
    return data


def _row_to_card(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    data["source_excerpts"] = json.loads(data.pop("source_excerpts_json") or "[]")
    data["kanban_task"] = json.loads(data.pop("kanban_task_json") or "{}")
    data["payload"] = json.loads(data.pop("payload_json") or "{}")
    return data


def grouped_cards(*, db_path: Path | None = None) -> dict[str, Any]:
    init_db(db_path)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT c.*, r.source_key, r.run_id, r.cron_job_id, r.cron_output_path
            FROM proposal_cards c
            JOIN proposal_runs r ON r.id = c.run_db_id
            WHERE c.status != 'rejected' AND c.archived_at IS NULL
            ORDER BY c.project, c.prong, c.created_at DESC, c.proposal_id
            """
        ).fetchall()
    finally:
        conn.close()
    projects: dict[str, dict[str, Any]] = {}
    for row in rows:
        card = _row_to_card(row)
        project = projects.setdefault(card["project"], {"project": card["project"], "prongs": {}})
        prong = project["prongs"].setdefault(card["prong"], {"prong": card["prong"], "cards": []})
        prong["cards"].append(card)
    return {
        "projects": [
            {"project": project["project"], "prongs": list(project["prongs"].values())}
            for project in projects.values()
        ]
    }


def record_approval(
    proposal_id: str,
    *,
    kanban_task_id: str,
    worker_url: str,
    actor: str | None = None,
    metadata: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    now = utc_now()
    conn = connect(db_path)
    try:
        with conn:
            row = conn.execute("SELECT * FROM proposal_cards WHERE proposal_id = ?", (proposal_id,)).fetchone()
            if not row:
                raise KeyError(proposal_id)
            conn.execute(
                """
                UPDATE proposal_cards
                SET status = 'approved', kanban_task_id = ?, worker_url = ?,
                    rejected_reason = NULL, archived_at = NULL, updated_at = ?
                WHERE proposal_id = ?
                """,
                (kanban_task_id, worker_url, now, proposal_id),
            )
            conn.execute(
                """
                INSERT INTO proposal_audit_events(proposal_id, action, actor, kanban_task_id, metadata_json, created_at)
                VALUES (?, 'approved', ?, ?, ?, ?)
                """,
                (proposal_id, actor, kanban_task_id, _json_dumps(metadata or {}), now),
            )
        card = get_card(proposal_id, db_path=db_path)
        assert card is not None
        return card
    finally:
        conn.close()


def record_rejection(
    proposal_id: str,
    *,
    reason: str,
    actor: str | None = None,
    metadata: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    init_db(db_path)
    now = utc_now()
    conn = connect(db_path)
    try:
        with conn:
            row = conn.execute("SELECT * FROM proposal_cards WHERE proposal_id = ?", (proposal_id,)).fetchone()
            if not row:
                raise KeyError(proposal_id)
            conn.execute(
                """
                UPDATE proposal_cards
                SET status = 'rejected', rejected_reason = ?, archived_at = ?, updated_at = ?
                WHERE proposal_id = ?
                """,
                (reason, now, now, proposal_id),
            )
            conn.execute(
                """
                INSERT INTO proposal_feedback(proposal_id, kind, body, metadata_json, created_at)
                VALUES (?, 'rejected', ?, ?, ?)
                """,
                (proposal_id, reason, _json_dumps(metadata or {}), now),
            )
            conn.execute(
                """
                INSERT INTO proposal_audit_events(proposal_id, action, actor, reason, metadata_json, created_at)
                VALUES (?, 'rejected', ?, ?, ?, ?)
                """,
                (proposal_id, actor, reason, _json_dumps(metadata or {}), now),
            )
        card = get_card(proposal_id, db_path=db_path)
        assert card is not None
        return card
    finally:
        conn.close()


def list_audit_events(proposal_id: str, *, db_path: Path | None = None) -> list[dict[str, Any]]:
    init_db(db_path)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM proposal_audit_events
            WHERE proposal_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (proposal_id,),
        ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["metadata"] = json.loads(event.pop("metadata_json") or "{}")
            events.append(event)
        return events
    finally:
        conn.close()


def get_card(proposal_id: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    init_db(db_path)
    conn = connect(db_path)
    try:
        row = conn.execute(
            """
            SELECT c.*, r.source_key, r.run_id, r.cron_job_id, r.cron_output_path
            FROM proposal_cards c
            JOIN proposal_runs r ON r.id = c.run_db_id
            WHERE c.proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()
        return _row_to_card(row) if row else None
    finally:
        conn.close()


def get_run(run_id: int | str, *, db_path: Path | None = None, include_source: bool = True) -> dict[str, Any] | None:
    init_db(db_path)
    conn = connect(db_path)
    try:
        if isinstance(run_id, int) or str(run_id).isdigit():
            row = conn.execute("SELECT * FROM proposal_runs WHERE id = ?", (int(run_id),)).fetchone()
        else:
            row = conn.execute("SELECT * FROM proposal_runs WHERE source_key = ? OR run_id = ?", (str(run_id), str(run_id))).fetchone()
        if not row:
            return None
        run = _row_to_run(row, include_source=include_source)
        cards = conn.execute("SELECT * FROM proposal_cards WHERE run_db_id = ? ORDER BY created_at DESC, proposal_id", (run["id"],)).fetchall()
        run["cards"] = [_row_to_card(card) for card in cards]
        return run
    finally:
        conn.close()


def list_parse_failures(*, db_path: Path | None = None) -> dict[str, Any]:
    init_db(db_path)
    conn = connect(db_path)
    try:
        rows = conn.execute(
            """
            SELECT * FROM proposal_runs
            WHERE status = 'malformed' OR parse_error IS NOT NULL
            ORDER BY updated_at DESC, id DESC
            """
        ).fetchall()
        return {"failures": [_row_to_run(row, include_source=False) for row in rows]}
    finally:
        conn.close()
