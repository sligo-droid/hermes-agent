"""Audited operator annotations for Command Center Work Items."""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

VALID_MODES = {"note", "correction"}
MAX_TEXT_LENGTH = 4000
MAX_TITLE_LENGTH = 200


def annotations_db_path() -> Path:
    return get_hermes_home() / "command_center" / "annotations.sqlite"


def _connect() -> sqlite3.Connection:
    path = annotations_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS command_center_annotations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            work_item_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            text TEXT NOT NULL,
            title TEXT,
            actor TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            target_kind TEXT NOT NULL,
            target_id TEXT NOT NULL,
            previous_title TEXT,
            previous_summary TEXT,
            previous_status TEXT,
            source_ref_json TEXT NOT NULL,
            execution_snapshot_json TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cca_work_item_created ON command_center_annotations(work_item_id, created_at, id)")
    return conn


def validate_annotation(*, mode: str, text: str, title: str | None = None) -> tuple[str, str, str | None]:
    clean_mode = str(mode or "").strip().lower()
    if clean_mode not in VALID_MODES:
        raise ValueError("mode must be 'note' or 'correction'")
    clean_text = str(text or "").strip()
    if not clean_text:
        raise ValueError("text is required")
    if len(clean_text) > MAX_TEXT_LENGTH:
        raise ValueError(f"text must be <= {MAX_TEXT_LENGTH} characters")
    clean_title = str(title or "").strip() or None
    if clean_title is not None and len(clean_title) > MAX_TITLE_LENGTH:
        raise ValueError(f"title must be <= {MAX_TITLE_LENGTH} characters")
    return clean_mode, clean_text, clean_title


def record_annotation(
    *,
    work_item_id: str,
    mode: str,
    text: str,
    actor: str,
    target_kind: str,
    target_id: str,
    previous_title: str | None,
    previous_summary: str | None,
    previous_status: str | None,
    source_ref: dict[str, Any],
    execution_snapshot: dict[str, Any],
    title: str | None = None,
    created_at: int | None = None,
) -> dict[str, Any]:
    clean_mode, clean_text, clean_title = validate_annotation(mode=mode, text=text, title=title)
    now = int(created_at if created_at is not None else time.time())
    conn = _connect()
    try:
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO command_center_annotations(
                    work_item_id, mode, text, title, actor, created_at,
                    target_kind, target_id, previous_title, previous_summary,
                    previous_status, source_ref_json, execution_snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(work_item_id),
                    clean_mode,
                    clean_text,
                    clean_title,
                    str(actor or "dashboard"),
                    now,
                    str(target_kind),
                    str(target_id),
                    previous_title,
                    previous_summary,
                    previous_status,
                    json.dumps(source_ref or {}, sort_keys=True),
                    json.dumps(execution_snapshot or {}, sort_keys=True),
                ),
            )
        annotation_id = int(cursor.lastrowid)
    finally:
        conn.close()
    return get_annotation(annotation_id) or {}


def get_annotation(annotation_id: int) -> dict[str, Any] | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM command_center_annotations WHERE id = ?", (annotation_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_annotation(row) if row else None


def list_annotations(work_item_id: str | None = None) -> list[dict[str, Any]]:
    conn = _connect()
    try:
        if work_item_id is None:
            rows = conn.execute("SELECT * FROM command_center_annotations ORDER BY created_at ASC, id ASC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM command_center_annotations WHERE work_item_id = ? ORDER BY created_at ASC, id ASC",
                (str(work_item_id),),
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_annotation(row) for row in rows]


def enrich_work_item(item: dict[str, Any]) -> dict[str, Any]:
    annotations = list_annotations(str(item.get("id") or ""))
    notes = [annotation for annotation in annotations if annotation.get("mode") == "note"]
    corrections = [annotation for annotation in annotations if annotation.get("mode") == "correction"]
    item["annotations"] = annotations
    item["operator_note_count"] = len(notes)
    item["latest_operator_note"] = notes[-1] if notes else None
    item["latest_correction"] = corrections[-1] if corrections else None
    return item


def operator_context_block(work_item_id: str) -> str:
    annotations = list_annotations(work_item_id)
    if not annotations:
        return ""
    lines = ["Operator annotations for Command Center Work Item:", f"- work_item_id: {work_item_id}"]
    latest_correction = next((annotation for annotation in reversed(annotations) if annotation.get("mode") == "correction"), None)
    notes = [annotation for annotation in annotations if annotation.get("mode") == "note"]
    if latest_correction:
        lines.extend(
            [
                "- latest_correction:",
                f"  title: {latest_correction.get('title') or 'none'}",
                f"  text: {latest_correction.get('text')}",
            ]
        )
    if notes:
        lines.append("- notes:")
        for note in notes[-5:]:
            lines.append(f"  - {note.get('text')}")
    lines.append("Do not mutate original proposal text, board root goals, or existing task bodies silently; treat these annotations as audited operator context.")
    return "\n".join(lines)


def _row_to_annotation(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "work_item_id": row["work_item_id"],
        "mode": row["mode"],
        "text": row["text"],
        "title": row["title"],
        "actor": row["actor"],
        "created_at": int(row["created_at"]),
        "target_kind": row["target_kind"],
        "target_id": row["target_id"],
        "previous_title": row["previous_title"],
        "previous_summary": row["previous_summary"],
        "previous_status": row["previous_status"],
        "source_ref": json.loads(row["source_ref_json"] or "{}"),
        "execution_snapshot": json.loads(row["execution_snapshot_json"] or "{}"),
    }
