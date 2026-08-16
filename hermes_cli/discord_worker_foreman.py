"""Foreman scanner and recovery-ticket writer for Discord worker boards."""

from __future__ import annotations

import hashlib
import json
import logging
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
    persist_board_run_summary,
    public_session_board_url,
    read_board_run_summary,
)
from hermes_cli.discord_worker_roles import (
    DISCORD_WORKER_META_KEY,
    REVIEW_LOOP_LIMIT_BLOCKED_REASON,
    ROLE_REVIEWER,
)
from hermes_cli.discord_worker_state import read_codex_worker_state
from utils import atomic_json_write


logger = logging.getLogger(__name__)


# Foreman scans every 30s by default. Flag no-progress workers after 30s so
# the next scan creates a recovery task within roughly one minute, while the
# dispatcher still uses its 60s stale reclaim threshold as the hard reset.
STALE_RUNNING_SECONDS = 30
ERROR_OUTCOMES = frozenset({"spawn_failed", "crashed", "timed_out", "gave_up"})
ALERT_DETECTOR_VERSION = 1
ALERT_STATE_VERSION = 1
DISCORD_ALERT_LIMIT = 2000
FOREMAN_DISCORD_CHANNEL_ID = "1504252294495998043"
FOREMAN_DISCORD_MENTION = "<@&1503914570077442058>"
BLOCKED_BOARD_MIN_AGE_SECONDS = 10 * 60
MANUAL_ESCALATION_TASK = "foreman_manual_escalation"
FOREMAN_MASTER_TASK_CREATED_BY = "discord-worker-foreman"
FOREMAN_MASTER_TASK_KEY_PREFIX = "discord-foreman"
FOREMAN_MASTER_TASK_ACTIVE_STATUSES = frozenset({"triage", "todo", "ready", "running", "review"})
FOREMAN_MASTER_TASK_SUPPRESS_STATUSES = FOREMAN_MASTER_TASK_ACTIVE_STATUSES | {"blocked"}
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
        "closeout_source",
        "closeout_status",
        "error_excerpt",
        "green_unmerged_since",
        "heartbeat_age_seconds",
        "last_heartbeat_at",
        "latest_run_error",
        "latest_run_id",
        "latest_run_outcome",
        "latest_run_status",
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
        "pr_number",
        "ready_count",
        "run_error",
        "run_id",
        "run_outcome",
        "run_status",
        "review_count",
        "session_url",
        "sidecar_event_count",
        "sidecar_error",
        "sidecar_exit_code",
        "sidecar_result_error",
        "sidecar_tool_trace_tail",
        "sidecar_timed_out",
        "sidecar_updated_at",
        "scheduled_count",
        "source_public_board_url",
        "source_public_ticket_url",
        "stalled_after_seconds",
        "stalled_age_seconds",
        "stalled_since",
        "stale_after_seconds",
        "task_assignee",
        "task_status",
        "thread_id",
        "thread_state",
        "todo_count",
        "worker_log_path",
        "running_count",
        "source_board",
        "source_blocked_reason",
        "source_discord_thread_url",
        "source_error_excerpt",
        "source_issue_kind",
        "source_run_error",
        "source_sidecar_error",
        "source_summary",
        "source_task_id",
    }
)
_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2, "critical": 3}
_TERMINAL_THREAD_STATES = frozenset({"blocked", "errored", "done", "archived", "cancelled"})


def _paused_corrupt_incident(board: str) -> Optional[dict[str, Any]]:
    try:
        state = kanban_db.corrupt_board_quarantine_state(board)
    except Exception:
        return None
    return state.get("incident") if state.get("skipped") else None


def _is_corrupt_board_db_error(exc: Exception) -> bool:
    return kanban_db.is_corrupt_board_db_error(exc)


def _record_corrupt_board(board: str, exc: Exception) -> Optional[dict[str, Any]]:
    incident = getattr(exc, "incident", None)
    if isinstance(incident, dict):
        return incident
    db_path = kanban_db.kanban_db_path(board)
    try:
        resolved = db_path.resolve()
    except OSError:
        resolved = db_path
    try:
        fingerprint = kanban_db._db_content_fingerprint(resolved)
    except Exception:
        fingerprint = None
    return kanban_db.record_corrupt_board_incident(
        board,
        resolved,
        str(getattr(exc, "reason", None) or exc),
        backup_path=getattr(exc, "backup_path", None),
        fingerprint=fingerprint,
        error_class=exc.__class__.__name__,
    )


def _log_corrupt_board_incident(
    board: str,
    incident: Optional[dict[str, Any]],
    exc: Exception,
) -> None:
    if not kanban_db.should_log_corrupt_board_incident(incident):
        return
    incident = incident or {}
    logger.error(
        "discord foreman: board %s database corruption incident; "
        "db_path=%s quarantine_path=%s reason=%s. Foreman polling is paused "
        "for this board while the DB fingerprint is unchanged. Repair guidance: "
        "restore a known-good backup or run `hermes kanban repair --board %s`, "
        "then retry after integrity checks pass.",
        board,
        incident.get("db_path") or str(kanban_db.kanban_db_path(board)),
        incident.get("quarantine_path") or getattr(exc, "backup_path", None) or "<unavailable>",
        incident.get("reason") or getattr(exc, "reason", None) or str(exc),
        board,
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
    closeout: dict[str, Any] = field(default_factory=dict)


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
        issues.extend(detect_green_unmerged_overdue(snapshot))
        issues.extend(detect_stalled_blocked_board(snapshot, now=now, min_age_seconds=min_age))
    return sorted(issues, key=lambda issue: (issue.board, issue.task_id, issue.kind))


def coalesce_foreman_issues(
    issues: Iterable[ForemanIssue],
    *,
    master_board: Optional[str] = None,
) -> list[ForemanIssue]:
    """Suppress redundant autonomous foreman work for a source board.

    Foreman should make at most one recovery path visible for a source board at
    a time. Human-intervention alerts are terminal escalations, so when one is
    present it consumes the source-board slot and suppresses the matching
    autonomous foreman retry prompt for that tick.
    """
    active_sources = _active_foreman_source_boards()
    active_sources.update(
        active_master_foreman_source_boards(
            master_board=master_board,
            statuses=FOREMAN_MASTER_TASK_SUPPRESS_STATUSES,
        )
    )
    seen_sources = set(active_sources)
    coalesced: list[ForemanIssue] = []
    for issue in sorted(
        issues,
        key=lambda item: (
            -_severity_rank(getattr(item, "severity", "")),
            str(getattr(item, "board", "") or ""),
            str(getattr(item, "task_id", "") or ""),
            str(getattr(item, "kind", "") or ""),
        ),
    ):
        source_board = _issue_source_board(issue)
        if getattr(issue, "kind", "") == "human_intervention_required":
            coalesced.append(issue)
            if source_board:
                seen_sources.add(source_board)
            continue
        if source_board and source_board in seen_sources:
            continue
        coalesced.append(issue)
        if source_board:
            seen_sources.add(source_board)
    return coalesced


def collect_human_intervention_issues(
    now: Optional[int] = None,
    *,
    blocked_board_min_age_seconds: int = BLOCKED_BOARD_MIN_AGE_SECONDS,
    assessment_fn: Optional[Any] = None,
    master_board: Optional[str] = None,
) -> list[ForemanIssue]:
    """Return human-escalation issues for blocked foreman-generated boards.

    A foreman board that is itself stalled after the configured age is already
    the terminal escalation path: the autonomous foreman attempt has blocked, so
    alert a human deterministically instead of letting a manual-configuration
    classifier suppress the alert. Non-stalled foreman failures still use the
    auxiliary classifier once per unique blockage and persist the result.
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
    issues.extend(_collect_blocked_master_foreman_tasks(master_board=master_board, now=now))
    for snapshot in collect_board_snapshots(foreman_generated_only=True):
        stalled = []
        if _foreman_board_explicitly_blocked(snapshot):
            stalled = [
                _with_foreman_source(issue, snapshot)
                for issue in detect_stalled_blocked_board(
                    snapshot,
                    now=now,
                    min_age_seconds=min_age,
                    include_worker_error_blockers=True,
                )
            ]
        if stalled:
            issues.extend(
                _manual_intervention_issue(
                    issue,
                    _foreman_blocked_attention_assessment(issue, now=now),
                )
                for issue in stalled
            )
            continue

        candidates: list[ForemanIssue] = []
        candidates.extend(detect_worker_errored(snapshot))
        candidates.extend(detect_missing_read_broker(snapshot))
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


def auto_close_completed_foreman_boards(now: Optional[int] = None) -> list[dict[str, Any]]:
    """Deterministically close stale Foreman boards whose source is complete."""
    now = int(time.time()) if now is None else int(now)
    closures: list[dict[str, Any]] = []
    for snapshot in collect_board_snapshots(foreman_generated_only=True):
        if snapshot.archived or _board_is_terminal(snapshot):
            continue
        blocker = _board_blocker_task(snapshot)
        if blocker is None or blocker.status != "blocked":
            continue
        if str(blocker.assignee or "").strip() != ROLE_REVIEWER:
            continue
        if not _task_has_worker_error_issue(blocker):
            continue

        source = _foreman_source_from_request(snapshot.request_text)
        source_board = str(source.get("source_board") or "").strip()
        if not source_board:
            continue
        source_worker = _discord_worker_meta_for_source_board(source_board)
        if source_worker is None:
            continue
        source_complete, pr_evidence = _source_board_auto_closure_ready(source_board, source_worker)
        if not source_complete:
            continue

        outcome = (
            "Deterministic Foreman reconciliation: source board is complete; "
            "closing stale Foreman reviewer runtime blocker."
        )
        _mark_board_complete_for_auto_closure(
            source_board,
            concise_outcome="Deterministic Foreman reconciliation marked the completed source board done.",
        )

        metadata = {
            "completed_directly": True,
            "auto_closed": True,
            "auto_closed_at": now,
            "source_board": source_board,
            "source_task_id": source.get("source_task_id") or "",
            "source_issue_kind": source.get("source_issue_kind") or "",
            "foreman_board": snapshot.board,
            "foreman_task_id": blocker.id,
            "original_blocker": _auto_closure_blocker_metadata(blocker),
            "pr": pr_evidence,
        }
        conn = kanban_db.connect(board=snapshot.board)
        try:
            completed = kanban_db.complete_task(
                conn,
                blocker.id,
                result=outcome,
                summary=outcome,
                metadata=metadata,
            )
        finally:
            conn.close()
        if not completed:
            continue

        _mark_board_complete_for_auto_closure(
            snapshot.board,
            concise_outcome="Deterministic Foreman reconciliation closed the stale recovery board.",
        )
        closures.append(
            {
                "foreman_board": snapshot.board,
                "foreman_task_id": blocker.id,
                "source_board": source_board,
                "source_task_id": source.get("source_task_id") or "",
                "source_issue_kind": source.get("source_issue_kind") or "",
                "pr_state": pr_evidence.get("state") or "",
                "pr_checks_status": pr_evidence.get("checks_status") or "",
                "pr_merge_commit": pr_evidence.get("merge_commit") or "",
            }
        )
    return closures


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
    due_human_input_keys: set[str] = set()
    for _, issue in sorted(enumerate(issues), key=lambda item: _alert_issue_sort_key(item[0], item[1])):
        human_input_key = _human_input_condition_key(issue)
        if issue.kind != "human_intervention_required" and (
            human_input_key in due_human_input_keys or _human_input_already_sent(state, issue)
        ):
            continue
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
        if _issue_daily_count(state, issue, now) >= int(cfg["daily_cap_per_board"]):
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
            if issue.kind == "human_intervention_required":
                due_human_input_keys.add(human_input_key)
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
    key = _issue_daily_key(issue, now)
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
    if issue.kind == "human_intervention_required":
        _record_human_input_sent(state, issue, now=now)
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
    safe_mention = _safe_foreman_mention(mention)
    evidence = _renderable_evidence(issue.evidence)
    if issue.kind == "human_intervention_required":
        return _render_human_intervention_alert(issue, evidence, safe_mention)

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


def _render_human_intervention_alert(
    issue: ForemanIssue,
    evidence: dict[str, Any],
    mention: str,
) -> str:
    source_task = str(evidence.get("source_task_id") or issue.task_id or "").strip()
    lines = [
        mention,
        "**Foreman needs human input**",
        "",
        f"Source thread: {_human_intervention_source_value(issue, evidence)}",
        f"Why: {_truncate_text(_human_intervention_reason(issue, evidence), 280)}",
    ]
    foreman_value = _human_intervention_foreman_value(evidence)
    if foreman_value:
        lines.append(f"Foreman attempt: {foreman_value}")
    next_text = _human_intervention_next_text(evidence, source_task)
    if next_text:
        lines.append(f"Next: {next_text}")
    return _truncate_text("\n".join(lines), DISCORD_ALERT_LIMIT)


def render_foreman_human_intervention_embed(issue: ForemanIssue) -> dict[str, Any]:
    """Return a Discord embed payload for human-intervention alerts."""
    if issue.kind != "human_intervention_required":
        return {}
    evidence = _renderable_evidence(issue.evidence)
    source_task = str(evidence.get("source_task_id") or issue.task_id or "").strip()
    fields = [
        {
            "name": "Source thread",
            "value": _human_intervention_source_value(issue, evidence),
            "inline": False,
        },
        {
            "name": "Why",
            "value": _truncate_text(_human_intervention_reason(issue, evidence), 900),
            "inline": False,
        },
    ]
    foreman_value = _human_intervention_foreman_value(evidence)
    if foreman_value:
        fields.append({"name": "Foreman attempt", "value": foreman_value, "inline": False})
    next_text = _human_intervention_next_text(evidence, source_task)
    if next_text:
        fields.append({"name": "Next", "value": next_text, "inline": False})
    return {
        "title": "Foreman needs human input",
        "color": 0xF59E0B,
        "fields": fields,
    }


def _human_intervention_source_value(issue: ForemanIssue, evidence: dict[str, Any]) -> str:
    source_board = str(evidence.get("source_board") or issue.board or "").strip()
    source_task = str(evidence.get("source_task_id") or issue.task_id or "").strip()
    if source_board and source_task:
        label = f"{source_board}/{source_task}"
    else:
        label = source_board or source_task or "source thread"
    label = _truncate_text(_sanitize_text(label), 160)
    url = str(evidence.get("source_discord_thread_url") or "").strip()
    return _markdown_link(label, url) if _is_public_url(url) else f"`{label}`"


def _human_intervention_foreman_value(evidence: dict[str, Any]) -> str:
    foreman_board = _truncate_text(_sanitize_text(str(evidence.get("foreman_board") or "").strip()), 120)
    if not foreman_board:
        return ""
    session_url = str(evidence.get("session_url") or "").strip()
    return _markdown_link(foreman_board, session_url) if _is_public_url(session_url) else f"`{foreman_board}`"


def _human_intervention_reason(issue: ForemanIssue, evidence: dict[str, Any]) -> str:
    reason = str(evidence.get("manual_intervention_reason") or "").strip()
    if reason and not _mentions_review_loop_limit(reason):
        return _truncate_text(_sanitize_text(reason), 900)
    return _truncate_text(_source_object_level_problem(issue, evidence), 900)


def _source_object_level_problem(issue: ForemanIssue, evidence: dict[str, Any]) -> str:
    blocked_reason = str(evidence.get("blocked_reason") or "").strip()
    candidates = [] if _mentions_review_loop_limit(blocked_reason) else [blocked_reason]
    candidates.extend(
        str(evidence.get(key) or "").strip()
        for key in (
            "source_blocked_reason",
            "source_run_error",
            "source_sidecar_error",
            "source_error_excerpt",
            "source_summary",
        )
    )
    for candidate in candidates:
        if candidate and not _mentions_review_loop_limit(candidate):
            return _sanitize_text(candidate)
    source_issue_kind = str(evidence.get("source_issue_kind") or "").strip()
    source_task = str(evidence.get("source_task_id") or issue.task_id or "").strip()
    if source_issue_kind and source_task:
        return _sanitize_text(f"{source_issue_kind} is still blocking {source_task}.")
    if source_issue_kind:
        return _sanitize_text(f"{source_issue_kind} is still blocking the source thread.")
    return _sanitize_text(str(issue.title or "Manual intervention is required."))


def _human_intervention_next_text(evidence: dict[str, Any], source_task: str) -> str:
    steps = _manual_instruction_steps(evidence)
    if not steps:
        steps = [
            "Add the missing credential, access grant, or configuration in the approved location.",
        ]
    text = "; ".join(_truncate_text(_sanitize_text(step), 180) for step in steps[:3])
    if source_task:
        text = f"{text}; then ask Hermes to retry `{_truncate_text(_sanitize_text(source_task), 80)}`."
    return _truncate_text(text, 700)


def _markdown_link(label: str, url: str) -> str:
    return f"[{label}]({url})"


def _mentions_review_loop_limit(value: Any) -> bool:
    return REVIEW_LOOP_LIMIT_BLOCKED_REASON.casefold() in str(value or "").casefold()


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


def create_foreman_master_task(
    issue: ForemanIssue,
    *,
    master_board: Optional[str] = None,
    assignee: Optional[str] = None,
) -> dict[str, Any]:
    """Create or reuse the master-board task for a Discord worker issue."""
    board = _resolve_master_board(master_board)
    owner = _resolve_master_assignee(assignee)
    source_board = _issue_source_board(issue)
    source_task = _issue_source_task_id(issue)
    idempotency_key = _foreman_master_task_idempotency_key(issue)
    if board == kanban_db.DEFAULT_BOARD:
        kanban_db.init_db(board=board)
    else:
        kanban_db.create_board(
            board,
            name="Hermes Foreman",
            description="Hermes-controlled recovery tasks for blocked worker boards",
        )
    conn = kanban_db.connect(board=board)
    try:
        existing = _find_master_task_by_key(conn, idempotency_key)
        if existing is not None:
            return {
                "task_id": existing.id,
                "created": False,
                "master_board": board,
                "source_board": source_board,
                "source_task_id": source_task,
                "idempotency_key": idempotency_key,
            }
        workspace = _foreman_master_workspace(issue)
        task_id = kanban_db.create_task(
            conn,
            title=_foreman_master_task_title(issue),
            body=render_foreman_master_task_body(issue),
            assignee=owner,
            created_by=FOREMAN_MASTER_TASK_CREATED_BY,
            workspace_kind=workspace["workspace_kind"],
            workspace_path=workspace["workspace_path"],
            tenant=source_board or None,
            priority=100,
            idempotency_key=idempotency_key,
            max_runtime_seconds=1800,
            board=board,
        )
        return {
            "task_id": task_id,
            "created": True,
            "master_board": board,
            "source_board": source_board,
            "source_task_id": source_task,
            "idempotency_key": idempotency_key,
        }
    finally:
        conn.close()


def render_foreman_master_task_body(issue: ForemanIssue) -> str:
    """Render the body for a master-board recovery task."""
    evidence = _renderable_evidence(issue.evidence)
    source_board = _issue_source_board(issue)
    source_task = _issue_source_task_id(issue)
    source_url = str(evidence.get("session_url") or "").strip()
    metadata = {
        "source_board": source_board,
        "source_task_id": source_task,
        "source_issue_kind": str(issue.kind or ""),
        "source_summary": _truncate_text(_sanitize_text(str(issue.title or "")), 300),
        "session_url": source_url,
        "run_id": evidence.get("run_id"),
        "run_outcome": evidence.get("run_outcome"),
        "run_error": evidence.get("run_error"),
        "sidecar_error": evidence.get("sidecar_error"),
        "error_excerpt": evidence.get("error_excerpt"),
        "thread_id": evidence.get("thread_id"),
        "source_discord_thread_url": evidence.get("source_discord_thread_url"),
    }
    metadata = {k: v for k, v in metadata.items() if v not in (None, "")}
    lines = [
        "Resolve a blocked Discord worker board from the Hermes master Kanban board.",
        "",
        "Source:",
        f"- Board: {source_board or issue.board}",
        f"- Task: {source_task or issue.task_id}",
        f"- Detector: {issue.kind}",
        f"- Summary: {_truncate_text(_sanitize_text(str(issue.title or '')), 240)}",
    ]
    if source_url:
        lines.append(f"- Board URL: {source_url}")
    lines.extend(
        [
            "",
            "Instructions:",
            "- Inspect the source board and task from live Kanban state before changing anything.",
            "- Make one autonomous recovery attempt when safe: repair board state, unblock, retry, close, or reassign as appropriate.",
            "- If the fix needs multiple steps, create child tickets on this same master board instead of creating a new foreman board.",
            "- If progress requires human-only credentials, admin access, or external infrastructure, block this master task with the exact manual-intervention reason.",
            "- Keep Discord quiet unless human input is required.",
            "- Verify the source board can progress or is correctly terminal, then complete this task with concise evidence.",
        ]
    )
    if evidence:
        lines.extend(["", "Evidence:"])
        for key in sorted(evidence):
            value = evidence[key]
            if key == "manual_intervention_steps":
                continue
            if key == "session_url" and (not isinstance(value, str) or not _is_public_url(value)):
                continue
            lines.append(f"- {key}: {_truncate_text(_sanitize_text(str(value)), 300)}")
    lines.extend(
        [
            "",
            "<foreman-metadata>",
            json.dumps(metadata, sort_keys=True, ensure_ascii=False),
            "</foreman-metadata>",
        ]
    )
    return "\n".join(lines)


def foreman_goal_thread_title(issue: ForemanIssue) -> str:
    """Return a compact Discord thread title for a foreman-created goal."""
    evidence = issue.evidence if isinstance(issue.evidence, dict) else {}
    board = str(evidence.get("source_board") or issue.board or "").strip()
    task = str(evidence.get("source_task_id") or issue.task_id or "").strip()
    if board and task:
        title = f"Foreman: fix {board}/{task}"
    elif board:
        title = f"Foreman: fix {board}"
    else:
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
        if _paused_corrupt_incident(board):
            logger.debug(
                "discord foreman: board %s paused for unchanged DB corruption; skipping poll",
                board,
            )
            continue
        try:
            snapshots.append(
                _build_board_snapshot(
                    board,
                    worker,
                    archived=bool(meta.get("archived")),
                    created_at=_coerce_optional_int(meta.get("created_at")),
                )
            )
        except Exception as exc:
            if _is_corrupt_board_db_error(exc):
                _log_corrupt_board_incident(board, _record_corrupt_board(board, exc), exc)
                continue
            raise
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

    thread_state = board_thread_state(board)
    summary = _read_or_refresh_board_run_summary(
        board,
        worker,
        task_snapshots,
        thread_state=thread_state,
    )
    return BoardSnapshot(
        board=board,
        thread_id=str(worker.get("thread_id") or ""),
        chat_id=str(worker.get("chat_id") or worker.get("thread_id") or ""),
        session_url=_public_worker_url_for_board(board, worker),
        thread_state=thread_state,
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
        closeout=(
            dict(worker.get("closeout"))
            if isinstance(worker.get("closeout"), dict)
            else {}
        ),
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


def detect_green_unmerged_overdue(snapshot: BoardSnapshot) -> list[ForemanIssue]:
    """Flag an active auto-merge PR that remains green beyond its threshold."""

    if snapshot.archived or not isinstance(snapshot.closeout, dict) or not snapshot.closeout:
        return []
    from hermes_cli.trusted_closeout import normalize_closeout_state

    state = normalize_closeout_state(snapshot.closeout)
    policy = state["policy"]
    pr = state["pr"]
    ci = state["ci"]
    telemetry = state["telemetry"]
    head_sha = str(pr.get("head_sha") or "").strip().lower()
    raw_policy = snapshot.closeout.get("policy") or {}
    merge_policy = raw_policy.get("merge") or policy["merge"]
    if (
        state["mode"] == "off"
        or merge_policy != "auto"
        or pr["is_draft"]
        or pr["state"] not in {"OPEN", "UNKNOWN", ""}
        or not head_sha
        or not telemetry.get("green_unmerged_overdue")
        or ci.get("status") != "passed"
        or str(ci.get("head_sha") or "").strip().lower() != head_sha
    ):
        return []

    for key, required_key in (
        ("local_verification", "require_local_verification"),
        ("review", "require_review"),
        ("visual_qa", "require_visual_qa"),
    ):
        if not policy[required_key]:
            continue
        receipt = state[key]
        if (
            str(receipt.get("status") or "").strip().lower()
            not in {"passed", "approved", "success"}
            or str(receipt.get("head_sha") or "").strip().lower() != head_sha
        ):
            return []

    return [
        ForemanIssue(
            kind="green_unmerged_overdue",
            board=snapshot.board,
            task_id="closeout",
            severity="warning",
            title="PR remains green but unmerged",
            evidence={
                "closeout_source": state["source"],
                "closeout_status": state["status"],
                "green_unmerged_since": telemetry.get("green_unmerged_since"),
                "pr_number": pr.get("number"),
                "session_url": snapshot.session_url,
                "thread_state": snapshot.thread_state,
            },
        )
    ]


def detect_stale_running(snapshot: BoardSnapshot, *, now: int) -> list[ForemanIssue]:
    issues: list[ForemanIssue] = []
    for task in snapshot.tasks:
        if task.status != "running":
            continue
        heartbeat = task.last_heartbeat_at
        heartbeat_age = None if heartbeat is None else max(0, now - int(heartbeat))
        if heartbeat_age is not None and heartbeat_age <= STALE_RUNNING_SECONDS:
            continue
        if heartbeat is None and not _running_task_old_enough(task, now=now):
            continue
        latest = task.latest_run
        sidecar = task.sidecar if isinstance(task.sidecar, dict) else {}
        result = _sidecar_result(task)
        events = sidecar.get("events") if isinstance(sidecar.get("events"), list) else []
        trace = sidecar.get("tool_trace") if isinstance(sidecar.get("tool_trace"), list) else []
        evidence: dict[str, Any] = {
            "source_board": snapshot.board,
            "source_task_id": task.id,
            "source_public_board_url": snapshot.session_url,
            "source_public_ticket_url": _public_ticket_url(snapshot, task),
            "last_heartbeat_at": heartbeat,
            "heartbeat_age_seconds": heartbeat_age,
            "stale_after_seconds": STALE_RUNNING_SECONDS,
            "worker_log_path": str(kanban_db.worker_log_path(task.id, board=snapshot.board)),
            "sidecar_updated_at": sidecar.get("updated_at"),
            "sidecar_event_count": len(events),
            "sidecar_tool_trace_tail": trace[-3:],
            "sidecar_result_error": result.get("error"),
            "sidecar_timed_out": result.get("timed_out"),
        }
        if latest:
            evidence.update(
                {
                    "latest_run_id": latest.id,
                    "latest_run_status": latest.status,
                    "latest_run_outcome": latest.outcome,
                    "latest_run_error": latest.error,
                }
            )
        issues.append(
            _issue(
                "stale_running",
                snapshot,
                task,
                "warning",
                "Running worker has no recent heartbeat",
                evidence,
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


def _read_or_refresh_board_run_summary(
    board: str,
    worker: dict[str, Any],
    tasks: tuple[TaskSnapshot, ...],
    *,
    thread_state: str,
) -> dict[str, Any]:
    summary = read_board_run_summary(board)
    if isinstance(summary, dict) and summary and not _board_run_summary_stale(
        summary,
        worker,
        tasks,
        thread_state=thread_state,
    ):
        return dict(summary)
    try:
        return persist_board_run_summary(board)
    except Exception:
        try:
            return build_board_run_summary(board)
        except Exception:
            return dict(summary) if isinstance(summary, dict) else {}


def _board_run_summary_stale(
    summary: dict[str, Any],
    worker: dict[str, Any],
    tasks: tuple[TaskSnapshot, ...],
    *,
    thread_state: str,
) -> bool:
    if not isinstance(summary, dict) or summary.get("board") in (None, ""):
        return True
    generated_at = _coerce_optional_int(summary.get("generated_at")) or 0
    worker_updated_at = _coerce_optional_int(worker.get("updated_at")) or 0
    if worker_updated_at and worker_updated_at > generated_at:
        return True
    if str(summary.get("goal_status") or "") != str(worker.get("goal_status") or ""):
        return True
    if str(summary.get("phase") or "") != str(worker.get("phase") or ""):
        return True
    if str(summary.get("thread_state") or "") != str(thread_state or ""):
        return True
    return _normalized_task_counts(summary.get("task_counts") or {}) != _task_status_counts(tasks)


def _normalized_task_counts(counts: dict[str, Any]) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for key, value in dict(counts or {}).items():
        if str(key) == "total":
            continue
        try:
            count = int(value or 0)
        except (TypeError, ValueError):
            count = 0
        if count:
            normalized[str(key)] = count
    return normalized


def _task_status_counts(tasks: Iterable[TaskSnapshot]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for task in tasks:
        status = str(task.status or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return {key: value for key, value in counts.items() if value}


def _public_ticket_url(snapshot: BoardSnapshot, task: TaskSnapshot) -> str:
    if not snapshot.session_url:
        return ""
    return f"{snapshot.session_url.rstrip('/')}/tickets/{task.id}"


def detect_stalled_blocked_board(
    snapshot: BoardSnapshot,
    *,
    now: int,
    min_age_seconds: int = BLOCKED_BOARD_MIN_AGE_SECONDS,
    include_worker_error_blockers: bool = False,
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
    if blocker is not None and not include_worker_error_blockers and _task_has_worker_error_issue(blocker):
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


def _running_task_old_enough(task: TaskSnapshot, *, now: int) -> bool:
    started = _running_task_started_at(task)
    if started is None:
        return False
    return max(0, int(now) - int(started)) > STALE_RUNNING_SECONDS


def _running_task_started_at(task: TaskSnapshot) -> Optional[int]:
    latest = task.latest_run
    for value in (
        latest.started_at if latest else None,
        task.started_at,
        task.created_at,
    ):
        parsed = _coerce_optional_int(value)
        if parsed is not None:
            return parsed
    return None


def _active_foreman_source_boards() -> set[str]:
    sources: set[str] = set()
    for snapshot in collect_board_snapshots(foreman_generated_only=True):
        if not _is_active_foreman_board(snapshot):
            continue
        source = _foreman_source_from_request(snapshot.request_text).get("source_board")
        if source:
            sources.add(source)
    return sources


def active_master_foreman_source_boards(
    *,
    master_board: Optional[str] = None,
    statuses: Optional[Iterable[str]] = None,
) -> set[str]:
    board = _resolve_master_board(master_board)
    wanted = set(statuses or FOREMAN_MASTER_TASK_ACTIVE_STATUSES)
    try:
        conn = kanban_db.connect(board=board)
    except Exception:
        return set()
    try:
        rows = conn.execute(
            "SELECT idempotency_key, body FROM tasks "
            "WHERE idempotency_key LIKE ? AND status IN ("
            + ",".join("?" for _ in wanted)
            + ")",
            (f"{FOREMAN_MASTER_TASK_KEY_PREFIX}:%", *sorted(wanted)),
        ).fetchall()
    finally:
        conn.close()
    sources: set[str] = set()
    for row in rows:
        source = _source_board_from_master_task_row(row)
        if source:
            sources.add(source)
    return sources


def _is_active_foreman_board(snapshot: BoardSnapshot) -> bool:
    if snapshot.archived or _board_is_terminal(snapshot):
        return False
    counts = _board_task_counts(snapshot)
    return any(int(counts.get(status) or 0) > 0 for status in _ACTIVE_BOARD_TASK_STATUSES)


def _issue_source_board(issue: ForemanIssue) -> str:
    evidence = issue.evidence if isinstance(issue.evidence, dict) else {}
    return str(evidence.get("source_board") or issue.board or "").strip()


def _board_is_terminal(snapshot: BoardSnapshot) -> bool:
    goal_status = _canonical_problem_text(snapshot.goal_status)
    phase = _canonical_problem_text(snapshot.phase)
    thread_state = _canonical_problem_text(snapshot.thread_state)
    return (
        goal_status in {"done", "cancelled"}
        or phase in {"complete", "cancelled"}
        or thread_state in {"done", "archived", "cancelled"}
    )


def _foreman_board_explicitly_blocked(snapshot: BoardSnapshot) -> bool:
    """Return True when the foreman worker board itself advertises a blocked state."""
    goal_status = _canonical_problem_text(snapshot.goal_status)
    phase = _canonical_problem_text(snapshot.phase)
    thread_state = _canonical_problem_text(snapshot.thread_state)
    return goal_status == "blocked" or phase == "blocked" or thread_state == "blocked"


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


def _discord_worker_meta_for_source_board(board: str) -> Optional[dict[str, Any]]:
    worker = _discord_worker_meta_for_workspace(board)
    if worker is None:
        return None
    if _is_foreman_generated_board(worker):
        return None
    return worker


def _discord_worker_meta_for_workspace(board: str) -> Optional[dict[str, Any]]:
    try:
        metadata = kanban_db.read_board_metadata(board)
    except Exception:
        return None
    worker = metadata.get(DISCORD_WORKER_META_KEY)
    if not isinstance(worker, dict) or worker.get("kind") != "discord_worker_board":
        return None
    return dict(worker)


def _source_board_auto_closure_ready(
    board: str,
    worker: dict[str, Any],
) -> tuple[bool, dict[str, Any]]:
    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()
    if not tasks or any(str(task.status or "") != "done" for task in tasks):
        return False, {}

    refreshed_worker = _refresh_source_pr_status_for_auto_closure(board, dict(worker))
    pr_evidence = _auto_closure_pr_evidence(refreshed_worker)
    if pr_evidence.get("has_pr_evidence") and not pr_evidence.get("safe"):
        return False, pr_evidence
    if not _source_worktree_clean_for_auto_closure(refreshed_worker):
        pr_evidence["safe"] = False
        pr_evidence["blocker"] = pr_evidence.get("blocker") or "source worktree has uncommitted changes"
        return False, pr_evidence
    return True, pr_evidence


def _refresh_source_pr_status_for_auto_closure(board: str, worker: dict[str, Any]) -> dict[str, Any]:
    """Best-effort live PR refresh for a source board before auto-closing."""
    pr_ref = str(worker.get("pr_url") or worker.get("pr_number") or "").strip()
    if not pr_ref:
        return worker
    root_text = str(worker.get("worktree_path") or worker.get("project_path") or "").strip()
    if not root_text:
        return worker
    root = Path(root_text).expanduser()
    if not root.is_dir():
        return worker
    try:
        from hermes_cli.kanban_codex_worker import _refresh_pr_status, _resolve_github_repo

        repo = _resolve_github_repo(worker, root)
        if not repo:
            return worker
        _refresh_pr_status(worker, root=root, repo=repo)
        from hermes_cli.discord_worker_boards import _update_worker_meta

        metadata = _update_worker_meta(board, worker)
        refreshed = metadata.get(DISCORD_WORKER_META_KEY)
        if isinstance(refreshed, dict):
            worker = dict(refreshed)
    except Exception as exc:
        worker["pr_status_error"] = _truncate_text(_sanitize_text(str(exc)), 240)
        worker["pr_blocker"] = worker["pr_status_error"]
    return worker


def _source_worktree_clean_for_auto_closure(worker: dict[str, Any]) -> bool:
    root_text = str(worker.get("worktree_path") or "").strip()
    if not root_text:
        return True
    root = Path(root_text).expanduser()
    if not root.is_dir():
        return True
    import subprocess

    try:
        status = subprocess.run(
            ["git", "status", "--short"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return False
    if status.returncode != 0:
        return False
    return not (status.stdout or "").strip()


def _auto_closure_pr_evidence(worker: dict[str, Any]) -> dict[str, Any]:
    state = str(worker.get("pr_state") or "").strip().upper()
    checks_status = str(worker.get("pr_checks_status") or "").strip().lower()
    merge_commit = str(
        worker.get("pr_merge_commit_sha")
        or worker.get("pr_merge_commit")
        or ""
    ).strip()
    blocker = str(
        worker.get("pr_blocker")
        or worker.get("pr_status_error")
        or worker.get("pr_error")
        or ""
    ).strip()
    evidence = {
        "url": str(worker.get("pr_url") or "").strip(),
        "number": str(worker.get("pr_number") or "").strip(),
        "state": state,
        "checks_status": checks_status,
        "merge_commit": merge_commit,
        "blocker": blocker,
        "has_pr_evidence": False,
        "safe": True,
    }
    pr_keys = (
        "pr_url",
        "pr_number",
        "pr_state",
        "pr_checks_status",
        "pr_merge_commit",
        "pr_merge_commit_sha",
        "pr_blocker",
        "pr_status_error",
        "pr_error",
        "pr_skipped_no_changes",
    )
    evidence["has_pr_evidence"] = any(str(worker.get(key) or "").strip() for key in pr_keys)
    if not evidence["has_pr_evidence"]:
        return evidence
    checks_ok = checks_status in {"passed", "success"}
    skipped_no_changes = bool(worker.get("pr_skipped_no_changes")) or state in {"NOT_NEEDED", "NO_CHANGES"}
    evidence["safe"] = (
        (
            (state == "MERGED" and bool(merge_commit))
            or skipped_no_changes
        )
        and checks_ok
        and not blocker
    )
    return evidence


def _auto_closure_blocker_metadata(task: TaskSnapshot) -> dict[str, Any]:
    latest = task.latest_run
    sidecar = _sidecar_result(task)
    return {
        "task_id": task.id,
        "assignee": task.assignee,
        "status": task.status,
        "last_failure_error": task.last_failure_error,
        "run_id": latest.id if latest else None,
        "run_status": latest.status if latest else "",
        "run_outcome": latest.outcome if latest else "",
        "run_error": latest.error if latest else "",
        "sidecar_error": sidecar.get("error"),
        "sidecar_timed_out": sidecar.get("timed_out"),
        "sidecar_exit_code": sidecar.get("exit_code"),
    }


def _mark_board_complete_for_auto_closure(board: str, *, concise_outcome: str) -> None:
    from hermes_cli.discord_worker_read import update_board

    update_board(
        board,
        goal_status="done",
        phase="complete",
        clear_blocked_reason=True,
        concise_outcome=concise_outcome,
        sync_summary=True,
        sync_reaction=True,
        persist_summary=True,
        dispatch_reason="foreman-auto-close-completed-board",
    )


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
        "summary": "source_summary",
        "blocked_reason": "source_blocked_reason",
        "run_error": "source_run_error",
        "sidecar_error": "source_sidecar_error",
        "error_excerpt": "source_error_excerpt",
    }
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip().lstrip("- ").strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        target = mapping.get(key.strip().casefold())
        if target and value.strip():
            source[target] = _truncate_text(_sanitize_text(value.strip()), 300)
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
        "manual_assessment_prompt_version": 2,
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
                "instructions (array of literal line-by-line actions for the human). "
                "Do not simply copy every blocker phrase into the checklist. Infer the minimum human-only "
                "actions needed and omit speculative prerequisites or implementation choices that are not "
                "directly required. Each instruction must start with a concrete verb like Open, Create, Add, "
                "Grant, Paste, Save, or Reply. Avoid vague steps such as 'provide access' or 'provision resources' "
                "unless you name the exact account, console, secret, config file, or person the human should use. "
                "When credentials are needed, say which service/account area to open, what credential/access to "
                "create or request, where it should be installed in Hermes or the project, and what to ask the "
                "agent to retry. Do not invent secret values."
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
                        "If true, include literal human instructions to the degree possible from the evidence."
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


def _foreman_blocked_attention_assessment(issue: ForemanIssue, *, now: int) -> dict[str, Any]:
    evidence = issue.evidence if isinstance(issue.evidence, dict) else {}
    problem = _source_object_level_problem(issue, evidence)
    source_board = str(evidence.get("source_board") or issue.board or "").strip()
    source_task = str(evidence.get("source_task_id") or issue.task_id or "").strip()
    source = "/".join(part for part in (source_board, source_task) if part) or "the source thread"
    reason = f"{source} is blocked on: {problem}"
    return {
        "assessed_at": now,
        "requires_manual_intervention": True,
        "reason": _truncate_text(_sanitize_text(reason), 300),
        "intervention_type": "foreman_blocked",
        "instructions": [
            "Open the master Kanban recovery task linked in this alert and inspect the blocked task.",
            "Decide whether to retry or reassign the recovery worker, add missing human context, or cancel the attempt.",
            "Reply in Discord with the next action Hermes should take, then ask Hermes to retry the blocked source task if appropriate.",
        ],
        "confidence": "high",
        "error": "",
    }


def _safe_foreman_mention(mention: str = "") -> str:
    raw = str(mention or FOREMAN_DISCORD_MENTION or "").strip()
    if "\n" in raw:
        raw = raw.splitlines()[0].strip()
    safe = _truncate_text(_sanitize_text(raw), 100).strip()
    return safe or FOREMAN_DISCORD_MENTION


def _public_worker_url_for_board(board: str, worker: dict[str, Any]) -> str:
    public_url = str(worker.get("public_url") or "").strip()
    if public_url:
        return public_url
    thread_id = str(worker.get("thread_id") or "").strip()
    legacy_board = f"discord-{thread_id}" if thread_id else ""
    route_id = thread_id if legacy_board and str(board or "") == legacy_board else str(board or thread_id)
    return public_session_board_url(route_id)


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


_FOREMAN_METADATA_RE = re.compile(
    r"<foreman-metadata>\s*(\{.*?\})\s*</foreman-metadata>",
    re.DOTALL,
)


def _collect_blocked_master_foreman_tasks(
    *,
    master_board: Optional[str],
    now: int,
) -> list[ForemanIssue]:
    board = _resolve_master_board(master_board)
    try:
        conn = kanban_db.connect(board=board)
    except Exception:
        return []
    try:
        tasks = []
        for task in kanban_db.list_tasks(conn, include_archived=False):
            if task.status != "blocked":
                continue
            if not str(task.idempotency_key or "").startswith(f"{FOREMAN_MASTER_TASK_KEY_PREFIX}:"):
                continue
            tasks.append((task, kanban_db.latest_run(conn, task.id)))
    finally:
        conn.close()
    issues: list[ForemanIssue] = []
    for task, latest in tasks:
        metadata = _extract_master_task_metadata(task.body or {})
        source_board = str(metadata.get("source_board") or _source_board_from_master_key(task.idempotency_key) or "").strip()
        source_task = str(metadata.get("source_task_id") or "").strip()
        reason = str(
            task.last_failure_error
            or task.result
            or (getattr(latest, "error", None) if latest else None)
            or (getattr(latest, "summary", None) if latest else None)
            or "Master recovery task is blocked."
        ).strip()
        source_issue = ForemanIssue(
            kind="master_task_blocked",
            board=board,
            task_id=task.id,
            severity="critical",
            title="Master recovery task is blocked",
            evidence=_sanitize_evidence(
                {
                    **metadata,
                    "source_board": source_board,
                    "source_task_id": source_task,
                    "source_issue_kind": metadata.get("source_issue_kind") or "master_task_blocked",
                    "blocked_reason": reason,
                    "foreman_board": board,
                    "foreman_task_id": task.id,
                    "llm_assessed_at": now,
                }
            ),
        )
        issues.append(
            _manual_intervention_issue(
                source_issue,
                {
                    "assessed_at": now,
                    "requires_manual_intervention": True,
                    "reason": reason or "Master recovery task is blocked.",
                    "intervention_type": "master_task_blocked",
                    "instructions": [
                        "Open the master Kanban recovery task and resolve its blocker.",
                        "Then retry or close the original Discord worker board task as appropriate.",
                    ],
                    "confidence": "high",
                },
            )
        )
    return issues


def _extract_master_task_metadata(body: Any) -> dict[str, Any]:
    match = _FOREMAN_METADATA_RE.search(str(body or ""))
    if not match:
        return {}
    try:
        parsed = json.loads(match.group(1))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return _sanitize_evidence(parsed) if isinstance(parsed, dict) else {}


def _resolve_master_board(master_board: Optional[str] = None) -> str:
    explicit = str(master_board or "").strip()
    if explicit:
        return explicit
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        raw = (((cfg.get("kanban") or {}).get("discord_worker") or {}).get("foreman") or {})
        if isinstance(raw, dict):
            configured = str(raw.get("master_board") or "").strip()
            if configured:
                return configured
    except Exception:
        pass
    return kanban_db.DEFAULT_BOARD


def _resolve_master_assignee(assignee: Optional[str] = None) -> str:
    explicit = str(assignee or "").strip()
    if explicit:
        return explicit
    try:
        from hermes_cli.config import load_config
        from hermes_cli.kanban_decompose import _resolve_orchestrator_profile

        return _resolve_orchestrator_profile(load_config() or {})
    except Exception:
        try:
            from hermes_cli.profiles import get_active_profile_name

            return get_active_profile_name() or "default"
        except Exception:
            return "default"


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _issue_source_task_id(issue: ForemanIssue) -> str:
    evidence = issue.evidence if isinstance(issue.evidence, dict) else {}
    return str(evidence.get("source_task_id") or issue.task_id or "").strip()


def _is_runtime_checkout_path(path: str) -> bool:
    try:
        candidate = Path(path).expanduser().resolve(strict=False)
        root = _repo_root().resolve(strict=False)
    except Exception:
        return False
    return candidate == root or root in candidate.parents


def _safe_foreman_workspace_path(value: Any) -> str:
    path = str(value or "").strip()
    if not path:
        return ""
    expanded = Path(path).expanduser()
    if not expanded.is_absolute() or _is_runtime_checkout_path(path):
        return ""
    return path


def _source_task_workspace_path(source_board: str, source_task: str) -> str:
    if not source_board or not source_task:
        return ""
    conn = None
    try:
        conn = kanban_db.connect(board=source_board)
        task = kanban_db.get_task(conn, source_task)
    except Exception:
        return ""
    finally:
        if conn is not None:
            conn.close()
    if task is None or str(task.workspace_kind or "") != "dir":
        return ""
    return _safe_foreman_workspace_path(task.workspace_path)


def _foreman_master_workspace(issue: ForemanIssue) -> dict[str, Any]:
    """Return isolated workspace args for master-board Foreman tasks.

    The gateway/foreman code often runs from the canonical checkout. Recovery
    tasks still need to operate from the source worker board's code island, not
    from the runtime checkout that created the alert.
    """
    source_board = _issue_source_board(issue)
    worker = _discord_worker_meta_for_workspace(source_board) if source_board else None
    if worker:
        path = _safe_foreman_workspace_path(worker.get("worktree_path"))
        if path:
            return {"workspace_kind": "dir", "workspace_path": path}
    source_task_path = _source_task_workspace_path(source_board, _issue_source_task_id(issue))
    if source_task_path:
        return {"workspace_kind": "dir", "workspace_path": source_task_path}
    evidence = issue.evidence if isinstance(issue.evidence, dict) else {}
    for key in ("worktree_path", "workspace_path"):
        path = _safe_foreman_workspace_path(evidence.get(key))
        if path:
            return {"workspace_kind": "dir", "workspace_path": path}
    return {"workspace_kind": "scratch", "workspace_path": None}


def _foreman_master_task_title(issue: ForemanIssue) -> str:
    source_board = _issue_source_board(issue)
    source_task = _issue_source_task_id(issue)
    if source_board and source_task:
        return _truncate_text(f"Recover {source_board}/{source_task}", 80)
    return _truncate_text(f"Recover {source_board or issue.board or 'Discord worker board'}", 80)


def _foreman_master_task_idempotency_key(issue: ForemanIssue) -> str:
    source_board = _issue_source_board(issue) or "unknown-board"
    source_task = _issue_source_task_id(issue) or "unknown-task"
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
        "kind": issue.kind,
        "source_board": source_board,
        "source_task_id": source_task,
        "run_id": evidence.get("run_id"),
        "run_outcome": evidence.get("run_outcome"),
        "sidecar_exit_code": evidence.get("sidecar_exit_code"),
        "problem": _canonical_problem_text(problem),
    }
    digest = _stable_digest(payload)[:16]
    return f"{FOREMAN_MASTER_TASK_KEY_PREFIX}:{source_board}:{source_task}:{issue.kind}:{digest}"


def _find_master_task_by_key(conn: Any, idempotency_key: str) -> Optional[Any]:
    row = conn.execute(
        "SELECT * FROM tasks WHERE idempotency_key = ? AND status != 'archived' "
        "ORDER BY created_at DESC LIMIT 1",
        (idempotency_key,),
    ).fetchone()
    return kanban_db.Task.from_row(row) if row is not None else None


def _source_board_from_master_task_row(row: Any) -> str:
    try:
        key_source = _source_board_from_master_key(row["idempotency_key"])
        if key_source:
            return key_source
        metadata = _extract_master_task_metadata(row["body"])
        return str(metadata.get("source_board") or "").strip()
    except Exception:
        return ""


def _source_board_from_master_key(idempotency_key: Any) -> str:
    raw = str(idempotency_key or "")
    prefix = f"{FOREMAN_MASTER_TASK_KEY_PREFIX}:"
    if not raw.startswith(prefix):
        return ""
    parts = raw.split(":", 4)
    return parts[1].strip() if len(parts) >= 2 else ""


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
            "human_input_alerts": {},
            "manual_assessments": {},
            "daily_counts": {},
        }
    alerts = raw.get("alerts")
    if not isinstance(alerts, dict):
        raw["alerts"] = {}
    groups = raw.get("issue_groups")
    if not isinstance(groups, dict):
        raw["issue_groups"] = {}
    human_input_alerts = raw.get("human_input_alerts")
    if not isinstance(human_input_alerts, dict):
        raw["human_input_alerts"] = {}
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
    state.setdefault("human_input_alerts", {})
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


def _alert_issue_sort_key(index: int, issue: ForemanIssue) -> tuple[int, int, int, int, int]:
    evidence = issue.evidence if isinstance(issue.evidence, dict) else {}
    board_created_at = _coerce_optional_int(evidence.get("board_created_at")) or 0
    stalled_since = _coerce_optional_int(evidence.get("stalled_since")) or 0
    is_human_intervention = issue.kind == "human_intervention_required"
    return (
        0 if is_human_intervention else 1,
        -_severity_rank(issue.severity),
        -board_created_at,
        -stalled_since,
        index,
    )


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


def _human_input_condition_key(issue: ForemanIssue) -> str:
    evidence = issue.evidence if isinstance(issue.evidence, dict) else {}
    source_issue_kind = str(evidence.get("source_issue_kind") or "").strip()
    payload = {
        "detector_version": ALERT_DETECTOR_VERSION,
        "source_board": _issue_source_board(issue),
        "source_task_id": str(evidence.get("source_task_id") or issue.task_id or "").strip(),
        "source_issue_kind": source_issue_kind or str(issue.kind or "").strip(),
    }
    return _stable_digest(payload)


def _human_input_already_sent(state: dict[str, Any], issue: ForemanIssue) -> bool:
    alerts = state.setdefault("human_input_alerts", {})
    entry = alerts.get(_human_input_condition_key(issue))
    return isinstance(entry, dict) and bool(entry.get("last_sent_at"))


def _record_human_input_sent(state: dict[str, Any], issue: ForemanIssue, *, now: int) -> None:
    alerts = state.setdefault("human_input_alerts", {})
    key = _human_input_condition_key(issue)
    entry = alerts.get(key)
    if not isinstance(entry, dict):
        entry = _new_alert_entry(now)
        alerts[key] = entry
    entry["last_sent_at"] = now
    entry["last_attempt_at"] = now
    entry["send_count"] = int(entry.get("send_count") or 0) + 1
    entry["failure_count"] = 0
    entry["next_retry_at"] = None
    entry["last_error"] = ""


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
        payload["board"] = issue.board
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


def _daily_key(scope: str, now: int) -> str:
    day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
    return f"{scope}:{day}"


def _issue_daily_scope(issue: ForemanIssue) -> str:
    """Return the daily-cap bucket for an alert issue.

    Human-intervention alerts are the terminal escalation path for a Foreman
    attempt. Keep them in their own per-board bucket so an earlier autonomous
    warning for the source board cannot consume the day's only notification slot.
    """
    if issue.kind == "human_intervention_required":
        return f"{issue.board}:human_intervention_required"
    return issue.board


def _issue_daily_key(issue: ForemanIssue, now: int) -> str:
    return _daily_key(_issue_daily_scope(issue), now)


def _issue_daily_count(state: dict[str, Any], issue: ForemanIssue, now: int) -> int:
    return int(state.setdefault("daily_counts", {}).get(_issue_daily_key(issue, now)) or 0)


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
    human_input_alerts = state.setdefault("human_input_alerts", {})
    for fingerprint, entry in list(human_input_alerts.items()):
        if not isinstance(entry, dict):
            human_input_alerts.pop(fingerprint, None)
            changed = True
            continue
        seen = int(entry.get("first_seen_at") or 0)
        sent = int(entry.get("last_sent_at") or 0)
        attempted = int(entry.get("last_attempt_at") or 0)
        if max(seen, sent, attempted) < cutoff:
            human_input_alerts.pop(fingerprint, None)
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
