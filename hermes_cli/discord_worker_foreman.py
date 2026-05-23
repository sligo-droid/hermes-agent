"""Read-only foreman scanner for Discord worker Kanban boards."""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from hermes_cli import kanban_db
from hermes_cli.discord_worker_boards import (
    board_thread_state,
    build_board_run_summary,
    public_session_board_url,
    read_board_run_summary,
)
from hermes_cli.discord_worker_roles import DISCORD_WORKER_META_KEY
from hermes_cli.discord_worker_state import read_codex_worker_state
from utils import atomic_json_write


STALE_RUNNING_SECONDS = kanban_db._STALE_HEARTBEAT_GAP_SECONDS
ERROR_OUTCOMES = frozenset({"spawn_failed", "crashed", "timed_out", "gave_up"})
ALERT_DETECTOR_VERSION = 1
ALERT_STATE_VERSION = 1
DISCORD_ALERT_LIMIT = 2000
FOREMAN_DISCORD_CHANNEL_ID = "1504252294495998043"
FOREMAN_DISCORD_MENTION = "<@1504235933598486580>"
_ALERT_DEFAULTS = {
    "cooldown_seconds": 3600,
    "retry_backoff_seconds": 300,
    "max_retry_backoff_seconds": 3600,
    "max_alerts_per_tick": 10,
    "daily_cap_per_board": 20,
    "retention_seconds": 30 * 24 * 3600,
    "terminal_suppression_age_seconds": 7 * 24 * 3600,
}
_MISSING_READ_BROKER_MARKERS = (
    "HERMES_DISCORD_WORKER_READ_URL",
    "HERMES_DISCORD_WORKER_READ_TOKEN",
    "read broker not configured",
    "discord worker read broker not configured",
)
_PATH_RE = re.compile(
    r"(?<![\w:/.-])(?:~[\w.-]*(?:/[^\s\"'<>),;{}\[\]]*)?"
    r"|/(?:home|Users|tmp|var|etc|opt|private|workspace|workspaces|mnt|srv|repo|root)"
    r"(?:/[^\s\"'<>),;{}\[\]]*)?|[A-Za-z]:\\[^\s\"'<>),;{}\[\]]+)"
)
_BEARER_TOKEN_RE = re.compile(
    r"(?i)\bauthorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]+"
)
_STANDALONE_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"sk-(?:(?:proj|live|test)-)?[A-Za-z0-9_-]{8,}"
    r"|gh[pousr]_[A-Za-z0-9_]{8,}"
    r"|xox[baprs]-[A-Za-z0-9-]{8,}"
    r"|[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{20,}"
    r")(?![A-Za-z0-9_])"
)
_TOKEN_RE = re.compile(
    r"(?i)(?:token|secret|api[_-]?key|authorization|password)\s*[:=]\s*[^\s,;]+"
)
_DISALLOWED_EVIDENCE_KEYS = frozenset(
    {
        "db_path",
        "env",
        "events",
        "final_text",
        "full_log",
        "log",
        "plan_text",
        "prompt",
        "prompts",
        "raw_events",
        "run_profile",
        "transcript",
        "workspace_path",
    }
)
_ALLOWED_RENDER_EVIDENCE_KEYS = frozenset(
    {
        "error_excerpt",
        "heartbeat_age_seconds",
        "last_heartbeat_at",
        "run_error",
        "run_id",
        "run_outcome",
        "run_status",
        "session_url",
        "sidecar_error",
        "sidecar_exit_code",
        "sidecar_timed_out",
        "stale_after_seconds",
        "task_assignee",
        "task_status",
        "thread_id",
        "thread_state",
    }
)
_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}
_TERMINAL_THREAD_STATES = frozenset({"blocked", "errored", "done", "archived", "cancelled"})


@dataclass(frozen=True)
class RunSnapshot:
    id: int
    status: str
    outcome: str
    started_at: Optional[int]
    ended_at: Optional[int]
    last_heartbeat_at: Optional[int]
    error: str = ""

    @classmethod
    def from_run(cls, run: Any) -> "RunSnapshot":
        return cls(
            id=int(getattr(run, "id", 0) or 0),
            status=str(getattr(run, "status", "") or ""),
            outcome=str(getattr(run, "outcome", "") or ""),
            started_at=getattr(run, "started_at", None),
            ended_at=getattr(run, "ended_at", None),
            last_heartbeat_at=getattr(run, "last_heartbeat_at", None),
            error=str(getattr(run, "error", "") or ""),
        )


@dataclass(frozen=True)
class TaskSnapshot:
    id: str
    title: str
    assignee: str
    status: str
    created_at: Optional[int]
    started_at: Optional[int]
    last_heartbeat_at: Optional[int]
    last_failure_error: str = ""
    latest_run: Optional[RunSnapshot] = None
    sidecar: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_task(cls, task: Any, *, latest_run: Optional[Any], sidecar: dict[str, Any]) -> "TaskSnapshot":
        return cls(
            id=str(getattr(task, "id", "") or ""),
            title=str(getattr(task, "title", "") or ""),
            assignee=str(getattr(task, "assignee", "") or ""),
            status=str(getattr(task, "status", "") or ""),
            created_at=getattr(task, "created_at", None),
            started_at=getattr(task, "started_at", None),
            last_heartbeat_at=getattr(task, "last_heartbeat_at", None),
            last_failure_error=str(getattr(task, "last_failure_error", "") or ""),
            latest_run=RunSnapshot.from_run(latest_run) if latest_run else None,
            sidecar=dict(sidecar) if isinstance(sidecar, dict) else {},
        )


@dataclass(frozen=True)
class BoardSnapshot:
    board: str
    thread_id: str
    chat_id: str
    session_url: str
    thread_state: str
    run_summary: dict[str, Any]
    tasks: tuple[TaskSnapshot, ...]
    archived: bool = False


@dataclass(frozen=True)
class ForemanIssue:
    kind: str
    board: str
    task_id: str
    severity: str
    title: str
    evidence: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "board": self.board,
            "task_id": self.task_id,
            "severity": self.severity,
            "title": _sanitize_text(self.title),
            "evidence": _sanitize_evidence(self.evidence),
        }


def collect_foreman_issues(now: Optional[int] = None) -> list[ForemanIssue]:
    """Return deterministic read-only foreman issues for Discord worker boards."""
    now = int(time.time()) if now is None else int(now)
    issues: list[ForemanIssue] = []
    for snapshot in collect_board_snapshots():
        issues.extend(detect_worker_errored(snapshot))
        issues.extend(detect_stale_running(snapshot, now=now))
        issues.extend(detect_missing_read_broker(snapshot))
    return sorted(issues, key=lambda issue: (issue.board, issue.task_id, issue.kind))


def alerts_due(
    issues: Iterable[ForemanIssue],
    *,
    now: Optional[int] = None,
    config: Optional[dict[str, Any]] = None,
) -> list[ForemanIssue]:
    """Return issues that should be attempted without marking them sent."""
    now = int(time.time()) if now is None else int(now)
    cfg = _alert_config(config)
    state = _read_alert_state()
    alerts = state.setdefault("alerts", {})
    changed = _gc_alert_state(state, now=now, retention_seconds=int(cfg["retention_seconds"]))
    due: list[ForemanIssue] = []
    for issue in issues:
        fingerprint = _issue_fingerprint(issue)
        entry = alerts.get(fingerprint)
        if not isinstance(entry, dict):
            entry = {
                "first_seen_at": now,
                "last_sent_at": None,
                "last_attempt_at": None,
                "last_state_key": "",
                "send_count": 0,
                "failure_count": 0,
                "next_retry_at": None,
                "last_error": "",
            }
            alerts[fingerprint] = entry
            changed = True
        elif not entry.get("first_seen_at"):
            entry["first_seen_at"] = now
            changed = True
        if len(due) >= int(cfg["max_alerts_per_tick"]):
            continue
        if _board_daily_count(state, issue.board, now) >= int(cfg["daily_cap_per_board"]):
            continue
        if not _terminal_issue_recent(
            issue,
            now=now,
            max_age_seconds=int(cfg["terminal_suppression_age_seconds"]),
        ):
            continue
        if _is_alert_due(issue, entry, now=now, config=cfg):
            due.append(issue)
    if changed:
        _write_alert_state(state)
    return due


def record_alert_sent(issue: ForemanIssue, *, now: Optional[int] = None) -> None:
    """Record a successful send for an issue."""
    now = int(time.time()) if now is None else int(now)
    state = _read_alert_state()
    entry = _alert_entry(state, issue, now=now)
    entry["last_sent_at"] = now
    entry["last_attempt_at"] = now
    entry["last_state_key"] = _issue_state_key(issue)
    entry["last_sent_severity"] = issue.severity
    entry["send_count"] = int(entry.get("send_count") or 0) + 1
    entry["failure_count"] = 0
    entry["next_retry_at"] = None
    entry["last_error"] = ""
    daily_counts = state.setdefault("daily_counts", {})
    key = _daily_key(issue.board, now)
    daily_counts[key] = int(daily_counts.get(key) or 0) + 1
    _write_alert_state(state)


def record_alert_failed(issue: ForemanIssue, error: str, *, now: Optional[int] = None) -> None:
    """Record a failed send attempt without marking the issue as sent."""
    now = int(time.time()) if now is None else int(now)
    state = _read_alert_state()
    entry = _alert_entry(state, issue, now=now)
    failure_count = int(entry.get("failure_count") or 0) + 1
    backoff = min(
        int(_ALERT_DEFAULTS["max_retry_backoff_seconds"]),
        int(_ALERT_DEFAULTS["retry_backoff_seconds"]) * (2 ** max(0, failure_count - 1)),
    )
    entry["last_attempt_at"] = now
    entry["failure_count"] = failure_count
    entry["next_retry_at"] = now + backoff
    entry["last_error"] = _truncate_text(_sanitize_text(error), 200)
    _write_alert_state(state)


def render_foreman_alert(issue: ForemanIssue, mention: str = "") -> str:
    """Render a bounded Discord-safe foreman alert."""
    # The foreman alert target is fixed; config/callers must not override it.
    safe_mention = FOREMAN_DISCORD_MENTION
    evidence = _renderable_evidence(issue.evidence)
    lines = []
    lines.append(safe_mention)
    lines.extend(
        [
            f"Foreman alert: {issue.severity.upper()} {issue.kind}",
            f"Board: {issue.board}",
            f"Task: {issue.task_id}",
        ]
    )
    run_id = evidence.get("run_id")
    if run_id not in (None, ""):
        lines.append(f"Run: {run_id}")
    session_url = evidence.get("session_url")
    if isinstance(session_url, str) and _is_public_url(session_url):
        lines.append(f"URL: {session_url}")
    lines.extend(
        [
            f"Reason: {_truncate_text(_sanitize_text(issue.title), 240)}",
            f"Next action: {_suggest_next_action(issue)}",
        ]
    )
    if evidence:
        lines.append("Evidence:")
        for key in sorted(evidence):
            value = evidence[key]
            if key == "session_url" and (not isinstance(value, str) or not _is_public_url(value)):
                continue
            lines.append(f"- {key}: {_truncate_text(_sanitize_text(str(value)), 180)}")
    return _truncate_text("\n".join(lines), DISCORD_ALERT_LIMIT)


def collect_board_snapshots() -> list[BoardSnapshot]:
    snapshots: list[BoardSnapshot] = []
    seen_db_paths: set[Path] = set()
    for meta in kanban_db.list_boards(include_archived=True):
        board = str(meta.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = meta.get(DISCORD_WORKER_META_KEY)
        if not isinstance(worker, dict) or worker.get("kind") != "discord_worker_board":
            continue
        db_path = _resolved_db_path(board)
        if db_path in seen_db_paths:
            continue
        seen_db_paths.add(db_path)
        snapshots.append(_build_board_snapshot(board, worker, archived=bool(meta.get("archived"))))
    return snapshots


def _build_board_snapshot(board: str, worker: dict[str, Any], *, archived: bool = False) -> BoardSnapshot:
    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        task_snapshots = tuple(
            TaskSnapshot.from_task(
                task,
                latest_run=kanban_db.latest_run(conn, task.id),
                sidecar=read_codex_worker_state(task.id, board=board),
            )
            for task in tasks
        )
    finally:
        conn.close()

    summary = read_board_run_summary(board)
    if not summary:
        try:
            summary = build_board_run_summary(board)
        except Exception:
            summary = {}
    return BoardSnapshot(
        board=board,
        thread_id=str(worker.get("thread_id") or ""),
        chat_id=str(worker.get("chat_id") or worker.get("thread_id") or ""),
        session_url=public_session_board_url(str(worker.get("thread_id") or "")),
        thread_state=board_thread_state(board),
        run_summary=dict(summary) if isinstance(summary, dict) else {},
        tasks=task_snapshots,
        archived=archived,
    )


def detect_worker_errored(snapshot: BoardSnapshot) -> list[ForemanIssue]:
    issues: list[ForemanIssue] = []
    for task in snapshot.tasks:
        latest = task.latest_run
        sidecar = _sidecar_result(task)
        evidence: dict[str, Any] = {}
        if latest and latest.outcome in ERROR_OUTCOMES:
            evidence = {
                "run_id": latest.id,
                "run_status": latest.status,
                "run_outcome": latest.outcome,
                "run_error": latest.error,
                "run_ended_at": latest.ended_at,
            }
        elif _sidecar_failed(sidecar):
            evidence = {
                "sidecar_error": sidecar.get("error"),
                "sidecar_timed_out": sidecar.get("timed_out"),
                "sidecar_exit_code": sidecar.get("exit_code"),
            }
        if evidence:
            issues.append(
                _issue(
                    "worker_errored",
                    snapshot,
                    task,
                    "error",
                    "Worker execution failed",
                    evidence,
                )
            )
    return issues


def detect_stale_running(snapshot: BoardSnapshot, *, now: int) -> list[ForemanIssue]:
    issues: list[ForemanIssue] = []
    for task in snapshot.tasks:
        if task.status != "running":
            continue
        heartbeat = task.last_heartbeat_at
        heartbeat_age = None if heartbeat is None else max(0, now - int(heartbeat))
        if heartbeat_age is not None and heartbeat_age <= STALE_RUNNING_SECONDS:
            continue
        issues.append(
            _issue(
                "stale_running",
                snapshot,
                task,
                "warning",
                "Running worker has no recent heartbeat",
                {
                    "last_heartbeat_at": heartbeat,
                    "heartbeat_age_seconds": heartbeat_age,
                    "stale_after_seconds": STALE_RUNNING_SECONDS,
                },
            )
        )
    return issues


def detect_missing_read_broker(snapshot: BoardSnapshot) -> list[ForemanIssue]:
    issues: list[ForemanIssue] = []
    for task in snapshot.tasks:
        texts = _task_error_texts(task)
        matched = next((text for text in texts if _mentions_missing_read_broker(text)), "")
        if not matched:
            continue
        issues.append(
            _issue(
                "missing_read_broker",
                snapshot,
                task,
                "error",
                "Discord worker read broker is not configured",
                {"error_excerpt": matched},
            )
        )
    return issues


def _issue(
    kind: str,
    snapshot: BoardSnapshot,
    task: TaskSnapshot,
    severity: str,
    title: str,
    evidence: dict[str, Any],
) -> ForemanIssue:
    base = {
        "thread_state": snapshot.thread_state,
        "task_status": task.status,
        "task_assignee": task.assignee,
        "board_archived": snapshot.archived,
    }
    base.update(evidence)
    if snapshot.thread_id:
        base["thread_id"] = snapshot.thread_id
    if snapshot.session_url:
        base["session_url"] = snapshot.session_url
    return ForemanIssue(
        kind=kind,
        board=snapshot.board,
        task_id=task.id,
        severity=severity,
        title=title,
        evidence=_sanitize_evidence(base),
    )


def _resolved_db_path(board: str) -> Path:
    try:
        return kanban_db.kanban_db_path(board=board).expanduser().resolve()
    except OSError:
        return kanban_db.kanban_db_path(board=board).expanduser().absolute()


def _sidecar_result(task: TaskSnapshot) -> dict[str, Any]:
    result = task.sidecar.get("result") if isinstance(task.sidecar, dict) else None
    return dict(result) if isinstance(result, dict) else {}


def _sidecar_failed(result: dict[str, Any]) -> bool:
    if not result:
        return False
    if str(result.get("error") or "").strip():
        return True
    if bool(result.get("timed_out")):
        return True
    exit_code = result.get("exit_code")
    if exit_code is None:
        return False
    try:
        return int(exit_code) != 0
    except (TypeError, ValueError):
        return True


def _task_error_texts(task: TaskSnapshot) -> Iterable[str]:
    if task.last_failure_error:
        yield task.last_failure_error
    if task.latest_run and task.latest_run.error:
        yield task.latest_run.error
    result = _sidecar_result(task)
    error = result.get("error")
    if error:
        yield str(error)


def _mentions_missing_read_broker(text: str) -> bool:
    folded = str(text or "").casefold()
    return any(marker.casefold() in folded for marker in _MISSING_READ_BROKER_MARKERS)


def _sanitize_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in evidence.items():
        key = str(key)
        if key in _DISALLOWED_EVIDENCE_KEYS:
            continue
        sanitized = _sanitize_value(value)
        if sanitized is not None:
            clean[key] = sanitized
    return clean


def _sanitize_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, (list, tuple)):
        clean = []
        for item in value[:10]:
            sanitized = _sanitize_value(item)
            if sanitized is not None:
                clean.append(sanitized)
        return clean
    if isinstance(value, dict):
        return _sanitize_evidence(value)
    return _sanitize_text(str(value))


def sanitize_foreman_text(value: Any, *, limit: int = 500) -> str:
    text = str(value or "")[:limit]
    text = _BEARER_TOKEN_RE.sub("[redacted]", text)
    text = _TOKEN_RE.sub("[redacted]", text)
    text = _STANDALONE_TOKEN_RE.sub("[redacted]", text)
    text = _PATH_RE.sub("[path]", text)
    return text


def _sanitize_text(value: str) -> str:
    return sanitize_foreman_text(value)


def _alert_config(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    cfg = dict(_ALERT_DEFAULTS)
    if config:
        for key in cfg:
            if key in config and config[key] is not None:
                cfg[key] = int(config[key])
    return cfg


def _alert_state_path() -> Path:
    return kanban_db.kanban_home() / "kanban" / "foreman-alerts.json"


def _read_alert_state() -> dict[str, Any]:
    path = _alert_state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict) or raw.get("version") != ALERT_STATE_VERSION:
        return {"version": ALERT_STATE_VERSION, "alerts": {}, "daily_counts": {}}
    alerts = raw.get("alerts")
    if not isinstance(alerts, dict):
        raw["alerts"] = {}
    daily_counts = raw.get("daily_counts")
    if not isinstance(daily_counts, dict):
        raw["daily_counts"] = {}
    return raw


def _write_alert_state(state: dict[str, Any]) -> None:
    state["version"] = ALERT_STATE_VERSION
    state.setdefault("alerts", {})
    state.setdefault("daily_counts", {})
    atomic_json_write(_alert_state_path(), state, indent=2, sort_keys=True)


def _alert_entry(state: dict[str, Any], issue: ForemanIssue, *, now: int) -> dict[str, Any]:
    alerts = state.setdefault("alerts", {})
    fingerprint = _issue_fingerprint(issue)
    entry = alerts.get(fingerprint)
    if not isinstance(entry, dict):
        entry = {
            "first_seen_at": now,
            "last_sent_at": None,
            "last_attempt_at": None,
            "last_state_key": "",
            "send_count": 0,
            "failure_count": 0,
            "next_retry_at": None,
            "last_error": "",
        }
        alerts[fingerprint] = entry
    return entry


def _issue_fingerprint(issue: ForemanIssue) -> str:
    payload = {
        "detector_version": ALERT_DETECTOR_VERSION,
        "kind": issue.kind,
        "board": issue.board,
        "task_id": issue.task_id,
    }
    return _stable_digest(payload)


def _issue_state_key(issue: ForemanIssue) -> str:
    payload = {
        "detector_version": ALERT_DETECTOR_VERSION,
        "kind": issue.kind,
        "board": issue.board,
        "task_id": issue.task_id,
        "severity": issue.severity,
        "title": issue.title,
        "evidence": _sanitize_evidence(issue.evidence),
    }
    return _stable_digest(payload)


def _stable_digest(payload: dict[str, Any]) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _is_alert_due(issue: ForemanIssue, entry: dict[str, Any], *, now: int, config: dict[str, Any]) -> bool:
    next_retry = entry.get("next_retry_at")
    if next_retry is not None and int(next_retry) > now:
        return False
    if int(entry.get("failure_count") or 0) > 0 and next_retry is not None and int(next_retry) <= now:
        return True
    last_sent = entry.get("last_sent_at")
    if not last_sent:
        return True
    if _is_terminal_or_archived_issue(issue):
        return False
    state_key = _issue_state_key(issue)
    if state_key != str(entry.get("last_state_key") or ""):
        return True
    if _severity_rank(issue.severity) > _severity_rank(str(entry.get("last_sent_severity") or "")):
        return True
    return now - int(last_sent) >= int(config["cooldown_seconds"])


def _is_terminal_or_archived_issue(issue: ForemanIssue) -> bool:
    evidence = issue.evidence if isinstance(issue.evidence, dict) else {}
    if bool(evidence.get("board_archived")):
        return True
    task_status = str(evidence.get("task_status") or "").casefold()
    if task_status and task_status != "running":
        return True
    thread_state = str(evidence.get("thread_state") or "").casefold()
    return thread_state in _TERMINAL_THREAD_STATES


def _terminal_issue_recent(issue: ForemanIssue, *, now: int, max_age_seconds: int) -> bool:
    if max_age_seconds <= 0 or not _is_terminal_or_archived_issue(issue):
        return True
    evidence = issue.evidence if isinstance(issue.evidence, dict) else {}
    ended_at = evidence.get("run_ended_at")
    if ended_at in (None, ""):
        return True
    try:
        return now - int(ended_at) <= max_age_seconds
    except (TypeError, ValueError):
        return True


def _severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get(str(severity or "").casefold(), 0)


def _daily_key(board: str, now: int) -> str:
    day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    return f"{board}:{day}"


def _board_daily_count(state: dict[str, Any], board: str, now: int) -> int:
    return int(state.setdefault("daily_counts", {}).get(_daily_key(board, now)) or 0)


def _gc_alert_state(state: dict[str, Any], *, now: int, retention_seconds: int) -> bool:
    changed = False
    cutoff = now - retention_seconds
    alerts = state.setdefault("alerts", {})
    for fingerprint, entry in list(alerts.items()):
        if not isinstance(entry, dict):
            alerts.pop(fingerprint, None)
            changed = True
            continue
        seen = int(entry.get("first_seen_at") or 0)
        sent = int(entry.get("last_sent_at") or 0)
        attempted = int(entry.get("last_attempt_at") or 0)
        if max(seen, sent, attempted) < cutoff:
            alerts.pop(fingerprint, None)
            changed = True
    today_prefix = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    daily_counts = state.setdefault("daily_counts", {})
    for key in list(daily_counts):
        if not str(key).endswith(f":{today_prefix}"):
            daily_counts.pop(key, None)
            changed = True
    return changed


def _renderable_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    clean = _sanitize_evidence(evidence)
    return {key: value for key, value in clean.items() if key in _ALLOWED_RENDER_EVIDENCE_KEYS}


def _is_public_url(value: str) -> bool:
    return value.startswith("https://") and not any(host in value for host in ("localhost", "127.0.0.1", "[::1]"))


def _suggest_next_action(issue: ForemanIssue) -> str:
    if issue.kind == "missing_read_broker":
        return "Configure the Discord worker read broker credentials, then retry the task."
    if issue.kind == "stale_running":
        return "Inspect the worker heartbeat and reclaim or restart the task if it is stuck."
    if issue.kind == "worker_errored":
        return "Inspect the failed worker run and unblock or retry with the recorded error fixed."
    return "Inspect the Kanban task and resolve the reported worker issue."


def _truncate_text(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    marker = "... [truncated]"
    if limit <= len(marker):
        return marker[:limit]
    return value[: limit - len(marker)] + marker
