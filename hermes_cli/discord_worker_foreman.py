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
BLOCKED_BOARD_MIN_AGE_SECONDS = 10 * 60
MANUAL_ESCALATION_TASK = "foreman_manual_escalation"
_ACTIVE_BOARD_TASK_STATUSES = frozenset(
    {"triage", "todo", "scheduled", "ready", "running", "blocked", "review"}
)
_ALERT_DEFAULTS = {
    "cooldown_seconds": 3600,
    "retry_backoff_seconds": 300,
    "max_retry_backoff_seconds": 3600,
    "max_alerts_per_tick": 10,
    "daily_cap_per_board": 20,
    "min_board_created_at": 0,
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
        "llm_assessed_at",
        "llm_confidence",
        "active_task_count",
        "blocked_count",
        "blocked_reason",
        "board_goal_status",
        "board_phase",
        "foreman_board",
        "foreman_task_id",
        "manual_intervention_reason",
        "manual_intervention_steps",
        "manual_intervention_type",
        "ready_count",
        "run_error",
        "run_id",
        "run_outcome",
        "run_status",
        "review_count",
        "session_url",
        "sidecar_error",
        "sidecar_exit_code",
        "sidecar_timed_out",
        "scheduled_count",
        "stalled_after_seconds",
        "stalled_age_seconds",
        "stalled_since",
        "stale_after_seconds",
        "task_assignee",
        "task_status",
        "thread_id",
        "thread_state",
        "todo_count",
        "running_count",
        "source_board",
        "source_issue_kind",
        "source_task_id",
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
    completed_at: Optional[int] = None
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
            completed_at=getattr(task, "completed_at", None),
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
    created_at: Optional[int] = None
    updated_at: Optional[int] = None
    goal_status: str = ""
    phase: str = ""
    blocked_reason: str = ""
    request_text: str = ""
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


def collect_foreman_issues(
    now: Optional[int] = None,
    *,
    blocked_board_min_age_seconds: int = BLOCKED_BOARD_MIN_AGE_SECONDS,
) -> list[ForemanIssue]:
    """Return deterministic read-only foreman issues for Discord worker boards."""
    now = int(time.time()) if now is None else int(now)
    min_age = _coerce_nonnegative_int(
        blocked_board_min_age_seconds,
        BLOCKED_BOARD_MIN_AGE_SECONDS,
    )
    issues: list[ForemanIssue] = []
    for snapshot in collect_board_snapshots():
        issues.extend(detect_worker_errored(snapshot))
        issues.extend(detect_stale_running(snapshot, now=now))
        issues.extend(detect_missing_read_broker(snapshot))
        issues.extend(detect_stalled_blocked_board(snapshot, now=now, min_age_seconds=min_age))
    return sorted(issues, key=lambda issue: (issue.board, issue.task_id, issue.kind))


def collect_human_intervention_issues(
    now: Optional[int] = None,
    *,
    blocked_board_min_age_seconds: int = BLOCKED_BOARD_MIN_AGE_SECONDS,
    assessment_fn: Optional[Any] = None,
) -> list[ForemanIssue]:
    """Return human-escalation issues for blocked foreman-generated boards.

    The manual-configuration decision is intentionally made by an auxiliary
    LLM once per unique foreman-board blockage, then persisted in foreman
    state so the watcher does not keep spending inference on the same block.
    """
    now = int(time.time()) if now is None else int(now)
    min_age = _coerce_nonnegative_int(
        blocked_board_min_age_seconds,
        BLOCKED_BOARD_MIN_AGE_SECONDS,
    )
    state = _read_alert_state()
    assessments = state.setdefault("manual_assessments", {})
    changed = _gc_alert_state(
        state,
        now=now,
        retention_seconds=int(_ALERT_DEFAULTS["retention_seconds"]),
    )
    issues: list[ForemanIssue] = []
    seen_keys: set[str] = set()
    for snapshot in collect_board_snapshots(foreman_generated_only=True):
        candidates: list[ForemanIssue] = []
        candidates.extend(detect_worker_errored(snapshot))
        candidates.extend(detect_missing_read_broker(snapshot))
        candidates.extend(detect_stalled_blocked_board(snapshot, now=now, min_age_seconds=min_age))
        candidates = [_with_foreman_source(issue, snapshot) for issue in candidates]
        for issue in candidates:
            key = _manual_assessment_key(issue)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            entry = assessments.get(key)
            if not isinstance(entry, dict):
                entry = _assess_manual_intervention(issue, now=now, assessment_fn=assessment_fn)
                assessments[key] = entry
                changed = True
            if bool(entry.get("requires_manual_intervention")):
                issues.append(_manual_intervention_issue(issue, entry))
    if changed:
        _write_alert_state(state)
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
    groups = state.setdefault("issue_groups", {})
    changed = _gc_alert_state(state, now=now, retention_seconds=int(cfg["retention_seconds"]))
    due: list[ForemanIssue] = []
    due_groups: set[str] = set()
    for issue in issues:
        if not _board_created_at_allowed(issue, min_created_at=int(cfg["min_board_created_at"])):
            continue
        fingerprint = _issue_fingerprint(issue)
        entry = alerts.get(fingerprint)
        if not isinstance(entry, dict):
            entry = _new_alert_entry(now)
            alerts[fingerprint] = entry
            changed = True
        elif not entry.get("first_seen_at"):
            entry["first_seen_at"] = now
            changed = True
        group_key = _issue_group_key(issue)
        group_entry = groups.get(group_key)
        if not isinstance(group_entry, dict):
            group_entry = _new_alert_entry(now)
            groups[group_key] = group_entry
            changed = True
        elif not group_entry.get("first_seen_at"):
            group_entry["first_seen_at"] = now
            changed = True
        if group_key in due_groups:
            continue
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
        if _is_alert_due(issue, entry, now=now, config=cfg) and _is_group_alert_due(
            issue,
            group_entry,
            now=now,
        ):
            due.append(issue)
            due_groups.add(group_key)
    if changed:
        _write_alert_state(state)
    return due


def startup_baseline_needed() -> bool:
    """Return True when foreman should treat the first scan as existing state."""
    state = _read_alert_state()
    return not bool(state.get("startup_baselined_at")) and not bool(state.get("alerts"))


def record_startup_baseline(
    issues: Iterable[ForemanIssue],
    *,
    now: Optional[int] = None,
    config: Optional[dict[str, Any]] = None,
) -> int:
    """Mark currently visible startup issues as historical without sending them."""
    now = int(time.time()) if now is None else int(now)
    cfg = _alert_config(config)
    state = _read_alert_state()
    state["startup_baselined_at"] = now
    alerts = state.setdefault("alerts", {})
    changed = _gc_alert_state(state, now=now, retention_seconds=int(cfg["retention_seconds"]))
    count = 0
    for issue in issues:
        if not _board_created_at_allowed(issue, min_created_at=int(cfg["min_board_created_at"])):
            continue
        fingerprint = _issue_fingerprint(issue)
        if isinstance(alerts.get(fingerprint), dict):
            continue
        entry = _new_alert_entry(now)
        entry["startup_suppressed_at"] = now
        entry["startup_suppressed_state_key"] = _issue_state_key(issue)
        alerts[fingerprint] = entry
        count += 1
        changed = True
    if changed or count or state.get("startup_baselined_at") == now:
        _write_alert_state(state)
    return count


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
    group_entry = _group_entry(state, issue, now=now)
    group_entry["last_sent_at"] = now
    group_entry["last_attempt_at"] = now
    group_entry["last_state_key"] = _issue_group_state_key(issue)
    group_entry["last_sent_severity"] = issue.severity
    group_entry["send_count"] = int(group_entry.get("send_count") or 0) + 1
    group_entry["failure_count"] = 0
    group_entry["next_retry_at"] = None
    group_entry["last_error"] = ""
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
    group_entry = _group_entry(state, issue, now=now)
    group_entry["last_attempt_at"] = now
    group_entry["failure_count"] = failure_count
    group_entry["next_retry_at"] = now + backoff
    group_entry["last_error"] = entry["last_error"]
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
    steps = _manual_instruction_steps(evidence)
    if steps:
        lines.append("Human instructions:")
        for index, step in enumerate(steps[:8], start=1):
            lines.append(f"{index}. {_truncate_text(_sanitize_text(step), 220)}")
    if evidence:
        lines.append("Evidence:")
        for key in sorted(evidence):
            value = evidence[key]
            if key == "manual_intervention_steps":
                continue
            if key == "session_url" and (not isinstance(value, str) or not _is_public_url(value)):
                continue
            lines.append(f"- {key}: {_truncate_text(_sanitize_text(str(value)), 180)}")
    return _truncate_text("\n".join(lines), DISCORD_ALERT_LIMIT)


def render_foreman_goal_prompt(issue: ForemanIssue) -> str:
    """Render a sanitized /goal prompt for Kanban-owned foreman escalation."""
    evidence = _renderable_evidence(issue.evidence)
    lines = [
        "/goal Foreman escalation: resolve a Discord worker issue.",
        "",
        "Problem:",
        f"- Severity: {str(issue.severity or '').upper()}",
        f"- Detector: {issue.kind}",
        f"- Board: {issue.board}",
        f"- Task: {issue.task_id}",
        f"- Summary: {_truncate_text(_sanitize_text(issue.title), 240)}",
        "",
        "Goal:",
        f"- {_suggest_next_action(issue)}",
        "- Update, retry, unblock, or close the affected Kanban task as appropriate.",
        "- Verify the board can progress without this foreman issue recurring.",
        "- Make one autonomous attempt to resolve the blocker.",
        "- If progress requires human-only configuration, credentials, account/admin access, "
        "or external infrastructure access, block with a concise manual-intervention reason instead of cycling.",
    ]
    session_url = evidence.get("session_url")
    if isinstance(session_url, str) and _is_public_url(session_url):
        lines.extend(["", f"Board URL: {session_url}"])
    if evidence:
        lines.extend(["", "Evidence:"])
        for key in sorted(evidence):
            value = evidence[key]
            if key == "session_url" and (not isinstance(value, str) or not _is_public_url(value)):
                continue
            lines.append(f"- {key}: {_truncate_text(_sanitize_text(str(value)), 180)}")
    return _truncate_text("\n".join(lines), DISCORD_ALERT_LIMIT)


def foreman_goal_thread_title(issue: ForemanIssue) -> str:
    """Return a compact Discord thread title for a foreman-created goal."""
    title = f"Foreman: {issue.kind} {issue.task_id}"
    return _truncate_text(_sanitize_text(re.sub(r"\s+", " ", title).strip()), 80)


def collect_board_snapshots(*, foreman_generated_only: bool = False) -> list[BoardSnapshot]:
    snapshots: list[BoardSnapshot] = []
    seen_db_paths: set[Path] = set()
    for meta in kanban_db.list_boards(include_archived=True):
        board = str(meta.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = meta.get(DISCORD_WORKER_META_KEY)
        if not isinstance(worker, dict) or worker.get("kind") != "discord_worker_board":
            continue
        is_foreman_generated = _is_foreman_generated_board(worker)
        if foreman_generated_only != is_foreman_generated:
            continue
        db_path = _resolved_db_path(board)
        if db_path in seen_db_paths:
            continue
        seen_db_paths.add(db_path)
        snapshots.append(
            _build_board_snapshot(
                board,
                worker,
                archived=bool(meta.get("archived")),
                created_at=_coerce_optional_int(meta.get("created_at")),
            )
        )
    return snapshots


def _build_board_snapshot(
    board: str,
    worker: dict[str, Any],
    *,
    archived: bool = False,
    created_at: Optional[int] = None,
) -> BoardSnapshot:
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
        created_at=created_at,
        updated_at=_coerce_optional_int(worker.get("updated_at")),
        goal_status=str(worker.get("goal_status") or ""),
        phase=str(worker.get("phase") or ""),
        blocked_reason=str(worker.get("blocked_reason") or ""),
        request_text=str(
            worker.get("root_goal")
            or worker.get("initial_request")
            or summary.get("root_goal")
            or ""
        ),
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


def detect_stalled_blocked_board(
    snapshot: BoardSnapshot,
    *,
    now: int,
    min_age_seconds: int = BLOCKED_BOARD_MIN_AGE_SECONDS,
) -> list[ForemanIssue]:
    """Detect board-level blockers that leave a worker board unable to progress."""
    if snapshot.archived or _board_is_terminal(snapshot):
        return []

    counts = _board_task_counts(snapshot)
    running_count = int(counts.get("running") or 0)
    if running_count > 0 or any(task.status == "running" for task in snapshot.tasks):
        return []

    active_count = sum(int(counts.get(status) or 0) for status in _ACTIVE_BOARD_TASK_STATUSES)
    if active_count <= 0 and _canonical_problem_text(snapshot.goal_status) != "blocked":
        return []

    reason = _board_blocked_reason(snapshot)
    goal_status = _canonical_problem_text(snapshot.goal_status)
    phase = _canonical_problem_text(snapshot.phase)
    blocked_count = int(counts.get("blocked") or 0)
    if goal_status != "blocked" and phase != "blocked" and not reason and blocked_count <= 0:
        return []

    blocker = _board_blocker_task(snapshot)
    if blocker is not None and _task_has_worker_error_issue(blocker):
        return []

    stalled_since = _board_stalled_since(snapshot, blocker)
    stalled_age = None if stalled_since is None else max(0, now - int(stalled_since))
    if stalled_age is not None and stalled_age < max(0, int(min_age_seconds)):
        return []
    if stalled_since is None and min_age_seconds > 0:
        return []

    task = blocker or _synthetic_board_task(snapshot)
    evidence = {
        "board_goal_status": snapshot.goal_status,
        "board_phase": snapshot.phase,
        "blocked_reason": reason,
        "active_task_count": active_count,
        "ready_count": counts.get("ready", 0),
        "todo_count": counts.get("todo", 0),
        "scheduled_count": counts.get("scheduled", 0),
        "blocked_count": counts.get("blocked", 0),
        "review_count": counts.get("review", 0),
        "running_count": running_count,
        "stalled_since": stalled_since,
        "stalled_age_seconds": stalled_age,
        "stalled_after_seconds": max(0, int(min_age_seconds)),
    }
    evidence.update(_foreman_source_from_request(snapshot.request_text))
    if stalled_since is not None:
        # Reuse the existing terminal-age suppression path for old board-level blockers.
        evidence["run_ended_at"] = stalled_since
    return [
        _issue(
            "board_stalled",
            snapshot,
            task,
            "warning",
            "Discord worker board is blocked with no running workers",
            evidence,
        )
    ]


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
    if snapshot.created_at is not None:
        base["board_created_at"] = snapshot.created_at
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


def _task_has_worker_error_issue(task: TaskSnapshot) -> bool:
    latest = task.latest_run
    if latest and latest.outcome in ERROR_OUTCOMES:
        return True
    return _sidecar_failed(_sidecar_result(task))


def _board_is_terminal(snapshot: BoardSnapshot) -> bool:
    goal_status = _canonical_problem_text(snapshot.goal_status)
    phase = _canonical_problem_text(snapshot.phase)
    thread_state = _canonical_problem_text(snapshot.thread_state)
    return (
        goal_status in {"done", "cancelled"}
        or phase in {"complete", "cancelled"}
        or thread_state in {"done", "archived", "cancelled"}
    )


def _board_task_counts(snapshot: BoardSnapshot) -> dict[str, int]:
    counts = {status: 0 for status in _ACTIVE_BOARD_TASK_STATUSES}
    for task in snapshot.tasks:
        status = str(task.status or "")
        counts[status] = int(counts.get(status) or 0) + 1
    if snapshot.tasks:
        return counts
    summary_counts = (
        snapshot.run_summary.get("task_counts")
        if isinstance(snapshot.run_summary, dict)
        else None
    )
    if isinstance(summary_counts, dict):
        for key, value in summary_counts.items():
            try:
                counts[str(key)] = int(value or 0)
            except (TypeError, ValueError):
                counts[str(key)] = 0
    return counts


def _board_blocked_reason(snapshot: BoardSnapshot) -> str:
    reason = str(snapshot.blocked_reason or "").strip()
    if not reason and isinstance(snapshot.run_summary, dict):
        reason = str(snapshot.run_summary.get("blocked_reason") or "").strip()
    if reason:
        return reason
    task = _board_blocker_task(snapshot)
    if task is None:
        return ""
    if task.last_failure_error:
        return task.last_failure_error
    if task.latest_run:
        return str(task.latest_run.error or "") or str(task.latest_run.outcome or "")
    return task.title


def _board_blocker_task(snapshot: BoardSnapshot) -> Optional[TaskSnapshot]:
    blocked = [task for task in snapshot.tasks if task.status == "blocked"]
    if blocked:
        return max(blocked, key=_task_blocker_timestamp)
    blocked_runs = [
        task
        for task in snapshot.tasks
        if task.latest_run and task.latest_run.outcome == "blocked"
    ]
    if blocked_runs:
        return max(blocked_runs, key=_task_blocker_timestamp)
    active = [task for task in snapshot.tasks if task.status in _ACTIVE_BOARD_TASK_STATUSES]
    if active:
        return max(active, key=_task_blocker_timestamp)
    return None


def _task_blocker_timestamp(task: TaskSnapshot) -> int:
    latest = task.latest_run
    values = [
        latest.ended_at if latest else None,
        latest.started_at if latest else None,
        task.completed_at,
        task.started_at,
        task.created_at,
    ]
    return max((_coerce_optional_int(value) or 0 for value in values), default=0)


def _board_stalled_since(snapshot: BoardSnapshot, blocker: Optional[TaskSnapshot]) -> Optional[int]:
    if blocker and blocker.latest_run and blocker.latest_run.outcome == "blocked":
        ended = _coerce_optional_int(blocker.latest_run.ended_at)
        if ended is not None:
            return ended
    for value in (
        snapshot.updated_at,
        snapshot.run_summary.get("completed_at") if isinstance(snapshot.run_summary, dict) else None,
        snapshot.run_summary.get("generated_at") if isinstance(snapshot.run_summary, dict) else None,
        blocker.started_at if blocker else None,
        blocker.created_at if blocker else None,
        snapshot.created_at,
    ):
        parsed = _coerce_optional_int(value)
        if parsed is not None:
            return parsed
    return None


def _synthetic_board_task(snapshot: BoardSnapshot) -> TaskSnapshot:
    return TaskSnapshot(
        id="__board__",
        title=snapshot.blocked_reason or "Board-level blocker",
        assignee="",
        status="blocked",
        created_at=snapshot.created_at,
        started_at=snapshot.updated_at or snapshot.created_at,
        last_heartbeat_at=None,
    )


def _foreman_source_from_request(text: str) -> dict[str, str]:
    source: dict[str, str] = {}
    mapping = {
        "board": "source_board",
        "task": "source_task_id",
        "detector": "source_issue_kind",
    }
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip().lstrip("- ").strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        target = mapping.get(key.strip().casefold())
        if target and value.strip():
            source[target] = _truncate_text(_sanitize_text(value.strip()), 120)
    return source


def _with_foreman_source(issue: ForemanIssue, snapshot: BoardSnapshot) -> ForemanIssue:
    source = _foreman_source_from_request(snapshot.request_text)
    if not source:
        return issue
    evidence = dict(issue.evidence) if isinstance(issue.evidence, dict) else {}
    for key, value in source.items():
        evidence.setdefault(key, value)
    return ForemanIssue(
        kind=issue.kind,
        board=issue.board,
        task_id=issue.task_id,
        severity=issue.severity,
        title=issue.title,
        evidence=_sanitize_evidence(evidence),
    )


def _manual_assessment_key(issue: ForemanIssue) -> str:
    evidence = issue.evidence if isinstance(issue.evidence, dict) else {}
    problem = (
        evidence.get("run_error")
        or evidence.get("sidecar_error")
        or evidence.get("error_excerpt")
        or evidence.get("blocked_reason")
        or issue.title
    )
    payload = {
        "detector_version": ALERT_DETECTOR_VERSION,
        "kind": "manual_intervention_assessment",
        "foreman_issue_kind": issue.kind,
        "foreman_board": issue.board,
        "foreman_task_id": issue.task_id,
        "source_board": evidence.get("source_board") or "",
        "source_task_id": evidence.get("source_task_id") or "",
        "source_issue_kind": evidence.get("source_issue_kind") or "",
        "problem": _canonical_problem_text(problem),
    }
    return _stable_digest(payload)


def _assess_manual_intervention(
    issue: ForemanIssue,
    *,
    now: int,
    assessment_fn: Optional[Any] = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "assessed_at": now,
        "requires_manual_intervention": False,
        "reason": "",
        "intervention_type": "",
        "confidence": "",
        "error": "",
    }
    try:
        raw = assessment_fn(issue) if assessment_fn is not None else _llm_assess_manual_intervention(issue)
        parsed = _normalize_manual_assessment(raw)
    except Exception as exc:
        entry["error"] = _truncate_text(_sanitize_text(str(exc)), 200)
        return entry
    entry.update(parsed)
    return entry


def _llm_assess_manual_intervention(issue: ForemanIssue) -> dict[str, Any]:
    from agent.auxiliary_client import call_llm

    evidence = _renderable_evidence(issue.evidence)
    messages = [
        {
            "role": "system",
            "content": (
                "You classify blocked Hermes foreman attempts. The foreman has already made one "
                "automated attempt to resolve a worker-board blockage. Decide whether this new "
                "blockage now requires a human to perform manual configuration, provide access, "
                "or supply information outside an AI agent's authority. Return only compact JSON "
                "with keys: requires_manual_intervention (boolean), reason (string), "
                "intervention_type (string), confidence (low|medium|high), "
                "instructions (array of short step-by-step strings for the human). "
                "When credentials are needed, make the instructions as explicit as possible: "
                "which service/account area to open, what credential/access to create or request, "
                "where it should be installed in Hermes or the project, and what to ask the agent to retry. "
                "Do not invent secret values."
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "foreman_issue": issue.to_dict(),
                    "renderable_evidence": evidence,
                    "decision_standard": (
                        "true only when a human must provide manual configuration, credentials, "
                        "account/admin access, external infrastructure access, or other non-agent action. "
                        "false when another autonomous code/debugging attempt is likely appropriate. "
                        "If true, include explicit human instructions to the degree possible from the evidence."
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
        },
    ]
    response = call_llm(
        task=MANUAL_ESCALATION_TASK,
        messages=messages,
        max_tokens=800,
        temperature=0,
        timeout=30,
    )
    content = str(response.choices[0].message.content or "")
    return _parse_manual_assessment_json(content)


def _parse_manual_assessment_json(content: str) -> dict[str, Any]:
    text = str(content or "").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("manual intervention assessment did not return a JSON object")
    return data


def _normalize_manual_assessment(raw: Any) -> dict[str, Any]:
    if isinstance(raw, bool):
        return {"requires_manual_intervention": raw}
    if not isinstance(raw, dict):
        raw = _parse_manual_assessment_json(str(raw or ""))
    return {
        "requires_manual_intervention": bool(raw.get("requires_manual_intervention")),
        "reason": _truncate_text(_sanitize_text(str(raw.get("reason") or "")), 300),
        "intervention_type": _truncate_text(_sanitize_text(str(raw.get("intervention_type") or "")), 80),
        "instructions": _normalize_instruction_steps(raw),
        "confidence": _truncate_text(_sanitize_text(str(raw.get("confidence") or "")), 20),
        "error": "",
    }


def _normalize_instruction_steps(raw: dict[str, Any]) -> list[str]:
    value = (
        raw.get("instructions")
        or raw.get("manual_intervention_steps")
        or raw.get("steps")
        or []
    )
    if isinstance(value, str):
        candidates = [line.strip() for line in value.splitlines()]
    elif isinstance(value, (list, tuple)):
        candidates = [str(item or "").strip() for item in value]
    else:
        candidates = []
    steps: list[str] = []
    for item in candidates:
        step = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", item).strip()
        if not step:
            continue
        steps.append(_truncate_text(_sanitize_text(step), 220))
        if len(steps) >= 8:
            break
    return steps


def _manual_instruction_steps(evidence: dict[str, Any]) -> list[str]:
    return _normalize_instruction_steps(
        {"instructions": evidence.get("manual_intervention_steps")}
    )


def _manual_intervention_issue(issue: ForemanIssue, assessment: dict[str, Any]) -> ForemanIssue:
    evidence = dict(issue.evidence) if isinstance(issue.evidence, dict) else {}
    evidence.update(
        {
            "foreman_board": issue.board,
            "foreman_task_id": issue.task_id,
            "manual_intervention_reason": assessment.get("reason") or "Manual intervention required.",
            "manual_intervention_steps": assessment.get("instructions") or [],
            "manual_intervention_type": assessment.get("intervention_type") or "manual_intervention",
            "llm_confidence": assessment.get("confidence") or "",
            "llm_assessed_at": assessment.get("assessed_at"),
        }
    )
    source_board = str(evidence.get("source_board") or issue.board)
    source_task = str(evidence.get("source_task_id") or issue.task_id)
    return ForemanIssue(
        kind="human_intervention_required",
        board=source_board,
        task_id=source_task,
        severity="critical",
        title="Foreman attempt requires human manual intervention",
        evidence=_sanitize_evidence(evidence),
    )


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
        return {
            "version": ALERT_STATE_VERSION,
            "alerts": {},
            "issue_groups": {},
            "manual_assessments": {},
            "daily_counts": {},
        }
    alerts = raw.get("alerts")
    if not isinstance(alerts, dict):
        raw["alerts"] = {}
    groups = raw.get("issue_groups")
    if not isinstance(groups, dict):
        raw["issue_groups"] = {}
    assessments = raw.get("manual_assessments")
    if not isinstance(assessments, dict):
        raw["manual_assessments"] = {}
    daily_counts = raw.get("daily_counts")
    if not isinstance(daily_counts, dict):
        raw["daily_counts"] = {}
    return raw


def _write_alert_state(state: dict[str, Any]) -> None:
    state["version"] = ALERT_STATE_VERSION
    state.setdefault("alerts", {})
    state.setdefault("issue_groups", {})
    state.setdefault("manual_assessments", {})
    state.setdefault("daily_counts", {})
    atomic_json_write(_alert_state_path(), state, indent=2, sort_keys=True)


def _alert_entry(state: dict[str, Any], issue: ForemanIssue, *, now: int) -> dict[str, Any]:
    alerts = state.setdefault("alerts", {})
    fingerprint = _issue_fingerprint(issue)
    entry = alerts.get(fingerprint)
    if not isinstance(entry, dict):
        entry = _new_alert_entry(now)
        alerts[fingerprint] = entry
    return entry


def _group_entry(state: dict[str, Any], issue: ForemanIssue, *, now: int) -> dict[str, Any]:
    groups = state.setdefault("issue_groups", {})
    group_key = _issue_group_key(issue)
    entry = groups.get(group_key)
    if not isinstance(entry, dict):
        entry = _new_alert_entry(now)
        groups[group_key] = entry
    return entry


def _new_alert_entry(now: int) -> dict[str, Any]:
    return {
        "first_seen_at": now,
        "last_sent_at": None,
        "last_attempt_at": None,
        "last_state_key": "",
        "send_count": 0,
        "failure_count": 0,
        "next_retry_at": None,
        "last_error": "",
    }


def _is_foreman_generated_board(worker: dict[str, Any]) -> bool:
    request = str(worker.get("initial_request") or "").lstrip()
    return request.startswith("Foreman escalation:") or request.startswith("/goal Foreman escalation:")


def _coerce_optional_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_nonnegative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = int(default)
    return max(0, parsed)


def _board_created_at_allowed(issue: ForemanIssue, *, min_created_at: int) -> bool:
    if min_created_at <= 0:
        return True
    evidence = issue.evidence if isinstance(issue.evidence, dict) else {}
    board_created_at = _coerce_optional_int(evidence.get("board_created_at"))
    if board_created_at is None:
        return False
    return board_created_at >= min_created_at


def _issue_fingerprint(issue: ForemanIssue) -> str:
    payload = {
        "detector_version": ALERT_DETECTOR_VERSION,
        "kind": issue.kind,
        "board": issue.board,
        "task_id": issue.task_id,
    }
    return _stable_digest(payload)


def _issue_group_key(issue: ForemanIssue) -> str:
    return _stable_digest(_issue_group_payload(issue))


def _issue_group_state_key(issue: ForemanIssue) -> str:
    payload = {
        **_issue_group_payload(issue),
        "severity": issue.severity,
    }
    return _stable_digest(payload)


def _issue_group_payload(issue: ForemanIssue) -> dict[str, Any]:
    evidence = issue.evidence if isinstance(issue.evidence, dict) else {}
    problem = (
        evidence.get("run_error")
        or evidence.get("sidecar_error")
        or evidence.get("error_excerpt")
        or issue.title
    )
    payload: dict[str, Any] = {
        "detector_version": ALERT_DETECTOR_VERSION,
        "kind": issue.kind,
        "problem": _canonical_problem_text(problem),
    }
    if issue.kind == "worker_errored":
        payload["run_outcome"] = _canonical_problem_text(evidence.get("run_outcome") or "")
    elif issue.kind == "stale_running":
        payload["task_assignee"] = _canonical_problem_text(evidence.get("task_assignee") or "")
        payload["stale_after_seconds"] = evidence.get("stale_after_seconds")
    elif issue.kind == "board_stalled":
        payload["board"] = issue.board
        payload["board_goal_status"] = _canonical_problem_text(evidence.get("board_goal_status") or "")
        payload["board_phase"] = _canonical_problem_text(evidence.get("board_phase") or "")
        payload["blocked_reason"] = _canonical_problem_text(evidence.get("blocked_reason") or "")
    elif issue.kind == "human_intervention_required":
        payload["board"] = issue.board
        payload["source_board"] = _canonical_problem_text(evidence.get("source_board") or "")
        payload["source_task_id"] = _canonical_problem_text(evidence.get("source_task_id") or "")
        payload["foreman_board"] = _canonical_problem_text(evidence.get("foreman_board") or "")
        payload["manual_intervention_type"] = _canonical_problem_text(
            evidence.get("manual_intervention_type") or ""
        )
    return payload


def _canonical_problem_text(value: Any) -> str:
    text = _sanitize_text(str(value or "")).casefold()
    text = re.sub(r"\bpid\s+\d+\b", "pid <pid>", text)
    text = re.sub(r"\b\d{4,}\b", "<num>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return _truncate_text(text, 300)


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
    if entry.get("startup_suppressed_at") and not entry.get("last_sent_at"):
        return False
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


def _is_group_alert_due(issue: ForemanIssue, entry: dict[str, Any], *, now: int) -> bool:
    next_retry = entry.get("next_retry_at")
    if next_retry is not None and int(next_retry) > now:
        return False
    if int(entry.get("failure_count") or 0) > 0 and next_retry is not None and int(next_retry) <= now:
        return True
    if entry.get("startup_suppressed_at") and not entry.get("last_sent_at"):
        return False
    last_sent = entry.get("last_sent_at")
    if not last_sent:
        return True
    if _is_terminal_or_archived_issue(issue):
        return False
    state_key = _issue_group_state_key(issue)
    if state_key != str(entry.get("last_state_key") or ""):
        return True
    if _severity_rank(issue.severity) > _severity_rank(str(entry.get("last_sent_severity") or "")):
        return True
    return False


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
    groups = state.setdefault("issue_groups", {})
    for fingerprint, entry in list(groups.items()):
        if not isinstance(entry, dict):
            groups.pop(fingerprint, None)
            changed = True
            continue
        seen = int(entry.get("first_seen_at") or 0)
        sent = int(entry.get("last_sent_at") or 0)
        attempted = int(entry.get("last_attempt_at") or 0)
        if max(seen, sent, attempted) < cutoff:
            groups.pop(fingerprint, None)
            changed = True
    assessments = state.setdefault("manual_assessments", {})
    for fingerprint, entry in list(assessments.items()):
        if not isinstance(entry, dict):
            assessments.pop(fingerprint, None)
            changed = True
            continue
        assessed = int(entry.get("assessed_at") or 0)
        if assessed < cutoff:
            assessments.pop(fingerprint, None)
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
    if issue.kind == "board_stalled":
        return "Inspect the board-level blocker, provide missing inputs, then unblock or retry the affected task."
    if issue.kind == "human_intervention_required":
        return "A human must provide the requested manual intervention, then unblock or retry the source task."
    return "Inspect the Kanban task and resolve the reported worker issue."


def _truncate_text(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    marker = "... [truncated]"
    if limit <= len(marker):
        return marker[:limit]
    return value[: limit - len(marker)] + marker
