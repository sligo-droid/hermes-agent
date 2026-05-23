"""Read-only foreman scanner for Discord worker Kanban boards."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
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


STALE_RUNNING_SECONDS = kanban_db._STALE_HEARTBEAT_GAP_SECONDS
ERROR_OUTCOMES = frozenset({"spawn_failed", "crashed", "timed_out", "gave_up"})
_MISSING_READ_BROKER_MARKERS = (
    "HERMES_DISCORD_WORKER_READ_URL",
    "HERMES_DISCORD_WORKER_READ_TOKEN",
    "read broker not configured",
    "discord worker read broker not configured",
)
_PATH_RE = re.compile(
    r"(?<![\w:/.-])(?:/(?:home|Users|tmp|var|etc|opt|private|workspace|workspaces|mnt|srv|repo|root)"
    r"(?:/[^\s\"'<>),;{}\[\]]*)?|[A-Za-z]:\\[^\s\"'<>),;{}\[\]]+)"
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
            "title": self.title,
            "evidence": dict(self.evidence),
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


def collect_board_snapshots() -> list[BoardSnapshot]:
    snapshots: list[BoardSnapshot] = []
    seen_db_paths: set[Path] = set()
    for meta in kanban_db.list_boards(include_archived=False):
        board = str(meta.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = meta.get(DISCORD_WORKER_META_KEY)
        if not isinstance(worker, dict) or worker.get("kind") != "discord_worker_board":
            continue
        db_path = _resolved_db_path(board)
        if db_path in seen_db_paths:
            continue
        seen_db_paths.add(db_path)
        snapshots.append(_build_board_snapshot(board, worker))
    return snapshots


def _build_board_snapshot(board: str, worker: dict[str, Any]) -> BoardSnapshot:
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


def _sanitize_text(value: str) -> str:
    text = str(value or "")[:500]
    text = _TOKEN_RE.sub("[redacted]", text)
    text = _PATH_RE.sub("[path]", text)
    return text
