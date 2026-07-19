"""Storage-neutral, bounded trusted GitHub closeout reconciliation.

The engine performs one synchronous reconciliation pass.  It owns no durable
storage, never sleeps, and never calls a model.  Callers persist the normalized
state and schedule another pass at ``next_due_at`` when needed.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from agent.runtime_phase_classification import classify_runtime_phase
from agent.runtime_spans import RuntimeSpanRecorder, sanitize_runtime_spans
from hermes_cli.github_remote import (
    github_cli_env,
    github_origin_repo,
    github_remote_preflight_error,
)


REQUIRED_PR_CHECKS: tuple[tuple[str, str], ...] = (
    ("Basic Tests", "basic"),
    ("PR Body Format", "pr body"),
)
_REQUIRED_PR_CHECK_WORKFLOWS = {
    ("Basic Tests", "basic"): ".github/workflows/tests.yml",
    ("PR Body Format", "pr body"): ".github/workflows/pr-body-format.yml",
}
CLOSEOUT_MODES = frozenset({"off", "shadow", "enforce"})
MERGE_POLICIES = frozenset({"auto", "manual", "never"})
PR_OPEN_POLICIES = frozenset({"after_review_approval", "never"})
SUCCESS_CLOSEOUT_STATUSES = frozenset(
    {"completed", "not_required", "pr_open", "post_merge_complete"}
)
TERMINAL_CLOSEOUT_STATUSES = frozenset(
    {*SUCCESS_CLOSEOUT_STATUSES, "blocked", "repair_required"}
)
_PENDING_CLOSEOUT_STATUSES = frozenset(
    {
        "pending",
        "pr_pending",
        "waiting_for_gates",
        "waiting_for_ci",
        "waiting_for_mergeability",
        "ready_pending",
        "post_merge_pending",
    }
)
_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_SECRET_RE = re.compile(
    r"(?i)(?:authorization|cookie|token|password|secret|api[_-]?key)\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_MAX_ERRORS = 8
_MAX_ERROR_CHARS = 600
_MAX_CHECK_FAILURES = 8
_DEFAULT_MAX_COMMANDS = 24
_MAX_COMMANDS = 32

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
CanonicalSync = Callable[[str, str, str], Any]


@dataclass(frozen=True)
class CloseoutTransition:
    """Result of one bounded reconciliation pass."""

    state: dict[str, Any]
    outcome: str
    next_due_at: float | None
    terminal: bool
    changed: bool
    wake_immediately: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": copy.deepcopy(self.state),
            "outcome": self.outcome,
            "next_due_at": self.next_due_at,
            "terminal": self.terminal,
            "changed": self.changed,
            "wake_immediately": self.wake_immediately,
        }


def _bounded_text(value: Any, *, limit: int = 240) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def sanitize_closeout_error(value: Any) -> str:
    """Return one bounded diagnostic without URLs, credentials, or raw output."""

    text = _bounded_text(value, limit=2400)
    text = _URL_RE.sub("[redacted-url]", text)
    text = _SECRET_RE.sub("[redacted-value]", text)
    text = _BEARER_RE.sub("[redacted-value]", text)
    text = re.sub(r"\b(?:gh[opsu]_[A-Za-z0-9_\-]{8,}|github_pat_[A-Za-z0-9_\-]{8,})\b", "[redacted-value]", text)
    return text[:_MAX_ERROR_CHARS]


def _safe_status(value: Any, *, default: str = "pending") -> str:
    status = re.sub(r"[^a-z0-9_\-]", "", str(value or "").strip().lower())
    return status[:48] or default


def _safe_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _safe_int(value: Any, *, minimum: int = 0, maximum: int = 1_000_000) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return minimum
    return max(minimum, min(maximum, number))


def _record(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _receipt(value: Any, *, default: str = "pending") -> dict[str, Any]:
    raw = _record(value)
    result: dict[str, Any] = {"status": _safe_status(raw.get("status"), default=default)}
    head_sha = str(raw.get("head_sha") or "").strip()
    if _SHA_RE.fullmatch(head_sha):
        result["head_sha"] = head_sha.lower()
    observed_sha = str(raw.get("observed_sha") or "").strip()
    if _SHA_RE.fullmatch(observed_sha):
        result["observed_sha"] = observed_sha.lower()
    checked_at = _safe_float(raw.get("checked_at"))
    if checked_at is not None:
        result["checked_at"] = checked_at
    code = _bounded_text(raw.get("diagnostic_code"), limit=80)
    if code:
        result["diagnostic_code"] = re.sub(r"[^A-Za-z0-9_.\-]", "_", code)
    try:
        baseline_pid = int(raw.get("baseline_pid"))
        baseline_start_time = int(raw.get("baseline_start_time"))
    except (TypeError, ValueError):
        baseline_pid = 0
        baseline_start_time = -1
    if baseline_pid > 0 and baseline_start_time >= 0:
        result["baseline_pid"] = baseline_pid
        result["baseline_start_time"] = baseline_start_time
    return result


def _normalize_errors(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    errors: list[dict[str, Any]] = []
    for raw in value[-_MAX_ERRORS:]:
        item = _record(raw)
        message = sanitize_closeout_error(item.get("message") or item.get("error"))
        if not message:
            continue
        error: dict[str, Any] = {
            "code": re.sub(r"[^a-z0-9_\-]", "_", str(item.get("code") or "closeout_error").lower())[:80],
            "message": message,
        }
        at = _safe_float(item.get("at"))
        if at is not None:
            error["at"] = at
        errors.append(error)
    return errors


def _normalize_post_merge(value: Any) -> dict[str, Any]:
    raw = _record(value)
    target_sha = str(raw.get("target_sha") or "").strip().lower()
    return {
        "target_sha": target_sha if _SHA_RE.fullmatch(target_sha) else "",
        "canonical_sync": _receipt(raw.get("canonical_sync"), default="not_started"),
        "ci": _receipt(raw.get("ci"), default="not_started"),
        "deployment": _receipt(raw.get("deployment"), default="not_configured"),
        "production_qa": _receipt(raw.get("production_qa"), default="not_configured"),
        "restart": _receipt(raw.get("restart"), default="not_configured"),
    }


def normalize_closeout_state(value: Any = None) -> dict[str, Any]:
    """Normalize all closeout sources to one bounded additive schema."""

    raw = _record(value)
    workspace = _record(raw.get("workspace"))
    policy = _record(raw.get("policy"))
    requirements = _record(policy.get("post_merge_requirements"))
    pr = _record(raw.get("pr"))
    ci = _record(raw.get("ci"))
    lease = _record(raw.get("lease"))
    telemetry = _record(raw.get("telemetry"))

    mode = str(raw.get("mode") or "off").strip().lower()
    if mode not in CLOSEOUT_MODES:
        mode = "off"
    merge_policy = str(policy.get("merge") or "auto").strip().lower()
    if merge_policy not in MERGE_POLICIES:
        merge_policy = "auto"
    pr_open_policy = str(policy.get("pr_open") or "after_review_approval").strip().lower()
    if pr_open_policy not in PR_OPEN_POLICIES:
        pr_open_policy = "after_review_approval"

    required_checks = [
        {"workflow": workflow, "check": check}
        for workflow, check in REQUIRED_PR_CHECKS
    ]
    failed = [
        _bounded_text(item, limit=160)
        for item in (ci.get("failed") if isinstance(ci.get("failed"), list) else [])[:_MAX_CHECK_FAILURES]
        if _bounded_text(item, limit=160)
    ]
    head_sha = str(pr.get("head_sha") or "").strip().lower()
    merge_sha = str(pr.get("merge_sha") or "").strip().lower()
    merge_attempted_head_sha = str(pr.get("merge_attempted_head_sha") or "").strip().lower()
    ci_head_sha = str(ci.get("head_sha") or "").strip().lower()

    normalized: dict[str, Any] = {
        "schema_version": 1,
        "id": _bounded_text(raw.get("id"), limit=160),
        "source": _safe_status(raw.get("source"), default="direct"),
        "mode": mode,
        "status": _safe_status(raw.get("status"), default="pending"),
        "workspace": {
            "path": str(workspace.get("path") or "")[:1200],
            "canonical_path": str(workspace.get("canonical_path") or "")[:1200],
            "repository": _bounded_text(workspace.get("repository"), limit=240),
            "branch": _bounded_text(workspace.get("branch"), limit=240),
            "base_branch": _bounded_text(workspace.get("base_branch") or "main", limit=240),
        },
        "policy": {
            "merge": merge_policy,
            "pr_open": pr_open_policy,
            "early_draft_pr": policy.get("early_draft_pr") is True,
            "require_local_verification": policy.get("require_local_verification") is True,
            "require_review": policy.get("require_review") is True,
            "require_visual_qa": policy.get("require_visual_qa") is True,
            "post_merge_requirements": {
                key: requirements.get(key) is True
                for key in ("canonical_sync", "ci", "deployment", "production_qa", "restart")
            },
        },
        "local_verification": _receipt(raw.get("local_verification"), default="not_required"),
        "review": _receipt(raw.get("review"), default="not_required"),
        "visual_qa": _receipt(raw.get("visual_qa"), default="not_required"),
        "pr": {
            "url": str(pr.get("url") or "")[:1200],
            "number": _bounded_text(pr.get("number"), limit=32),
            "title": _bounded_text(pr.get("title"), limit=200),
            "body": str(pr.get("body") or "")[:8000],
            "state": str(pr.get("state") or "").strip().upper()[:32],
            "is_draft": pr.get("is_draft") is True,
            "head_sha": head_sha if _SHA_RE.fullmatch(head_sha) else "",
            "merge_sha": merge_sha if _SHA_RE.fullmatch(merge_sha) else "",
            "merge_state": str(pr.get("merge_state") or "unknown").strip().upper()[:48],
            "mergeable": pr.get("mergeable", "unknown"),
            "review_decision": str(pr.get("review_decision") or "unknown").strip().upper()[:48],
            "ready_at": _safe_float(pr.get("ready_at")),
            "merge_attempted_head_sha": (
                merge_attempted_head_sha if _SHA_RE.fullmatch(merge_attempted_head_sha) else ""
            ),
        },
        "ci": {
            "head_sha": ci_head_sha if _SHA_RE.fullmatch(ci_head_sha) else "",
            "status": _safe_status(ci.get("status"), default="not_checked"),
            "total": _safe_int(ci.get("total"), maximum=len(REQUIRED_PR_CHECKS)),
            "failed": failed,
            "wait_state": _safe_status(ci.get("wait_state"), default="queued"),
            "required": required_checks,
        },
        "canonical_sync": _receipt(raw.get("canonical_sync"), default="not_started"),
        "post_merge": _normalize_post_merge(raw.get("post_merge")),
        "telemetry": {
            "green_unmerged_since": _safe_float(telemetry.get("green_unmerged_since")),
            "green_unmerged_overdue": telemetry.get("green_unmerged_overdue") is True,
            "last_transition": _safe_status(telemetry.get("last_transition"), default="none"),
            "phase_spans": sanitize_runtime_spans(
                telemetry.get("phase_spans"),
                max_spans=120,
            ),
        },
        "revision": _safe_int(raw.get("revision"), maximum=2_147_483_647),
        "lease": {
            "owner": _bounded_text(lease.get("owner"), limit=160),
            "until": _safe_float(lease.get("until")),
        },
        "next_due_at": _safe_float(raw.get("next_due_at")),
        "errors": _normalize_errors(raw.get("errors")),
    }
    return normalized


def _check_name(item: Mapping[str, Any]) -> str:
    return str(
        item.get("name")
        or item.get("context")
        or item.get("workflowName")
        or item.get("__typename")
        or "check"
    )


def _check_app_identity(item: Mapping[str, Any]) -> str:
    app = item.get("app")
    if not isinstance(app, Mapping):
        return ""
    slug = str(app.get("slug") or "").strip().casefold()
    name = str(app.get("name") or "").strip().casefold()
    if (not slug or slug == "github-actions") and (not name or name == "github actions"):
        return "github-actions"
    return slug or name


def _check_workflow_path(item: Mapping[str, Any]) -> str:
    workflow = item.get("workflow")
    if isinstance(workflow, Mapping):
        value = str(workflow.get("path") or "").strip()
        if value:
            return value
    return str(item.get("workflowPath") or "").strip()


def _check_identity(item: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(item.get("workflowName") or ""),
        _check_name(item),
        _check_app_identity(item),
        _check_workflow_path(item),
    )


def _is_trusted_required_check(item: Mapping[str, Any]) -> bool:
    visible_identity = (str(item.get("workflowName") or ""), _check_name(item))
    return (
        _check_app_identity(item) == "github-actions"
        and _check_workflow_path(item) == _REQUIRED_PR_CHECK_WORKFLOWS.get(visible_identity)
    )


def _check_sort_key(item: Mapping[str, Any], index: int) -> tuple[str, int, str, int]:
    timestamp = str(
        item.get("completedAt")
        or item.get("startedAt")
        or item.get("updatedAt")
        or item.get("createdAt")
        or ""
    )
    numeric = item.get("databaseId") or item.get("runNumber") or item.get("id") or 0
    try:
        numeric_value = int(numeric)
    except (TypeError, ValueError):
        numeric_value = 0
    return (
        timestamp,
        numeric_value,
        str(item.get("url") or item.get("detailsUrl") or ""),
        index,
    )


def latest_logical_checks(items: Any) -> list[dict[str, Any]]:
    """Collapse historical attempts to the newest item per logical check."""

    latest: dict[
        tuple[str, str, str, str],
        tuple[tuple[str, int, str, int], dict[str, Any]],
    ] = {}
    if not isinstance(items, list):
        return []
    for index, raw in enumerate(items[:200]):
        if not isinstance(raw, Mapping):
            continue
        item = dict(raw)
        identity = _check_identity(item)
        sort_key = _check_sort_key(item, index)
        if identity not in latest or sort_key > latest[identity][0]:
            latest[identity] = (sort_key, item)
    return [entry[1] for entry in latest.values()]


def summarize_required_checks(items: Any, *, head_sha: str) -> dict[str, Any]:
    """Summarize only exact required gates for the current trusted PR head."""

    selected: dict[tuple[str, str], dict[str, Any]] = {}
    if isinstance(items, list):
        for index, raw in enumerate(items[:200]):
            if not isinstance(raw, Mapping):
                continue
            item_head = str(raw.get("headSha") or raw.get("headRefOid") or "").strip().lower()
            if head_sha and item_head != head_sha.lower():
                continue
            item = dict(raw)
            visible_identity = (str(item.get("workflowName") or ""), _check_name(item))
            if visible_identity not in REQUIRED_PR_CHECKS or not _is_trusted_required_check(item):
                continue
            prior = selected.get(visible_identity)
            if prior is None or _check_sort_key(item, index) >= _check_sort_key(prior, 0):
                selected[visible_identity] = item

    failed: list[str] = []
    pending = False
    running = False
    for workflow, check in REQUIRED_PR_CHECKS:
        item = selected.get((workflow, check))
        if item is None:
            pending = True
            continue
        status = str(item.get("status") or item.get("state") or "").strip().upper()
        conclusion = str(item.get("conclusion") or item.get("conclusionState") or "").strip().upper()
        display = f"{workflow} / {check}"
        if conclusion in {
            "ACTION_REQUIRED",
            "CANCELLED",
            "ERROR",
            "FAILED",
            "FAILURE",
            "STALE",
            "STARTUP_FAILURE",
            "TIMED_OUT",
        } or status in {
            "ERROR",
            "FAILED",
            "FAILURE",
            "STALE",
            "STARTUP_FAILURE",
        }:
            failed.append(display)
        elif conclusion in {"SUCCESS", "NEUTRAL", "SKIPPED"}:
            continue
        else:
            pending = True
            running = running or status in {"IN_PROGRESS", "QUEUED", "REQUESTED", "PENDING", "WAITING"}
    if failed:
        status = "failed"
        wait_state = "rerun_required"
    elif pending:
        status = "pending"
        wait_state = "running" if running else "queued"
    else:
        status = "passed"
        wait_state = "complete"
    return {
        "head_sha": head_sha.lower() if _SHA_RE.fullmatch(head_sha or "") else "",
        "status": status,
        "total": len(selected),
        "failed": failed[:_MAX_CHECK_FAILURES],
        "wait_state": wait_state,
        "required": [
            {"workflow": workflow, "check": check}
            for workflow, check in REQUIRED_PR_CHECKS
        ],
    }


def enrich_required_check_identities(
    payload: Mapping[str, Any],
    *,
    repo: str,
    root: Path,
    run: CommandRunner,
) -> dict[str, Any]:
    """Rebuild required-check identity from bounded exact-head REST evidence."""

    enriched = dict(payload)
    raw_items = payload.get("statusCheckRollup")
    untrusted_items: list[Any] = []
    raw_urls: set[str] = set()
    if isinstance(raw_items, list):
        for raw in raw_items[:200]:
            if not isinstance(raw, Mapping):
                untrusted_items.append(raw)
                continue
            item = dict(raw)
            details_url = str(item.get("detailsUrl") or item.get("url") or "").strip()
            if details_url:
                raw_urls.add(details_url)
            for key in ("app", "workflow", "workflowPath", "headSha", "headRefOid"):
                item.pop(key, None)
            untrusted_items.append(item)
    enriched["statusCheckRollup"] = untrusted_items

    head_sha = str(payload.get("headRefOid") or "").strip().lower()
    if not isinstance(raw_items, list) or not _SHA_RE.fullmatch(head_sha):
        return enriched

    errors: list[str] = []

    def record_error(label: str, detail: Any) -> None:
        safe = sanitize_closeout_error(detail) or "request failed without diagnostic output"
        errors.append(f"{label}: {safe}")

    try:
        check_runs_result = run(
            [
                "gh",
                "api",
                f"repos/{repo}/commits/{head_sha}/check-runs?filter=all&per_page=100",
            ],
            cwd=root,
            timeout=30,
            github=True,
        )
    except Exception as exc:
        record_error("Required check identity lookup failed", exc)
        check_runs_result = None
    if check_runs_result is None or check_runs_result.returncode != 0:
        if check_runs_result is not None:
            record_error(
                f"Required check identity lookup failed (exit {check_runs_result.returncode})",
                check_runs_result.stderr or check_runs_result.stdout,
            )
        enriched["_required_check_identity_error"] = sanitize_closeout_error("; ".join(errors))
        return enriched
    try:
        check_runs_payload = json.loads(check_runs_result.stdout or "{}")
    except json.JSONDecodeError as exc:
        record_error("Required check identity lookup returned invalid JSON", exc)
        enriched["_required_check_identity_error"] = sanitize_closeout_error("; ".join(errors))
        return enriched
    raw_check_runs = (
        check_runs_payload.get("check_runs")
        if isinstance(check_runs_payload, Mapping)
        else None
    )
    if not isinstance(raw_check_runs, list):
        record_error("Required check identity lookup returned no check-runs list", "invalid response")
        enriched["_required_check_identity_error"] = sanitize_closeout_error("; ".join(errors))
        return enriched

    required_by_name = {check: (workflow, check) for workflow, check in REQUIRED_PR_CHECKS}
    grouped: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {
        identity: {} for identity in REQUIRED_PR_CHECKS
    }
    for raw_check_run in raw_check_runs[:100]:
        if not isinstance(raw_check_run, Mapping):
            continue
        app = raw_check_run.get("app")
        app_slug = (
            str(app.get("slug") or "").strip().casefold()
            if isinstance(app, Mapping)
            else ""
        )
        run_head = str(raw_check_run.get("head_sha") or "").strip().lower()
        details_url = str(raw_check_run.get("details_url") or "").strip()
        check_name = str(raw_check_run.get("name") or "").strip()
        identity = required_by_name.get(check_name)
        match = re.fullmatch(
            r"https://github\.com/([^/]+/[^/]+)/actions/runs/(\d+)(?:/job/\d+)?/?",
            details_url,
            flags=re.IGNORECASE,
        )
        if (
            identity is None
            or app_slug != "github-actions"
            or run_head != head_sha
            or details_url not in raw_urls
            or not match
            or match.group(1).casefold() != repo.casefold()
        ):
            continue
        run_id = match.group(2)
        grouped[identity].setdefault(run_id, []).append(dict(raw_check_run))

    def check_run_sort_key(item: Mapping[str, Any]) -> tuple[int, str, str, str]:
        try:
            numeric_id = int(
                item.get("id")
                or item.get("database_id")
                or item.get("databaseId")
                or 0
            )
        except (TypeError, ValueError):
            numeric_id = 0
        return (
            numeric_id,
            str(item.get("created_at") or ""),
            str(item.get("started_at") or ""),
            str(item.get("details_url") or ""),
        )

    queues: dict[tuple[str, str], list[tuple[str, list[dict[str, Any]]]]] = {}
    for identity in REQUIRED_PR_CHECKS:
        run_groups = list(grouped[identity].items())
        for _run_id, check_runs in run_groups:
            check_runs.sort(key=check_run_sort_key, reverse=True)
        run_groups.sort(
            key=lambda entry: (check_run_sort_key(entry[1][0]), entry[0]),
            reverse=True,
        )
        queues[identity] = run_groups

    trusted_items: list[dict[str, Any]] = []
    resolved_identities: set[tuple[str, str]] = set()
    uncertain_identities: set[tuple[str, str]] = set()
    resolved_runs: dict[str, Mapping[str, Any]] = {}
    failed_runs: dict[str, str] = {}
    query_count = 0
    positions = {identity: 0 for identity in REQUIRED_PR_CHECKS}

    while query_count < 8:
        progressed = False
        for identity in REQUIRED_PR_CHECKS:
            if identity in resolved_identities or identity in uncertain_identities:
                continue
            position = positions[identity]
            if position >= len(queues[identity]):
                continue
            progressed = True
            run_id, check_runs = queues[identity][position]
            positions[identity] = position + 1
            if (
                run_id not in resolved_runs
                and run_id not in failed_runs
                and query_count >= 8
            ):
                positions[identity] = position
                continue
            if run_id not in resolved_runs and run_id not in failed_runs:
                query_count += 1
                try:
                    result = run(
                        ["gh", "api", f"repos/{repo}/actions/runs/{run_id}"],
                        cwd=root,
                        timeout=30,
                        github=True,
                    )
                except Exception as exc:
                    failed_runs[run_id] = sanitize_closeout_error(exc)
                else:
                    if result.returncode != 0:
                        failed_runs[run_id] = sanitize_closeout_error(
                            result.stderr or result.stdout or f"exit {result.returncode}"
                        )
                    else:
                        try:
                            candidate = json.loads(result.stdout or "{}")
                        except json.JSONDecodeError as exc:
                            failed_runs[run_id] = sanitize_closeout_error(exc)
                        else:
                            if isinstance(candidate, Mapping):
                                resolved_runs[run_id] = candidate
                            else:
                                failed_runs[run_id] = "workflow-run response was not an object"
            if run_id in failed_runs:
                record_error(
                    f"Required workflow identity lookup failed for {identity[0]} / {identity[1]}",
                    failed_runs[run_id],
                )
                uncertain_identities.add(identity)
                continue
            workflow_run = resolved_runs.get(run_id)
            if not isinstance(workflow_run, Mapping):
                continue
            workflow_path = str(workflow_run.get("path") or "").strip()
            workflow_head = str(workflow_run.get("head_sha") or "").strip().lower()
            if workflow_head != head_sha:
                record_error(
                    f"Required workflow identity head mismatch for {identity[0]} / {identity[1]}",
                    "workflow run did not match the current PR head",
                )
                uncertain_identities.add(identity)
                continue
            if workflow_path != _REQUIRED_PR_CHECK_WORKFLOWS[identity]:
                continue
            workflow, check = identity
            for check_run in check_runs:
                trusted_items.append(
                    {
                        "workflowName": workflow,
                        "name": check,
                        "status": str(check_run.get("status") or ""),
                        "conclusion": str(check_run.get("conclusion") or ""),
                        "headSha": head_sha,
                        "databaseId": check_run.get("id") or 0,
                        "startedAt": str(check_run.get("started_at") or ""),
                        "completedAt": str(check_run.get("completed_at") or ""),
                        "detailsUrl": str(check_run.get("details_url") or ""),
                        "app": {"name": "GitHub Actions", "slug": "github-actions"},
                        "workflow": {"path": workflow_path},
                    }
                )
            resolved_identities.add(identity)
        if not progressed:
            break

    budget_exhausted = any(
        identity not in resolved_identities
        and identity not in uncertain_identities
        and positions[identity] < len(queues[identity])
        for identity in REQUIRED_PR_CHECKS
    )
    if budget_exhausted:
        record_error(
            "Required workflow identity lookup budget exhausted",
            "more candidate workflow runs remained after 8 bounded queries",
        )

    enriched["statusCheckRollup"] = untrusted_items + trusted_items
    if errors:
        enriched["_required_check_identity_error"] = sanitize_closeout_error("; ".join(errors))
    return enriched


def _default_run(
    args: list[str],
    *,
    cwd: Path,
    timeout: int | float = 60,
    github: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=github_cli_env() if github else None,
    )


def _default_sync(canonical_path: str, branch: str, merge_sha: str) -> Any:
    from hermes_cli.canonical_checkout_sync import sync_protected_canonical_checkout

    return sync_protected_canonical_checkout(canonical_path, branch, merge_sha)


def _detail(result: subprocess.CompletedProcess[str], fallback: str) -> str:
    return sanitize_closeout_error(result.stderr or result.stdout or fallback)


def _append_error(state: dict[str, Any], *, code: str, message: Any, now: float) -> None:
    safe = sanitize_closeout_error(message)
    if not safe:
        return
    errors = list(state.get("errors") or [])[-(_MAX_ERRORS - 1) :]
    errors.append({"code": _safe_status(code, default="closeout_error"), "message": safe, "at": now})
    state["errors"] = errors


def _gate_passed(receipt: Mapping[str, Any], *, required: bool, head_sha: str) -> bool:
    if not required:
        return True
    status = str(receipt.get("status") or "").strip().lower()
    receipt_head = str(receipt.get("head_sha") or "").strip().lower()
    return status in {"passed", "approved", "success"} and bool(head_sha) and receipt_head == head_sha.lower()


def _invalidate_head_bound_state(state: dict[str, Any], *, old_head: str, new_head: str) -> None:
    if not old_head or old_head == new_head:
        return
    for key in ("local_verification", "review", "visual_qa"):
        receipt = state[key]
        if str(receipt.get("head_sha") or "").strip().lower() != new_head:
            state[key] = {"status": "stale"}
    state["ci"] = {
        "head_sha": new_head,
        "status": "not_checked",
        "total": 0,
        "failed": [],
        "wait_state": "queued",
        "required": [
            {"workflow": workflow, "check": check}
            for workflow, check in REQUIRED_PR_CHECKS
        ],
    }
    state["pr"]["merge_attempted_head_sha"] = ""
    state["pr"]["ready_at"] = None
    state["telemetry"]["green_unmerged_since"] = None
    state["telemetry"]["green_unmerged_overdue"] = False


def _update_green_unmerged_telemetry(
    state: dict[str, Any],
    *,
    eligible: bool,
    now: float,
    overdue_seconds: float,
) -> bool:
    """Update current-head green/unmerged telemetry and return first-green state."""

    telemetry = state["telemetry"]
    if not eligible:
        telemetry["green_unmerged_since"] = None
        telemetry["green_unmerged_overdue"] = False
        return False
    since = _safe_float(telemetry.get("green_unmerged_since"))
    newly_green = since is None
    if since is None:
        since = now
    raw_threshold = _safe_float(overdue_seconds)
    threshold = max(0.0, min(30 * 24 * 3600.0, raw_threshold or 0.0))
    telemetry["green_unmerged_since"] = since
    telemetry["green_unmerged_overdue"] = bool(
        threshold > 0 and now - since >= threshold
    )
    return newly_green


def _pr_ref(state: Mapping[str, Any]) -> str:
    pr = _record(state.get("pr"))
    return str(pr.get("url") or pr.get("number") or "").strip()


def _apply_pr_payload(state: dict[str, Any], payload: Mapping[str, Any]) -> str:
    """Apply one authoritative GitHub PR snapshot, including same-head changes."""

    pr = state["pr"]
    if payload.get("url"):
        pr["url"] = str(payload.get("url"))[:1200]
    if payload.get("number") is not None:
        pr["number"] = _bounded_text(payload.get("number"), limit=32)
    old_head = str(pr.get("head_sha") or "").strip().lower()
    new_head = str(payload.get("headRefOid") or "").strip().lower()
    if not _SHA_RE.fullmatch(new_head):
        raise ValueError("GitHub returned an invalid PR head SHA")
    _invalidate_head_bound_state(state, old_head=old_head, new_head=new_head)
    pr["head_sha"] = new_head
    pr["state"] = str(payload.get("state") or "unknown").upper()[:32]
    pr["is_draft"] = payload.get("isDraft") is True
    pr["merge_state"] = str(payload.get("mergeStateStatus") or "unknown").upper()[:48]
    pr["mergeable"] = payload.get("mergeable", "unknown")
    pr["review_decision"] = str(payload.get("reviewDecision") or "unknown").upper()[:48]
    merge_commit = payload.get("mergeCommit") if isinstance(payload.get("mergeCommit"), Mapping) else {}
    reported_merge_sha = str(merge_commit.get("oid") or "").strip().lower()
    pr["merge_sha"] = reported_merge_sha if _SHA_RE.fullmatch(reported_merge_sha) else ""
    state["ci"] = summarize_required_checks(payload.get("statusCheckRollup"), head_sha=new_head)
    return new_head


def _mergeability_disposition(value: Any) -> str:
    """Normalize GitHub mergeability to ready, pending, or blocked."""

    if value is True:
        return "ready"
    if value is False:
        return "blocked"
    normalized = str(value or "").strip().upper()
    if normalized in {"MERGEABLE", "TRUE"}:
        return "ready"
    if normalized in {"CONFLICTING", "FALSE", "NOT_MERGEABLE"}:
        return "blocked"
    return "pending"


def _transition(
    original: Mapping[str, Any],
    state: dict[str, Any],
    *,
    outcome: str,
    next_due_at: float | None,
    terminal: bool,
    wake_immediately: bool = False,
) -> CloseoutTransition:
    state["status"] = outcome
    state["next_due_at"] = next_due_at
    state["telemetry"]["last_transition"] = outcome
    comparable = copy.deepcopy(state)
    comparable["revision"] = original.get("revision", 0)
    changed = comparable != original
    if changed:
        state["revision"] = _safe_int(original.get("revision"), maximum=2_147_483_646) + 1
    return CloseoutTransition(
        state=state,
        outcome=outcome,
        next_due_at=next_due_at,
        terminal=terminal,
        changed=changed,
        wake_immediately=wake_immediately,
    )


def _blocked(
    original: Mapping[str, Any],
    state: dict[str, Any],
    *,
    code: str,
    message: Any,
    now: float,
    retry: bool = False,
    poll_seconds: float = 30.0,
) -> CloseoutTransition:
    _append_error(state, code=code, message=message, now=now)
    observational = state.get("mode") == "shadow"
    return _transition(
        original,
        state,
        outcome="pending" if retry or observational else "blocked",
        next_due_at=now + poll_seconds if retry or observational else None,
        terminal=False,
    )


def closeout_terminal_eligible(value: Any) -> bool:
    """Return whether enforced structured closeout permits terminal completion."""

    state = normalize_closeout_state(value)
    if state["status"] not in SUCCESS_CLOSEOUT_STATUSES:
        return False
    if state["mode"] == "off":
        return state["status"] == "not_required"
    if state["mode"] == "shadow":
        # Shadow may persist terminal observations, but never authorizes the
        # owning work item to complete or displaces its legacy finalizer.
        return False
    if state["status"] == "post_merge_complete":
        target = state["post_merge"]["target_sha"]
        if not target:
            return False
        for name, required in state["policy"]["post_merge_requirements"].items():
            if not required:
                continue
            receipt = state["post_merge"][name]
            if receipt.get("status") != "passed" or receipt.get("observed_sha") != target:
                return False
    return True


def _command_span_operation(args: list[str]) -> tuple[str, str]:
    """Return one allowlisted operation/phase without persisting command args."""

    tokens = [str(item or "").strip().lower() for item in args[:3]]
    first = tokens[0] if tokens else ""
    second = tokens[1] if len(tokens) > 1 else ""
    third = tokens[2] if len(tokens) > 2 else ""
    if first == "git":
        operation = f"git_{re.sub(r'[^a-z0-9_-]', '', second) or 'command'}"
    elif first == "gh":
        if second in {"run", "workflow", "check"}:
            operation = f"github_ci_{re.sub(r'[^a-z0-9_-]', '', third) or second}"
        elif second == "pr":
            operation = f"github_pr_{re.sub(r'[^a-z0-9_-]', '', third) or 'command'}"
        elif second == "auth":
            operation = "github_auth"
        else:
            operation = "github_command"
    else:
        operation = "closeout_command"
    return operation, classify_runtime_phase(operation)


def _reconcile_trusted_closeout_impl(
    value: Any,
    *,
    now: float | None = None,
    poll_seconds: float = 30.0,
    run: CommandRunner | None = None,
    sync_canonical: CanonicalSync | None = None,
    post_merge_config: Mapping[str, Any] | None = None,
    green_unmerged_overdue_seconds: float = 0.0,
    max_commands: int = _DEFAULT_MAX_COMMANDS,
    mutation_allowed: Callable[[], bool] | None = None,
    _span_recorder: RuntimeSpanRecorder | None = None,
    _span_parent_id: str = "",
    _span_attempt_id: str = "",
) -> CloseoutTransition:
    """Perform one bounded GitHub closeout reconciliation pass."""

    state = normalize_closeout_state(value)
    original = copy.deepcopy(state)
    current_time = float(time.time() if now is None else now)
    poll = max(1.0, min(3600.0, float(poll_seconds)))
    runner = run or _default_run
    command_count = 0
    command_count_lock = threading.Lock()
    ownership_allows_mutation = mutation_allowed or (lambda: True)

    def execute(
        args: list[str],
        *,
        cwd: Path,
        timeout: int | float = 60,
        github: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        nonlocal command_count
        if not ownership_allows_mutation():
            raise RuntimeError("closeout lease ownership lost")
        with command_count_lock:
            command_count += 1
            over_budget = command_count > max(1, min(_MAX_COMMANDS, int(max_commands)))
        if over_budget:
            raise RuntimeError("closeout command budget exceeded")
        return runner(args, cwd=cwd, timeout=timeout, github=github)

    if state["mode"] == "off":
        return _transition(original, state, outcome="not_required", next_due_at=None, terminal=True)

    workspace = state["workspace"]
    root_text = str(workspace.get("path") or "").strip()
    root = Path(root_text).expanduser().resolve(strict=False) if root_text else Path()
    if not root_text or not root.is_dir():
        return _blocked(
            original,
            state,
            code="workspace_unavailable",
            message="Mutable closeout workspace is unavailable",
            now=current_time,
        )
    branch = str(workspace.get("branch") or "").strip()
    base_branch = str(workspace.get("base_branch") or "main").strip() or "main"
    repo = str(workspace.get("repository") or "").strip()
    if not branch:
        return _blocked(original, state, code="branch_missing", message="Closeout branch is missing", now=current_time)

    remote_error = github_remote_preflight_error(root, operation="reconcile trusted closeout")
    if remote_error:
        return _blocked(original, state, code="github_remote_preflight", message=remote_error, now=current_time)
    origin_repo = github_origin_repo(root)
    if origin_repo:
        if repo and repo != origin_repo:
            return _blocked(
                original,
                state,
                code="origin_repository_mismatch",
                message="Configured closeout repository does not match the checkout origin",
                now=current_time,
            )
        repo = origin_repo
        workspace["repository"] = origin_repo
    if not repo:
        return _blocked(
            original,
            state,
            code="repository_missing",
            message="Closeout repository could not be resolved from the checkout origin",
            now=current_time,
        )

    try:
        auth = execute(["gh", "auth", "status"], cwd=root, timeout=30, github=True)
    except Exception as exc:
        return _blocked(original, state, code="github_auth_error", message=exc, now=current_time, retry=True, poll_seconds=poll)
    if auth.returncode != 0:
        return _blocked(
            original,
            state,
            code="github_auth_unavailable",
            message=_detail(auth, "GitHub CLI authentication is unavailable"),
            now=current_time,
            retry=True,
            poll_seconds=poll,
        )

    policy = state["policy"]
    if policy["pr_open"] == "never":
        return _transition(original, state, outcome="completed", next_due_at=None, terminal=True)

    pr_ref = _pr_ref(state)
    if not pr_ref:
        try:
            listed = execute(
                [
                    "gh",
                    "pr",
                    "list",
                    "--repo",
                    repo,
                    "--head",
                    branch,
                    "--base",
                    base_branch,
                    "--state",
                    "open",
                    "--json",
                    "number,url",
                    "--jq",
                    ".[0]",
                ],
                cwd=root,
                timeout=30,
                github=True,
            )
        except Exception as exc:
            return _blocked(original, state, code="pr_discovery_error", message=exc, now=current_time, retry=True, poll_seconds=poll)
        if listed.returncode != 0:
            return _blocked(
                original,
                state,
                code="pr_discovery_failed",
                message=_detail(listed, "PR discovery failed"),
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )
        try:
            listed_payload = json.loads(listed.stdout or "null")
        except json.JSONDecodeError:
            listed_payload = None
        if isinstance(listed_payload, Mapping):
            state["pr"]["url"] = str(listed_payload.get("url") or "")[:1200]
            state["pr"]["number"] = _bounded_text(listed_payload.get("number"), limit=32)
            pr_ref = _pr_ref(state)

    if not pr_ref:
        if state["mode"] == "shadow":
            # Shadow records the transition that enforcement would request but
            # never pushes or creates a pull request.
            return _transition(
                original,
                state,
                outcome="pr_pending",
                next_due_at=current_time + poll,
                terminal=False,
            )
        try:
            pushed = execute(
                ["git", "push", "-u", "origin", branch],
                cwd=root,
                timeout=300,
                github=True,
            )
        except Exception as exc:
            return _blocked(original, state, code="push_error", message=exc, now=current_time)
        if pushed.returncode != 0:
            return _blocked(original, state, code="push_failed", message=_detail(pushed, "git push failed"), now=current_time)
        create_args = [
            "gh",
            "pr",
            "create",
            "--repo",
            repo,
            "--base",
            base_branch,
            "--head",
            branch,
            "--title",
            state["pr"]["title"] or f"Closeout {branch}",
            "--body",
            state["pr"]["body"] or "Trusted Hermes closeout.",
        ]
        if policy["early_draft_pr"]:
            create_args.append("--draft")
        try:
            created = execute(create_args, cwd=root, timeout=120, github=True)
        except Exception as exc:
            return _blocked(original, state, code="pr_create_error", message=exc, now=current_time)
        if created.returncode != 0:
            return _blocked(original, state, code="pr_create_failed", message=_detail(created, "PR creation failed"), now=current_time)
        url = next((line.strip() for line in reversed((created.stdout or "").splitlines()) if line.strip()), "")
        if not url:
            return _blocked(original, state, code="pr_create_missing_url", message="PR creation returned no URL", now=current_time)
        state["pr"]["url"] = url[:1200]
        return _transition(
            original,
            state,
            outcome="pr_pending",
            next_due_at=current_time,
            terminal=False,
            wake_immediately=True,
        )

    try:
        viewed = execute(
            [
                "gh",
                "pr",
                "view",
                pr_ref,
                "--repo",
                repo,
                "--json",
                "number,url,state,headRefOid,mergedAt,mergeCommit,mergeStateStatus,mergeable,isDraft,reviewDecision,statusCheckRollup",
            ],
            cwd=root,
            timeout=60,
            github=True,
        )
    except Exception as exc:
        return _blocked(original, state, code="pr_refresh_error", message=exc, now=current_time, retry=True, poll_seconds=poll)
    if viewed.returncode != 0:
        return _blocked(
            original,
            state,
            code="pr_refresh_failed",
            message=_detail(viewed, "PR refresh failed"),
            now=current_time,
            retry=True,
            poll_seconds=poll,
        )
    try:
        payload = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return _blocked(original, state, code="pr_refresh_invalid_json", message=exc, now=current_time, retry=True, poll_seconds=poll)
    if not isinstance(payload, Mapping):
        return _blocked(original, state, code="pr_refresh_invalid_payload", message="PR refresh returned non-object JSON", now=current_time, retry=True, poll_seconds=poll)
    payload = enrich_required_check_identities(
        payload,
        repo=repo,
        root=root,
        run=execute,
    )
    identity_error = sanitize_closeout_error(
        payload.pop("_required_check_identity_error", "")
    )
    if identity_error:
        return _blocked(
            original,
            state,
            code="required_check_identity_failed",
            message=identity_error,
            now=current_time,
            retry=True,
            poll_seconds=poll,
        )

    pr = state["pr"]
    try:
        new_head = _apply_pr_payload(state, payload)
    except ValueError as exc:
        return _blocked(
            original,
            state,
            code="invalid_pr_head",
            message=exc,
            now=current_time,
            retry=True,
            poll_seconds=poll,
        )

    if pr["state"] == "MERGED":
        _update_green_unmerged_telemetry(
            state,
            eligible=False,
            now=current_time,
            overdue_seconds=green_unmerged_overdue_seconds,
        )
        merge_sha = pr["merge_sha"]
        if not merge_sha:
            return _blocked(
                original,
                state,
                code="merge_sha_missing",
                message="Merged PR did not report an exact merge SHA",
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )

        from hermes_cli.post_merge_receipts import (
            collect_post_merge_receipts,
            initialize_post_merge_receipts,
        )

        # The independently reported merge SHA must reach durable state before
        # any collector starts. A subsequent reconciliation pass performs the
        # concurrent collection and returns one gathered atomic update.
        persisted_target = str(original["post_merge"].get("target_sha") or "").strip().lower()
        if persisted_target != merge_sha:
            state["post_merge"] = initialize_post_merge_receipts(state, target_sha=merge_sha)
            state["canonical_sync"] = dict(state["post_merge"]["canonical_sync"])
            return _transition(
                original,
                state,
                outcome="post_merge_pending",
                next_due_at=current_time,
                terminal=False,
                wake_immediately=True,
            )

        state["post_merge"] = collect_post_merge_receipts(
            state,
            config=post_merge_config,
            run=execute,
            sync_canonical=sync_canonical,
            now=current_time,
            read_only=state["mode"] != "enforce",
            mutation_allowed=ownership_allows_mutation,
            span_recorder=_span_recorder,
            span_parent_id=_span_parent_id,
            span_attempt_id=_span_attempt_id,
        )
        state["canonical_sync"] = dict(state["post_merge"]["canonical_sync"])
        required_post_merge = policy["post_merge_requirements"]
        pending_receipts: list[str] = []
        failed_receipts: list[str] = []
        blocked_receipts: list[str] = []
        for name, required in required_post_merge.items():
            if not required:
                continue
            receipt = state["post_merge"][name]
            receipt_status = str(receipt.get("status") or "").strip().lower()
            if state["mode"] != "enforce":
                if receipt_status not in {"passed", "failed", "blocked", "not_configured"}:
                    pending_receipts.append(name)
                continue
            if receipt_status == "passed":
                if receipt.get("observed_sha") != merge_sha:
                    failed_receipts.append(name)
            elif receipt_status == "failed":
                failed_receipts.append(name)
            elif receipt_status in {"blocked", "not_configured"}:
                blocked_receipts.append(name)
            else:
                pending_receipts.append(name)
        if blocked_receipts:
            _append_error(
                state,
                code="post_merge_receipt_blocked",
                message="Required post-merge receipt is blocked: " + ", ".join(blocked_receipts),
                now=current_time,
            )
            return _transition(
                original,
                state,
                outcome="blocked",
                next_due_at=None,
                terminal=True,
            )
        if failed_receipts:
            _append_error(
                state,
                code="post_merge_receipt_failed",
                message="Required post-merge receipt failed: " + ", ".join(failed_receipts),
                now=current_time,
            )
            return _transition(
                original,
                state,
                outcome="repair_required",
                next_due_at=None,
                terminal=True,
            )
        if pending_receipts:
            return _transition(
                original,
                state,
                outcome="post_merge_pending",
                next_due_at=current_time + poll,
                terminal=False,
            )
        return _transition(original, state, outcome="post_merge_complete", next_due_at=None, terminal=True)

    if pr["state"] not in {"OPEN", "UNKNOWN", ""}:
        _update_green_unmerged_telemetry(
            state,
            eligible=False,
            now=current_time,
            overdue_seconds=green_unmerged_overdue_seconds,
        )
        return _blocked(original, state, code="pr_not_open", message=f"PR state is {pr['state'] or 'unknown'}", now=current_time)
    if pr["review_decision"] == "CHANGES_REQUESTED":
        _update_green_unmerged_telemetry(
            state,
            eligible=False,
            now=current_time,
            overdue_seconds=green_unmerged_overdue_seconds,
        )
        return _blocked(original, state, code="changes_requested", message="PR has requested review changes", now=current_time)

    local_ok = _gate_passed(
        state["local_verification"],
        required=policy["require_local_verification"],
        head_sha=new_head,
    )
    review_ok = _gate_passed(state["review"], required=policy["require_review"], head_sha=new_head)
    visual_ok = _gate_passed(state["visual_qa"], required=policy["require_visual_qa"], head_sha=new_head)
    ci_status = state["ci"]["status"]
    gates_ok = local_ok and review_ok and visual_ok and ci_status == "passed"

    if not gates_ok:
        _update_green_unmerged_telemetry(
            state,
            eligible=False,
            now=current_time,
            overdue_seconds=green_unmerged_overdue_seconds,
        )
        if ci_status == "failed":
            _append_error(
                state,
                code="required_checks_failed",
                message="Current-head required CI checks failed and require repair or rerun",
                now=current_time,
            )
            if state["mode"] == "shadow":
                return _transition(
                    original,
                    state,
                    outcome="waiting_for_ci",
                    next_due_at=current_time + poll,
                    terminal=False,
                )
            return _transition(
                original,
                state,
                outcome="repair_required",
                next_due_at=None,
                terminal=True,
            )
        outcome = "waiting_for_gates" if ci_status == "passed" else "waiting_for_ci"
        return _transition(original, state, outcome=outcome, next_due_at=current_time + poll, terminal=False)

    if pr["is_draft"]:
        _update_green_unmerged_telemetry(
            state,
            eligible=False,
            now=current_time,
            overdue_seconds=green_unmerged_overdue_seconds,
        )
        if state["mode"] == "shadow":
            return _transition(
                original,
                state,
                outcome="ready_pending",
                next_due_at=current_time + poll,
                terminal=False,
            )
        try:
            readied = execute(
                ["gh", "pr", "ready", _pr_ref(state), "--repo", repo],
                cwd=root,
                timeout=60,
                github=True,
            )
        except Exception as exc:
            return _blocked(original, state, code="pr_ready_error", message=exc, now=current_time, retry=True, poll_seconds=poll)
        if readied.returncode != 0:
            return _blocked(
                original,
                state,
                code="pr_ready_failed",
                message=_detail(readied, "PR ready transition failed"),
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )
        pr["is_draft"] = False
        pr["ready_at"] = current_time
        return _transition(
            original,
            state,
            outcome="ready_pending",
            next_due_at=current_time,
            terminal=False,
            wake_immediately=True,
        )

    merge_policy = policy["merge"]
    if merge_policy in {"manual", "never"}:
        _update_green_unmerged_telemetry(
            state,
            eligible=False,
            now=current_time,
            overdue_seconds=green_unmerged_overdue_seconds,
        )
        return _transition(original, state, outcome="pr_open", next_due_at=None, terminal=True)

    newly_green = _update_green_unmerged_telemetry(
        state,
        eligible=True,
        now=current_time,
        overdue_seconds=green_unmerged_overdue_seconds,
    )
    merge_state = pr["merge_state"]
    mergeability = _mergeability_disposition(pr.get("mergeable"))
    if merge_state in {"", "UNKNOWN", "UNSTABLE"} or mergeability == "pending":
        return _transition(
            original,
            state,
            outcome="waiting_for_mergeability",
            next_due_at=current_time if newly_green else current_time + poll,
            terminal=False,
            wake_immediately=newly_green,
        )
    if merge_state not in {"CLEAN", "HAS_HOOKS"} or mergeability == "blocked":
        return _blocked(
            original,
            state,
            code="mergeability_blocked",
            message=f"PR merge state is {merge_state}; mergeable is {pr.get('mergeable')}",
            now=current_time,
            poll_seconds=poll,
        )

    if state["mode"] == "shadow":
        return _transition(
            original,
            state,
            outcome="pending",
            next_due_at=current_time + poll,
            terminal=False,
        )

    # Refresh the authoritative head immediately before the merge mutation. The
    # exact-head guard below protects the remaining race between this read and
    # GitHub accepting the merge request.
    try:
        premerge = execute(
            [
                "gh",
                "pr",
                "view",
                _pr_ref(state),
                "--repo",
                repo,
                "--json",
                "number,url,state,headRefOid,mergedAt,mergeCommit,mergeStateStatus,mergeable,isDraft,reviewDecision,statusCheckRollup",
            ],
            cwd=root,
            timeout=60,
            github=True,
        )
    except Exception as exc:
        return _blocked(original, state, code="premerge_refresh_error", message=exc, now=current_time, retry=True, poll_seconds=poll)
    if premerge.returncode != 0:
        return _blocked(
            original,
            state,
            code="premerge_refresh_failed",
            message=_detail(premerge, "Pre-merge PR refresh failed"),
            now=current_time,
            retry=True,
            poll_seconds=poll,
        )
    try:
        premerge_payload = json.loads(premerge.stdout or "{}")
    except json.JSONDecodeError as exc:
        return _blocked(original, state, code="premerge_refresh_invalid_json", message=exc, now=current_time, retry=True, poll_seconds=poll)
    if not isinstance(premerge_payload, Mapping):
        return _blocked(original, state, code="premerge_refresh_invalid_payload", message="Pre-merge PR refresh returned non-object JSON", now=current_time, retry=True, poll_seconds=poll)
    premerge_payload = enrich_required_check_identities(
        premerge_payload,
        repo=repo,
        root=root,
        run=execute,
    )
    identity_error = sanitize_closeout_error(
        premerge_payload.pop("_required_check_identity_error", "")
    )
    if identity_error:
        return _blocked(
            original,
            state,
            code="premerge_required_check_identity_failed",
            message=identity_error,
            now=current_time,
            retry=True,
            poll_seconds=poll,
        )
    try:
        refreshed_head = _apply_pr_payload(state, premerge_payload)
    except ValueError as exc:
        return _blocked(
            original,
            state,
            code="invalid_premerge_head",
            message=exc,
            now=current_time,
            retry=True,
            poll_seconds=poll,
        )

    # Another actor may merge after the initial OPEN snapshot but before this
    # authoritative pre-mutation refresh. Treat an exact merge commit as a
    # successful externally completed merge and enter normal post-merge pickup.
    if pr["state"] == "MERGED":
        merge_sha = str(pr.get("merge_sha") or "").strip().lower()
        if not _SHA_RE.fullmatch(merge_sha):
            return _blocked(
                original,
                state,
                code="merge_sha_missing",
                message="Pre-merge refresh reported MERGED without an exact merge SHA",
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )
        from hermes_cli.post_merge_receipts import initialize_post_merge_receipts

        state["post_merge"] = initialize_post_merge_receipts(state, target_sha=merge_sha)
        state["canonical_sync"] = dict(state["post_merge"]["canonical_sync"])
        _update_green_unmerged_telemetry(
            state,
            eligible=False,
            now=current_time,
            overdue_seconds=green_unmerged_overdue_seconds,
        )
        return _transition(
            original,
            state,
            outcome="post_merge_pending",
            next_due_at=current_time,
            terminal=False,
            wake_immediately=True,
        )

    # Re-evaluate every gate from the just-applied snapshot. This is required
    # even when the head SHA did not change because draft/review/check/merge
    # state can change independently on GitHub.
    if pr["state"] != "OPEN":
        return _blocked(
            original,
            state,
            code="premerge_pr_not_open",
            message=f"Pre-merge PR state is {pr['state'] or 'unknown'}",
            now=current_time,
            poll_seconds=poll,
        )
    if pr["is_draft"]:
        return _transition(
            original,
            state,
            outcome="ready_pending",
            next_due_at=current_time + poll,
            terminal=False,
        )
    if pr["review_decision"] == "CHANGES_REQUESTED":
        return _blocked(
            original,
            state,
            code="premerge_changes_requested",
            message="Pre-merge review decision requests changes",
            now=current_time,
            poll_seconds=poll,
        )
    refreshed_local_ok = _gate_passed(
        state["local_verification"],
        required=policy["require_local_verification"],
        head_sha=refreshed_head,
    )
    refreshed_review_ok = _gate_passed(
        state["review"],
        required=policy["require_review"],
        head_sha=refreshed_head,
    )
    refreshed_visual_ok = _gate_passed(
        state["visual_qa"],
        required=policy["require_visual_qa"],
        head_sha=refreshed_head,
    )
    refreshed_ci_status = state["ci"]["status"]
    if not (refreshed_local_ok and refreshed_review_ok and refreshed_visual_ok):
        return _transition(
            original,
            state,
            outcome="waiting_for_gates",
            next_due_at=current_time + poll,
            terminal=False,
        )
    if refreshed_ci_status == "failed":
        _append_error(
            state,
            code="required_checks_failed",
            message="Latest pre-merge required CI checks failed and require repair or rerun",
            now=current_time,
        )
        return _transition(
            original,
            state,
            outcome="repair_required",
            next_due_at=None,
            terminal=True,
        )
    if refreshed_ci_status != "passed":
        return _transition(
            original,
            state,
            outcome="waiting_for_ci",
            next_due_at=current_time + poll,
            terminal=False,
        )
    refreshed_mergeability = _mergeability_disposition(pr.get("mergeable"))
    if pr["merge_state"] in {"", "UNKNOWN", "UNSTABLE"} or refreshed_mergeability == "pending":
        return _transition(
            original,
            state,
            outcome="waiting_for_mergeability",
            next_due_at=current_time + poll,
            terminal=False,
        )
    if pr["merge_state"] not in {"CLEAN", "HAS_HOOKS"} or refreshed_mergeability == "blocked":
        return _blocked(
            original,
            state,
            code="premerge_mergeability_blocked",
            message=(
                f"Pre-merge state is {pr['merge_state']}; "
                f"mergeable is {pr.get('mergeable')}"
            ),
            now=current_time,
            poll_seconds=poll,
        )

    pr["merge_attempted_head_sha"] = refreshed_head
    try:
        merged = execute(
            [
                "gh",
                "pr",
                "merge",
                _pr_ref(state),
                "--repo",
                repo,
                "--merge",
                "--delete-branch",
                "--match-head-commit",
                refreshed_head,
            ],
            cwd=root,
            timeout=300,
            github=True,
        )
    except Exception as exc:
        return _blocked(original, state, code="pr_merge_error", message=exc, now=current_time, retry=True, poll_seconds=poll)
    if merged.returncode != 0:
        return _blocked(
            original,
            state,
            code="pr_merge_failed",
            message=_detail(merged, "PR merge failed"),
            now=current_time,
            retry=True,
            poll_seconds=poll,
        )
    return _transition(
        original,
        state,
        outcome="pending",
        next_due_at=current_time,
        terminal=False,
        wake_immediately=True,
    )


def reconcile_trusted_closeout(
    value: Any,
    *,
    now: float | None = None,
    poll_seconds: float = 30.0,
    run: CommandRunner | None = None,
    sync_canonical: CanonicalSync | None = None,
    post_merge_config: Mapping[str, Any] | None = None,
    green_unmerged_overdue_seconds: float = 0.0,
    max_commands: int = _DEFAULT_MAX_COMMANDS,
    mutation_allowed: Callable[[], bool] | None = None,
) -> CloseoutTransition:
    """Perform one bounded pass and attach trusted closeout/runtime spans."""

    normalized = normalize_closeout_state(value)
    recorder = RuntimeSpanRecorder(
        work_id=str(normalized.get("id") or "closeout"),
        max_spans=80,
    )
    attempt_id = f"revision-{int(normalized.get('revision') or 0) + 1}"
    mode = str(normalized.get("mode") or "off")
    source = str(normalized.get("source") or "direct")
    repository = str(normalized.get("workspace", {}).get("repository") or "")
    pass_span = recorder.start(
        "trusted_closeout",
        phase="closeout",
        attempt_id=attempt_id,
        metadata={
            "operation": "trusted_closeout",
            "mode": mode,
            "source": source,
            "repository": repository,
        },
    )
    base_runner = run or _default_run

    def instrumented_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: int | float = 60,
        github: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        operation, phase = _command_span_operation(args)
        handle = recorder.start(
            operation,
            phase=phase,
            parent_id=pass_span.id,
            attempt_id=attempt_id,
            concurrency_id=f"closeout-{attempt_id}",
            metadata={"operation": operation, "repository": repository},
        )
        try:
            result = base_runner(
                args,
                cwd=cwd,
                timeout=timeout,
                github=github,
            )
        except subprocess.TimeoutExpired:
            recorder.finish(handle, status="timeout")
            raise
        except Exception:
            recorder.finish(handle, status="error")
            raise
        recorder.finish(
            handle,
            status="ok" if int(getattr(result, "returncode", 1) or 0) == 0 else "error",
        )
        return result

    try:
        transition = _reconcile_trusted_closeout_impl(
            normalized,
            now=now,
            poll_seconds=poll_seconds,
            run=instrumented_run,
            sync_canonical=sync_canonical,
            post_merge_config=post_merge_config,
            green_unmerged_overdue_seconds=green_unmerged_overdue_seconds,
            max_commands=max_commands,
            mutation_allowed=mutation_allowed,
            _span_recorder=recorder,
            _span_parent_id=pass_span.id,
            _span_attempt_id=attempt_id,
        )
    except Exception:
        recorder.finish(pass_span, status="error")
        raise
    recorder.finish(
        pass_span,
        status=(
            "blocked"
            if transition.outcome in {"blocked", "repair_required"}
            else "ok"
        ),
        metadata={"outcome": transition.outcome},
    )

    state = copy.deepcopy(transition.state)
    telemetry = state.get("telemetry") if isinstance(state.get("telemetry"), dict) else {}
    prior_spans = normalized.get("telemetry", {}).get("phase_spans", [])
    combined_spans = sanitize_runtime_spans(
        [*prior_spans, *recorder.export()],
        max_spans=120,
    )
    spans_changed = combined_spans != telemetry.get("phase_spans", [])
    telemetry["phase_spans"] = combined_spans
    state["telemetry"] = telemetry
    changed = transition.changed or spans_changed
    if spans_changed and not transition.changed:
        state["revision"] = _safe_int(
            normalized.get("revision"),
            maximum=2_147_483_646,
        ) + 1
    return CloseoutTransition(
        state=state,
        outcome=transition.outcome,
        next_due_at=transition.next_due_at,
        terminal=transition.terminal,
        changed=changed,
        wake_immediately=transition.wake_immediately,
    )


__all__ = [
    "CLOSEOUT_MODES",
    "CloseoutTransition",
    "REQUIRED_PR_CHECKS",
    "TERMINAL_CLOSEOUT_STATUSES",
    "closeout_terminal_eligible",
    "enrich_required_check_identities",
    "latest_logical_checks",
    "normalize_closeout_state",
    "reconcile_trusted_closeout",
    "sanitize_closeout_error",
    "summarize_required_checks",
]
