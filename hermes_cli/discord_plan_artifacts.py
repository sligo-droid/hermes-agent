"""Durable Discord plan artifacts.

Discord forces long bot responses to be split across multiple messages.  For
plan-like responses, keep the full unsplit text in a profile-scoped local
artifact and map Discord snowflakes to that artifact so later thread/link
references can recover exact context without reconstructing it from history.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_constants import get_hermes_home

ARTIFACT_ROOT_RELATIVE = Path("artifacts") / "discord-plans"
INDEX_FILENAME = "index.sqlite"

PLAN_MARKER_RE = re.compile(
    r"(?im)(^|\n)\s*(?:#{1,4}\s*)?(?:"
    r"plan|implementation phases?|implementation plan|recommendation|"
    r"acceptance criteria|proposed architecture|deliverables|test plan|"
    r"final recommendation|success means|stop when|phase\s+\d+|"
    r"\d+\.\s+phase|next steps|risks?|scope|definition of done"
    r")\b"
)
PLAN_STRUCTURE_RE = re.compile(r"(?im)^\s*(?:[-*]|\d+[.)])\s+\S+")
DISCORD_ID_RE = re.compile(r"^\d{16,24}$")

MIN_PLAN_CHARS = 1_800
MIN_STRUCTURED_LONG_CHARS = 3_000


@dataclass(frozen=True)
class DiscordPlanArtifactRecord:
    artifact_id: str
    artifact_path: str
    content_sha256: str
    kind: str = "discord_plan"
    created_at: str = ""
    updated_at: str = ""
    thread_id: str = ""
    channel_id: str = ""
    guild_id: str = ""
    parent_channel_id: str = ""
    source_url: str = ""
    source_message_id: str = ""
    session_id: str = ""
    command: str = ""
    bot_message_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None
    content: str = ""

    def as_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        data: dict[str, Any] = {
            "artifact_id": self.artifact_id,
            "artifact_path": self.artifact_path,
            "content_sha256": self.content_sha256,
            "kind": self.kind,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "thread_id": self.thread_id,
            "channel_id": self.channel_id,
            "guild_id": self.guild_id,
            "parent_channel_id": self.parent_channel_id,
            "source_url": self.source_url,
            "source_message_id": self.source_message_id,
            "session_id": self.session_id,
            "command": self.command,
            "bot_message_ids": list(self.bot_message_ids),
            "metadata": dict(self.metadata or {}),
        }
        if include_content:
            data["content"] = self.content
        return data


def artifact_root() -> Path:
    return get_hermes_home() / ARTIFACT_ROOT_RELATIVE


def index_path() -> Path:
    return artifact_root() / INDEX_FILENAME


def should_persist_discord_plan(
    content: str,
    *,
    metadata: Optional[dict[str, Any]] = None,
    chunk_count: int = 0,
) -> bool:
    """Return true when a Discord bot response should become a plan artifact.

    This intentionally avoids persisting every bot reply.  It captures long,
    structured plan-like outputs and known plan-producing command surfaces.
    """
    text = str(content or "").strip()
    if len(text) < MIN_PLAN_CHARS:
        return False

    meta = metadata if isinstance(metadata, dict) else {}
    command = str(meta.get("command") or meta.get("invoked_command") or "").strip().lower()
    kind = str(meta.get("kind") or meta.get("response_kind") or "").strip().lower()
    if command in {"meeting", "/meeting", "goal", "/goal", "plan", "/plan"}:
        return True
    if kind in {"meeting_plan", "implementation_plan", "discord_plan", "plan"}:
        return True

    has_marker = bool(PLAN_MARKER_RE.search(text))
    if len(text) >= MIN_PLAN_CHARS and has_marker and chunk_count > 1:
        return True
    if len(text) >= MIN_STRUCTURED_LONG_CHARS and has_marker and PLAN_STRUCTURE_RE.search(text):
        return True
    return False


def persist_discord_plan_artifact(
    content: str,
    *,
    thread_id: str | None = None,
    channel_id: str | None = None,
    guild_id: str | None = None,
    parent_channel_id: str | None = None,
    source_url: str | None = None,
    source_message_id: str | None = None,
    session_id: str | None = None,
    command: str | None = None,
    kind: str = "discord_plan",
    bot_message_ids: Iterable[str] | None = None,
    metadata: Optional[dict[str, Any]] = None,
    chunk_count: int = 0,
    force: bool = False,
) -> DiscordPlanArtifactRecord | None:
    """Persist a plan-like Discord response and index its Discord ids.

    Returns the persisted record.  Returns ``None`` when the content is not
    plan-like and ``force`` is false.
    """
    text = str(content or "").strip()
    if not text:
        return None
    meta = dict(metadata or {})
    if not force and not should_persist_discord_plan(text, metadata=meta, chunk_count=chunk_count):
        return None

    now = _now_iso()
    clean_bot_ids = tuple(_clean_snowflake(value) for value in (bot_message_ids or ()))
    clean_bot_ids = tuple(value for value in clean_bot_ids if value)
    thread = _clean_snowflake(thread_id)
    channel = _clean_snowflake(channel_id)
    guild = _clean_snowflake(guild_id)
    parent = _clean_snowflake(parent_channel_id)
    source_message = _clean_snowflake(source_message_id)
    session = str(session_id or "").strip()
    command_value = str(command or meta.get("command") or meta.get("invoked_command") or "").strip()
    kind_value = str(kind or "discord_plan").strip() or "discord_plan"
    source = str(source_url or "").strip() or _build_source_url(
        guild_id=guild,
        parent_channel_id=parent,
        channel_id=channel,
        thread_id=thread,
    )

    content_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    artifact_id = hashlib.sha256(
        "\0".join([thread or channel, content_sha, kind_value]).encode("utf-8")
    ).hexdigest()[:32]
    relative_path = _artifact_relative_path(
        created_at=now,
        thread_id=thread or channel or "discord",
        artifact_id=artifact_id,
        content_sha=content_sha,
    )
    root = artifact_root()
    artifact_path = root / relative_path
    _write_text_private(
        artifact_path,
        _render_markdown_artifact(
            artifact_id=artifact_id,
            content=text,
            content_sha256=content_sha,
            kind=kind_value,
            created_at=now,
            thread_id=thread,
            channel_id=channel,
            guild_id=guild,
            parent_channel_id=parent,
            source_url=source,
            source_message_id=source_message,
            session_id=session,
            command=command_value,
            bot_message_ids=clean_bot_ids,
        ),
    )

    metadata_json = json.dumps(meta, sort_keys=True, ensure_ascii=False)
    bot_ids_json = json.dumps(list(clean_bot_ids), ensure_ascii=False)
    conn = _connect()
    try:
        conn.execute(
            """
            INSERT INTO artifacts (
                artifact_id, artifact_path, content_sha256, kind, created_at, updated_at,
                thread_id, channel_id, guild_id, parent_channel_id, source_url,
                source_message_id, session_id, command, bot_message_ids, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(artifact_id) DO UPDATE SET
                artifact_path=excluded.artifact_path,
                updated_at=excluded.updated_at,
                thread_id=excluded.thread_id,
                channel_id=excluded.channel_id,
                guild_id=excluded.guild_id,
                parent_channel_id=excluded.parent_channel_id,
                source_url=excluded.source_url,
                source_message_id=excluded.source_message_id,
                session_id=excluded.session_id,
                command=excluded.command,
                bot_message_ids=excluded.bot_message_ids,
                metadata_json=excluded.metadata_json
            """,
            (
                artifact_id,
                str(artifact_path),
                content_sha,
                kind_value,
                now,
                now,
                thread,
                channel,
                guild,
                parent,
                source,
                source_message,
                session,
                command_value,
                bot_ids_json,
                metadata_json,
            ),
        )
        _upsert_mappings(
            conn,
            artifact_id=artifact_id,
            created_at=now,
            thread_id=thread,
            channel_id=channel,
            source_message_id=source_message,
            bot_message_ids=clean_bot_ids,
        )
        conn.commit()
    finally:
        conn.close()

    return DiscordPlanArtifactRecord(
        artifact_id=artifact_id,
        artifact_path=str(artifact_path),
        content_sha256=content_sha,
        kind=kind_value,
        created_at=now,
        updated_at=now,
        thread_id=thread,
        channel_id=channel,
        guild_id=guild,
        parent_channel_id=parent,
        source_url=source,
        source_message_id=source_message,
        session_id=session,
        command=command_value,
        bot_message_ids=clean_bot_ids,
        metadata=meta,
        content=text,
    )


def lookup_discord_plan_artifact(identifier: str) -> DiscordPlanArtifactRecord | None:
    """Look up the newest plan artifact mapped to a Discord snowflake/id."""
    ident = _clean_snowflake(identifier)
    if not ident:
        return None
    db = index_path()
    if not db.exists():
        return None
    conn = _connect(create=False)
    try:
        row = conn.execute(
            """
            SELECT a.*
            FROM mappings m
            JOIN artifacts a ON a.artifact_id = m.artifact_id
            WHERE m.identifier_value = ?
            ORDER BY a.updated_at DESC, a.created_at DESC
            LIMIT 1
            """,
            (ident,),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT * FROM artifacts
                WHERE thread_id = ? OR channel_id = ? OR source_message_id = ?
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 1
                """,
                (ident, ident, ident),
            ).fetchone()
        if row is None:
            return None
        return _record_from_row(row)
    finally:
        conn.close()


def list_discord_plan_artifacts(*, limit: int = 20) -> list[DiscordPlanArtifactRecord]:
    db = index_path()
    if not db.exists():
        return []
    conn = _connect(create=False)
    try:
        rows = conn.execute(
            "SELECT * FROM artifacts ORDER BY updated_at DESC, created_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return [_record_from_row(row) for row in rows]
    finally:
        conn.close()


def _record_from_row(row: sqlite3.Row) -> DiscordPlanArtifactRecord:
    metadata: dict[str, Any] = {}
    try:
        loaded = json.loads(row["metadata_json"] or "{}")
        if isinstance(loaded, dict):
            metadata = loaded
    except Exception:
        metadata = {}
    try:
        bot_ids_raw = json.loads(row["bot_message_ids"] or "[]")
        bot_ids = tuple(str(value) for value in bot_ids_raw if str(value).strip())
    except Exception:
        bot_ids = ()
    path = str(row["artifact_path"] or "")
    content = ""
    if path:
        try:
            content = _extract_content_from_artifact(Path(path))
        except Exception:
            content = ""
    return DiscordPlanArtifactRecord(
        artifact_id=str(row["artifact_id"] or ""),
        artifact_path=path,
        content_sha256=str(row["content_sha256"] or ""),
        kind=str(row["kind"] or "discord_plan"),
        created_at=str(row["created_at"] or ""),
        updated_at=str(row["updated_at"] or ""),
        thread_id=str(row["thread_id"] or ""),
        channel_id=str(row["channel_id"] or ""),
        guild_id=str(row["guild_id"] or ""),
        parent_channel_id=str(row["parent_channel_id"] or ""),
        source_url=str(row["source_url"] or ""),
        source_message_id=str(row["source_message_id"] or ""),
        session_id=str(row["session_id"] or ""),
        command=str(row["command"] or ""),
        bot_message_ids=bot_ids,
        metadata=metadata,
        content=content,
    )


def _connect(*, create: bool = True) -> sqlite3.Connection:
    db = index_path()
    if create:
        _ensure_private_dir(db.parent)
    conn = sqlite3.connect(str(db), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    if create:
        _ensure_schema(conn)
        try:
            os.chmod(db, 0o600)
        except OSError:
            pass
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS artifacts (
            artifact_id TEXT PRIMARY KEY,
            artifact_path TEXT NOT NULL,
            content_sha256 TEXT NOT NULL,
            kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            thread_id TEXT,
            channel_id TEXT,
            guild_id TEXT,
            parent_channel_id TEXT,
            source_url TEXT,
            source_message_id TEXT,
            session_id TEXT,
            command TEXT,
            bot_message_ids TEXT,
            metadata_json TEXT
        );
        CREATE TABLE IF NOT EXISTS mappings (
            identifier_type TEXT NOT NULL,
            identifier_value TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY(identifier_type, identifier_value),
            FOREIGN KEY(artifact_id) REFERENCES artifacts(artifact_id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_discord_plan_mappings_value
            ON mappings(identifier_value);
        CREATE INDEX IF NOT EXISTS idx_discord_plan_artifacts_thread
            ON artifacts(thread_id, updated_at);
        """
    )


def _upsert_mappings(
    conn: sqlite3.Connection,
    *,
    artifact_id: str,
    created_at: str,
    thread_id: str,
    channel_id: str,
    source_message_id: str,
    bot_message_ids: Iterable[str],
) -> None:
    mappings: list[tuple[str, str]] = []
    if thread_id:
        mappings.append(("thread", thread_id))
    if channel_id:
        mappings.append(("channel", channel_id))
    if source_message_id:
        mappings.append(("source_message", source_message_id))
    for message_id in bot_message_ids:
        if message_id:
            mappings.append(("bot_message", message_id))
    seen: set[tuple[str, str]] = set()
    for identifier_type, identifier_value in mappings:
        key = (identifier_type, identifier_value)
        if key in seen:
            continue
        seen.add(key)
        conn.execute(
            """
            INSERT INTO mappings(identifier_type, identifier_value, artifact_id, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(identifier_type, identifier_value) DO UPDATE SET
                artifact_id=excluded.artifact_id,
                created_at=excluded.created_at
            """,
            (identifier_type, identifier_value, artifact_id, created_at),
        )


def _artifact_relative_path(
    *,
    created_at: str,
    thread_id: str,
    artifact_id: str,
    content_sha: str,
) -> Path:
    day = (created_at.split("T", 1)[0] or "unknown").strip()
    safe_thread = re.sub(r"[^0-9A-Za-z_.-]+", "-", thread_id).strip("-") or "discord"
    return Path("plans") / day / f"discord-{safe_thread}-{artifact_id}-{content_sha[:12]}.md"


def _render_markdown_artifact(
    *,
    artifact_id: str,
    content: str,
    content_sha256: str,
    kind: str,
    created_at: str,
    thread_id: str,
    channel_id: str,
    guild_id: str,
    parent_channel_id: str,
    source_url: str,
    source_message_id: str,
    session_id: str,
    command: str,
    bot_message_ids: Iterable[str],
) -> str:
    lines = [
        "# Discord Plan Artifact",
        "",
        f"Artifact ID: {artifact_id}",
        f"Kind: {kind}",
        f"Created: {created_at}",
        f"Content SHA256: {content_sha256}",
    ]
    if source_url:
        lines.append(f"Source URL: {source_url}")
    if guild_id:
        lines.append(f"Guild ID: {guild_id}")
    if parent_channel_id:
        lines.append(f"Parent channel ID: {parent_channel_id}")
    if channel_id:
        lines.append(f"Channel ID: {channel_id}")
    if thread_id:
        lines.append(f"Thread ID: {thread_id}")
    if source_message_id:
        lines.append(f"Source message ID: {source_message_id}")
    if session_id:
        lines.append(f"Session ID: {session_id}")
    if command:
        lines.append(f"Command: {command}")
    ids = [str(value) for value in bot_message_ids if str(value).strip()]
    if ids:
        lines.append("Posted message IDs:")
        lines.extend(f"- {value}" for value in ids)
    lines.extend(["", "---", "", content.strip(), ""])
    return "\n".join(lines)


def _extract_content_from_artifact(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    marker = "\n---\n\n"
    if marker in text:
        return text.split(marker, 1)[1].strip()
    return text.strip()


def _write_text_private(path: Path, content: str) -> None:
    _ensure_private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    os.replace(str(tmp), str(path))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    current = path
    root = artifact_root()
    while True:
        try:
            os.chmod(current, 0o700)
        except OSError:
            pass
        if current == root or current.parent == current:
            break
        if not str(current).startswith(str(root)):
            break
        current = current.parent


def _build_source_url(
    *,
    guild_id: str,
    parent_channel_id: str,
    channel_id: str,
    thread_id: str,
) -> str:
    if guild_id and parent_channel_id and thread_id:
        return f"https://discord.com/channels/{guild_id}/{parent_channel_id}/{thread_id}"
    if guild_id and channel_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}"
    return ""


def _clean_snowflake(value: Any) -> str:
    text = str(value or "").strip()
    return text if DISCORD_ID_RE.match(text) else ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
