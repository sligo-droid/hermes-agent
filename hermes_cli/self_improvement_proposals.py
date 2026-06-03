"""Storage and ingestion helpers for self-improvement proposals.

Strict proposal block contract, version 1:

{
  "hermes_self_improvement_proposals_version": 1,
  "project": "sligo",
  "prong": "airflow_doctor",
  "proposals": [
    {
      "title": "Short title",
      "summary": "One paragraph summary",
      "body": "Detailed recommendation",
      "evidence": [{"label": "source", "detail": "..."}],
      "worker_prompt": "Prompt for a later implementation worker",
      "acceptance_criteria": ["Observable completion criterion"],
      "priority": "medium",
      "confidence": 0.8,
      "effort": "small",
      "suggested_assignee": "dev",
      "suggested_skills": ["skill-name"]
    }
  ]
}

The payload may be supplied directly through cron output metadata using one of
``proposal_json``, ``proposal_block``, ``self_improvement_proposals``, or
``structured_proposals``. Text output may contain the same JSON between
``HERMES_SELF_IMPROVEMENT_PROPOSALS_JSON_START`` and
``HERMES_SELF_IMPROVEMENT_PROPOSALS_JSON_END`` markers. Proposal payloads never
control execution workspace, board, assignee, or skills; those are resolved from
trusted config after project/prong validation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from hermes_cli import kanban_db
from hermes_constants import get_hermes_home
from hermes_cli.config import DEFAULT_CONFIG, load_config


PARSER_VERSION = 1
CONTRACT_VERSION = 1
START_MARKER = "HERMES_SELF_IMPROVEMENT_PROPOSALS_JSON_START"
END_MARKER = "HERMES_SELF_IMPROVEMENT_PROPOSALS_JSON_END"
PROMPT_CONTEXT_MARKER = "HERMES_SELF_IMPROVEMENT_PROPOSAL_CONTEXT"
_OUTCOME_FEEDBACK_TYPES = {"outcome", "kanban_status"}
_INITIALIZED_PATHS: set[str] = set()
_INIT_LOCK = threading.Lock()
_SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|credential|private[_-]?key|auth)",
    re.IGNORECASE,
)


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS proposal_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_key TEXT UNIQUE,
    project TEXT,
    prong TEXT,
    parse_status TEXT NOT NULL CHECK (parse_status IN ('parsed', 'parse_error')),
    parse_error TEXT,
    parser_version INTEGER NOT NULL,
    contract_version INTEGER,
    cron_job_id TEXT,
    cron_job_name TEXT,
    cron_run_id TEXT,
    cron_output_path TEXT,
    cron_output_sha256 TEXT,
    source_timestamp INTEGER,
    model TEXT,
    provider TEXT,
    profile TEXT,
    workdir TEXT,
    metadata_json TEXT,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS proposal_cards (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT NOT NULL UNIQUE,
    run_id INTEGER NOT NULL REFERENCES proposal_runs(id) ON DELETE CASCADE,
    project TEXT NOT NULL,
    prong TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'proposed',
    lifecycle_status TEXT NOT NULL DEFAULT 'new',
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    body TEXT NOT NULL,
    worker_prompt TEXT NOT NULL,
    acceptance_criteria_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    priority TEXT NOT NULL,
    confidence REAL NOT NULL,
    effort TEXT NOT NULL,
    suggested_assignee TEXT,
    suggested_skills_json TEXT NOT NULL,
    resolved_workspace_path TEXT NOT NULL,
    resolved_board TEXT NOT NULL,
    resolved_assignee TEXT NOT NULL,
    resolved_skills_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    decision TEXT,
    decision_reason TEXT,
    decided_by TEXT,
    decided_at INTEGER,
    linked_kanban_task_id TEXT,
    linked_worker_run_id TEXT,
    source_run_id INTEGER NOT NULL,
    source_output_path TEXT,
    source_output_sha256 TEXT,
    source_timestamp INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS proposal_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT NOT NULL,
    feedback_type TEXT NOT NULL,
    body TEXT NOT NULL,
    author TEXT,
    metadata_json TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_proposal_runs_project_prong ON proposal_runs(project, prong);
CREATE INDEX IF NOT EXISTS idx_proposal_runs_status ON proposal_runs(parse_status);
CREATE INDEX IF NOT EXISTS idx_proposal_cards_project_prong ON proposal_cards(project, prong);
CREATE INDEX IF NOT EXISTS idx_proposal_cards_status ON proposal_cards(status);
CREATE INDEX IF NOT EXISTS idx_proposal_feedback_card ON proposal_feedback(card_id);
"""


class ProposalParseError(ValueError):
    """Raised when strict proposal JSON is missing or malformed."""


def proposal_db_path(config: dict[str, Any] | None = None) -> Path:
    cfg = config or load_config()
    rel = str(cfg.get("self_improvement", {}).get("storage_db") or "self_improvement/proposals.db")
    path = Path(rel)
    if path.is_absolute():
        return path
    return get_hermes_home() / path


def connect(db_path: str | Path | None = None, config: dict[str, Any] | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path is not None else proposal_db_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    if resolved not in _INITIALIZED_PATHS:
        with _INIT_LOCK:
            if resolved not in _INITIALIZED_PATHS:
                conn = sqlite3.connect(str(path))
                try:
                    conn.row_factory = sqlite3.Row
                    conn.executescript(SCHEMA_SQL)
                    conn.commit()
                finally:
                    conn.close()
                _INITIALIZED_PATHS.add(resolved)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_self_improvement_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = config or load_config()
    return cfg.get("self_improvement") or DEFAULT_CONFIG["self_improvement"]


def list_projects(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    projects = load_self_improvement_config(config).get("projects") or {}
    return [sanitize_project(project_id, project) for project_id, project in projects.items()]


def sanitize_project(project_id: str, project: dict[str, Any]) -> dict[str, Any]:
    prongs = project.get("prongs") or {}
    return {
        "id": project_id,
        "name": str(project.get("name") or project_id),
        "board": str(project.get("board") or ""),
        "assignee": str(project.get("assignee") or ""),
        "skills": _string_list(project.get("skills")),
        "prongs": [
            {
                "id": prong_id,
                "name": str(prong.get("name") or prong_id),
                "enabled": bool(prong.get("enabled", True)),
            }
            for prong_id, prong in prongs.items()
        ],
    }


def validate_project_prong(
    project_id: str,
    prong_id: str,
    config: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    si = load_self_improvement_config(config)
    projects = si.get("projects") or {}
    project = projects.get(project_id)
    if not isinstance(project, dict):
        raise ValueError(f"Unknown self_improvement project: {project_id}")
    prongs = project.get("prongs") or {}
    prong = prongs.get(prong_id)
    if not isinstance(prong, dict):
        raise ValueError(f"Unknown self_improvement prong for {project_id}: {prong_id}")
    if not prong.get("enabled", True):
        raise ValueError(f"Self_improvement prong is disabled: {project_id}/{prong_id}")
    return project, prong


def resolve_execution_context(
    project_id: str,
    prong_id: str,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    project, prong = validate_project_prong(project_id, prong_id, config)
    workspace = str(project.get("workspace_path") or ".")
    if "\x00" in workspace:
        raise ValueError("Project workspace_path contains a NUL byte")
    return {
        "project": project_id,
        "prong": prong_id,
        "workspace_path": workspace,
        "board": str(project.get("board") or "default"),
        "assignee": str(prong.get("assignee") or project.get("assignee") or ""),
        "skills": _string_list(prong.get("skills") or project.get("skills")),
    }


def validate_approval_project_workspace(
    project_id: str,
    workspace_path: str,
    config: dict[str, Any] | None = None,
) -> bool:
    projects = load_self_improvement_config(config).get("projects") or {}
    project = projects.get(project_id)
    if not isinstance(project, dict):
        raise ValueError(f"Unknown self_improvement project: {project_id}")
    expected = Path(str(project.get("workspace_path") or ".")).expanduser().resolve()
    actual = Path(workspace_path).expanduser().resolve()
    if actual != expected:
        raise ValueError(f"Workspace does not match configured project workspace for {project_id}")
    return True


def ingest_proposal_output(
    *,
    metadata: dict[str, Any] | None = None,
    output_text: str | None = None,
    config: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    metadata = metadata or {}
    now = int(time.time())
    output_path = _metadata_str(metadata, "cron_output_path", "output_path", "path")
    output_hash = _source_hash(output_path, output_text, metadata)
    source_timestamp = _metadata_int(metadata, "source_timestamp", "created_at", "completed_at") or now
    run_key = _run_key(metadata, output_path, output_hash, source_timestamp)

    try:
        payload = parse_proposal_payload(metadata=metadata, output_text=output_text)
        project_id = _required_str(payload, "project")
        prong_id = _required_str(payload, "prong")
        context = resolve_execution_context(project_id, prong_id, config)
        proposals = payload.get("proposals")
        if not isinstance(proposals, list) or not proposals:
            raise ProposalParseError("proposals must be a non-empty list")
        cards = [_normalize_proposal(item, context) for item in proposals]
        parse_status = "parsed"
        parse_error = None
        contract_version = CONTRACT_VERSION
    except Exception as exc:
        project_id = None
        prong_id = None
        context = None
        cards = []
        parse_status = "parse_error"
        parse_error = str(exc)
        contract_version = None

    with connect(db_path, config) as conn:
        run_id = _insert_run(
            conn,
            run_key=run_key,
            project=project_id,
            prong=prong_id,
            parse_status=parse_status,
            parse_error=parse_error,
            contract_version=contract_version,
            metadata=metadata,
            output_path=output_path,
            output_hash=output_hash,
            source_timestamp=source_timestamp,
            now=now,
        )
        inserted = []
        if parse_status == "parsed" and context is not None:
            for card in cards:
                inserted.append(_insert_card(conn, run_id, card, context, output_path, output_hash, source_timestamp, now))
        conn.commit()

    return {
        "run_id": run_id,
        "parse_status": parse_status,
        "parse_error": parse_error,
        "card_ids": inserted,
    }


def parse_proposal_payload(*, metadata: dict[str, Any] | None = None, output_text: str | None = None) -> dict[str, Any]:
    raw = _extract_raw_payload(metadata or {}, output_text)
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
        raise ProposalParseError(f"Malformed proposal JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ProposalParseError("Proposal payload must be a JSON object")
    version = payload.get("hermes_self_improvement_proposals_version")
    if version != CONTRACT_VERSION:
        raise ProposalParseError("Unsupported or missing proposal contract version")
    allowed = {"hermes_self_improvement_proposals_version", "project", "prong", "proposals"}
    extra = sorted(set(payload) - allowed)
    if extra:
        raise ProposalParseError(f"Unexpected proposal payload fields: {', '.join(extra)}")
    return payload


def list_runs(
    *,
    project: str | None = None,
    prong: str | None = None,
    parse_status: str | None = None,
    limit: int = 50,
    db_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if project:
        clauses.append("project = ?")
        params.append(project)
    if prong:
        clauses.append("prong = ?")
        params.append(prong)
    if parse_status:
        clauses.append("parse_status = ?")
        params.append(parse_status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with connect(db_path, config) as conn:
        rows = conn.execute(
            f"SELECT * FROM proposal_runs {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [sanitize_run(dict(row)) for row in rows]


def list_proposals(
    *,
    project: str | None = None,
    prong: str | None = None,
    status: str | None = None,
    limit: int = 50,
    db_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if project:
        clauses.append("project = ?")
        params.append(project)
    if prong:
        clauses.append("prong = ?")
        params.append(prong)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(int(limit), 500)))
    with connect(db_path, config) as conn:
        rows = conn.execute(
            f"SELECT * FROM proposal_cards {where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
    return [sanitize_proposal(dict(row)) for row in rows]


def get_proposal_detail(
    card_id: str,
    *,
    db_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    with connect(db_path, config) as conn:
        row = conn.execute("SELECT * FROM proposal_cards WHERE card_id = ?", (card_id,)).fetchone()
    return sanitize_proposal(dict(row)) if row else None


def add_feedback(
    card_id: str,
    *,
    feedback_type: str,
    body: str,
    author: str | None = None,
    metadata: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    feedback_type = str(feedback_type or "comment").strip().lower()
    if feedback_type not in {"comment", "reject", "approve", "edit", *_OUTCOME_FEEDBACK_TYPES}:
        raise ValueError("feedback_type must be comment, reject, approve, edit, outcome, or kanban_status")
    body = str(body or "").strip()
    if not body:
        raise ValueError("feedback body is required")
    now = int(time.time())
    with connect(db_path, config) as conn:
        if conn.execute("SELECT 1 FROM proposal_cards WHERE card_id = ?", (card_id,)).fetchone() is None:
            raise KeyError(card_id)
        cur = conn.execute(
            """
            INSERT INTO proposal_feedback (card_id, feedback_type, body, author, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (card_id, feedback_type, body, author, json.dumps(sanitize_payload(metadata or {}), sort_keys=True), now),
        )
        conn.commit()
        return {
            "id": cur.lastrowid,
            "card_id": card_id,
            "feedback_type": feedback_type,
            "body": body,
            "author": author,
            "metadata": sanitize_payload(metadata or {}),
            "created_at": now,
        }


def list_feedback(
    card_id: str,
    *,
    db_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    with connect(db_path, config) as conn:
        rows = conn.execute(
            "SELECT * FROM proposal_feedback WHERE card_id = ? ORDER BY id ASC",
            (card_id,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "card_id": row["card_id"],
            "feedback_type": row["feedback_type"],
            "body": row["body"],
            "author": row["author"],
            "metadata": sanitize_payload(_json_value(row["metadata_json"], {})),
            "created_at": row["created_at"],
        }
        for row in rows
    ]


def edit_proposal(
    card_id: str,
    *,
    title: str | None = None,
    summary: str | None = None,
    body: str | None = None,
    priority: str | None = None,
    confidence: float | None = None,
    effort: str | None = None,
    acceptance_criteria: list[str] | None = None,
    db_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    updates: dict[str, Any] = {}
    if title is not None:
        updates["title"] = _non_empty_edit_str(title, "title")
    if summary is not None:
        updates["summary"] = _non_empty_edit_str(summary, "summary")
    if body is not None:
        updates["body"] = _non_empty_edit_str(body, "body")
    if priority is not None:
        value = str(priority).strip().lower()
        if value not in {"low", "medium", "high", "urgent"}:
            raise ValueError("priority must be low, medium, high, or urgent")
        updates["priority"] = value
    if confidence is not None:
        if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
            raise ValueError("confidence must be a number between 0 and 1")
        updates["confidence"] = float(confidence)
    if effort is not None:
        value = str(effort).strip().lower()
        if value not in {"small", "medium", "large"}:
            raise ValueError("effort must be small, medium, or large")
        updates["effort"] = value
    if acceptance_criteria is not None:
        if not isinstance(acceptance_criteria, list) or not all(isinstance(v, str) and v.strip() for v in acceptance_criteria):
            raise ValueError("acceptance_criteria must be a non-empty list of strings")
        updates["acceptance_criteria_json"] = json.dumps([v.strip() for v in acceptance_criteria])
    if not updates:
        raise ValueError("no editable fields supplied")
    updates["updated_at"] = int(time.time())
    assignments = ", ".join(f"{name} = ?" for name in updates)
    with connect(db_path, config) as conn:
        cur = conn.execute(
            f"UPDATE proposal_cards SET {assignments} WHERE card_id = ? AND status = 'proposed'",
            [*updates.values(), card_id],
        )
        if cur.rowcount == 0:
            exists = conn.execute("SELECT 1 FROM proposal_cards WHERE card_id = ?", (card_id,)).fetchone()
            if exists is None:
                raise KeyError(card_id)
            raise ValueError("only proposed cards may be edited")
        conn.commit()
    detail = get_proposal_detail(card_id, db_path=db_path, config=config)
    if detail is None:
        raise KeyError(card_id)
    return detail


def approve_proposal(
    card_id: str,
    *,
    actor: str | None = None,
    config: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    with connect(db_path, config) as conn:
        row = conn.execute("SELECT * FROM proposal_cards WHERE card_id = ?", (card_id,)).fetchone()
        if row is None:
            raise KeyError(card_id)
        card = sanitize_proposal(dict(row))
        if card["status"] == "rejected":
            raise ValueError("rejected cards cannot be approved")
        validate_approval_project_workspace(card["project"], card["resolved_workspace_path"], config)
        board = str(card["resolved_board"] or "default")
        kanban_db.init_db(board=board)
        with kanban_db.connect(board=board) as kb_conn:
            task_id = kanban_db.create_task(
                kb_conn,
                title=card["title"],
                body=build_kanban_task_body(card),
                assignee=card["resolved_assignee"],
                created_by="self-improvement",
                workspace_kind="dir",
                workspace_path=card["resolved_workspace_path"],
                priority=_kanban_priority(card.get("priority")),
                idempotency_key=f"self-improvement:{card_id}",
                skills=card["resolved_skills"],
                initial_status="running",
                board=board,
            )
        conn.execute(
            """
            UPDATE proposal_cards
            SET status = 'approved', lifecycle_status = 'worker_created', decision = 'approved',
                decided_by = COALESCE(decided_by, ?), decided_at = COALESCE(decided_at, ?),
                linked_kanban_task_id = ?, updated_at = ?
            WHERE card_id = ?
            """,
            (actor, now, task_id, now, card_id),
        )
        conn.execute(
            """
            INSERT INTO proposal_feedback (card_id, feedback_type, body, author, metadata_json, created_at)
            VALUES (?, 'approve', ?, ?, ?, ?)
            """,
            (card_id, f"Approved into Kanban task {task_id}", actor, json.dumps({"board": board, "task_id": task_id}), now),
        )
        conn.commit()
    detail = get_proposal_detail(card_id, db_path=db_path, config=config)
    return _with_worker_link(detail, board=board, task_id=task_id)


def reject_proposal(
    card_id: str,
    *,
    reason: str | None = None,
    strength: str | None = None,
    actor: str | None = None,
    db_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    reason_text = str(reason or "").strip()
    metadata = {"strength": str(strength).strip()} if strength else {}
    with connect(db_path, config) as conn:
        row = conn.execute("SELECT * FROM proposal_cards WHERE card_id = ?", (card_id,)).fetchone()
        if row is None:
            raise KeyError(card_id)
        if row["status"] == "approved":
            raise ValueError("approved cards cannot be rejected")
        conn.execute(
            """
            UPDATE proposal_cards
            SET status = 'rejected', lifecycle_status = 'archived', decision = 'rejected',
                decision_reason = ?, decided_by = COALESCE(decided_by, ?),
                decided_at = COALESCE(decided_at, ?), updated_at = ?
            WHERE card_id = ?
            """,
            (reason_text or None, actor, now, now, card_id),
        )
        conn.execute(
            """
            INSERT INTO proposal_feedback (card_id, feedback_type, body, author, metadata_json, created_at)
            VALUES (?, 'reject', ?, ?, ?, ?)
            """,
            (card_id, reason_text or "Rejected", actor, json.dumps(metadata, sort_keys=True), now),
        )
        conn.commit()
    detail = get_proposal_detail(card_id, db_path=db_path, config=config)
    if detail is None:
        raise KeyError(card_id)
    return detail


def correlate_linked_kanban_outcomes(
    *,
    project: str | None = None,
    prong: str | None = None,
    db_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """Mirror linked Kanban task status into compact proposal card state.

    Kanban remains the source of truth for execution. This helper only stores a
    small derived lifecycle/status feedback note so future proposal cron prongs
    can learn from outcomes without rereading worker boards or Discord history.
    """
    clauses = ["linked_kanban_task_id IS NOT NULL", "status = 'approved'"]
    params: list[Any] = []
    if project:
        clauses.append("project = ?")
        params.append(project)
    if prong:
        clauses.append("prong = ?")
        params.append(prong)
    params.append(max(1, min(int(limit), 500)))
    now = int(time.time())
    checked = updated = missing = 0
    with connect(db_path, config) as conn:
        rows = conn.execute(
            f"SELECT * FROM proposal_cards WHERE {' AND '.join(clauses)} ORDER BY updated_at DESC LIMIT ?",
            params,
        ).fetchall()
        for row in rows:
            checked += 1
            board = str(row["resolved_board"] or "default")
            task_id = str(row["linked_kanban_task_id"] or "")
            try:
                kanban_db.init_db(board=board)
                with kanban_db.connect(board=board) as kb_conn:
                    task = kanban_db.get_task(kb_conn, task_id)
            except Exception:
                task = None
            if task is None:
                missing += 1
                continue
            lifecycle = _lifecycle_for_kanban_status(task.status)
            if lifecycle == row["lifecycle_status"]:
                continue
            conn.execute(
                "UPDATE proposal_cards SET lifecycle_status = ?, updated_at = ? WHERE card_id = ?",
                (lifecycle, now, row["card_id"]),
            )
            conn.execute(
                """
                INSERT INTO proposal_feedback (card_id, feedback_type, body, author, metadata_json, created_at)
                VALUES (?, 'kanban_status', ?, 'kanban', ?, ?)
                """,
                (
                    row["card_id"],
                    f"Linked Kanban task {task_id} is {task.status}",
                    json.dumps({"board": board, "task_id": task_id, "task_status": task.status}, sort_keys=True),
                    now,
                ),
            )
            updated += 1
        conn.commit()
    return {"checked": checked, "updated": updated, "missing": missing}


def build_feedback_context(
    project: str,
    prong: str,
    *,
    db_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    """Return compact project/prong feedback for future proposal generation."""
    validate_project_prong(project, prong, config)
    correlate_linked_kanban_outcomes(project=project, prong=prong, db_path=db_path, config=config)
    cap = max(1, min(int(limit), 25))
    with connect(db_path, config) as conn:
        approved = conn.execute(
            """
            SELECT * FROM proposal_cards
            WHERE project = ? AND prong = ? AND status = 'approved'
            ORDER BY decided_at DESC, updated_at DESC LIMIT ?
            """,
            (project, prong, cap),
        ).fetchall()
        rejected = conn.execute(
            """
            SELECT * FROM proposal_cards
            WHERE project = ? AND prong = ? AND status = 'rejected'
            ORDER BY decided_at DESC, updated_at DESC LIMIT ?
            """,
            (project, prong, cap),
        ).fetchall()
        outcomes = conn.execute(
            """
            SELECT * FROM proposal_cards
            WHERE project = ? AND prong = ? AND status = 'approved'
              AND lifecycle_status IN ('completed', 'blocked', 'failed')
            ORDER BY updated_at DESC LIMIT ?
            """,
            (project, prong, cap * 2),
        ).fetchall()
        feedback_rows = conn.execute(
            """
            SELECT f.* FROM proposal_feedback f
            JOIN proposal_cards c ON c.card_id = f.card_id
            WHERE c.project = ? AND c.prong = ?
            ORDER BY f.id DESC LIMIT ?
            """,
            (project, prong, cap * 4),
        ).fetchall()
    completed = []
    failed_or_blocked = []
    for row in outcomes:
        item = _feedback_card_summary(dict(row))
        if row["lifecycle_status"] == "completed":
            completed.append(item)
        else:
            failed_or_blocked.append(item)
    preferences = _operator_preferences(feedback_rows, cap)
    return {
        "project": project,
        "prong": prong,
        "recent_approved_proposals": [_feedback_card_summary(dict(row)) for row in approved],
        "recent_rejected_proposals": [_feedback_card_summary(dict(row), include_reason=True) for row in rejected],
        "operator_preferences_and_patterns": preferences,
        "approved_then_failed_or_blocked_outcomes": failed_or_blocked[:cap],
        "completed_approved_outcomes": completed[:cap],
    }


def build_proposal_prompt_context(
    project: str,
    prong: str,
    *,
    db_path: str | Path | None = None,
    config: dict[str, Any] | None = None,
) -> str:
    """Build a concise cron prompt block for structured proposal prongs."""
    feedback = build_feedback_context(project, prong, db_path=db_path, config=config)
    contract = {
        "hermes_self_improvement_proposals_version": CONTRACT_VERSION,
        "project": project,
        "prong": prong,
        "proposals": [
            {
                "title": "Short implementation title",
                "summary": "One paragraph summary",
                "body": "Detailed rationale and implementation notes",
                "evidence": [{"label": "source", "detail": "compact evidence"}],
                "worker_prompt": "Self-contained worker prompt for a later Kanban task",
                "acceptance_criteria": ["Observable completion criterion"],
                "priority": "medium",
                "confidence": 0.8,
                "effort": "small",
                "suggested_assignee": "dev",
                "suggested_skills": [],
            }
        ],
    }
    lines = [
        f"## {PROMPT_CONTEXT_MARKER}",
        "Use this compact feedback context instead of raw Discord or worker-board history.",
        "Avoid repeating recently rejected ideas unless the rejection reason is directly addressed.",
        "Prefer proposals similar to completed approved outcomes and avoid patterns from blocked/failed outcomes.",
        "Return only strict JSON between the required markers; do not include Markdown prose outside the markers.",
        START_MARKER,
        json.dumps(contract, indent=2, sort_keys=True),
        END_MARKER,
        "## Feedback Context JSON",
        json.dumps(feedback, indent=2, sort_keys=True),
    ]
    return "\n".join(lines)


def build_cron_job_prompt_context(
    job: dict[str, Any],
    *,
    config: dict[str, Any] | None = None,
    db_path: str | Path | None = None,
) -> str:
    """Return self-improvement context for an opted-in cron job, else empty."""
    target = _self_improvement_target_for_job(job, config)
    if target is None:
        return ""
    return build_proposal_prompt_context(
        target["project"],
        target["prong"],
        db_path=db_path,
        config=config,
    )


def build_kanban_task_body(card: dict[str, Any]) -> str:
    criteria = card.get("acceptance_criteria") or []
    lines = [
        "Self-Improvement proposal approved for implementation.",
        "",
        "Worker prompt:",
        str(card.get("worker_prompt") or "").strip(),
        "",
        "Acceptance criteria:",
    ]
    lines.extend(f"- {str(item).strip()}" for item in criteria if str(item).strip())
    lines.extend([
        "",
        "Proposal context:",
        str(card.get("body") or card.get("summary") or "").strip(),
    ])
    return "\n".join(lines).strip()


def _with_worker_link(detail: dict[str, Any] | None, *, board: str, task_id: str) -> dict[str, Any]:
    if detail is None:
        raise KeyError(task_id)
    result = dict(detail)
    result["linked_kanban_board"] = board
    result["worker"] = {"board": board, "task_id": task_id, "url": _worker_url(board, task_id)}
    return result


def sanitize_run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "project": row.get("project"),
        "prong": row.get("prong"),
        "parse_status": row.get("parse_status"),
        "parse_error": row.get("parse_error"),
        "parser_version": row.get("parser_version"),
        "contract_version": row.get("contract_version"),
        "cron_job_id": row.get("cron_job_id"),
        "cron_job_name": row.get("cron_job_name"),
        "cron_run_id": row.get("cron_run_id"),
        "cron_output_path": row.get("cron_output_path"),
        "cron_output_sha256": row.get("cron_output_sha256"),
        "source_timestamp": row.get("source_timestamp"),
        "model": row.get("model"),
        "provider": row.get("provider"),
        "profile": row.get("profile"),
        "workdir": row.get("workdir"),
        "created_at": row.get("created_at"),
    }


def sanitize_proposal(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "card_id": row.get("card_id"),
        "run_id": row.get("run_id"),
        "project": row.get("project"),
        "prong": row.get("prong"),
        "status": row.get("status"),
        "lifecycle_status": row.get("lifecycle_status"),
        "title": row.get("title"),
        "summary": row.get("summary"),
        "body": row.get("body"),
        "worker_prompt": row.get("worker_prompt"),
        "acceptance_criteria": _json_value(row.get("acceptance_criteria_json"), []),
        "evidence": sanitize_payload(_json_value(row.get("evidence_json"), [])),
        "priority": row.get("priority"),
        "confidence": row.get("confidence"),
        "effort": row.get("effort"),
        "suggested_assignee": row.get("suggested_assignee"),
        "suggested_skills": _json_value(row.get("suggested_skills_json"), []),
        "resolved_workspace_path": row.get("resolved_workspace_path"),
        "resolved_board": row.get("resolved_board"),
        "resolved_assignee": row.get("resolved_assignee"),
        "resolved_skills": _json_value(row.get("resolved_skills_json"), []),
        "idempotency_key": row.get("idempotency_key"),
        "decision": row.get("decision"),
        "decision_reason": row.get("decision_reason"),
        "decided_by": row.get("decided_by"),
        "decided_at": row.get("decided_at"),
        "linked_kanban_task_id": row.get("linked_kanban_task_id"),
        "linked_worker_run_id": row.get("linked_worker_run_id"),
        "source_output_path": row.get("source_output_path"),
        "source_output_sha256": row.get("source_output_sha256"),
        "source_timestamp": row.get("source_timestamp"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                clean[str(key)] = "[REDACTED]"
            else:
                clean[str(key)] = sanitize_payload(item)
        return clean
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _extract_raw_payload(metadata: dict[str, Any], output_text: str | None) -> Any:
    for key in ("proposal_json", "proposal_block", "self_improvement_proposals", "structured_proposals"):
        if key in metadata and metadata[key] not in (None, ""):
            return metadata[key]
    if output_text:
        start = output_text.find(START_MARKER)
        end = output_text.find(END_MARKER)
        if start >= 0 and end > start:
            return output_text[start + len(START_MARKER):end].strip()
    raise ProposalParseError("No structured self-improvement proposal JSON found")


def _normalize_proposal(item: Any, context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ProposalParseError("Each proposal must be an object")
    allowed = {
        "title", "summary", "body", "evidence", "worker_prompt",
        "acceptance_criteria", "priority", "confidence", "effort",
        "suggested_assignee", "suggested_skills",
    }
    extra = sorted(set(item) - allowed)
    if extra:
        raise ProposalParseError(f"Unexpected proposal fields: {', '.join(extra)}")
    title = _required_str(item, "title")
    summary = _required_str(item, "summary")
    body = _required_str(item, "body")
    worker_prompt = _required_str(item, "worker_prompt")
    acceptance_criteria = item.get("acceptance_criteria")
    if not isinstance(acceptance_criteria, list) or not all(isinstance(v, str) and v.strip() for v in acceptance_criteria):
        raise ProposalParseError("acceptance_criteria must be a non-empty list of strings")
    evidence = item.get("evidence")
    if not isinstance(evidence, list):
        raise ProposalParseError("evidence must be a list")
    priority = str(item.get("priority") or "medium").lower()
    if priority not in {"low", "medium", "high", "urgent"}:
        raise ProposalParseError("priority must be low, medium, high, or urgent")
    confidence = item.get("confidence")
    if not isinstance(confidence, (int, float)) or confidence < 0 or confidence > 1:
        raise ProposalParseError("confidence must be a number between 0 and 1")
    effort = str(item.get("effort") or "medium").lower()
    if effort not in {"small", "medium", "large"}:
        raise ProposalParseError("effort must be small, medium, or large")
    suggested_skills = _string_list(item.get("suggested_skills"))
    idempotency_key = _stable_hash({
        "project": context["project"],
        "prong": context["prong"],
        "title": title,
        "summary": summary,
        "worker_prompt": worker_prompt,
    })
    return {
        "title": title,
        "summary": summary,
        "body": body,
        "worker_prompt": worker_prompt,
        "acceptance_criteria": acceptance_criteria,
        "evidence": sanitize_payload(evidence),
        "priority": priority,
        "confidence": float(confidence),
        "effort": effort,
        "suggested_assignee": item.get("suggested_assignee") if isinstance(item.get("suggested_assignee"), str) else None,
        "suggested_skills": suggested_skills,
        "idempotency_key": idempotency_key,
    }


def _insert_run(conn: sqlite3.Connection, **kwargs: Any) -> int:
    metadata = sanitize_payload(kwargs["metadata"])
    fields = {
        "run_key": kwargs["run_key"],
        "project": kwargs["project"],
        "prong": kwargs["prong"],
        "parse_status": kwargs["parse_status"],
        "parse_error": kwargs["parse_error"],
        "parser_version": PARSER_VERSION,
        "contract_version": kwargs["contract_version"],
        "cron_job_id": _metadata_str(metadata, "cron_job_id", "job_id"),
        "cron_job_name": _metadata_str(metadata, "cron_job_name", "job_name"),
        "cron_run_id": _metadata_str(metadata, "cron_run_id", "run_id"),
        "cron_output_path": kwargs["output_path"],
        "cron_output_sha256": kwargs["output_hash"],
        "source_timestamp": kwargs["source_timestamp"],
        "model": _metadata_str(metadata, "model"),
        "provider": _metadata_str(metadata, "provider"),
        "profile": _metadata_str(metadata, "profile"),
        "workdir": _metadata_str(metadata, "workdir", "cwd"),
        "metadata_json": json.dumps(metadata, sort_keys=True),
        "created_at": kwargs["now"],
    }
    names = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT OR IGNORE INTO proposal_runs ({names}) VALUES ({placeholders})",
        list(fields.values()),
    )
    row = conn.execute("SELECT id FROM proposal_runs WHERE run_key = ?", (kwargs["run_key"],)).fetchone()
    return int(row["id"])


def _insert_card(
    conn: sqlite3.Connection,
    run_id: int,
    card: dict[str, Any],
    context: dict[str, Any],
    output_path: str | None,
    output_hash: str | None,
    source_timestamp: int,
    now: int,
) -> str:
    card_id = "sip_" + card["idempotency_key"][:16]
    fields = {
        "card_id": card_id,
        "run_id": run_id,
        "project": context["project"],
        "prong": context["prong"],
        "status": "proposed",
        "lifecycle_status": "new",
        "title": card["title"],
        "summary": card["summary"],
        "body": card["body"],
        "worker_prompt": card["worker_prompt"],
        "acceptance_criteria_json": json.dumps(card["acceptance_criteria"]),
        "evidence_json": json.dumps(card["evidence"], sort_keys=True),
        "priority": card["priority"],
        "confidence": card["confidence"],
        "effort": card["effort"],
        "suggested_assignee": card["suggested_assignee"],
        "suggested_skills_json": json.dumps(card["suggested_skills"]),
        "resolved_workspace_path": context["workspace_path"],
        "resolved_board": context["board"],
        "resolved_assignee": context["assignee"],
        "resolved_skills_json": json.dumps(context["skills"]),
        "idempotency_key": card["idempotency_key"],
        "source_run_id": run_id,
        "source_output_path": output_path,
        "source_output_sha256": output_hash,
        "source_timestamp": source_timestamp,
        "created_at": now,
        "updated_at": now,
    }
    names = ", ".join(fields)
    placeholders = ", ".join("?" for _ in fields)
    conn.execute(
        f"INSERT OR IGNORE INTO proposal_cards ({names}) VALUES ({placeholders})",
        list(fields.values()),
    )
    return card_id


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProposalParseError(f"{key} must be a non-empty string")
    return value.strip()


def _string_list(value: Any) -> list[str]:
    if not value:
        return []
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _non_empty_edit_str(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} must be a non-empty string")
    return text


def _kanban_priority(priority: Any) -> int:
    return {"low": -1, "medium": 0, "high": 1, "urgent": 2}.get(str(priority or "medium").lower(), 0)


def _lifecycle_for_kanban_status(status: str) -> str:
    value = str(status or "").strip().lower()
    if value == "done":
        return "completed"
    if value == "blocked":
        return "blocked"
    if value == "archived":
        return "archived"
    if value in {"running", "review"}:
        return "in_progress"
    return "worker_created"


def _feedback_card_summary(row: dict[str, Any], *, include_reason: bool = False) -> dict[str, Any]:
    item = {
        "card_id": row.get("card_id"),
        "title": row.get("title"),
        "summary": row.get("summary"),
        "priority": row.get("priority"),
        "confidence": row.get("confidence"),
        "effort": row.get("effort"),
        "status": row.get("status"),
        "lifecycle_status": row.get("lifecycle_status"),
    }
    if include_reason and row.get("decision_reason"):
        item["reason"] = row.get("decision_reason")
    return item


def _operator_preferences(rows: list[sqlite3.Row], limit: int) -> list[dict[str, Any]]:
    prefs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        ftype = str(row["feedback_type"] or "")
        if ftype not in {"comment", "reject", "edit", "outcome", "kanban_status"}:
            continue
        body = str(row["body"] or "").strip()
        if not body:
            continue
        compact = body[:300]
        key = f"{ftype}:{compact.lower()}"
        if key in seen:
            continue
        seen.add(key)
        prefs.append({"type": ftype, "body": compact, "author": row["author"]})
        if len(prefs) >= limit:
            break
    return prefs


def _self_improvement_target_for_job(job: dict[str, Any], config: dict[str, Any] | None) -> dict[str, str] | None:
    raw = job.get("self_improvement")
    if isinstance(raw, dict) and raw.get("enabled", True):
        project = str(raw.get("project") or "").strip()
        prong = str(raw.get("prong") or "").strip()
        if project and prong:
            return {"project": project, "prong": prong}

    job_id = str(job.get("id") or "").strip()
    job_name = str(job.get("name") or "").strip()
    projects = load_self_improvement_config(config).get("projects") or {}
    for project_id, project in projects.items():
        if not isinstance(project, dict):
            continue
        for prong_id, prong in (project.get("prongs") or {}).items():
            if not isinstance(prong, dict) or not prong.get("cron_prompt_context", False):
                continue
            ids = {str(v).strip() for v in _string_list(prong.get("cron_job_ids"))}
            names = {str(v).strip() for v in _string_list(prong.get("cron_job_names"))}
            if (job_id and job_id in ids) or (job_name and job_name in names):
                return {"project": str(project_id), "prong": str(prong_id)}
    return None


def _worker_url(board: str, task_id: str) -> str:
    try:
        from hermes_cli.discord_worker_boards import public_session_board_url

        public_url = public_session_board_url(board)
        if public_url:
            return public_url
    except Exception:
        pass
    return f"/workers/{board}?task={task_id}"


def _metadata_str(metadata: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, (int, float)):
            return str(value)
    return None


def _metadata_int(metadata: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _json_value(raw: Any, fallback: Any) -> Any:
    if not raw:
        return fallback
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _source_hash(output_path: str | None, output_text: str | None, metadata: dict[str, Any]) -> str | None:
    provided = _metadata_str(metadata, "cron_output_sha256", "output_sha256", "sha256")
    if provided:
        return provided
    if output_path and os.path.exists(output_path):
        h = hashlib.sha256()
        with open(output_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    if output_text is not None:
        return hashlib.sha256(output_text.encode("utf-8")).hexdigest()
    return None


def _run_key(metadata: dict[str, Any], output_path: str | None, output_hash: str | None, source_timestamp: int) -> str:
    explicit = _metadata_str(metadata, "proposal_run_key", "cron_run_id", "run_id")
    if explicit:
        return explicit
    return _stable_hash({"path": output_path, "sha256": output_hash, "timestamp": source_timestamp})


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
