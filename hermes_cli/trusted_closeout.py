"""Storage-neutral, bounded trusted GitHub closeout reconciliation.

The engine performs one synchronous reconciliation pass.  It owns no durable
storage, never sleeps, and never calls a model.  Callers persist the normalized
state and schedule another pass at ``next_due_at`` when needed.
"""

from __future__ import annotations

import copy
import datetime as dt
import inspect
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
from hermes_cli.closeout_execution import (
    CommandEffect,
    RemoteMutationUncertain,
    classify_closeout_command,
    run_closeout_command,
)
from hermes_cli.github_remote import (
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


def _repo_uses_trusted_required_checks(root: Path) -> bool:
    """Return whether this repository opts into Hermes' named CI gates.

    The trusted closeout engine is also used for external project repositories.
    Requiring Hermes-specific workflow/check names there leaves closeout waiting
    forever for checks that repository can never produce.  An existing workflow
    directory with none of the trusted workflow files is an explicit opt-out.
    A missing directory retains the historical behavior for callers/tests that
    operate before a checkout is fully materialized.
    """

    workflows = root / ".github" / "workflows"
    if not workflows.is_dir():
        return True
    return any((root / path).is_file() for path in _REQUIRED_PR_CHECK_WORKFLOWS.values())


def repo_uses_trusted_required_checks(root: Path) -> bool:
    """Return whether this checkout requires Hermes' named CI identities."""

    return _repo_uses_trusted_required_checks(root)


CLOSEOUT_MODES = frozenset({"off", "shadow", "enforce"})
PR_OPEN_POLICIES = frozenset({"after_review_approval", "never"})
SUCCESS_CLOSEOUT_STATUSES = frozenset(
    {"completed", "not_required", "pr_open", "pr_published"}
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
        "waiting_for_preview",
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
_DEFAULT_MAX_COMMANDS = 26
_MAX_COMMANDS = 32
_CHECK_RUNS_PER_PAGE = 100
_MAX_CHECK_RUN_PAGES = 2

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
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


def _normalize_mutation_uncertainty(value: Any) -> dict[str, Any]:
    raw = _record(value)
    if str(raw.get("status") or "").strip().lower() != "uncertain":
        return {"status": "none"}
    result: dict[str, Any] = {
        "status": "uncertain",
        "operation": _safe_status(raw.get("operation"), default="remote_mutation")[:80],
    }
    at = _safe_float(raw.get("at"))
    if at is not None:
        result["at"] = at
    head_sha = str(raw.get("head_sha") or "").strip().lower()
    if _SHA_RE.fullmatch(head_sha):
        result["head_sha"] = head_sha
    for key in ("branch", "base_branch", "repository"):
        text = _bounded_text(raw.get(key), limit=240)
        if text:
            result[key] = text
    try:
        baseline_pid = int(raw.get("baseline_pid"))
        baseline_start = int(raw.get("baseline_start_time"))
        if baseline_pid > 0 and baseline_start >= 0:
            result["baseline_pid"] = baseline_pid
            result["baseline_start_time"] = baseline_start
    except (TypeError, ValueError):
        pass
    return result


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


def _normalize_preview(value: Any) -> dict[str, Any]:
    raw = _record(value)
    observed_sha = str(raw.get("observed_sha") or "").strip().lower()
    status = _safe_status(raw.get("status"), default="not_checked")
    url = str(raw.get("url") or "").strip()
    return {
        "provider": "vercel",
        "status": status,
        "observed_sha": observed_sha if _SHA_RE.fullmatch(observed_sha) else "",
        "url": url[:1200] if url.startswith("https://") else "",
        "deployment_id": _bounded_text(raw.get("deployment_id"), limit=40),
        "diagnostic_code": _safe_status(
            raw.get("diagnostic_code"),
            default="",
        ),
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
    pr_open_policy = str(policy.get("pr_open") or "after_review_approval").strip().lower()
    if pr_open_policy not in PR_OPEN_POLICIES:
        pr_open_policy = "after_review_approval"

    raw_required_checks = ci.get("required") if isinstance(ci.get("required"), list) else []
    required_checks = [
        {
            "workflow": _bounded_text(item.get("workflow"), limit=160),
            "check": _bounded_text(item.get("check"), limit=160),
        }
        for item in raw_required_checks[:100]
        if isinstance(item, Mapping) and _bounded_text(item.get("check"), limit=160)
    ] or [
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
    pending_push_head_sha = str(pr.get("pending_push_head_sha") or "").strip().lower()
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
            # Discord closeout is publication-only. Legacy persisted auto/manual
            # values normalize to never so an upgrade cannot merge in-flight PRs.
            "merge": "never",
            "pr_open": pr_open_policy,
            "early_draft_pr": policy.get("early_draft_pr") is True,
            "require_preview": policy.get("require_preview") is True,
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
            "pending_push_head_sha": (
                pending_push_head_sha if _SHA_RE.fullmatch(pending_push_head_sha) else ""
            ),
        },
        "ci": {
            "head_sha": ci_head_sha if _SHA_RE.fullmatch(ci_head_sha) else "",
            "status": _safe_status(ci.get("status"), default="not_checked"),
            "total": _safe_int(ci.get("total"), maximum=100),
            "failed": failed,
            "wait_state": _safe_status(ci.get("wait_state"), default="queued"),
            "required": required_checks,
        },
        "preview": _normalize_preview(raw.get("preview")),
        "canonical_sync": _receipt(raw.get("canonical_sync"), default="not_started"),
        "post_merge": _normalize_post_merge(raw.get("post_merge")),
        "mutation_uncertainty": _normalize_mutation_uncertainty(
            raw.get("mutation_uncertainty")
        ),
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
        "lease_generation": _safe_int(
            raw.get("lease_generation"),
            maximum=2_147_483_647,
        ),
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

    def fail_identity_lookup() -> dict[str, Any]:
        enriched["_required_check_identity_error"] = sanitize_closeout_error(
            "; ".join(errors)
        )
        return enriched

    collected_check_runs: list[dict[str, Any]] = []
    expected_total: int | None = None
    total_count_present: bool | None = None
    collected_count = 0
    for page in range(1, _MAX_CHECK_RUN_PAGES + 1):
        endpoint = (
            f"repos/{repo}/commits/{head_sha}/"
            f"check-runs?filter=all&per_page={_CHECK_RUNS_PER_PAGE}"
        )
        if page > 1:
            endpoint += f"&page={page}"
        try:
            check_runs_result = run(
                ["gh", "api", endpoint],
                cwd=root,
                timeout=30,
                github=True,
            )
        except Exception as exc:
            record_error("Required check identity lookup failed", exc)
            return fail_identity_lookup()
        if check_runs_result.returncode != 0:
            record_error(
                f"Required check identity lookup failed (exit {check_runs_result.returncode})",
                check_runs_result.stderr or check_runs_result.stdout,
            )
            return fail_identity_lookup()
        try:
            check_runs_payload = json.loads(check_runs_result.stdout or "{}")
        except json.JSONDecodeError as exc:
            record_error("Required check identity lookup returned invalid JSON", exc)
            return fail_identity_lookup()
        if not isinstance(check_runs_payload, Mapping):
            record_error(
                "Required check identity lookup returned an invalid page",
                "response was not an object",
            )
            return fail_identity_lookup()
        page_check_runs = check_runs_payload.get("check_runs")
        if not isinstance(page_check_runs, list):
            record_error(
                "Required check identity lookup returned no check-runs list",
                "invalid response",
            )
            return fail_identity_lookup()
        if len(page_check_runs) > _CHECK_RUNS_PER_PAGE:
            record_error(
                "Required check identity pagination was inconsistent",
                "a page exceeded the requested page size",
            )
            return fail_identity_lookup()
        if any(not isinstance(item, Mapping) for item in page_check_runs):
            record_error(
                "Required check identity lookup returned an invalid check-run",
                "check-runs entries must be objects",
            )
            return fail_identity_lookup()

        page_has_total = "total_count" in check_runs_payload
        if total_count_present is None:
            total_count_present = page_has_total
        elif total_count_present != page_has_total:
            record_error(
                "Required check identity pagination was inconsistent",
                "total_count presence changed between pages",
            )
            return fail_identity_lookup()
        if page_has_total:
            raw_total = check_runs_payload.get("total_count")
            if (
                isinstance(raw_total, bool)
                or not isinstance(raw_total, int)
                or raw_total < 0
            ):
                record_error(
                    "Required check identity pagination returned invalid total_count",
                    "total_count must be a non-negative integer",
                )
                return fail_identity_lookup()
            total_count = raw_total
            if expected_total is None:
                expected_total = total_count
            elif expected_total != total_count:
                record_error(
                    "Required check identity pagination was inconsistent",
                    "total_count changed between pages",
                )
                return fail_identity_lookup()

        collected_check_runs.extend(dict(item) for item in page_check_runs)
        collected_count += len(page_check_runs)
        if expected_total is not None:
            if collected_count > expected_total:
                record_error(
                    "Required check identity pagination was inconsistent",
                    "pages contained more entries than total_count",
                )
                return fail_identity_lookup()
            remaining = expected_total - collected_count
            if remaining == 0:
                break
            if len(page_check_runs) < _CHECK_RUNS_PER_PAGE:
                record_error(
                    "Required check identity pagination was inconsistent",
                    "a non-final page was shorter than the requested page size",
                )
                return fail_identity_lookup()
        elif len(page_check_runs) < _CHECK_RUNS_PER_PAGE:
            break

        if page == _MAX_CHECK_RUN_PAGES:
            record_error(
                "Required check identity pagination budget exhausted",
                f"more than {_MAX_CHECK_RUN_PAGES} check-run pages were required",
            )
            return fail_identity_lookup()

    deduplicated_check_runs: dict[int, dict[str, Any]] = {}
    for raw_check_run in collected_check_runs:
        raw_id = raw_check_run.get("id")
        if isinstance(raw_id, bool):
            check_run_id = 0
        else:
            try:
                check_run_id = int(raw_id)
            except (TypeError, ValueError):
                check_run_id = 0
        if check_run_id <= 0:
            record_error(
                "Required check identity lookup returned an invalid check-run ID",
                "check-run IDs must be positive integers",
            )
            return fail_identity_lookup()
        prior = deduplicated_check_runs.get(check_run_id)
        current = dict(raw_check_run)
        if prior is not None and prior != current:
            record_error(
                "Required check identity pagination was inconsistent",
                "a duplicate check-run ID had conflicting data",
            )
            return fail_identity_lookup()
        deduplicated_check_runs[check_run_id] = current
    if (
        expected_total is not None
        and len(deduplicated_check_runs) != expected_total
    ):
        record_error(
            "Required check identity pagination was inconsistent",
            "deduplicated check-run count did not match total_count",
        )
        return fail_identity_lookup()
    raw_check_runs = [
        deduplicated_check_runs[check_run_id]
        for check_run_id in sorted(deduplicated_check_runs)
    ]

    required_by_name = {check: (workflow, check) for workflow, check in REQUIRED_PR_CHECKS}
    grouped: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = {
        identity: {} for identity in REQUIRED_PR_CHECKS
    }
    for raw_check_run in raw_check_runs:
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
    control: Any | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_closeout_command(
        args,
        cwd=cwd,
        timeout=timeout,
        github=github,
        control=control,
    )


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
    state["preview"] = {
        "provider": "vercel",
        "status": "not_checked",
        "observed_sha": new_head,
        "url": "",
        "deployment_id": "",
        "diagnostic_code": "",
    }
    state["pr"]["merge_attempted_head_sha"] = ""
    state["pr"]["ready_at"] = None
    state["telemetry"]["green_unmerged_since"] = None
    state["telemetry"]["green_unmerged_overdue"] = False


def _pr_ref(state: Mapping[str, Any]) -> str:
    pr = _record(state.get("pr"))
    return str(pr.get("url") or pr.get("number") or "").strip()


def _apply_pr_payload(
    state: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    require_trusted_checks: bool = True,
) -> str:
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
    if require_trusted_checks:
        state["ci"] = summarize_required_checks(payload.get("statusCheckRollup"), head_sha=new_head)
    else:
        state["ci"] = {
            "head_sha": new_head,
            "status": "passed",
            "total": 0,
            "failed": [],
            "wait_state": "not_required",
            "required": [],
        }
    return new_head


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
    if state["status"] in {"pr_open", "pr_published"} and state["policy"]["require_preview"]:
        preview = state["preview"]
        if (
            preview.get("status") != "ready"
            or not preview.get("url")
            or preview.get("observed_sha") != state["pr"].get("head_sha")
        ):
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
    max_commands: int = _DEFAULT_MAX_COMMANDS,
    mutation_allowed: Callable[[], bool] | None = None,
    mutation_started: Callable[[str, Mapping[str, Any]], bool] | None = None,
    control: Any | None = None,
    _span_recorder: RuntimeSpanRecorder | None = None,
    _span_parent_id: str = "",
    _span_attempt_id: str = "",
) -> CloseoutTransition:
    """Perform one bounded GitHub closeout reconciliation pass."""

    state = normalize_closeout_state(value)
    state["telemetry"]["green_unmerged_since"] = None
    state["telemetry"]["green_unmerged_overdue"] = False
    original = copy.deepcopy(state)
    current_time = float(time.time() if now is None else now)
    poll = max(1.0, min(3600.0, float(poll_seconds)))
    runner = run or _default_run
    command_count = 0
    command_count_lock = threading.Lock()
    remote_mutation_starts: dict[str, float] = {}
    local_branch_head = ""

    def ownership_allows_mutation() -> bool:
        if mutation_allowed is not None:
            try:
                if not mutation_allowed():
                    return False
            except Exception:
                return False
        checker = getattr(control, "mutation_allowed", None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                return False
        return True

    try:
        runner_parameters = inspect.signature(runner).parameters
    except Exception:
        runner_parameters = {}
    runner_accepts_control = "control" in runner_parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in runner_parameters.values()
    )

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
        classified = classify_closeout_command(args)
        if classified.effect == CommandEffect.REMOTE_MUTATION:
            started_at = time.time() if mutation_started is not None else current_time
            mutation_context = {
                "at": started_at,
                "head_sha": local_branch_head
                or str(state.get("pr", {}).get("head_sha") or ""),
                "branch": str(state.get("workspace", {}).get("branch") or ""),
                "base_branch": str(
                    state.get("workspace", {}).get("base_branch") or "main"
                ),
                "repository": str(
                    state.get("workspace", {}).get("repository") or ""
                ),
            }
            if mutation_started is not None:
                try:
                    started = mutation_started(
                        classified.operation,
                        mutation_context,
                    )
                except Exception:
                    started = False
                if not started:
                    cancel = getattr(control, "cancel", None)
                    if callable(cancel):
                        try:
                            cancel("closeout mutation fence rejected")
                        except TypeError:
                            cancel()
                    raise RuntimeError("closeout mutation fence rejected")
            remote_mutation_starts[classified.operation] = started_at
        kwargs: dict[str, Any] = {
            "cwd": cwd,
            "timeout": timeout,
            "github": github,
        }
        if runner_accepts_control:
            kwargs["control"] = control
        return runner(args, **kwargs)

    def uncertain_operation() -> str:
        uncertainty = state.get("mutation_uncertainty")
        if not isinstance(uncertainty, Mapping):
            return ""
        if str(uncertainty.get("status") or "") != "uncertain":
            return ""
        return str(uncertainty.get("operation") or "")

    def clear_uncertainty(*operations: str) -> None:
        if uncertain_operation() in operations:
            state["mutation_uncertainty"] = {"status": "none"}

    def remote_mutation_uncertain(
        exc: RemoteMutationUncertain,
    ) -> CloseoutTransition:
        uncertainty = {
            "status": "uncertain",
            "operation": exc.operation,
            "at": remote_mutation_starts.get(exc.operation, current_time),
            "head_sha": local_branch_head
            or str(state.get("pr", {}).get("head_sha") or ""),
        }
        if exc.operation == "github_pr_create":
            uncertainty.update(
                {
                    "branch": branch,
                    "base_branch": base_branch,
                    "repository": repo,
                }
            )
        state["mutation_uncertainty"] = uncertainty
        return _blocked(
            original,
            state,
            code="remote_mutation_uncertain",
            message=(
                "Remote mutation outcome is uncertain; authoritative "
                "re-observation is required before retry"
            ),
            now=current_time,
            retry=True,
            poll_seconds=poll,
        )

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

    def resolve_local_branch_head() -> tuple[str, subprocess.CompletedProcess[str]]:
        nonlocal local_branch_head
        if local_branch_head:
            return local_branch_head, subprocess.CompletedProcess([], 0, local_branch_head, "")
        uncertainty = (
            state.get("mutation_uncertainty")
            if isinstance(state.get("mutation_uncertainty"), Mapping)
            else {}
        )
        fenced_head = (
            str(uncertainty.get("head_sha") or "").strip().lower()
            if uncertain_operation() == "git_push"
            else ""
        )
        persisted_head = str(state.get("pr", {}).get("head_sha") or "").strip().lower()
        immutable_head = fenced_head or persisted_head
        if immutable_head:
            if not _SHA_RE.fullmatch(immutable_head):
                return "", subprocess.CompletedProcess(
                    [],
                    1,
                    "",
                    "Persisted closeout head is not an exact Git SHA",
                )
            local_branch_head = immutable_head
            return local_branch_head, subprocess.CompletedProcess(
                [],
                0,
                local_branch_head,
                "",
            )
        result = execute(
            ["git", "rev-parse", f"refs/heads/{branch}"],
            cwd=root,
            timeout=20,
        )
        candidate = str(result.stdout or "").strip().lower()
        if result.returncode == 0 and _SHA_RE.fullmatch(candidate):
            local_branch_head = candidate
            state["pr"]["head_sha"] = candidate
        return local_branch_head, result

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
        uncertain_create = uncertain_operation() == "github_pr_create"
        pr_list_state = "all" if uncertain_create else "open"
        expected_create_head = ""
        if uncertain_create:
            uncertainty = (
                state.get("mutation_uncertainty")
                if isinstance(state.get("mutation_uncertainty"), Mapping)
                else {}
            )
            fenced_head = str(uncertainty.get("head_sha") or "").strip().lower()
            if fenced_head:
                if not _SHA_RE.fullmatch(fenced_head):
                    return _blocked(
                        original,
                        state,
                        code="pr_create_reobservation_fenced_head_invalid",
                        message="Fenced PR-create head SHA is invalid",
                        now=current_time,
                        retry=True,
                        poll_seconds=poll,
                    )
                expected_create_head = fenced_head
            else:
                # Legacy uncertainty records may predate durable head capture.
                expected_create_head, local_head_result = resolve_local_branch_head()
                if local_head_result.returncode != 0 or not expected_create_head:
                    return _blocked(
                        original,
                        state,
                        code="pr_create_reobservation_local_head_failed",
                        message=_detail(
                            local_head_result,
                            "local branch head query failed",
                        ),
                        now=current_time,
                        retry=True,
                        poll_seconds=poll,
                    )
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
                    pr_list_state,
                    "--limit",
                    "20",
                    "--json",
                    (
                        "number,url,state,headRefOid,headRefName,baseRefName,"
                        "headRepository,headRepositoryOwner,createdAt"
                    ),
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
            listed_payload = json.loads(listed.stdout or "[]")
        except json.JSONDecodeError as exc:
            if uncertain_create:
                return _blocked(
                    original,
                    state,
                    code="pr_create_reobservation_invalid_json",
                    message=exc,
                    now=current_time,
                    retry=True,
                    poll_seconds=poll,
                )
            listed_payload = []
        candidates = (
            [item for item in listed_payload[:20] if isinstance(item, Mapping)]
            if isinstance(listed_payload, list)
            else [listed_payload]
            if isinstance(listed_payload, Mapping)
            else []
        )
        if uncertain_create and not candidates:
            return _blocked(
                original,
                state,
                code="pr_create_reobservation_empty",
                message=(
                    "PR-create re-observation returned no authoritative candidates"
                ),
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )
        selected: Mapping[str, Any] | None = candidates[0] if candidates else None
        if uncertain_create:
            uncertainty = state.get("mutation_uncertainty")
            uncertainty_at = (
                _safe_float(uncertainty.get("at"))
                if isinstance(uncertainty, Mapping)
                else None
            )
            def exact_uncertain_create_match(candidate: Mapping[str, Any]) -> bool:
                if str(candidate.get("state") or "").strip().upper() != "OPEN":
                    return False
                if str(candidate.get("headRefOid") or "").strip().lower() != expected_create_head:
                    return False
                if str(candidate.get("headRefName") or "").strip() != branch:
                    return False
                if str(candidate.get("baseRefName") or "").strip() != base_branch:
                    return False
                head_repository = candidate.get("headRepository")
                head_owner = candidate.get("headRepositoryOwner")
                repository_identity = ""
                if isinstance(head_repository, Mapping):
                    repository_identity = str(
                        head_repository.get("nameWithOwner") or ""
                    ).strip()
                    if not repository_identity:
                        repository_name = str(head_repository.get("name") or "").strip()
                        owner_login = (
                            str(head_owner.get("login") or "").strip()
                            if isinstance(head_owner, Mapping)
                            else ""
                        )
                        if repository_name and owner_login:
                            repository_identity = f"{owner_login}/{repository_name}"
                if repository_identity.lower() != repo.lower():
                    return False
                if uncertainty_at is not None:
                    created_text = str(candidate.get("createdAt") or "").strip()
                    try:
                        created_at = dt.datetime.fromisoformat(
                            created_text.replace("Z", "+00:00")
                        ).timestamp()
                    except (TypeError, ValueError):
                        return False
                    if created_at + 2.0 < uncertainty_at:
                        return False
                return True

            selected = next(
                (candidate for candidate in candidates if exact_uncertain_create_match(candidate)),
                None,
            )
            if candidates and selected is None:
                return _blocked(
                    original,
                    state,
                    code="pr_create_reobservation_identity_mismatch",
                    message=(
                        "Branch-matched PR history did not match the fenced create "
                        "attempt identity"
                    ),
                    now=current_time,
                    retry=True,
                    poll_seconds=poll,
                )
        if isinstance(selected, Mapping):
            state["pr"]["url"] = str(selected.get("url") or "")[:1200]
            state["pr"]["number"] = _bounded_text(selected.get("number"), limit=32)
            pr_ref = _pr_ref(state)
        if uncertain_create and not pr_ref:
            return _blocked(
                original,
                state,
                code="pr_create_reobservation_identity_incomplete",
                message="Matched PR-create observation lacked a stable PR identity",
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )
        if pr_ref:
            clear_uncertainty("github_pr_create")
            clear_uncertainty("git_push")

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
        local_head, local_head_result = resolve_local_branch_head()
        if local_head_result.returncode != 0 or not local_head:
            return _blocked(
                original,
                state,
                code="local_branch_head_failed",
                message=_detail(local_head_result, "local branch head query failed"),
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )
        push_required = True
        if uncertain_operation() == "git_push":
            try:
                remote_head_result = execute(
                    [
                        "git",
                        "ls-remote",
                        "--heads",
                        "origin",
                        f"refs/heads/{branch}",
                    ],
                    cwd=root,
                    timeout=60,
                    github=True,
                )
            except Exception as exc:
                return _blocked(
                    original,
                    state,
                    code="push_reobservation_error",
                    message=exc,
                    now=current_time,
                    retry=True,
                    poll_seconds=poll,
                )
            if remote_head_result.returncode != 0:
                return _blocked(
                    original,
                    state,
                    code="push_reobservation_failed",
                    message=_detail(remote_head_result, "remote branch query failed"),
                    now=current_time,
                    retry=True,
                    poll_seconds=poll,
                )
            remote_line = next(
                (
                    line.strip()
                    for line in str(remote_head_result.stdout or "").splitlines()
                    if line.strip()
                ),
                "",
            )
            remote_head = remote_line.split()[0].lower() if remote_line else ""
            if not _SHA_RE.fullmatch(local_head):
                return _blocked(
                    original,
                    state,
                    code="push_reobservation_invalid_local_head",
                    message="Local branch head is not an exact Git SHA",
                    now=current_time,
                )
            if remote_head and not _SHA_RE.fullmatch(remote_head):
                return _blocked(
                    original,
                    state,
                    code="push_reobservation_invalid_remote_head",
                    message="Remote branch head is not an exact Git SHA",
                    now=current_time,
                    retry=True,
                    poll_seconds=poll,
                )
            if remote_head and remote_head != local_head:
                return _blocked(
                    original,
                    state,
                    code="push_reobservation_head_changed",
                    message="Remote branch changed after an uncertain push",
                    now=current_time,
                )
            clear_uncertainty("git_push")
            push_required = remote_head != local_head

        if push_required:
            try:
                pushed = execute(
                    [
                        "git",
                        "push",
                        "-u",
                        "origin",
                        f"{local_head}:refs/heads/{branch}",
                    ],
                    cwd=root,
                    timeout=300,
                    github=True,
                )
            except RemoteMutationUncertain as exc:
                return remote_mutation_uncertain(exc)
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
        create_args.append("--draft")
        try:
            created = execute(create_args, cwd=root, timeout=120, github=True)
        except RemoteMutationUncertain as exc:
            return remote_mutation_uncertain(exc)
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
    require_trusted_checks = _repo_uses_trusted_required_checks(root)
    if require_trusted_checks:
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

    pending_push_head = str(state["pr"].get("pending_push_head_sha") or "")
    if state["mode"] == "enforce" and pending_push_head:
        observed_pr_head = str(payload.get("headRefOid") or "").strip().lower()
        if not _SHA_RE.fullmatch(observed_pr_head):
            return _blocked(
                original,
                state,
                code="invalid_pr_head",
                message="PR head is not an exact Git SHA",
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )
        if observed_pr_head != pending_push_head:
            if uncertain_operation() == "git_push":
                uncertainty = _record(state.get("mutation_uncertainty"))
                if str(uncertainty.get("head_sha") or "").strip().lower() != pending_push_head:
                    return _blocked(
                        original,
                        state,
                        code="push_reobservation_head_mismatch",
                        message="The uncertain push does not target the pending verified head",
                        now=current_time,
                    )
                return _blocked(
                    original,
                    state,
                    code="push_reobservation_pending",
                    message="Remote PR head has not reached the fenced push head",
                    now=current_time,
                    retry=True,
                    poll_seconds=poll,
                )
            try:
                pushed = execute(
                    [
                        "git",
                        "push",
                        "-u",
                        "origin",
                        f"{pending_push_head}:refs/heads/{branch}",
                    ],
                    cwd=root,
                    timeout=300,
                    github=True,
                )
            except RemoteMutationUncertain as exc:
                return remote_mutation_uncertain(exc)
            except Exception as exc:
                return _blocked(
                    original,
                    state,
                    code="push_error",
                    message=exc,
                    now=current_time,
                )
            if pushed.returncode != 0:
                return _blocked(
                    original,
                    state,
                    code="push_failed",
                    message=_detail(pushed, "git push failed"),
                    now=current_time,
                )
            return _transition(
                original,
                state,
                outcome="pr_pending",
                next_due_at=current_time,
                terminal=False,
                wake_immediately=True,
            )
        state["pr"]["pending_push_head_sha"] = ""
        clear_uncertainty("git_push")

    pr = state["pr"]
    try:
        new_head = _apply_pr_payload(
            state,
            payload,
            require_trusted_checks=require_trusted_checks,
        )
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

    if pr["state"] not in {"OPEN", "UNKNOWN", ""}:
        return _blocked(original, state, code="pr_not_open", message=f"PR state is {pr['state'] or 'unknown'}", now=current_time)

    if policy["require_preview"]:
        from hermes_cli.preview_deployments import collect_vercel_preview

        try:
            preview = collect_vercel_preview(
                repository=repo,
                head_sha=new_head,
                branch=branch,
                pr_number=int(pr.get("number") or 0),
                root=root,
                run=execute,
            )
        except Exception as exc:
            return _blocked(
                original,
                state,
                code="preview_lookup_error",
                message=exc,
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )
        state["preview"] = preview.as_dict()
        if preview.status == "failed":
            _append_error(
                state,
                code="preview_deployment_failed",
                message="The exact-head Vercel preview deployment failed",
                now=current_time,
            )
            return _transition(
                original,
                state,
                outcome="repair_required" if state["mode"] == "enforce" else "waiting_for_preview",
                next_due_at=None if state["mode"] == "enforce" else current_time + poll,
                terminal=state["mode"] == "enforce",
            )

    preview_ready = (
        not policy["require_preview"]
        or (
            state["preview"].get("status") == "ready"
            and state["preview"].get("observed_sha") == new_head
            and bool(state["preview"].get("url"))
        )
    )
    if not preview_ready:
        return _transition(
            original,
            state,
            outcome="waiting_for_preview",
            next_due_at=current_time + poll,
            terminal=False,
        )

    if pr["review_decision"] == "CHANGES_REQUESTED":
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

    visual_qa_passed = _gate_passed(
        state["visual_qa"],
        required=True,
        head_sha=new_head,
    )
    if uncertain_operation() == "github_pr_ready":
        uncertainty = _record(state.get("mutation_uncertainty"))
        uncertain_head = str(uncertainty.get("head_sha") or "").strip().lower()
        if uncertain_head != new_head:
            return _blocked(
                original,
                state,
                code="pr_ready_reobservation_head_mismatch",
                message="The uncertain PR-ready mutation does not target the current PR head",
                now=current_time,
            )
        clear_uncertainty("github_pr_ready")
    if state["mode"] == "enforce" and visual_qa_passed and not pr["is_draft"]:
        pr["ready_at"] = pr.get("ready_at") or current_time

    if state["mode"] == "enforce" and pr["is_draft"] and visual_qa_passed:
        try:
            ready = execute(
                ["gh", "pr", "ready", pr_ref, "--repo", repo],
                cwd=root,
                timeout=60,
                github=True,
            )
        except RemoteMutationUncertain as exc:
            return remote_mutation_uncertain(exc)
        except Exception as exc:
            return _blocked(
                original,
                state,
                code="pr_ready_error",
                message=exc,
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )

        try:
            ready_view = execute(
                [
                    "gh",
                    "pr",
                    "view",
                    pr_ref,
                    "--repo",
                    repo,
                    "--json",
                    "number,url,state,headRefOid,isDraft",
                ],
                cwd=root,
                timeout=60,
                github=True,
            )
        except Exception as exc:
            return _blocked(
                original,
                state,
                code="pr_ready_refresh_error",
                message=exc,
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )
        if ready_view.returncode != 0:
            return _blocked(
                original,
                state,
                code="pr_ready_refresh_failed",
                message=_detail(ready_view, "PR readiness refresh failed"),
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )
        try:
            ready_payload = json.loads(ready_view.stdout or "{}")
        except json.JSONDecodeError as exc:
            return _blocked(
                original,
                state,
                code="pr_ready_refresh_invalid_json",
                message=exc,
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )
        observed_head = (
            str(ready_payload.get("headRefOid") or "").strip().lower()
            if isinstance(ready_payload, Mapping)
            else ""
        )
        if observed_head != new_head:
            return _blocked(
                original,
                state,
                code="pr_ready_head_changed",
                message="The PR head changed while GitHub marked it ready",
                now=current_time,
            )
        if not isinstance(ready_payload, Mapping) or ready_payload.get("isDraft") is True:
            return _blocked(
                original,
                state,
                code=(
                    "pr_ready_failed"
                    if ready.returncode != 0
                    else "pr_ready_unconfirmed"
                ),
                message=_detail(ready, "GitHub did not confirm the PR as ready"),
                now=current_time,
                retry=True,
                poll_seconds=poll,
            )
        if str(ready_payload.get("state") or "").strip().upper() != "OPEN":
            return _blocked(
                original,
                state,
                code="pr_not_open",
                message="The PR stopped being open while GitHub marked it ready",
                now=current_time,
            )
        pr["is_draft"] = False
        pr["ready_at"] = current_time
        if ready_payload.get("url"):
            pr["url"] = str(ready_payload.get("url"))[:1200]
        if ready_payload.get("number") is not None:
            pr["number"] = _bounded_text(ready_payload.get("number"), limit=32)

    return _transition(
        original,
        state,
        outcome="pr_published",
        next_due_at=None,
        terminal=True,
    )


def reconcile_trusted_closeout(
    value: Any,
    *,
    now: float | None = None,
    poll_seconds: float = 30.0,
    run: CommandRunner | None = None,
    max_commands: int = _DEFAULT_MAX_COMMANDS,
    mutation_allowed: Callable[[], bool] | None = None,
    mutation_started: Callable[[str, Mapping[str, Any]], bool] | None = None,
    control: Any | None = None,
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
    try:
        base_runner_parameters = inspect.signature(base_runner).parameters
    except Exception:
        base_runner_parameters = {}
    base_runner_accepts_control = "control" in base_runner_parameters or any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        for parameter in base_runner_parameters.values()
    )

    def instrumented_run(
        args: list[str],
        *,
        cwd: Path,
        timeout: int | float = 60,
        github: bool = False,
        control: Any | None = None,
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
            runner_kwargs: dict[str, Any] = {
                "cwd": cwd,
                "timeout": timeout,
                "github": github,
            }
            if base_runner_accepts_control:
                runner_kwargs["control"] = control
            result = base_runner(args, **runner_kwargs)
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
            max_commands=max_commands,
            mutation_allowed=mutation_allowed,
            mutation_started=mutation_started,
            control=control,
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
    "repo_uses_trusted_required_checks",
    "reconcile_trusted_closeout",
    "sanitize_closeout_error",
    "summarize_required_checks",
]
