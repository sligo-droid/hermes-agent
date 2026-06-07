"""Read-only runtime evidence bundles for repeated Hermes diagnostics.

This module is intentionally deterministic and stdlib-only. It gathers the
small set of local facts agents repeatedly need when debugging Discord/mainline
runtime issues: gateway timing logs, response-ready API counts, session rows,
kanban task/run durations, board metadata, crash/block events, and sanitized log
snippets.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from hermes_constants import get_hermes_home


_MAX_LOG_BYTES = 256 * 1024
_MAX_SNIPPETS = 24
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|authorization|bearer|token|password|secret)"
    r"([\s:=]+)([^\s,;\]})]+)"
)
_RESPONSE_READY_RE = re.compile(
    r"response ready: platform=(?P<platform>\S+) chat=(?P<chat>\S+) "
    r"time=(?P<seconds>[0-9.]+)s api_calls=(?P<api_calls>\d+) "
    r"response=(?P<chars>\d+) chars"
)
_TIME_PREFIX_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}[^ ]* [^ ]+|\d{4}-\d{2}-\d{2}T[^ ]+)")
_CRASH_BLOCK_RE = re.compile(
    r"(?i)\b(crash|crashed|exception|traceback|blocked|timed out|timeout|spawn_failed|failed)\b"
)
_PR_BRANCH_KEYS = re.compile(r"(?i)(pr|pull|github|branch|repo|merge)")
_BOARD_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,63}$")


@dataclass(frozen=True)
class RuntimeEvidenceRequest:
    thread_id: str | None = None
    session_id: str | None = None
    board: str | None = None
    hermes_home: Path | None = None
    max_log_lines: int = _MAX_SNIPPETS


def _home(req: RuntimeEvidenceRequest) -> Path:
    return req.hermes_home or get_hermes_home()


def _redact(value: str) -> str:
    return _SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", value)


def _trim(value: Any, limit: int = 500) -> str | None:
    if value is None:
        return None
    text = _redact(str(value).replace("\n", "\\n"))
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _sqlite_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        uri = f"file:{path.resolve()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


def _state_db_path(home: Path) -> Path:
    return home / "state.db"


def _board_slug(board: str | None) -> str:
    slug = (board or "default").strip().lower() or "default"
    if not _BOARD_SLUG_RE.match(slug):
        raise ValueError(
            "invalid board slug: must be 1-64 lowercase alphanumerics, hyphens, or underscores"
        )
    return slug


def _kanban_db_path(home: Path, board: str | None) -> Path:
    slug = _board_slug(board)
    if slug == "default":
        return home / "kanban.db"
    return home / "kanban" / "boards" / slug / "kanban.db"


def _board_json_path(home: Path, board: str | None) -> Path:
    slug = _board_slug(board)
    if slug == "default":
        return home / "kanban" / "boards" / "default" / "board.json"
    return home / "kanban" / "boards" / slug / "board.json"


def _safe_subset(
    data: dict[str, Any], *, key_filter: re.Pattern[str] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in sorted(data):
        if key_filter is not None and not key_filter.search(str(key)):
            continue
        value = data[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True, ensure_ascii=False)
        out[str(key)] = _trim(value, 300)
    return out


def _collect_session(home: Path, session_id: str | None) -> dict[str, Any]:
    if not session_id:
        return {"requested": None, "found": False}
    path = _state_db_path(home)
    conn = _sqlite_ro(path)
    if conn is None:
        return {"requested": session_id, "found": False, "db_path": str(path)}
    try:
        if not _table_exists(conn, "sessions"):
            return {"requested": session_id, "found": False, "db_path": str(path)}
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        session = _row_dict(row)
        if session is None:
            return {"requested": session_id, "found": False, "db_path": str(path)}
        keep = {
            "id",
            "source",
            "user_id",
            "model",
            "parent_session_id",
            "started_at",
            "ended_at",
            "end_reason",
            "message_count",
            "tool_call_count",
            "api_call_count",
            "handoff_platform",
            "handoff_error",
            "title",
        }
        result = {key: _trim(session.get(key), 300) for key in sorted(keep) if key in session}
        if _table_exists(conn, "messages"):
            msg = conn.execute(
                "SELECT MIN(timestamp) AS first_message_at, MAX(timestamp) AS last_message_at, "
                "COUNT(*) AS messages FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if msg:
                result["message_window"] = dict(msg)
        return {"requested": session_id, "found": True, "db_path": str(path), "session": result}
    finally:
        conn.close()


def _parse_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        raw = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return raw if isinstance(raw, dict) else {}


def _task_query_clauses(
    conn: sqlite3.Connection, thread_id: str | None, session_id: str | None
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    task_cols = _columns(conn, "tasks")
    if session_id and "session_id" in task_cols:
        clauses.append("t.session_id = ?")
        params.append(session_id)
    if thread_id and _table_exists(conn, "kanban_notify_subs"):
        clauses.append(
            "EXISTS (SELECT 1 FROM kanban_notify_subs ns "
            "WHERE ns.task_id = t.id AND ns.thread_id = ?)"
        )
        params.append(thread_id)
    where = " OR ".join(clauses)
    return (f"WHERE {where}" if where else "", params)


def _collect_kanban(
    home: Path, board: str | None, thread_id: str | None, session_id: str | None
) -> dict[str, Any]:
    path = _kanban_db_path(home, board)
    board_meta = _read_json(_board_json_path(home, board))
    board_evidence = _safe_subset(board_meta, key_filter=_PR_BRANCH_KEYS)
    conn = _sqlite_ro(path)
    if conn is None:
        return {
            "board": board or "default",
            "db_path": str(path),
            "found": False,
            "board_metadata": board_evidence,
        }
    try:
        if not _table_exists(conn, "tasks"):
            return {
                "board": board or "default",
                "db_path": str(path),
                "found": False,
                "board_metadata": board_evidence,
            }
        where, params = _task_query_clauses(conn, thread_id, session_id)
        task_rows = conn.execute(
            "SELECT * FROM tasks t "
            f"{where} "
            "ORDER BY COALESCE(t.started_at, t.created_at) DESC, t.id DESC LIMIT 20",
            params,
        ).fetchall()
        tasks: list[dict[str, Any]] = []
        task_ids = [row["id"] for row in task_rows]
        for row in task_rows:
            item = _row_dict(row) or {}
            keep = {
                "id",
                "title",
                "assignee",
                "status",
                "created_at",
                "started_at",
                "completed_at",
                "workspace_kind",
                "workspace_path",
                "branch_name",
                "consecutive_failures",
                "last_failure_error",
                "current_run_id",
                "session_id",
            }
            tasks.append({key: _trim(item.get(key), 500) for key in sorted(keep) if key in item})

        runs: list[dict[str, Any]] = []
        if task_ids and _table_exists(conn, "task_runs"):
            placeholders = ",".join("?" for _ in task_ids)
            for row in conn.execute(
                "SELECT * FROM task_runs "
                f"WHERE task_id IN ({placeholders}) "
                "ORDER BY started_at DESC, id DESC LIMIT 30",
                task_ids,
            ).fetchall():
                run = _row_dict(row) or {}
                started = run.get("started_at")
                ended = run.get("ended_at")
                duration = None
                if started is not None and ended is not None:
                    duration = int(ended) - int(started)
                metadata = _safe_subset(
                    _parse_metadata(run.get("metadata")), key_filter=_PR_BRANCH_KEYS
                )
                runs.append(
                    {
                        "id": run.get("id"),
                        "task_id": run.get("task_id"),
                        "profile": _trim(run.get("profile"), 120),
                        "status": run.get("status"),
                        "outcome": run.get("outcome"),
                        "started_at": started,
                        "ended_at": ended,
                        "duration_seconds": duration,
                        "error": _trim(run.get("error"), 500),
                        "summary": _trim(run.get("summary"), 500),
                        "metadata": metadata,
                    }
                )

        events: list[dict[str, Any]] = []
        if task_ids and _table_exists(conn, "task_events"):
            placeholders = ",".join("?" for _ in task_ids)
            for row in conn.execute(
                "SELECT * FROM task_events "
                f"WHERE task_id IN ({placeholders}) "
                "AND (kind LIKE '%block%' OR kind LIKE '%crash%' OR kind LIKE '%fail%' "
                "OR kind LIKE '%timeout%' OR kind LIKE '%reclaim%') "
                "ORDER BY created_at DESC, id DESC LIMIT 30",
                task_ids,
            ).fetchall():
                ev = _row_dict(row) or {}
                events.append(
                    {
                        "id": ev.get("id"),
                        "task_id": ev.get("task_id"),
                        "run_id": ev.get("run_id"),
                        "kind": ev.get("kind"),
                        "created_at": ev.get("created_at"),
                        "payload": _trim(ev.get("payload"), 700),
                    }
                )
        return {
            "board": board or "default",
            "db_path": str(path),
            "found": True,
            "board_metadata": board_evidence,
            "tasks": tasks,
            "runs": runs,
            "events": events,
        }
    finally:
        conn.close()


def _candidate_log_paths(home: Path) -> list[Path]:
    logs = home / "logs"
    return [
        logs / "gateway.log",
        logs / "errors.log",
        logs / "agent.log",
        logs / "tui_gateway_crash.log",
    ]


def _read_log_lines(path: Path) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > _MAX_LOG_BYTES:
                fh.seek(size - _MAX_LOG_BYTES)
                fh.readline()
            data = fh.read()
    except OSError:
        return []
    return data.decode("utf-8", errors="replace").splitlines()


def _matches_context(line: str, needles: Iterable[str]) -> bool:
    return any(needle and needle in line for needle in needles)


def _line_ts(line: str) -> str | None:
    m = _TIME_PREFIX_RE.match(line)
    return m.group("ts") if m else None


def _collect_logs(home: Path, req: RuntimeEvidenceRequest) -> dict[str, Any]:
    needles = [n for n in (req.thread_id, req.session_id) if n]
    snippets: list[dict[str, Any]] = []
    response_ready: list[dict[str, Any]] = []
    crash_or_block: list[dict[str, Any]] = []
    for path in _candidate_log_paths(home):
        lines = _read_log_lines(path)
        if not lines:
            continue
        for line in lines:
            context_match = _matches_context(line, needles)
            rr = _RESPONSE_READY_RE.search(line)
            if rr and (not req.thread_id or context_match or rr.group("chat") == req.thread_id):
                item = {
                    "log": path.name,
                    "timestamp": _line_ts(line),
                    "platform": rr.group("platform"),
                    "chat": rr.group("chat"),
                    "time_seconds": float(rr.group("seconds")),
                    "api_calls": int(rr.group("api_calls")),
                    "response_chars": int(rr.group("chars")),
                }
                response_ready.append(item)
                snippets.append(
                    {"log": path.name, "line": _trim(line, 700), "reason": "response_ready"}
                )
                continue
            if _CRASH_BLOCK_RE.search(line) and (not needles or context_match):
                crash_or_block.append(
                    {"log": path.name, "timestamp": _line_ts(line), "line": _trim(line, 700)}
                )
                snippets.append({"log": path.name, "line": _trim(line, 700), "reason": "crash_or_block"})
            elif context_match:
                snippets.append({"log": path.name, "line": _trim(line, 700), "reason": "context"})
    return {
        "response_ready": response_ready[-req.max_log_lines :],
        "crash_or_block": crash_or_block[-req.max_log_lines :],
        "snippets": snippets[-req.max_log_lines :],
    }


def collect_runtime_evidence(req: RuntimeEvidenceRequest) -> dict[str, Any]:
    home = _home(req)
    return {
        "inputs": {
            "thread_id": req.thread_id,
            "session_id": req.session_id,
            "board": req.board or "default",
            "hermes_home": str(home),
        },
        "session": _collect_session(home, req.session_id),
        "kanban": _collect_kanban(home, req.board, req.thread_id, req.session_id),
        "logs": _collect_logs(home, req),
    }


def _format_section(title: str, lines: list[str]) -> str:
    body = "\n".join(lines) if lines else "(none)"
    return f"## {title}\n{body}"


def format_text(bundle: dict[str, Any]) -> str:
    inputs = bundle["inputs"]
    sections: list[str] = [
        _format_section(
            "Inputs",
            [
                f"{key}: {inputs.get(key)}"
                for key in ("thread_id", "session_id", "board", "hermes_home")
            ],
        )
    ]
    session = bundle["session"]
    session_lines = [f"found: {session.get('found')}", f"db_path: {session.get('db_path')}"]
    if session.get("session"):
        for key, value in session["session"].items():
            session_lines.append(f"{key}: {value}")
    sections.append(_format_section("Session", session_lines))

    kanban = bundle["kanban"]
    kanban_lines = [f"found: {kanban.get('found')}", f"db_path: {kanban.get('db_path')}"]
    if kanban.get("board_metadata"):
        kanban_lines.append("board_metadata: " + json.dumps(kanban["board_metadata"], sort_keys=True))
    for task in kanban.get("tasks", []):
        kanban_lines.append("task: " + json.dumps(task, sort_keys=True))
    for run in kanban.get("runs", []):
        kanban_lines.append("run: " + json.dumps(run, sort_keys=True))
    for event in kanban.get("events", []):
        kanban_lines.append("event: " + json.dumps(event, sort_keys=True))
    sections.append(_format_section("Kanban", kanban_lines))

    logs = bundle["logs"]
    response_lines = [json.dumps(item, sort_keys=True) for item in logs.get("response_ready", [])]
    sections.append(_format_section("Response Ready", response_lines))
    crash_lines = [json.dumps(item, sort_keys=True) for item in logs.get("crash_or_block", [])]
    sections.append(_format_section("Crashes And Blocks", crash_lines))
    snippet_lines = [f"{item['log']} [{item['reason']}]: {item['line']}" for item in logs.get("snippets", [])]
    sections.append(_format_section("Log Snippets", snippet_lines))
    return "\n\n".join(sections) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect a read-only Hermes runtime evidence bundle.")
    parser.add_argument("--thread-id", help="Discord thread id to filter logs/kanban subscriptions.")
    parser.add_argument("--session-id", help="Hermes session id to inspect.")
    parser.add_argument("--board", help="Kanban board slug; defaults to default.")
    parser.add_argument("--home", type=Path, help="Hermes home override for tests/offline inspection.")
    parser.add_argument(
        "--max-log-lines",
        type=int,
        default=_MAX_SNIPPETS,
        help="Maximum entries per log section.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    req = RuntimeEvidenceRequest(
        thread_id=args.thread_id,
        session_id=args.session_id,
        board=args.board,
        hermes_home=args.home,
        max_log_lines=max(1, args.max_log_lines),
    )
    try:
        bundle = collect_runtime_evidence(req)
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    if args.json:
        print(json.dumps(bundle, indent=2, sort_keys=True))
    else:
        print(format_text(bundle), end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
