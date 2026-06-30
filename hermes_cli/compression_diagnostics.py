"""Read-only compression/context exhaustion diagnostics."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_constants import display_hermes_home, get_hermes_home
from hermes_cli.config import load_config


CLASS_CONTEXT_SKIP = "context-overflow transcript persistence skip"
CLASS_COMPRESSION_SUCCESS = "successful compression"
CLASS_COMPRESSION_FALLBACK = "failed compression with fallback"
CLASS_AUTO_RESET = "auto-reset after compression exhaustion"
CLASS_ZERO_BOUNDARY = "expected zero-message compression boundary session"
CLASS_UNKNOWN = "unknown"

_SESSION_ID_RE = r"(?P<session>[A-Za-z0-9_.:-]+)"
_LOG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "context_skip",
        re.compile(
            r"Skipping transcript persistence for context-overflow failure in session "
            + _SESSION_ID_RE
        ),
    ),
    (
        "auto_reset",
        re.compile(r"Auto-resetting session " + _SESSION_ID_RE + r" after compression exhaustion"),
    ),
    (
        "compression_fallback",
        re.compile(
            r"Summary generation failed.*(?:fallback context summary|Inserted a fallback context marker|Recovered using main model)",
            re.IGNORECASE,
        ),
    ),
    (
        "compression_failed",
        re.compile(r"(?:Context compression failed|compression failed after|Summary generation failed)", re.IGNORECASE),
    ),
    (
        "compression_success",
        re.compile(r"(?:Compressed: \d+ -> \d+ messages|Compression #\d+ complete)", re.IGNORECASE),
    ),
)
_BRACKET_SESSION_RE = re.compile(r"\[(?P<session>[A-Za-z0-9_.:-]+)\]")


@dataclass
class LogEvent:
    kind: str
    session_id: str | None
    timestamp: float | None
    source: str


@dataclass
class SessionEvidence:
    session_id: str
    source: str = "unknown"
    model: str = "unknown"
    provider: str = "unknown"
    tokens: str = "unknown"
    context: str = "unknown"
    started_at: float | None = None
    ended_at: float | None = None
    end_reason: str = "unknown"
    message_count: int | None = None
    parent_session_id: str | None = None
    parent_end_reason: str | None = None
    parent_ended_at: float | None = None
    last_role: str = "unknown"
    events: set[str] = field(default_factory=set)
    evidence: list[str] = field(default_factory=list)


def parse_since(value: str | None) -> float:
    """Return an epoch lower bound. Defaults to the last 24 hours."""
    if not value:
        return time.time() - 24 * 60 * 60
    raw = value.strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", raw, re.IGNORECASE)
    if match:
        amount = float(match.group(1))
        unit = match.group(2).lower()
        multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return time.time() - amount * multiplier
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--since must be like 24h, 30m, or an ISO timestamp") from exc


def _parse_log_timestamp(line: str) -> float | None:
    prefix = line[:32]
    for fmt in ("%Y-%m-%d %H:%M:%S,%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(prefix[: len(datetime.now().strftime(fmt))], fmt)
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def parse_log_lines(lines: list[str], *, since_ts: float) -> list[LogEvent]:
    events: list[LogEvent] = []
    for line in lines:
        ts = _parse_log_timestamp(line)
        if ts is not None and ts < since_ts:
            continue
        for kind, pattern in _LOG_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            session_id = match.groupdict().get("session")
            if not session_id:
                bracket = _BRACKET_SESSION_RE.search(line)
                session_id = bracket.group("session") if bracket else None
            events.append(LogEvent(kind=kind, session_id=session_id, timestamp=ts, source="log"))
            break
    return events


def read_log_events(log_dir: Path, *, since_ts: float, max_lines: int = 20000) -> list[LogEvent]:
    if not log_dir.exists():
        return []
    candidates = sorted(
        [p for p in log_dir.glob("*.log*") if p.is_file()],
        key=lambda p: p.stat().st_mtime if p.exists() else 0,
        reverse=True,
    )[:12]
    lines: list[str] = []
    for path in candidates:
        try:
            file_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        lines.extend(file_lines[-max_lines:])
        if len(lines) >= max_lines:
            lines = lines[-max_lines:]
    return parse_log_lines(lines, since_ts=since_ts)


def _safe_json_loads(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _provider_from_config(model_config: dict[str, Any], billing_provider: str | None) -> str:
    for key in ("provider", "provider_name", "api_provider"):
        value = model_config.get(key)
        if value:
            return str(value)
    return billing_provider or "unknown"


def read_session_evidence(db_path: Path, *, since_ts: float, session_ids: set[str]) -> dict[str, SessionEvidence]:
    if not db_path.exists():
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT s.*, p.end_reason AS parent_end_reason, p.ended_at AS parent_ended_at
            FROM sessions s
            LEFT JOIN sessions p ON p.id = s.parent_session_id
            WHERE s.started_at >= ? OR s.ended_at >= ? OR s.id IN (%s)
            ORDER BY COALESCE(s.ended_at, s.started_at) DESC
            """
            % (",".join("?" for _ in session_ids) if session_ids else "NULL"),
            [since_ts, since_ts, *session_ids],
        ).fetchall()
        result: dict[str, SessionEvidence] = {}
        for row in rows:
            model_config = _safe_json_loads(row["model_config"])
            last_role_row = conn.execute(
                "SELECT role FROM messages WHERE session_id = ? ORDER BY timestamp DESC, id DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            input_tokens = row["input_tokens"] or 0
            output_tokens = row["output_tokens"] or 0
            context_limit = model_config.get("context_length") or model_config.get("context_window") or model_config.get("max_context_tokens")
            evidence = SessionEvidence(
                session_id=row["id"],
                source=row["source"] or "unknown",
                model=row["model"] or str(model_config.get("model") or "unknown"),
                provider=_provider_from_config(model_config, row["billing_provider"]),
                tokens=str(input_tokens + output_tokens) if input_tokens or output_tokens else "unknown",
                context=str(context_limit) if context_limit else "unknown",
                started_at=row["started_at"],
                ended_at=row["ended_at"],
                end_reason=row["end_reason"] or "unknown",
                message_count=row["message_count"],
                parent_session_id=row["parent_session_id"],
                parent_end_reason=row["parent_end_reason"],
                parent_ended_at=row["parent_ended_at"],
                last_role=last_role_row["role"] if last_role_row else "none",
            )
            result[evidence.session_id] = evidence
        return result
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _compression_settings() -> str:
    try:
        compression = load_config().get("compression", {})
    except Exception:
        compression = {}
    return "threshold={threshold} target={target} protect_tail={tail}".format(
        threshold=compression.get("threshold", "unknown"),
        target=compression.get("target_ratio", "unknown"),
        tail=compression.get("protect_last_n", "unknown"),
    )


def aggregate(db_path: Path, log_dir: Path, *, since_ts: float, limit: int) -> list[SessionEvidence]:
    log_events = read_log_events(log_dir, since_ts=since_ts)
    log_session_ids = {e.session_id for e in log_events if e.session_id}
    sessions = read_session_evidence(db_path, since_ts=since_ts, session_ids=log_session_ids)
    unknown_counter = 0
    for event in log_events:
        sid = event.session_id
        if not sid:
            unknown_counter += 1
            sid = f"log-only-{unknown_counter}"
        evidence = sessions.setdefault(sid, SessionEvidence(session_id=sid))
        evidence.events.add(event.kind)
        evidence.evidence.append(event.kind)
    for evidence in sessions.values():
        if evidence.end_reason == "compression":
            evidence.events.add("compression_success")
            evidence.evidence.append("state:end_reason=compression")
        if (
            evidence.parent_session_id
            and evidence.parent_end_reason == "compression"
            and (evidence.message_count or 0) == 0
        ):
            evidence.events.add("zero_boundary")
            evidence.evidence.append("state:parent compression + zero messages")
    diagnostic_rows = [row for row in sessions.values() if classify(row) != CLASS_UNKNOWN]
    return sorted(
        diagnostic_rows,
        key=lambda e: max(e.ended_at or 0, e.started_at or 0),
        reverse=True,
    )[: max(1, limit)]


def classify(evidence: SessionEvidence) -> str:
    events = evidence.events
    if "auto_reset" in events:
        return CLASS_AUTO_RESET
    if "context_skip" in events:
        return CLASS_CONTEXT_SKIP
    if "zero_boundary" in events:
        return CLASS_ZERO_BOUNDARY
    if "compression_fallback" in events or ("compression_failed" in events and "compression_success" in events):
        return CLASS_COMPRESSION_FALLBACK
    if "compression_success" in events:
        return CLASS_COMPRESSION_SUCCESS
    return CLASS_UNKNOWN


def _completion(evidence: SessionEvidence) -> str:
    classification = classify(evidence)
    if classification in {CLASS_AUTO_RESET, CLASS_CONTEXT_SKIP}:
        return "failure-visible" if "auto_reset" in evidence.events else "unknown"
    if evidence.last_role == "assistant":
        return "assistant-message"
    return "unknown"


def format_report(rows: list[SessionEvidence], *, db_path: Path, log_dir: Path, since_ts: float) -> str:
    settings = _compression_settings()
    lines = [
        "Hermes compression diagnostics",
        f"window_since={datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()} db={db_path} logs={log_dir}",
        f"compression_config={settings}",
        "session_id | source | model/provider | tokens/context | classification | persistence | reset | fallback | zero_msg_boundary | completion | evidence",
    ]
    for row in rows:
        classification = classify(row)
        evidence = ",".join(dict.fromkeys(row.evidence)) or "state-only"
        lines.append(
            " | ".join(
                [
                    row.session_id,
                    row.source,
                    f"{row.model}/{row.provider}",
                    f"{row.tokens}/{row.context}",
                    classification,
                    "skipped" if "context_skip" in row.events else "unknown",
                    "yes" if "auto_reset" in row.events else "unknown",
                    "yes" if "compression_fallback" in row.events else "unknown",
                    "yes" if "zero_boundary" in row.events else "no",
                    _completion(row),
                    evidence,
                ]
            )
        )
    if not rows:
        lines.append("No compression/context exhaustion evidence found in the requested window.")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    since_ts = parse_since(getattr(args, "since", None))
    hermes_home = get_hermes_home()
    db_path = Path(getattr(args, "db_path", None) or hermes_home / "state.db")
    log_dir = Path(getattr(args, "log_dir", None) or hermes_home / "logs")
    rows = aggregate(db_path, log_dir, since_ts=since_ts, limit=getattr(args, "limit", 25))
    report = format_report(rows, db_path=db_path, log_dir=log_dir, since_ts=since_ts)
    if not getattr(args, "db_path", None) and str(hermes_home) in report:
        report = report.replace(str(hermes_home), display_hermes_home())
    print(report)
    return 0
