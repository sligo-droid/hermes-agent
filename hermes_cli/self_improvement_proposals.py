"""Storage and config helpers for Sligo self-improvement proposals."""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home
from hermes_cli.config import load_config

SCHEMA_VERSION = 2

PROPOSAL_BLOCK_KEY = "hermes_self_improvement_proposals"
STRUCTURED_CRON_PARSER = "cron_json_block"
STRUCTURED_CRON_PARSER_VERSION = "1"
LEGACY_CRON_PARSER = "legacy_parse"
LEGACY_CRON_PARSER_VERSION = "1"

RUN_PUBLIC_FIELDS = {
    "id",
    "project_key",
    "prong_key",
    "source_type",
    "source_id",
    "source_url",
    "source_title",
    "parser_name",
    "parser_version",
    "parser_metadata",
    "parse_status",
    "parse_error",
    "status",
    "started_at",
    "completed_at",
    "created_at",
    "updated_at",
}

CARD_PUBLIC_FIELDS = {
    "id",
    "run_id",
    "project_key",
    "prong_key",
    "title",
    "summary",
    "body",
    "rationale",
    "expected_outcome",
    "status",
    "priority",
    "tags",
    "source_type",
    "source_id",
    "source_url",
    "source_title",
    "parser_name",
    "parser_version",
    "parser_metadata",
    "worker_board",
    "worker_task_id",
    "worker_status",
    "worker_url",
    "approved_by",
    "approved_at",
    "rejected_by",
    "rejected_at",
    "decision_reason",
    "operator_feedback",
    "outcome_status",
    "outcome_summary",
    "created_at",
    "updated_at",
}

JSON_FIELDS = {
    "metadata",
    "parser_metadata",
    "source_metadata",
    "audit_log",
    "tags",
}

VALID_RUN_STATUSES = {"ingested", "parsed", "failed", "complete"}
VALID_CARD_STATUSES = {"proposed", "approved", "rejected", "in_progress", "completed", "failed"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_db_path() -> Path:
    return get_hermes_home() / "self_improvement" / "proposals.db"


@contextmanager
def connect(db_path: Path | None = None) -> Iterable[sqlite3.Connection]:
    path = db_path or get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migrate(conn)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS proposal_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_key TEXT NOT NULL,
                prong_key TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                source_type TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                source_title TEXT NOT NULL DEFAULT '',
                source_metadata TEXT NOT NULL DEFAULT '{}',
                parser_name TEXT NOT NULL DEFAULT '',
                parser_version TEXT NOT NULL DEFAULT '',
                parser_metadata TEXT NOT NULL DEFAULT '{}',
                raw_input_ref TEXT NOT NULL DEFAULT '',
                metadata TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL DEFAULT 'ingested',
                started_at TEXT,
                completed_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS proposal_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL REFERENCES proposal_runs(id) ON DELETE CASCADE,
                project_key TEXT NOT NULL,
                prong_key TEXT NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                rationale TEXT NOT NULL DEFAULT '',
                expected_outcome TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'proposed',
                priority TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                source_type TEXT NOT NULL DEFAULT '',
                source_id TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                source_title TEXT NOT NULL DEFAULT '',
                source_metadata TEXT NOT NULL DEFAULT '{}',
                parser_name TEXT NOT NULL DEFAULT '',
                parser_version TEXT NOT NULL DEFAULT '',
                parser_metadata TEXT NOT NULL DEFAULT '{}',
                worker_board TEXT NOT NULL DEFAULT '',
                worker_task_id TEXT NOT NULL DEFAULT '',
                worker_status TEXT NOT NULL DEFAULT '',
                worker_url TEXT NOT NULL DEFAULT '',
                approved_by TEXT NOT NULL DEFAULT '',
                approved_at TEXT,
                rejected_by TEXT NOT NULL DEFAULT '',
                rejected_at TEXT,
                decision_reason TEXT NOT NULL DEFAULT '',
                operator_feedback TEXT NOT NULL DEFAULT '',
                outcome_status TEXT NOT NULL DEFAULT '',
                outcome_summary TEXT NOT NULL DEFAULT '',
                audit_log TEXT NOT NULL DEFAULT '[]',
                metadata TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_proposal_runs_scope_created
                ON proposal_runs(project_key, prong_key, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_proposal_cards_scope_status_created
                ON proposal_cards(project_key, prong_key, status, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_proposal_cards_worker_task
                ON proposal_cards(worker_board, worker_task_id);
            """
        )
        version = 1
    if version < 2:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(proposal_runs)").fetchall()}
        if "parse_status" not in existing:
            conn.execute("ALTER TABLE proposal_runs ADD COLUMN parse_status TEXT NOT NULL DEFAULT ''")
        if "parse_error" not in existing:
            conn.execute("ALTER TABLE proposal_runs ADD COLUMN parse_error TEXT NOT NULL DEFAULT ''")
        version = 2
    current_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if current_version != SCHEMA_VERSION:
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def resolve_project_prong(
    project: str,
    prong: str | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    proposals = (config or load_config()).get("self_improvement", {}).get("proposals", {})
    projects = proposals.get("projects", {}) or {}
    aliases = proposals.get("project_aliases", {}) or {}
    project_key = aliases.get(project, project)
    project_cfg = projects.get(project_key)
    if not isinstance(project_cfg, dict):
        raise KeyError(f"Unknown self_improvement proposal project: {project}")

    prongs = project_cfg.get("prongs", {}) or {}
    prong_aliases = project_cfg.get("prong_aliases", {}) or {}
    prong_key = prong_aliases.get(prong, prong) if prong else project_cfg.get("default_prong", "")
    if not prong_key and len(prongs) == 1:
        prong_key = next(iter(prongs))
    prong_cfg = prongs.get(prong_key)
    if not isinstance(prong_cfg, dict):
        raise KeyError(f"Unknown self_improvement proposal prong: {project_key}/{prong or ''}")

    return {
        "project_key": project_key,
        "project": project_cfg,
        "prong_key": prong_key,
        "prong": prong_cfg,
    }


def proposal_json_block_reference(project_key: str, prong_key: str) -> str:
    """Return compact cron prompt instructions for structured proposals."""
    return (
        "If you identify self-improvement work, include exactly one JSON block named "
        f"{PROPOSAL_BLOCK_KEY!r}. Do not infer proposals outside this block. "
        "Use this shape:\n"
        "```json\n"
        + json.dumps(
            {
                PROPOSAL_BLOCK_KEY: {
                    "parser_version": STRUCTURED_CRON_PARSER_VERSION,
                    "project": project_key,
                    "prong": prong_key,
                    "proposals": [
                        {
                            "title": "Small actionable change",
                            "summary": "One sentence summary.",
                            "body": "Implementation notes and scope.",
                            "rationale": "Why this is worth doing.",
                            "expected_outcome": "How success is recognized.",
                            "priority": "medium",
                            "tags": ["self-improvement"],
                        }
                    ],
                }
            },
            indent=2,
        )
        + "\n```"
    )


def resolve_cron_project_prong(
    job: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Resolve self-improvement proposal scope for a cron job from config."""
    cfg = config or load_config()
    proposals = cfg.get("self_improvement", {}).get("proposals", {}) if isinstance(cfg, dict) else {}
    cron_jobs = proposals.get("cron_jobs", {}) or {}
    if not isinstance(cron_jobs, dict):
        return None
    mapping = None
    for key in (text(job.get("id")).strip(), text(job.get("name")).strip()):
        if key and isinstance(cron_jobs.get(key), dict):
            mapping = cron_jobs[key]
            break
    if not mapping or mapping.get("enabled") is False:
        return None
    project = text(mapping.get("project") or mapping.get("project_key")).strip()
    prong = text(mapping.get("prong") or mapping.get("prong_key")).strip() or None
    if not project:
        return None
    resolved = resolve_project_prong(project, prong, config=cfg)
    return {"project_key": resolved["project_key"], "prong_key": resolved["prong_key"], "mapping": mapping}


def build_cron_feedback_prompt_section(job: dict[str, Any], *, config: dict[str, Any] | None = None) -> str:
    """Build compact feedback context for a configured self-improvement cron job."""
    scope = resolve_cron_project_prong(job, config=config)
    if not scope:
        return ""
    feedback = build_feedback_context(scope["project_key"], scope["prong_key"])
    return (
        "## Self-Improvement Proposal Feedback Context\n"
        "Use this operator feedback when deciding which future proposals are worth emitting. "
        "Only emit proposals for this configured project/prong.\n\n"
        f"```json\n{json.dumps(feedback, sort_keys=True, separators=(',', ':'))}\n```\n\n"
        + proposal_json_block_reference(scope["project_key"], scope["prong_key"])
        + "\n\n"
    )


def ingest_run(data: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    def op(c: sqlite3.Connection) -> dict[str, Any]:
        now = utc_now()
        row = c.execute(
            "SELECT * FROM proposal_runs WHERE idempotency_key = ?",
            (required_str(data, "idempotency_key"),),
        ).fetchone()
        if row:
            return decode_row(row)
        status = data.get("status", "ingested")
        if status not in VALID_RUN_STATUSES:
            raise ValueError(f"Invalid proposal run status: {status}")
        values = {
            "project_key": required_str(data, "project_key"),
            "prong_key": required_str(data, "prong_key"),
            "idempotency_key": required_str(data, "idempotency_key"),
            "source_type": text(data.get("source_type")),
            "source_id": text(data.get("source_id")),
            "source_url": text(data.get("source_url")),
            "source_title": text(data.get("source_title")),
            "source_metadata": json_text(data.get("source_metadata"), default={}),
            "parser_name": text(data.get("parser_name")),
            "parser_version": text(data.get("parser_version")),
            "parser_metadata": json_text(data.get("parser_metadata"), default={}),
            "parse_status": text(data.get("parse_status")),
            "parse_error": text(data.get("parse_error")),
            "raw_input_ref": text(data.get("raw_input_ref")),
            "metadata": json_text(data.get("metadata"), default={}),
            "status": status,
            "started_at": data.get("started_at") or now,
            "completed_at": data.get("completed_at"),
            "created_at": now,
            "updated_at": now,
        }
        cols = ", ".join(values)
        placeholders = ", ".join(["?"] * len(values))
        cur = c.execute(
            f"INSERT INTO proposal_runs ({cols}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        return get_run(cur.lastrowid, conn=c, public=False)

    if conn is not None:
        return op(conn)
    with connect() as c:
        return op(c)


def ingest_card(data: dict[str, Any], *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    def op(c: sqlite3.Connection) -> dict[str, Any]:
        now = utc_now()
        row = c.execute(
            "SELECT * FROM proposal_cards WHERE idempotency_key = ?",
            (required_str(data, "idempotency_key"),),
        ).fetchone()
        if row:
            return decode_row(row)
        status = data.get("status", "proposed")
        if status not in VALID_CARD_STATUSES:
            raise ValueError(f"Invalid proposal card status: {status}")
        run = get_run(int(data["run_id"]), conn=c, public=False)
        values = {
            "run_id": int(data["run_id"]),
            "project_key": text(data.get("project_key") or run["project_key"]),
            "prong_key": text(data.get("prong_key") or run["prong_key"]),
            "idempotency_key": required_str(data, "idempotency_key"),
            "title": required_str(data, "title"),
            "summary": text(data.get("summary")),
            "body": text(data.get("body")),
            "rationale": text(data.get("rationale")),
            "expected_outcome": text(data.get("expected_outcome")),
            "status": status,
            "priority": text(data.get("priority")),
            "tags": json_text(data.get("tags"), default=[]),
            "source_type": text(data.get("source_type") or run["source_type"]),
            "source_id": text(data.get("source_id") or run["source_id"]),
            "source_url": text(data.get("source_url") or run["source_url"]),
            "source_title": text(data.get("source_title") or run["source_title"]),
            "source_metadata": json_text(data.get("source_metadata"), default={}),
            "parser_name": text(data.get("parser_name") or run["parser_name"]),
            "parser_version": text(data.get("parser_version") or run["parser_version"]),
            "parser_metadata": json_text(data.get("parser_metadata") or run["parser_metadata"], default={}),
            "metadata": json_text(data.get("metadata"), default={}),
            "audit_log": json_text([audit_event("ingested", data.get("created_by"), "")]),
            "created_at": now,
            "updated_at": now,
        }
        cols = ", ".join(values)
        placeholders = ", ".join(["?"] * len(values))
        cur = c.execute(
            f"INSERT INTO proposal_cards ({cols}) VALUES ({placeholders})",
            tuple(values.values()),
        )
        return get_card(cur.lastrowid, conn=c, public=False)

    if conn is not None:
        return op(conn)
    with connect() as c:
        return op(c)


def ingest_cron_proposal_output(
    job: dict[str, Any],
    output_text: str,
    *,
    source_output_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Ingest a configured cron run's proposal JSON block without crashing cron."""
    try:
        scope = resolve_cron_project_prong(job, config=config)
    except Exception as exc:
        return {"ingested": False, "error": str(exc)}
    if not scope:
        return None

    raw_text = text(output_text)
    source_sha = hashlib.sha256(raw_text.encode("utf-8")).hexdigest()
    job_id = text(job.get("id"))
    run_key = f"cron:{scope['project_key']}:{scope['prong_key']}:{job_id}:{source_sha}"
    source_metadata = {
        "job_id": job_id,
        "job_name": text(job.get("name")),
        "source_output_path": text(source_output_path),
        "sha256": source_sha,
    }

    parser_name = STRUCTURED_CRON_PARSER
    parser_version = STRUCTURED_CRON_PARSER_VERSION
    parser_metadata: dict[str, Any] = {"block_key": PROPOSAL_BLOCK_KEY}
    parse_status = "parsed"
    parse_error = ""
    cards: list[dict[str, Any]] = []

    try:
        block = _extract_proposal_block(raw_text)
        proposals_value = block.get("proposals")
        if not isinstance(proposals_value, list):
            raise ValueError("proposal block field 'proposals' must be a list")
        parser_version = text(block.get("parser_version") or STRUCTURED_CRON_PARSER_VERSION)
        cards = _normalize_structured_cards(proposals_value)
    except ValueError as exc:
        legacy_cards = _legacy_parse_markdown_cards(raw_text)
        if legacy_cards:
            parser_name = LEGACY_CRON_PARSER
            parser_version = LEGACY_CRON_PARSER_VERSION
            parser_metadata.update({"confidence": "low", "legacy_parse_error": str(exc)})
            parse_status = "partial"
            parse_error = str(exc)
            cards = legacy_cards
        else:
            parse_status = "failed"
            parse_error = str(exc)

    with connect() as conn:
        run = ingest_run(
            {
                "project_key": scope["project_key"],
                "prong_key": scope["prong_key"],
                "idempotency_key": run_key,
                "source_type": "cron",
                "source_id": job_id,
                "source_title": text(job.get("name")),
                "source_metadata": source_metadata,
                "parser_name": parser_name,
                "parser_version": parser_version,
                "parser_metadata": parser_metadata,
                "parse_status": parse_status,
                "parse_error": parse_error,
                "raw_input_ref": text(source_output_path),
                "status": "failed" if parse_status == "failed" else "parsed",
                "metadata": {"card_count": len(cards)},
            },
            conn=conn,
        )
        ingested_cards = []
        if parse_status != "failed":
            for index, card in enumerate(cards):
                card_key = text(card.get("idempotency_key")) or _card_idempotency_key(run_key, card, index)
                ingested_cards.append(
                    ingest_card(
                        {
                            **card,
                            "run_id": run["id"],
                            "idempotency_key": card_key,
                            "source_metadata": source_metadata,
                            "parser_metadata": parser_metadata,
                            "metadata": {"parse_status": parse_status},
                        },
                        conn=conn,
                    )
                )
        return {
            "ingested": True,
            "run_id": run["id"],
            "card_count": len(ingested_cards),
            "parse_status": run.get("parse_status") or parse_status,
            "parse_error": run.get("parse_error") or parse_error,
            "parser_name": run.get("parser_name") or parser_name,
        }


def get_run(run_id: int, *, conn: sqlite3.Connection | None = None, public: bool = True) -> dict[str, Any]:
    def op(c: sqlite3.Connection) -> dict[str, Any]:
        row = c.execute("SELECT * FROM proposal_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown proposal run: {run_id}")
        return sanitize_run(decode_row(row)) if public else decode_row(row)

    if conn is not None:
        return op(conn)
    with connect() as c:
        return op(c)


def get_card(card_id: int, *, conn: sqlite3.Connection | None = None, public: bool = True) -> dict[str, Any]:
    def op(c: sqlite3.Connection) -> dict[str, Any]:
        row = c.execute("SELECT * FROM proposal_cards WHERE id = ?", (card_id,)).fetchone()
        if row is None:
            raise KeyError(f"Unknown proposal card: {card_id}")
        return sanitize_card(decode_row(row)) if public else decode_row(row)

    if conn is not None:
        return op(conn)
    with connect() as c:
        return op(c)


def list_cards(
    *,
    project_key: str | None = None,
    prong_key: str | None = None,
    status: str | None = None,
    limit: int = 50,
    conn: sqlite3.Connection | None = None,
    public: bool = True,
) -> list[dict[str, Any]]:
    def op(c: sqlite3.Connection) -> list[dict[str, Any]]:
        clauses = []
        params: list[Any] = []
        for col, val in (("project_key", project_key), ("prong_key", prong_key), ("status", status)):
            if val:
                clauses.append(f"{col} = ?")
                params.append(val)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(limit)
        rows = c.execute(
            f"SELECT * FROM proposal_cards{where} ORDER BY created_at DESC, id DESC LIMIT ?",
            tuple(params),
        ).fetchall()
        records = [decode_row(row) for row in rows]
        return [sanitize_card(record) for record in records] if public else records

    if conn is not None:
        return op(conn)
    with connect() as c:
        return op(c)


def transition_card(
    card_id: int,
    status: str,
    *,
    actor: str = "",
    reason: str = "",
    feedback: str = "",
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if status not in VALID_CARD_STATUSES:
        raise ValueError(f"Invalid proposal card status: {status}")

    def op(c: sqlite3.Connection) -> dict[str, Any]:
        card = get_card(card_id, conn=c, public=False)
        now = utc_now()
        updates: dict[str, Any] = {"status": status, "updated_at": now}
        if status == "approved":
            updates.update({"approved_by": actor, "approved_at": card.get("approved_at") or now})
        if status == "rejected":
            updates.update({"rejected_by": actor, "rejected_at": card.get("rejected_at") or now})
        if reason:
            updates["decision_reason"] = reason
        if feedback:
            updates["operator_feedback"] = feedback
        updates["audit_log"] = json_text(card.get("audit_log", []) + [audit_event(status, actor, reason or feedback)])
        set_clause = ", ".join(f"{col} = ?" for col in updates)
        c.execute(f"UPDATE proposal_cards SET {set_clause} WHERE id = ?", (*updates.values(), card_id))
        return get_card(card_id, conn=c, public=False)

    if conn is not None:
        return op(conn)
    with connect() as c:
        return op(c)


def record_decision(
    card_id: int,
    decision: str,
    *,
    actor: str = "",
    reason: str = "",
    feedback: str = "",
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be 'approved' or 'rejected'")
    return transition_card(card_id, decision, actor=actor, reason=reason, feedback=feedback, conn=conn)


def link_worker_task(
    card_id: int,
    *,
    board: str,
    task_id: str,
    status: str = "ready",
    url: str = "",
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    def op(c: sqlite3.Connection) -> dict[str, Any]:
        card = get_card(card_id, conn=c, public=False)
        if card["worker_board"] == board and card["worker_task_id"] == task_id:
            return card
        now = utc_now()
        audit = card.get("audit_log", []) + [audit_event("worker_linked", "", f"{board}:{task_id}")]
        c.execute(
            """
            UPDATE proposal_cards
            SET worker_board = ?, worker_task_id = ?, worker_status = ?, worker_url = ?, audit_log = ?, updated_at = ?
            WHERE id = ?
            """,
            (board, task_id, status, url, json_text(audit), now, card_id),
        )
        return get_card(card_id, conn=c, public=False)

    if conn is not None:
        return op(conn)
    with connect() as c:
        return op(c)


def record_outcome(
    card_id: int,
    *,
    status: str,
    summary: str,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    def op(c: sqlite3.Connection) -> dict[str, Any]:
        card = get_card(card_id, conn=c, public=False)
        now = utc_now()
        audit = card.get("audit_log", []) + [audit_event("outcome", "", f"{status}: {summary}")]
        c.execute(
            """
            UPDATE proposal_cards
            SET outcome_status = ?, outcome_summary = ?, audit_log = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, summary, json_text(audit), now, card_id),
        )
        return get_card(card_id, conn=c, public=False)

    if conn is not None:
        return op(conn)
    with connect() as c:
        return op(c)


def build_feedback_context(
    project_key: str,
    prong_key: str,
    *,
    limit: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    max_items = int(limit or load_config().get("self_improvement", {}).get("proposals", {}).get("feedback_context_limit", 20))

    def op(c: sqlite3.Connection) -> dict[str, Any]:
        rows = c.execute(
            """
            SELECT * FROM proposal_cards
            WHERE project_key = ? AND prong_key = ? AND status IN ('approved', 'rejected', 'completed', 'failed')
            ORDER BY updated_at DESC, id DESC
            LIMIT ?
            """,
            (project_key, prong_key, max_items),
        ).fetchall()
        cards = [decode_row(row) for row in rows]
        approved = [compact_card(card) for card in cards if card["status"] in {"approved", "completed", "failed"}]
        rejected = [compact_card(card) for card in cards if card["status"] == "rejected"]
        outcomes = [compact_outcome(card) for card in cards if card.get("outcome_status") or card.get("outcome_summary")]
        preferences = recurring_preferences(cards)
        return {
            "project_key": project_key,
            "prong_key": prong_key,
            "approved": approved[:max_items],
            "rejected": rejected[:max_items],
            "outcomes": outcomes[:max_items],
            "operator_preferences": preferences[:max_items],
        }

    if conn is not None:
        return op(conn)
    with connect() as c:
        return op(c)


def sanitize_run(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in RUN_PUBLIC_FIELDS if key in record}


def sanitize_card(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in CARD_PUBLIC_FIELDS if key in record}


def decode_row(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for field in JSON_FIELDS:
        if field in data:
            data[field] = parse_json(data[field])
    return data


def compact_card(card: dict[str, Any]) -> dict[str, str]:
    return {
        "title": truncate(card.get("title", "")),
        "summary": truncate(card.get("summary") or card.get("body", "")),
        "reason": truncate(card.get("decision_reason", "")),
        "feedback": truncate(card.get("operator_feedback", "")),
    }


def compact_outcome(card: dict[str, Any]) -> dict[str, str]:
    return {
        "title": truncate(card.get("title", "")),
        "status": truncate(card.get("outcome_status", ""), 80),
        "summary": truncate(card.get("outcome_summary", "")),
    }


def recurring_preferences(cards: list[dict[str, Any]]) -> list[str]:
    counts: dict[str, int] = {}
    for card in cards:
        for value in (card.get("operator_feedback"), card.get("decision_reason")):
            item = truncate(value or "")
            if item:
                counts[item] = counts.get(item, 0) + 1
    return [item for item, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0])) if count > 1]


def _extract_proposal_block(output_text: str) -> dict[str, Any]:
    candidates: list[str] = []
    for match in re.finditer(r"```(?:json)?\s*(.*?)```", output_text, flags=re.IGNORECASE | re.DOTALL):
        body = match.group(1).strip()
        if PROPOSAL_BLOCK_KEY in body:
            candidates.append(body)
    candidates.extend(_balanced_json_candidates(output_text))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get(PROPOSAL_BLOCK_KEY), dict):
            return parsed[PROPOSAL_BLOCK_KEY]
    if PROPOSAL_BLOCK_KEY in output_text:
        raise ValueError("proposal JSON block is malformed")
    raise ValueError("missing required proposal JSON block")


def _balanced_json_candidates(output_text: str) -> list[str]:
    candidates: list[str] = []
    key_index = output_text.find(PROPOSAL_BLOCK_KEY)
    while key_index >= 0:
        start = output_text.rfind("{", 0, key_index)
        while start >= 0:
            depth = 0
            in_string = False
            escape = False
            for index in range(start, len(output_text)):
                char = output_text[index]
                if in_string:
                    if escape:
                        escape = False
                    elif char == "\\":
                        escape = True
                    elif char == '"':
                        in_string = False
                    continue
                if char == '"':
                    in_string = True
                elif char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        candidates.append(output_text[start : index + 1])
                        break
            start = output_text.rfind("{", 0, start)
        key_index = output_text.find(PROPOSAL_BLOCK_KEY, key_index + 1)
    return candidates


def _normalize_structured_cards(items: list[Any]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"proposal at index {index} must be an object")
        title = text(item.get("title")).strip()
        if not title:
            raise ValueError(f"proposal at index {index} is missing title")
        cards.append(
            {
                "title": title,
                "summary": text(item.get("summary")),
                "body": text(item.get("body")),
                "rationale": text(item.get("rationale")),
                "expected_outcome": text(item.get("expected_outcome")),
                "priority": text(item.get("priority")),
                "tags": item.get("tags") if isinstance(item.get("tags"), list) else [],
                "idempotency_key": text(item.get("idempotency_key")),
            }
        )
    return cards


def _legacy_parse_markdown_cards(output_text: str) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    for match in re.finditer(r"^#{2,4}\s+(?:Proposal\s*[:\-]\s*)?(.+?)\s*$", output_text, flags=re.MULTILINE):
        title = match.group(1).strip()
        if not title or title.lower().startswith(("prompt", "response", "cron job")):
            continue
        start = match.end()
        next_match = re.search(r"^#{2,4}\s+", output_text[start:], flags=re.MULTILINE)
        end = start + next_match.start() if next_match else len(output_text)
        body = output_text[start:end].strip()
        cards.append(
            {
                "title": title[:240],
                "summary": truncate(body or title),
                "body": body,
                "priority": "",
                "tags": ["legacy_parse"],
            }
        )
    return cards


def _card_idempotency_key(run_key: str, card: dict[str, Any], index: int) -> str:
    digest = hashlib.sha256(
        json.dumps({"index": index, "card": card}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"{run_key}:card:{digest}"


def audit_event(action: str, actor: str | None, note: str | None) -> dict[str, str]:
    return {"at": utc_now(), "action": action, "actor": text(actor), "note": text(note)}


def required_str(data: dict[str, Any], key: str) -> str:
    value = text(data.get(key))
    if not value:
        raise ValueError(f"Missing required proposal field: {key}")
    return value


def text(value: Any) -> str:
    return "" if value is None else str(value)


def json_text(value: Any, *, default: Any = None) -> str:
    if value is None:
        value = default
    parse_json(json.dumps(value, sort_keys=True, separators=(",", ":")))
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def parse_json(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value or "null")
    return value


def truncate(value: str, limit: int = 240) -> str:
    value = " ".join(text(value).split())
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."
