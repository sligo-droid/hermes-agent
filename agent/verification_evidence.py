"""Lightweight verification evidence classification and claim gating."""

from __future__ import annotations

import json
import re
import shlex
import sqlite3
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home


_DB_LOCK = threading.Lock()
_MAX_OUTPUT_SUMMARY_CHARS = 2000
_MAX_EVIDENCE_AGE_DAYS = 30
_MAX_EVENTS_PER_SESSION_ROOT = 100
_MAX_TOTAL_UNREFERENCED_EVENTS = 10_000
_AD_HOC_SCRIPT_NAME_PREFIXES = ("hermes-verify-", "hermes-ad-hoc-")
_VERIFY_SCHEMA_VERSION = 1
_SHA_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


@dataclass(frozen=True)
class VerificationEvidence:
    command: str
    canonical_command: str
    kind: str
    scope: str
    status: str
    exit_code: int
    cwd: str
    root: str
    session_id: str
    output_summary: str = ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retention_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=_MAX_EVIDENCE_AGE_DAYS)).isoformat()


def _db_path() -> Path:
    return get_hermes_home() / "verification_evidence.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            session_id TEXT NOT NULL,
            cwd TEXT NOT NULL,
            root TEXT NOT NULL,
            command TEXT NOT NULL,
            canonical_command TEXT NOT NULL,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL,
            status TEXT NOT NULL,
            exit_code INTEGER NOT NULL,
            output_summary TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS verification_state (
            session_id TEXT NOT NULL,
            root TEXT NOT NULL,
            last_event_id INTEGER,
            last_edit_at TEXT,
            changed_paths_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY (session_id, root)
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_verification_events_session_root
        ON verification_events(session_id, root, id DESC)
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(_VERIFY_SCHEMA_VERSION),),
    )
    conn.commit()


_VERIFY_COMMAND_RE = re.compile(
    r"\b(pytest|vitest|playwright|chromium|browser|smoke|check|status|ci|deploy|deployed|"
    r"production|prod|preview|modal|health|run_tests\.sh)\b|"
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:[^\s;&|]*[-:]?)?(?:test|tests|verify|verification)\b|"
    r"\b(?:cargo|go)\s+test\b|"
    r"(?:^|[\s;&|])(?:\./)?(?:scripts/)?(?:test|tests|run_tests)\.sh\b|"
    r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+\S*(?:verify|verification)\b",
    re.IGNORECASE,
)
_PROTECTED_CHECKOUT_GUARDRAIL_RE = re.compile(
    r"\bBLOCKED:\s*refusing to run a non-read-only terminal command from a protected canonical checkout\b",
    re.IGNORECASE,
)
_WORKFLOW_LOOKUP_ERROR_RE = re.compile(
    r"\bcould not find any workflows? named\b|"
    r"\bno workflows? (?:found|matched|matching)\b|"
    r"\bworkflows?\b[^\n]{0,120}\b(?:not found|does not exist|unknown)\b|"
    r"\b(?:not found|does not exist|unknown)\b[^\n]{0,120}\bworkflows?\b",
    re.IGNORECASE,
)
_BROWSER_RE = re.compile(r"\b(browser|playwright|chromium|chrome|modal)\b", re.IGNORECASE)
_AUTHENTICATED_QA_COMMAND_RE = re.compile(
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?qa:auth\b",
    re.IGNORECASE,
)
_GITHUB_CLOSED_PR_VERIFY_RE = re.compile(
    r"\bgh\s+api\s+repos/[^\s]+/pulls/\d+\b",
    re.IGNORECASE,
)
_CURRENT_MAIN_RE = re.compile(r"(?m)^CURRENT_MAIN=([0-9a-f]{40}|[0-9a-f]{64})\s*$")
_GH_CHECK_LINE_RE = re.compile(
    r"(?m)^[^\t\n]+\t(pass|fail|pending|skipping|cancelled|timed_out)\t",
    re.IGNORECASE,
)
_BROWSER_AUTH_BOUNDARY_RE = re.compile(
    r'''(?:"title"\s*:\s*"[^"\n]*\b(?:sign[ -]?in|log[ -]?in|login)\b)|'''
    r"(?:<title>[^<\n]*\b(?:sign[ -]?in|log[ -]?in|login)\b)|"
    r"\b(?:authentication required|authorization required|unauthorized)\b|"
    r'''\btextbox\s+["']password["'][^\n]{0,240}\bbutton\s+["'](?:sign[ -]?in|log[ -]?in)["']''',
    re.IGNORECASE,
)
_BROWSER_ERROR_PAGE_RE = re.compile(
    r"\bcloudflare tunnel error\b|\berror\s*1033\b|"
    r"\b(?:bad gateway|service unavailable|gateway timeout|internal server error)\b",
    re.IGNORECASE,
)
_PRODUCTION_RE = re.compile(r"\b(production|prod|deployed?|live)\b", re.IGNORECASE)
_EXTERNAL_URL_RE = re.compile(
    r"https?://(?!(?:127(?:\.\d{1,3}){3}|localhost|0\.0\.0\.0|\[::1\])(?::|/|$))",
    re.IGNORECASE,
)
_CI_RE = re.compile(r"\b(ci|checks?|status|gh\s+pr\s+checks|test|tests|pytest|vitest)\b", re.IGNORECASE)
_CI_CLAIM_RE = re.compile(
    r"\b(?:ci|continuous integration|tests?|pytest|vitest|checks?|pr checks?|build checks?)\b",
    re.IGNORECASE,
)
_CI_COMMAND_RE = re.compile(
    r"\b(?:ci|checks?|status|gh\s+pr\s+checks|pytest|vitest)\b|"
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:[^\s;&|]*[-:]?)?(?:test|tests|check|verify|verification)\b|"
    r"\b(?:cargo|go)\s+test\b|"
    r"(?:^|[\s;&|])(?:\./)?(?:scripts/)?(?:test|tests|run_tests)\.sh\b|"
    r"\bpython(?:\d+(?:\.\d+)?)?\s+-m\s+\S*(?:verify|verification)\b",
    re.IGNORECASE,
)
_DEPLOY_RE = re.compile(r"\b(deploy|deployed|deployment)\b", re.IGNORECASE)
_MERGE_RE = re.compile(r"\b(merge|merged|pull|pr)\b", re.IGNORECASE)
_SUCCESS_RE = re.compile(r"\b(success|passed|pass|ok|complete|completed|visible|found|healthy)\b", re.IGNORECASE)
_TIMEOUT_RE = re.compile(r"\b(timed?\s*out|timeout|deadline|expired)\b", re.IGNORECASE)
_EXPLICIT_TIMEOUT_OUTCOME_RE = re.compile(
    r"\b(?:timed?\s+out|deadline exceeded)\b",
    re.IGNORECASE,
)
_SHELL_SEGMENT_RE = re.compile(r"\s*(?:&&|\|\||[;\n])\s*")
_UNSAFE_VERIFY_SHELL_RE = re.compile(r"\|\||(?<!&)&(?!&)|(?<!\|)\|(?!\|)|[<>`]|\$\(")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$")
_GIT_OPTION_ARGS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
_NON_VERIFY_GIT_PATHSPEC_COMMANDS = {"add", "rm", "mv", "restore", "checkout", "reset"}
_READ_ONLY_INSPECTION_COMMANDS = {
    "awk",
    "cat",
    "echo",
    "find",
    "grep",
    "head",
    "ls",
    "printf",
    "rg",
    "sed",
    "stat",
    "tail",
    "wc",
}
_NON_VERIFICATION_GIT_COMMANDS = {
    "add",
    "branch",
    "commit",
    "config",
    "fetch",
    "log",
    "push",
    "remote",
    "rev-list",
    "rev-parse",
    "show",
    "status",
    "tag",
}
_GH_PR_CLOSE_SUCCESS_RE = re.compile(r"(?m)^\s*(?:[✓✔]\s+)?Closed pull request\b", re.IGNORECASE)
_GH_PR_CREATE_RE = re.compile(r"\bgh\s+pr\s+create\b", re.IGNORECASE)
_GH_PR_CLOSE_RE = re.compile(r"\bgh\s+pr\s+close\b", re.IGNORECASE)
_GH_PR_MERGE_RE = re.compile(r"\bgh\s+pr\s+merge\b", re.IGNORECASE)
_GH_PR_STATUS_RE = re.compile(r"\bgh\s+pr\s+status\b", re.IGNORECASE)
_GITHUB_PR_URL_RE = re.compile(r"https://github\.com/[^\s/]+/[^\s/]+/pull/\d+", re.IGNORECASE)
_GH_PR_COMMAND_RE = re.compile(r"\bgh\s+pr\s+(?:create|close|view|merge|status)\b", re.IGNORECASE)
_GH_PR_MUTATION_RE = re.compile(r"\bgh\s+pr\s+(?:create|close|merge)\b", re.IGNORECASE)
_GH_PR_CHECKS_RE = re.compile(r"\bgh\s+pr\s+checks\b", re.IGNORECASE)
_GH_CHECK_RUNS_RE = re.compile(r"\bgh\s+api\s+repos/[^\s]+/commits/[^\s]+/check-runs\b", re.IGNORECASE)
_GH_PULL_API_RE = re.compile(r"\bgh\s+api\s+repos/[^\s]+/pulls/\d+\b", re.IGNORECASE)
_GH_RUN_RE = re.compile(r"\bgh\s+run\s+(?:list|view|watch)\b", re.IGNORECASE)
_GH_PR_NUMBER_RE = re.compile(r"\bgh\s+pr\s+(?:checks|close|view|merge)\s+(\d+)\b", re.IGNORECASE)
_GH_REPO_ARG_RE = re.compile(r"(?:^|\s)--repo(?:=|\s+)([^\s]+)", re.IGNORECASE)
_GH_PULL_API_PARTS_RE = re.compile(r"\bgh\s+api\s+repos/([^\s]+)/pulls/(\d+)\b", re.IGNORECASE)
_GH_CHECK_RUNS_PARTS_RE = re.compile(
    r"\bgh\s+api\s+repos/([^\s]+)/commits/([^\s]+)/check-runs\b",
    re.IGNORECASE,
)
_EXPLICIT_DEPLOY_COMMAND_RE = re.compile(
    r"\b(?:vercel|flyctl|railway|render)\b|"
    r"\bgh\s+api\s+repos/[^\s]+/deployments\b|"
    r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?[^\s;&|]*(?:deploy|release)[^\s;&|]*\b",
    re.IGNORECASE,
)

_CLAIM_WORD_RE = re.compile(
    r"\b(shipped|verified|visible|checked|confirmed|passed|deployed|merged|closed|unchanged)\b",
    re.IGNORECASE,
)
_NEGATED_CLAIM_RE = re.compile(r"\b(?:not|isn['’]?t|failed|failure|blocked|unverified|not_verified)\b", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_SPLIT_RE = re.compile(r"\s*(?:;|\b(?:and|but)\b)\s*", re.IGNORECASE)
_UNMERGED_PR_CLAIM_RE = re.compile(
    r"\bclosed\b[^.!?\n]{0,80}\b(?:without (?:a )?merge|unmerged)\b|"
    r"\bclosed\b[^.!?\n]{0,80}\bnot merged\b",
    re.IGNORECASE,
)
_MAIN_UNCHANGED_CLAIM_RE = re.compile(
    r"\b(?:main|base branch)\b[^.!?\n]{0,100}"
    r"\b(?:unchanged|did not change|stayed the same|remained the same)\b|"
    r"\bno changes?\b[^.!?\n]{0,60}\b(?:main|base branch)\b",
    re.IGNORECASE,
)
_EXPLICIT_PR_CLAIM_RE = re.compile(
    r"(?:(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:\s+|#))?"
    r"(?:PR|pull request)\s*#?(?P<number>\d+)|"
    r"(?P<url>https://github\.com/(?P<url_repo>[^/\s]+/[^/\s]+)/pull/(?P<url_number>\d+))",
    re.IGNORECASE,
)

_SURFACE_LABELS = {
    "browser": "browser verification",
    "production": "production verification",
    "production_browser": "production browser verification",
    "ci": "CI verification",
    "deployment": "deployment verification",
    "pr": "PR/merge verification",
    "main_branch": "main branch verification",
    "verification": "verification",
}


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    try:
        data = json.loads(value)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _last_embedded_json_object(value: Any) -> dict[str, Any]:
    """Return the last JSON object embedded in bounded command output."""
    text = str(value or "")
    decoder = json.JSONDecoder()
    found: dict[str, Any] = {}
    found_span = 0
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            candidate, end = decoder.raw_decode(text[index:])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(candidate, dict) and end > found_span:
            found = candidate
            found_span = end
    return found


def _embedded_json_objects(value: Any) -> list[dict[str, Any]]:
    """Return non-overlapping JSON objects embedded in command output."""

    text = str(value or "")
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    index = 0
    while index < len(text):
        start = text.find("{", index)
        if start < 0:
            break
        try:
            candidate, end = decoder.raw_decode(text[start:])
        except (TypeError, ValueError, json.JSONDecodeError):
            index = start + 1
            continue
        if isinstance(candidate, dict):
            objects.append(candidate)
        index = start + max(1, end)
    return objects


def _normalized_check_status(status: Any, conclusion: Any = None) -> str:
    conclusion_text = str(conclusion or "").strip().lower()
    status_text = str(status or "").strip().lower()
    value = conclusion_text or status_text
    if value in {"pass", "passed", "success", "successful", "skipping", "skipped", "neutral"}:
        return "success"
    if value in {
        "fail",
        "failed",
        "failure",
        "cancelled",
        "canceled",
        "timed_out",
        "action_required",
        "stale",
        "startup_failure",
    }:
        return "failure"
    if value in {"pending", "queued", "in_progress", "requested", "waiting", "expected"}:
        return "pending"
    if status_text == "completed" and not conclusion_text:
        return "pending"
    return ""


def _github_check_states(output: str) -> dict[str, str]:
    """Extract latest named GitHub check states from tables or JSON payloads."""

    states: dict[str, str] = {}
    for line in str(output or "").splitlines():
        fields = line.split("\t")
        if len(fields) < 2:
            continue
        name = fields[0].strip()
        state = _normalized_check_status(fields[1])
        if name and state:
            states[name] = state

    for payload in _embedded_json_objects(output):
        candidates: list[Any] = []
        for key in ("checks", "check_runs", "statusCheckRollup"):
            value = payload.get(key)
            if isinstance(value, list):
                candidates.extend(value)
        for raw in candidates:
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            state = _normalized_check_status(raw.get("status"), raw.get("conclusion"))
            if name and state:
                states[name] = state
    return states


def _github_pr_payload(output: str) -> dict[str, Any]:
    for payload in reversed(_embedded_json_objects(output)):
        if any(key in payload for key in ("state", "closed", "merged", "merged_at", "mergedAt")):
            return payload
    return {}


def _github_evidence_subjects(
    command: str,
    payload: dict[str, Any],
) -> tuple[str, str]:
    """Return PR and CI subjects so unrelated repairs cannot overwrite failures."""

    repo_match = _GH_REPO_ARG_RE.search(command)
    repo = repo_match.group(1).strip().lower() if repo_match else ""
    pr_match = _GH_PR_NUMBER_RE.search(command)
    pr_number = pr_match.group(1) if pr_match else ""
    pull_match = _GH_PULL_API_PARTS_RE.search(command)
    if pull_match:
        repo = pull_match.group(1).strip().lower()
        pr_number = pull_match.group(2)

    url = str(payload.get("html_url") or payload.get("url") or "")
    url_match = re.search(r"https://github\.com/([^/]+/[^/]+)/pull/(\d+)", url, re.IGNORECASE)
    if url_match:
        repo = url_match.group(1).lower()
        pr_number = url_match.group(2)

    pr_subject = f"github:{repo + ':' if repo else ''}pr:{pr_number}" if pr_number else ""
    check_match = _GH_CHECK_RUNS_PARTS_RE.search(command)
    if not check_match:
        return pr_subject, pr_subject

    check_repo = check_match.group(1).strip().lower()
    check_sha = check_match.group(2).strip().lower()
    head_sha = str(payload.get("head_sha") or payload.get("headRefOid") or "").strip().lower()
    if pr_subject and head_sha and check_sha == head_sha:
        return pr_subject, pr_subject
    return pr_subject, f"github:{check_repo}:commit:{check_sha}"


def _github_lifecycle_evidence(
    command: str,
    output: str,
    *,
    is_error: bool,
    order: int | None,
) -> list[dict[str, Any]]:
    """Classify typed GitHub lifecycle evidence independently of shell exit."""

    if not any(
        pattern.search(command)
        for pattern in (_GH_PR_COMMAND_RE, _GH_PR_CHECKS_RE, _GH_CHECK_RUNS_RE, _GH_PULL_API_RE)
    ):
        return []

    evidence: list[dict[str, Any]] = []
    payload = _github_pr_payload(output)
    pr_subject, ci_subject = _github_evidence_subjects(command, payload)

    checks_attempted = bool(
        _GH_PR_CHECKS_RE.search(command)
        or _GH_CHECK_RUNS_RE.search(command)
        or _GH_PR_STATUS_RE.search(command)
    )
    check_states = _github_check_states(output)
    if check_states:
        values = list(check_states.values())
        status = (
            "failure"
            if any(value == "failure" for value in values)
            else "pending"
            if any(value == "pending" for value in values)
            else "success"
        )
        evidence.append(
            {
                "schema_version": 1,
                "surface": "ci",
                "check_name": _text("GitHub checks", limit=160),
                "status": status,
                "order": int(order or 0),
                "subject": ci_subject,
                "detail": json.dumps(check_states, sort_keys=True, separators=(",", ":"))[:240],
            }
        )
    elif is_error and checks_attempted:
        evidence.append(
            {
                "schema_version": 1,
                "surface": "ci",
                "check_name": _text(command, limit=160),
                "status": "failure",
                "order": int(order or 0),
                "subject": ci_subject,
                "detail": _text(output, limit=240),
            }
        )

    state = str(payload.get("state") or "").strip().lower()
    closed = payload.get("closed") is True or state in {"closed", "merged"}
    merged = payload.get("merged")
    merged_at = payload.get("merged_at", payload.get("mergedAt"))
    merge_commit = payload.get("mergeCommit")
    close_confirmed = bool(_GH_PR_CLOSE_SUCCESS_RE.search(output))
    merged_confirmed = merged is True or merged_at is not None or merge_commit is not None
    merge_fields_present = any(
        key in payload for key in ("merged", "merged_at", "mergedAt", "mergeCommit")
    )
    unmerged_confirmed = merged is False or close_confirmed or (
        closed and merge_fields_present and merged_at is None and merge_commit is None
    )
    pr_evidence_recorded = False
    if closed or close_confirmed:
        if _GH_PR_CLOSE_RE.search(command):
            pr_success = unmerged_confirmed and not merged_confirmed
        elif _GH_PR_MERGE_RE.search(command):
            pr_success = merged_confirmed
        else:
            pr_success = True
        evidence.append(
            {
                "schema_version": 1,
                "surface": "pr",
                "check_name": _text(
                    f"closed PR verification {payload.get('html_url') or payload.get('url') or ''}",
                    limit=160,
                ),
                "status": "success" if pr_success else "failure",
                "order": int(order or 0),
                "subject": pr_subject,
                "head_sha": str(
                    payload.get("head_sha") or payload.get("headRefOid") or ""
                ).strip().lower(),
                "merged_confirmed": merged_confirmed,
                "unmerged_confirmed": unmerged_confirmed,
                "detail": json.dumps(
                    {
                        "state": state or ("closed" if close_confirmed else ""),
                        "closed": closed or close_confirmed,
                        "merged": merged,
                        "merged_at": merged_at,
                        "merged_confirmed": merged_confirmed,
                        "unmerged_confirmed": unmerged_confirmed,
                        "head_sha": payload.get("head_sha") or payload.get("headRefOid"),
                        "base_sha": payload.get("base_sha"),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )[:240],
            }
        )
        pr_evidence_recorded = True
    elif _GH_PR_CREATE_RE.search(command):
        pr_url = _GITHUB_PR_URL_RE.search(output)
        if pr_url is not None:
            evidence.append(
                {
                    "schema_version": 1,
                    "surface": "pr",
                    "check_name": "PR created",
                    "status": "success",
                    "order": int(order or 0),
                    "subject": pr_subject,
                    "detail": pr_url.group(0)[:240],
                }
            )
            pr_evidence_recorded = True
    if is_error and _GH_PR_MUTATION_RE.search(command) and not pr_evidence_recorded:
        evidence.append(
            {
                "schema_version": 1,
                "surface": "pr",
                "check_name": _text(command, limit=160),
                "status": "failure",
                "order": int(order or 0),
                "subject": pr_subject,
                "detail": _text(output, limit=240),
            }
        )
    return evidence


def _authenticated_qa_evidence(
    command: str,
    output: str,
    *,
    is_error: bool,
    order: int | None,
) -> list[dict[str, Any]]:
    """Classify repository-native authenticated browser QA output."""
    if not _AUTHENTICATED_QA_COMMAND_RE.search(command):
        return []
    payload = _last_embedded_json_object(output)
    base_url = str(payload.get("baseUrl") or "").strip()
    routes = payload.get("routes") if isinstance(payload.get("routes"), list) else []
    paths = payload.get("paths") if isinstance(payload.get("paths"), list) else []
    if not base_url or not routes:
        return []
    route_count = payload.get("routeCount")
    try:
        route_count_ok = int(route_count) == len(routes) and len(routes) > 0
    except (TypeError, ValueError):
        route_count_ok = False
    routes_ok = all(
        isinstance(item, dict)
        and str(item.get("path") or "").startswith("/")
        and str(item.get("finalPath") or "").startswith("/")
        for item in routes
    )
    success = bool(
        not is_error
        and payload.get("ok") is True
        and route_count_ok
        and routes_ok
        and int(payload.get("consoleErrorCount") or 0) == 0
        and int(payload.get("pageErrorCount") or 0) == 0
    )
    detail = json.dumps(
        {
            "ok": payload.get("ok") is True,
            "baseUrl": base_url,
            "paths": [str(path)[:120] for path in paths[:8]],
            "routeCount": len(routes),
            "consoleErrorCount": payload.get("consoleErrorCount"),
            "pageErrorCount": payload.get("pageErrorCount"),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    check_name = _text(f"qa:auth {base_url}", limit=160)
    return [
        {
            "schema_version": 1,
            "surface": surface,
            "check_name": check_name,
            "status": "success" if success else "failure",
            "order": int(order or 0),
            "detail": detail[:240],
        }
        for surface in _surfaces_for("browser_authenticated_qa", base_url, detail)
    ]


def _closed_pr_without_merge_evidence(
    command: str,
    output: str,
    *,
    is_error: bool,
    order: int | None,
) -> list[dict[str, Any]]:
    """Classify structured proof that a green PR closed without changing main."""
    if not _GITHUB_CLOSED_PR_VERIFY_RE.search(command):
        return []
    payload = _last_embedded_json_object(output)
    state = str(payload.get("state") or "").strip().lower()
    merged = payload.get("merged")
    merged_at = payload.get("merged_at")
    base_sha = str(payload.get("base_sha") or "").strip().lower()
    head_sha = str(payload.get("head_sha") or "").strip().lower()
    main_match = _CURRENT_MAIN_RE.search(output)
    current_main = main_match.group(1).lower() if main_match else ""
    check_states = [match.lower() for match in _GH_CHECK_LINE_RE.findall(output)]
    pr_success = bool(
        not is_error
        and state == "closed"
        and merged is False
        and merged_at is None
        and _SHA_RE.fullmatch(base_sha)
        and _SHA_RE.fullmatch(head_sha)
        and current_main == base_sha
    )
    checks_success = bool(check_states) and all(
        state in {"pass", "skipping"} for state in check_states
    )
    detail = json.dumps(
        {
            "state": state,
            "merged": merged,
            "merged_at": merged_at,
            "head_sha": head_sha,
            "base_sha": base_sha,
            "current_main": current_main,
            "check_states": check_states[:20],
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    check_name = _text(f"closed PR verification {payload.get('html_url') or ''}", limit=160)
    evidence = [
        {
            "schema_version": 1,
            "surface": "pr",
            "check_name": check_name,
            "status": "success" if pr_success else "failure",
            "order": int(order or 0),
            "detail": detail[:240],
        }
    ]
    if check_states:
        evidence.append(
            {
                "schema_version": 1,
                "surface": "ci",
                "check_name": check_name,
                "status": "success" if checks_success else "failure",
                "order": int(order or 0),
                "detail": detail[:240],
            }
        )
    return evidence


def _text(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    return str(value).strip()[:limit]


def _tool_result_timed_out(
    data: dict[str, Any],
    result_text: str,
    *,
    failed: bool,
) -> bool:
    """Trust execution outcome fields, never timeout words in command source."""

    if not failed:
        return False
    try:
        if int(data.get("exit_code")) == 124:
            return True
    except (TypeError, ValueError):
        pass
    error_text = str(data.get("error") or "").strip()
    if error_text:
        return bool(_TIMEOUT_RE.search(error_text))
    return bool(_EXPLICIT_TIMEOUT_OUTCOME_RE.search(result_text))


def _surfaces_for(tool_name: str, check_name: str, detail: str) -> list[str]:
    if tool_name == "terminal":
        return _terminal_surfaces(check_name)

    haystack = f"{tool_name} {check_name} {detail}"
    surfaces: list[str] = []
    if tool_name.startswith("browser") or _BROWSER_RE.search(haystack):
        surfaces.append("browser")
    if _PRODUCTION_RE.search(haystack) or _EXTERNAL_URL_RE.search(haystack):
        surfaces.append("production")
    if "browser" in surfaces and "production" in surfaces:
        surfaces.append("production_browser")
    return surfaces or ["verification"]


def _terminal_surfaces(command: str) -> list[str]:
    """Return surfaces from command semantics, never incidental output prose."""

    text = str(command or "")
    lowered = text.lower()
    if re.search(r"\bgit\b[^\n;&|]*\bdiff\b[^\n;&|]*--check\b", text, re.IGNORECASE):
        surfaces = ["verification"]
    else:
        surfaces = []
    if (
        _GH_PR_CHECKS_RE.search(text)
        or _GH_RUN_RE.search(text)
        or re.search(r"\b(?:pytest|vitest)\b", text, re.IGNORECASE)
        or re.search(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?[^\s;&|]*(?:test|tests|check|verify|verification)\b", text, re.IGNORECASE)
        or re.search(r"(?:^|[\s;&|])(?:\./)?(?:scripts/)?(?:test|tests|run_tests)\.sh\b", text, re.IGNORECASE)
        or re.search(r"\b(?:cargo|go)\s+test\b", text, re.IGNORECASE)
        or re.search(r"\bgh\s+pr\s+status\b", text, re.IGNORECASE)
    ):
        surfaces.append("ci")
    if _GH_PR_COMMAND_RE.search(text) or _GH_PULL_API_RE.search(text):
        surfaces.append("pr")
    if _EXPLICIT_DEPLOY_COMMAND_RE.search(text):
        surfaces.append("deployment")
    if _BROWSER_RE.search(text):
        surfaces.append("browser")
    if (
        _PRODUCTION_RE.search(text)
        and ("browser" in surfaces or "deployment" in surfaces or "--prod" in lowered)
    ) or ("browser" in surfaces and _EXTERNAL_URL_RE.search(text)):
        surfaces.append("production")
    if "browser" in surfaces and "production" in surfaces:
        surfaces.append("production_browser")
    return list(dict.fromkeys(surfaces)) or ["verification"]


def _normalized_evidence_surfaces(item: dict[str, Any]) -> list[str]:
    surface = str(item.get("surface") or "").strip()
    if surface != "ci":
        return [surface] if surface else []
    check_name = str(item.get("check_name") or "")
    detail = str(item.get("detail") or "")
    haystack = f"{check_name}\n{detail}"
    if _BROWSER_RE.search(haystack) and not _CI_COMMAND_RE.search(check_name):
        # Older ledgers could mislabel ad-hoc Playwright/Chromium browser probes
        # as CI solely because the script imported "@playwright/test". Do not let
        # that stale label contradict a later independent CI claim. New evidence
        # recording classifies these probes as browser evidence directly.
        return []
    return ["ci"]


def _is_non_verification_git_pathspec_segment(segment: str) -> bool:
    try:
        parts = shlex.split(segment)
    except ValueError:
        parts = segment.split()
    if not parts or parts[0] != "git":
        return False

    index = 1
    while index < len(parts):
        token = parts[index]
        if token in _GIT_OPTION_ARGS:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in _GIT_OPTION_ARGS):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        break

    if index >= len(parts):
        return False
    return parts[index] in (_NON_VERIFY_GIT_PATHSPEC_COMMANDS | _NON_VERIFICATION_GIT_COMMANDS)


def _is_read_only_inspection_segment(segment: str) -> bool:
    """Return True when a shell segment only displays or searches content.

    Verification-looking words in filenames (for example
    ``authenticated-qa-smoke.mjs``) must not turn ``sed``/``grep`` source
    inspection into pass/fail evidence. Actual execution of that same script
    remains verification because its executable is ``node``, not an inspector.
    """

    try:
        parts = shlex.split(segment)
    except ValueError:
        parts = segment.split()
    if not parts:
        return False
    index = 0
    while index < len(parts) and _ENV_ASSIGNMENT_RE.fullmatch(parts[index]):
        index += 1
    if index >= len(parts):
        return False
    return Path(parts[index]).name in _READ_ONLY_INSPECTION_COMMANDS


def _clean_token(token: str) -> str:
    token = token.strip()
    while token.startswith("./"):
        token = token[2:]
    return token


def _canonical_tokens(canonical: str) -> list[str]:
    try:
        return [_clean_token(t) for t in shlex.split(canonical) if t]
    except ValueError:
        return []


def _strip_command_prefix(tokens: list[str]) -> list[str]:
    remaining = list(tokens)
    if remaining and remaining[0] == "env":
        remaining = remaining[1:]
    while remaining and "=" in remaining[0] and not remaining[0].startswith("-"):
        remaining = remaining[1:]
    while remaining and remaining[0] in {"command", "time", "noglob"}:
        remaining = remaining[1:]
    return remaining


def _equivalent_needles(needle: list[str]) -> list[list[str]]:
    candidates = [needle]
    if len(needle) >= 3 and needle[1] == "run":
        package_manager = needle[0]
        script_name = needle[2]
        if package_manager in {"npm", "pnpm", "yarn", "bun"}:
            candidates.append([package_manager, script_name])
    if len(needle) == 1 and "/" in needle[0]:
        candidates.extend([["bash", needle[0]], ["sh", needle[0]]])
    if needle == ["pytest"]:
        candidates.extend(
            [
                ["python", "-m", "pytest"],
                ["python3", "-m", "pytest"],
                ["uv", "run", "pytest"],
                ["poetry", "run", "pytest"],
                ["pipenv", "run", "pytest"],
            ]
        )
    return candidates


def _canonical_match_for_tokens(
    tokens: list[str],
    canonical_commands: list[str],
) -> tuple[str, list[str]] | None:
    candidate_tokens = _strip_command_prefix(tokens)
    for canonical in canonical_commands:
        needle = _canonical_tokens(canonical)
        if not needle:
            continue
        for candidate in _equivalent_needles(needle):
            if candidate_tokens[:len(candidate)] == candidate:
                return canonical, candidate_tokens[len(candidate):]
    return None


def _is_narrow_verification_setup(tokens: list[str]) -> bool:
    """Allow only bounded environment setup before verification commands."""

    if not tokens:
        return False
    if all(_ENV_ASSIGNMENT_RE.fullmatch(token) for token in tokens):
        return True
    if tokens[0] == "export" and len(tokens) > 1:
        return all(_ENV_ASSIGNMENT_RE.fullmatch(token) for token in tokens[1:])
    if tokens[0] in {"source", "."} and len(tokens) == 2:
        return Path(tokens[1]).name == "activate"
    if len(tokens) >= 2 and tokens[0] in {"conda", "pyenv"} and tokens[1] == "activate":
        return len(tokens) == 3
    return False


def _verification_only_segments(
    command: str, *, posix: bool = True,
) -> list[list[str]] | None:
    """Parse the small accepted shell subset, failing closed on other shapes."""

    if _UNSAFE_VERIFY_SHELL_RE.search(command):
        return None
    raw_segments = _SHELL_SEGMENT_RE.split(command.strip())
    if not raw_segments or any(not segment.strip() for segment in raw_segments):
        return None
    segments: list[list[str]] = []
    for segment in raw_segments:
        try:
            tokens = shlex.split(segment, posix=posix)
        except ValueError:
            return None
        if not tokens:
            return None
        segments.append(tokens)
    return segments


def _find_canonical_match(command: str, canonical_commands: list[str]) -> tuple[str, list[str]] | None:
    # Windows paths need ``posix=False`` so backslashes survive tokenization.
    # Keep the fork's fail-closed whole-command validation in both modes.
    for posix in (True, False):
        segments = _verification_only_segments(command, posix=posix)
        if not segments:
            continue
        first_match: tuple[str, list[str]] | None = None
        verification_started = False
        eligible = True
        for tokens in segments:
            match = _canonical_match_for_tokens(tokens, canonical_commands)
            if match is not None:
                verification_started = True
                if first_match is None:
                    first_match = match
                continue
            if not verification_started and _is_narrow_verification_setup(tokens):
                continue
            # Reject trailing/parallel mutation rather than crediting an
            # earlier successful verification segment.
            eligible = False
            break
        if eligible and first_match is not None:
            return first_match
    return None


def _kind_for_command(canonical: str) -> str:
    lowered = canonical.lower()
    if any(word in lowered for word in ("lint", "eslint", "ruff")):
        return "lint"
    if any(word in lowered for word in ("typecheck", "tsc", "mypy", "pyright", "ty")):
        return "typecheck"
    if "build" in lowered:
        return "build"
    if "fmt" in lowered or "format" in lowered:
        return "format"
    if "check" in lowered and "test" not in lowered:
        return "check"
    return "test"


def _looks_like_target(arg: str) -> bool:
    if not arg or arg.startswith("-") or "=" in arg:
        return False
    return (
        "/" in arg
        or "\\" in arg
        or "::" in arg
        or arg.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".java"))
        or arg.startswith(("test_", "tests", "spec", "__tests__"))
    )


def _scope_for_args(args: list[str]) -> str:
    return "targeted" if any(_looks_like_target(arg) for arg in args) else "full"


def _is_under_temp_dir(token: str) -> bool:
    if not token or token.startswith("-"):
        return False
    try:
        path = Path(token).expanduser()
        if not path.is_absolute():
            return False
        resolved = path.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        return resolved == temp_root or temp_root in resolved.parents
    except Exception:
        return False


def _is_under_root(token: str, root: str | Path | None) -> bool:
    if not root:
        return False
    try:
        path = Path(token).expanduser().resolve()
        root_path = Path(root).expanduser().resolve()
        return path == root_path or root_path in path.parents
    except Exception:
        return False


def _is_temp_script_path(token: str, root: str | Path | None) -> bool:
    try:
        name = Path(token).expanduser().name
    except Exception:
        return False
    return (
        name.startswith(_AD_HOC_SCRIPT_NAME_PREFIXES)
        and _is_under_temp_dir(token)
        and not _is_under_root(token, root)
    )


def _ad_hoc_script_args(tokens: list[str], root: str | Path | None) -> list[str] | None:
    candidate_tokens = _strip_command_prefix(tokens)
    if not candidate_tokens:
        return None
    command = candidate_tokens[0]
    if _is_temp_script_path(command, root):
        return candidate_tokens[1:]
    if command in {"python", "python3", "node", "bash", "sh", "ruby", "perl"}:
        for idx, token in enumerate(candidate_tokens[1:], start=1):
            if token == "--":
                continue
            if _is_temp_script_path(token, root):
                return candidate_tokens[idx + 1:]
            if not token.startswith("-"):
                return None
    return None


def _find_ad_hoc_match(command: str, root: str | Path | None) -> list[str] | None:
    for posix in (True, False):
        segments = _verification_only_segments(command, posix=posix)
        if not segments:
            continue
        first_args: list[str] | None = None
        verification_started = False
        eligible = True
        for tokens in segments:
            trailing_args = _ad_hoc_script_args(tokens, root)
            if trailing_args is not None:
                verification_started = True
                if first_args is None:
                    first_args = trailing_args
                continue
            if not verification_started and _is_narrow_verification_setup(tokens):
                continue
            eligible = False
            break
        if eligible and first_args is not None:
            return first_args
    return None


def _summarize_output(output: str) -> str:
    text = (output or "").strip()
    if len(text) <= _MAX_OUTPUT_SUMMARY_CHARS:
        return text
    head = _MAX_OUTPUT_SUMMARY_CHARS // 3
    tail = _MAX_OUTPUT_SUMMARY_CHARS - head
    return text[:head] + f"\n... [{len(text) - _MAX_OUTPUT_SUMMARY_CHARS} chars omitted] ...\n" + text[-tail:]


def _prune_old_events(conn: sqlite3.Connection, *, session_id: str, root: str) -> None:
    cutoff = _retention_cutoff()
    conn.execute(
        """
        DELETE FROM verification_events
        WHERE session_id = ? AND root = ?
          AND id NOT IN (
              SELECT id FROM verification_events
              WHERE session_id = ? AND root = ?
              ORDER BY id DESC LIMIT ?
          )
        """,
        (session_id, root, session_id, root, _MAX_EVENTS_PER_SESSION_ROOT),
    )
    conn.execute(
        """
        DELETE FROM verification_events
        WHERE created_at < ?
          AND id NOT IN (
              SELECT last_event_id FROM verification_state
              WHERE last_event_id IS NOT NULL
          )
        """,
        (cutoff,),
    )


def classify_verification_command(
    command: str,
    *,
    cwd: str | Path | None = None,
    session_id: str | None = None,
    exit_code: int = 0,
    output: str = "",
) -> VerificationEvidence | None:
    if not command or not isinstance(command, str):
        return None
    try:
        from agent.coding_context import project_facts_for

        facts = project_facts_for(cwd)
    except Exception:
        facts = None
    if not facts:
        return None

    verify_commands = list(facts.get("verifyCommands") or [])
    match = _find_canonical_match(command, verify_commands)
    is_ad_hoc = False
    if match is None and not verify_commands:
        ad_hoc_args = _find_ad_hoc_match(command, facts.get("root"))
        if ad_hoc_args is not None:
            match = ("ad-hoc verification script", ad_hoc_args)
            is_ad_hoc = True
    if match is None:
        return None

    canonical, trailing_args = match
    return VerificationEvidence(
        command=command,
        canonical_command=canonical,
        kind="ad_hoc" if is_ad_hoc else _kind_for_command(canonical),
        scope="targeted" if is_ad_hoc else _scope_for_args(trailing_args),
        status="passed" if int(exit_code) == 0 else "failed",
        exit_code=int(exit_code),
        cwd=str(Path(cwd or ".").resolve()),
        root=str(facts.get("root") or Path(cwd or ".").resolve()),
        session_id=str(session_id or "default"),
        output_summary=_summarize_output(output),
    )


def _terminal_command_looks_like_verification(command: str) -> bool:
    for segment in _SHELL_SEGMENT_RE.split(command):
        segment = segment.strip()
        if (
            not segment
            or _is_non_verification_git_pathspec_segment(segment)
            or _is_read_only_inspection_segment(segment)
        ):
            continue
        if _VERIFY_COMMAND_RE.search(segment):
            return True
    return False


def classify_tool_verification_evidence(
    tool_name: str,
    tool_args: dict[str, Any] | None,
    result: Any,
    is_error: bool,
    *,
    order: int | None = None,
) -> list[dict[str, Any]]:
    """Return normalized verification evidence emitted by an explicit check.

    The classifier is intentionally conservative: terminal failures are only
    captured when the command itself looks like a verification/status/smoke
    attempt, while browser tool failures are inherently browser evidence.
    """
    name = str(tool_name or "")
    args = tool_args if isinstance(tool_args, dict) else {}
    data = _json_object(result)
    if (name.startswith("browser") or name == "read_only_verify") and not data:
        data = _last_embedded_json_object(result)
    if name.startswith("browser") and data:
        frame_top = (
            data.get("frame_tree", {}).get("top", {})
            if isinstance(data.get("frame_tree"), dict)
            else {}
        )
        full_result_text = json.dumps(
            {
                "url": data.get("url") or frame_top.get("url"),
                "origin": frame_top.get("origin"),
                "title": data.get("title"),
                "snapshot": data.get("snapshot"),
                "analysis": data.get("analysis"),
                "success": data.get("success"),
                "ok": data.get("ok"),
            },
            ensure_ascii=True,
            separators=(",", ":"),
        )
    else:
        full_result_text = str(data.get("output") or data.get("error") or result or "")
    result_text = _text(full_result_text)
    raw_check_name = str(args.get("command") or args.get("url") or args.get("route") or name)
    check_name = _text(raw_check_name, limit=160)

    if name == "verify_main_parent":
        receipt = data.get("main_branch_evidence")
        pr_receipt = data.get("pr_evidence")
        if (
            data.get("error") is None
            and isinstance(data.get("success"), bool)
            and data.get("exit_code") in {0, 1}
            and isinstance(receipt, dict)
            and isinstance(pr_receipt, dict)
        ):
            repository = str(data.get("repository") or "").lower()
            repository_root = str(data.get("repository_root") or "")
            try:
                pr_number = int(data.get("pr_number") or 0)
            except (TypeError, ValueError):
                pr_number = 0
            head_sha = str(data.get("head_sha") or "").lower()
            pr_head_sha = str(pr_receipt.get("head_sha") or "").lower()
            pr_status = str(pr_receipt.get("status") or "")
            remote_main = str(receipt.get("remote_main") or "").lower()
            commit_parent = str(receipt.get("commit_parent") or "").lower()
            status = str(receipt.get("status") or "")
            if (
                re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", repository)
                and repository_root
                and pr_number > 0
                and pr_status in {"success", "failure"}
                and status in {"success", "failure"}
                and _SHA_RE.fullmatch(head_sha)
                and _SHA_RE.fullmatch(pr_head_sha)
                and _SHA_RE.fullmatch(remote_main)
                and _SHA_RE.fullmatch(commit_parent)
                and (status == "success") == (
                    remote_main == commit_parent and head_sha == pr_head_sha
                )
                and data.get("success") == (
                    status == "success" and pr_status == "success"
                )
                and data.get("exit_code") == (
                    0 if status == "success" and pr_status == "success" else 1
                )
                and (status == "failure" or pr_status == "failure" or not is_error)
            ):
                subject = f"github:{repository}:pr:{pr_number}"
                pr_verified = bool(
                    pr_status == "success"
                    and str(pr_receipt.get("state") or "") == "closed"
                    and pr_receipt.get("merged") is False
                    and str(pr_receipt.get("base_ref") or "") == "main"
                )
                return [
                    {
                        "schema_version": 1,
                        "surface": "pr",
                        "check_name": f"typed closed PR verification {repository}#{pr_number}",
                        "status": "success" if pr_verified else "failure",
                        "order": int(order or 0),
                        "subject": subject,
                        "provenance": "typed_host",
                        "trust_rank": 2,
                        "head_sha": pr_head_sha,
                        "merged_confirmed": pr_receipt.get("merged") is True,
                        "unmerged_confirmed": pr_verified,
                        "detail": json.dumps(pr_receipt, sort_keys=True, separators=(",", ":"))[:240],
                    },
                    {
                        "schema_version": 1,
                        "surface": "main_branch",
                        "check_name": "typed PR head and origin/main parent comparison",
                        "status": status,
                        "order": int(order or 0),
                        "subject": subject,
                        "provenance": "typed_host",
                        "trust_rank": 2,
                        "head_sha": head_sha,
                        "detail": json.dumps(
                            {
                                "repository": repository,
                                "repository_root": repository_root,
                                "pr_number": pr_number,
                                "head_sha": head_sha,
                                "pr_head_sha": pr_head_sha,
                                "remote_main": remote_main,
                                "commit_parent": commit_parent,
                                "proven": True,
                            },
                            sort_keys=True,
                            separators=(",", ":"),
                        )[:240],
                    },
                ]
        return []

    if name == "read_only_verify":
        nested = (
            data.get("verification_evidence")
            if isinstance(data.get("verification_evidence"), dict)
            else {}
        )
        success = data.get("success") is True and data.get("exit_code") == 0 and not is_error
        evidence = {
            "schema_version": 1,
            "surface": "verification",
            "check_name": check_name or name,
            "status": "success" if success else "failure",
            "order": int(order or 0),
            "detail": result_text[:240],
        }
        if success and str(nested.get("status") or "") == "passed":
            repository_root = str(nested.get("repository_root") or "").strip()
            canonical_command = str(nested.get("canonical_command") or "").strip()
            scope = str(nested.get("scope") or "").strip()
            verified_head_sha = str(nested.get("verified_head_sha") or "").strip().lower()
            if (
                repository_root
                and canonical_command
                and scope in {"full", "targeted"}
                and _SHA_RE.fullmatch(verified_head_sha)
            ):
                evidence.update(
                    {
                        "repository_root": repository_root,
                        "canonical_command": canonical_command,
                        "scope": scope,
                        "verified_head_sha": verified_head_sha,
                    }
                )
        return [evidence]

    if name == "terminal" and _PROTECTED_CHECKOUT_GUARDRAIL_RE.search(result_text):
        return []

    if name == "terminal":
        authenticated_qa = _authenticated_qa_evidence(
            raw_check_name,
            full_result_text,
            is_error=is_error,
            order=order,
        )
        if authenticated_qa:
            return authenticated_qa
        github_lifecycle = _github_lifecycle_evidence(
            raw_check_name,
            full_result_text,
            is_error=is_error,
            order=order,
        )
        github_command = any(
            pattern.search(raw_check_name)
            for pattern in (_GH_PR_COMMAND_RE, _GH_PR_CHECKS_RE, _GH_CHECK_RUNS_RE, _GH_PULL_API_RE)
        )
        if github_lifecycle or github_command:
            return github_lifecycle
        closed_pr_evidence = _closed_pr_without_merge_evidence(
            raw_check_name,
            full_result_text,
            is_error=is_error,
            order=order,
        )
        if closed_pr_evidence:
            return closed_pr_evidence
        # Inspect the complete command. Compound closeout commands often put
        # formatting, staging, or synchronization before the actual tests, so
        # the verification segment may begin after the bounded display label.
        if not _terminal_command_looks_like_verification(raw_check_name):
            return []
        # A miss while resolving a workflow identity says nothing about the
        # state of CI or a deployment. In particular, ``gh run list`` exits 1
        # when a guessed display name does not exist. Recording that as a
        # failed run turns a configuration/lookup mistake into false negative
        # delivery evidence.
        if _WORKFLOW_LOOKUP_ERROR_RE.search(result_text):
            return []
    elif not name.startswith("browser") and name not in {"webfetch", "web_search"}:
        return []

    failed = bool(
        is_error or data.get("success") is False or data.get("ok") is False
    )
    timed_out = _tool_result_timed_out(data, result_text, failed=failed)
    status = "timeout" if timed_out else ("failure" if is_error else "success")
    if not is_error and name != "terminal" and data:
        if data.get("success") is False or data.get("ok") is False:
            status = "timeout" if timed_out else "failure"
        elif data.get("success") is True or data.get("ok") is True:
            status = "success"
    if name.startswith("browser") and status == "success" and (
        _BROWSER_AUTH_BOUNDARY_RE.search(result_text)
        or _BROWSER_ERROR_PAGE_RE.search(result_text)
    ):
        # Navigation transport success is not page verification. A login wall
        # leaves the requested authenticated surface unverified, and an
        # infrastructure error page is direct negative browser evidence.
        status = "failure"

    surfaces = _surfaces_for(name, raw_check_name, result_text)
    return [
        {
            "schema_version": 1,
            "surface": surface,
            "check_name": check_name or name,
            "status": status,
            "order": int(order or 0),
            "detail": result_text[:240],
        }
        for surface in surfaces
    ]


def _visual_receipt_tag(result_data: dict[str, Any]) -> dict[str, Any] | None:
    """Extract only the dedicated tool's host-produced receipt field."""

    value = result_data.get("visual_qa_receipt") if isinstance(result_data, dict) else None
    return value if isinstance(value, dict) else None


def classify_tool_visual_receipt(
    tool_name: str,
    tool_args: dict[str, Any] | None,
    result: Any,
    is_error: bool,
    *,
    order: int | None = None,
    requirement: Any = None,
) -> dict[str, Any] | None:
    """Return one host-produced receipt from the dedicated ``visual_qa`` tool.

    Generic browser/terminal/vision arguments and results can never opt into
    receipt status, so unsupported model-authored receipt tags are ignored.
    """
    name = str(tool_name or "")
    if name != "visual_qa" or not isinstance(tool_args, dict):
        return None
    raw_assertions = tool_args.get("assertions")
    if not isinstance(raw_assertions, list) or not raw_assertions:
        return None
    try:
        from agent.visual_assertions import (
            diagnose_orchestrated_visual_contract,
            validate_visual_execution_contract,
            visual_assertion_contract_id,
            visual_execution_contract_id,
            visual_requirement_for_execution_contract,
        )
        from agent.visual_qa import (
            normalize_visual_requirement,
            visual_requirement_uses_orchestrator_contract,
        )

        effective_requirement = normalize_visual_requirement(requirement)
        if effective_requirement["level"] == "none":
            effective_requirement = visual_requirement_for_execution_contract(tool_args)
        orchestrated = visual_requirement_uses_orchestrator_contract(
            effective_requirement
        )
        contract = (
            diagnose_orchestrated_visual_contract(tool_args)["contract"]
            if orchestrated
            else validate_visual_execution_contract(effective_requirement, tool_args)
        )
        assertions = contract.get("assertions") or []
    except Exception:
        return None
    # Legacy coverage validation must preserve every submitted assertion.
    # Orchestrated diagnosis already fails closed on malformed/duplicate input
    # and deliberately omits only cursorless optional diagnostics.
    if not orchestrated and len(assertions) != len(raw_assertions):
        return None
    assertion_ids = [item["id"] for item in assertions]
    contract_id = (
        visual_execution_contract_id(contract)
        if orchestrated
        else visual_assertion_contract_id(assertions)
    )
    if not contract_id:
        return None

    tag = _visual_receipt_tag(_json_object(result))
    if tag is None:
        return None
    if tag.get("contract_id") != contract_id or tag.get("assertion_ids") != assertion_ids:
        return None
    candidate = dict(tag)
    if order is not None:
        candidate["order"] = int(order)
    # A tool error cannot become a passing receipt merely because stale tag
    # data said passed.  It is still valid only if all receipt fields are
    # explicit and safe after this status correction.
    if is_error and str(candidate.get("status") or "").lower() in {"passed", "pass", "success"}:
        candidate["status"] = "failed"
    try:
        from agent.visual_qa import sanitize_visual_receipt

        return sanitize_visual_receipt(
            candidate,
            requirement=effective_requirement,
        )
    except Exception:
        return None


def latest_evidence_by_surface(evidence: Any) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not isinstance(evidence, list):
        return latest
    for item in evidence:
        if not isinstance(item, dict):
            continue
        surfaces = _normalized_evidence_surfaces(item)
        if not surfaces:
            continue
        order = int(item.get("order") or 0)
        for surface in surfaces:
            current = latest.get(surface)
            current_subject = str((current or {}).get("subject") or "")
            new_subject = str(item.get("subject") or "")
            current_status = str((current or {}).get("status") or "").lower()
            new_status = str(item.get("status") or "").lower()
            current_trust = int((current or {}).get("trust_rank") or 0)
            new_trust = int(item.get("trust_rank") or 0)
            same_subject = current_subject == new_subject
            if not same_subject and current_subject and new_subject:
                current_pr = re.fullmatch(r"github:(?:(.+):)?pr:(\d+)", current_subject)
                new_pr = re.fullmatch(r"github:(?:(.+):)?pr:(\d+)", new_subject)
                same_subject = bool(
                    current_pr
                    and new_pr
                    and current_pr.group(2) == new_pr.group(2)
                    and (
                        current_pr.group(1) is None
                        or new_pr.group(1) is None
                        or current_pr.group(1) == new_pr.group(1)
                    )
                )
            current_rank = 2 if ":pr:" in current_subject else 1 if ":commit:" in current_subject else 0
            new_rank = 2 if ":pr:" in new_subject else 1 if ":commit:" in new_subject else 0
            unrelated_lower_priority_subject = bool(
                current
                and current_subject
                and new_subject
                and not same_subject
                and new_rank < current_rank
            )
            unrelated_equal_priority_success_cannot_clear_failure = bool(
                current
                and current_subject
                and new_subject
                and not same_subject
                and new_rank == current_rank
                and current_status in {"failure", "timeout", "pending"}
                and new_status == "success"
            )
            unrelated_equal_priority_subject_is_not_a_repair = bool(
                surface in {"pr", "main_branch"}
                and current
                and current_subject
                and new_subject
                and not same_subject
                and new_rank == current_rank
            )
            lower_trust_cannot_replace = bool(
                current
                and current_subject
                and new_subject
                and same_subject
                and new_trust < current_trust
            )
            if (
                not unrelated_lower_priority_subject
                and not unrelated_equal_priority_success_cannot_clear_failure
                and not unrelated_equal_priority_subject_is_not_a_repair
                and not lower_trust_cannot_replace
                and (current is None or order >= int(current.get("order") or 0))
            ):
                normalized_item = dict(item)
                normalized_item["surface"] = surface
                latest[surface] = normalized_item
    return latest


def evidence_from_runtime_breakdown(runtime_breakdown: Any) -> list[dict[str, Any]]:
    if not isinstance(runtime_breakdown, dict):
        return []
    evidence = runtime_breakdown.get("verification_evidence")
    if isinstance(evidence, list):
        return [item for item in evidence if isinstance(item, dict)]
    return []


def visual_receipts_from_runtime_breakdown(runtime_breakdown: Any) -> list[dict[str, Any]]:
    """Return compact visual receipts carried alongside ordinary evidence."""
    if not isinstance(runtime_breakdown, dict):
        return []
    receipts = runtime_breakdown.get("visual_qa_receipts")
    if isinstance(receipts, list):
        return [item for item in receipts if isinstance(item, dict)]
    return []


def _trusted_repository_snapshot(root: str | Path | None) -> dict[str, str]:
    """Capture the exact repository root and HEAD at verification completion."""

    if not root:
        return {}
    try:
        root_path = Path(root).expanduser().resolve(strict=True)
        top_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
        repository_root = Path(str(top_result.stdout or "").strip()).resolve(strict=True)
        root_path.relative_to(repository_root)
        verified_head_sha = str(head_result.stdout or "").strip().lower()
    except Exception:
        return {}
    if (
        top_result.returncode != 0
        or head_result.returncode != 0
        or status_result.returncode != 0
        or bool(str(status_result.stdout or "").strip())
        or _SHA_RE.fullmatch(verified_head_sha) is None
    ):
        return {}
    return {
        "repository_root": str(repository_root),
        "verified_head_sha": verified_head_sha,
    }


def record_terminal_result(
    *,
    command: str,
    cwd: str | Path | None,
    session_id: str | None,
    exit_code: int,
    output: str = "",
) -> dict[str, Any] | None:
    evidence = classify_verification_command(
        command,
        cwd=cwd,
        session_id=session_id,
        exit_code=exit_code,
        output=output,
    )
    if evidence is None:
        return None

    created_at = _utc_now()
    with _DB_LOCK:
        with _connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO verification_events(
                    created_at, session_id, cwd, root, command, canonical_command,
                    kind, scope, status, exit_code, output_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    created_at,
                    evidence.session_id,
                    evidence.cwd,
                    evidence.root,
                    evidence.command,
                    evidence.canonical_command,
                    evidence.kind,
                    evidence.scope,
                    evidence.status,
                    evidence.exit_code,
                    evidence.output_summary,
                ),
            )
            if cur.lastrowid is None:
                raise RuntimeError("verification event insert did not return an id")
            event_id = int(cur.lastrowid)
            conn.execute(
                """
                INSERT INTO verification_state(
                    session_id, root, last_event_id, last_edit_at, changed_paths_json
                ) VALUES (?, ?, ?, NULL, '[]')
                ON CONFLICT(session_id, root) DO UPDATE SET
                    last_event_id = excluded.last_event_id,
                    last_edit_at = NULL,
                    changed_paths_json = '[]'
                """,
                (evidence.session_id, evidence.root, event_id),
            )
            _prune_old_events(conn, session_id=evidence.session_id, root=evidence.root)
            conn.commit()

    result = {"id": event_id, **evidence.__dict__, "created_at": created_at}
    if evidence.status == "passed":
        result.update(_trusted_repository_snapshot(evidence.root))
    return result


def mark_workspace_edited(
    *,
    session_id: str | None,
    cwd: str | Path | None,
    paths: list[str] | tuple[str, ...] | set[str] | None = None,
) -> dict[str, Any] | None:
    """Mark a workspace's verification evidence stale after a landed edit."""
    try:
        from agent.coding_context import project_facts_for

        facts = project_facts_for(cwd)
    except Exception:
        facts = None
    if not facts:
        return None

    sid = str(session_id or "default")
    root = str(facts.get("root") or Path(cwd or ".").resolve())
    changed_paths = sorted({str(p) for p in (paths or []) if p})
    edited_at = _utc_now()

    with _DB_LOCK:
        with _connect() as conn:
            row = conn.execute(
                """
                SELECT changed_paths_json FROM verification_state
                WHERE session_id = ? AND root = ?
                """,
                (sid, root),
            ).fetchone()
            existing: set[str] = set()
            if row is not None:
                try:
                    existing = set(json.loads(row["changed_paths_json"] or "[]"))
                except (TypeError, ValueError):
                    existing = set()
            merged = sorted((existing | set(changed_paths)))[-200:]
            conn.execute(
                """
                INSERT INTO verification_state(
                    session_id, root, last_event_id, last_edit_at, changed_paths_json
                ) VALUES (?, ?, NULL, ?, ?)
                ON CONFLICT(session_id, root) DO UPDATE SET
                    last_edit_at = excluded.last_edit_at,
                    changed_paths_json = excluded.changed_paths_json
                """,
                (sid, root, edited_at, json.dumps(merged)),
            )
            conn.commit()

    return {
        "session_id": sid,
        "root": root,
        "last_edit_at": edited_at,
        "changed_paths": changed_paths,
    }


def verification_status(
    *,
    session_id: str | None,
    cwd: str | Path | None,
) -> dict[str, Any]:
    try:
        from agent.coding_context import project_facts_for

        facts = project_facts_for(cwd)
    except Exception:
        facts = None
    if not facts:
        return {"status": "not_applicable", "evidence": None}

    sid = str(session_id or "default")
    root = str(facts.get("root") or Path(cwd or ".").resolve())
    with _DB_LOCK:
        with _connect() as conn:
            state = conn.execute(
                """
                SELECT last_event_id, last_edit_at, changed_paths_json
                FROM verification_state
                WHERE session_id = ? AND root = ?
                """,
                (sid, root),
            ).fetchone()
            if state is None:
                return {
                    "status": "unverified",
                    "evidence": None,
                    "root": root,
                    "session_id": sid,
                    "changed_paths": [],
                }
            event = None
            if state["last_event_id"] is not None:
                event = conn.execute(
                    "SELECT * FROM verification_events WHERE id = ?",
                    (state["last_event_id"],),
                ).fetchone()

    changed_paths: list[str] = []
    try:
        changed_paths = json.loads(state["changed_paths_json"] or "[]")
    except (TypeError, ValueError):
        changed_paths = []

    if event is None:
        return {
            "status": "unverified",
            "evidence": None,
            "root": root,
            "session_id": sid,
            "changed_paths": changed_paths,
        }

    evidence = dict(event)
    status = "stale" if state["last_edit_at"] and state["last_edit_at"] > evidence["created_at"] else evidence["status"]
    return {
        "status": status,
        "evidence": evidence,
        "root": root,
        "session_id": sid,
        "changed_paths": changed_paths,
    }


def _surface_claimed(text: str, surface: str) -> bool:
    relevant_text = "\n".join(
        line for line in str(text or "").splitlines() if "verification downgrade" not in line.lower()
    )
    if not relevant_text.strip():
        return False
    if surface in {"browser", "production", "production_browser", "ci", "deployment", "pr", "main_branch"}:
        sentences = [part for part in _SENTENCE_SPLIT_RE.split(relevant_text) if part.strip()]
        relevant = [part for part in sentences if _surface_terms_present(part, surface)]
        if not relevant:
            return False
        relevant_text = " ".join(relevant)
    claim_match = _CLAIM_WORD_RE.search(relevant_text)
    if not claim_match:
        return False
    prefix = relevant_text[max(0, claim_match.start() - 80) : claim_match.start()]
    if _NEGATED_CLAIM_RE.search(prefix):
        return False
    lowered = relevant_text.lower()
    if surface == "production_browser":
        return bool(_PRODUCTION_RE.search(relevant_text) and _BROWSER_RE.search(relevant_text))
    if surface == "browser":
        return bool(_BROWSER_RE.search(relevant_text) or "modal" in lowered or "visible" in lowered)
    if surface == "production":
        return bool(_PRODUCTION_RE.search(relevant_text))
    if surface == "ci":
        return bool(_CI_RE.search(relevant_text))
    if surface == "deployment":
        return bool(_DEPLOY_RE.search(relevant_text))
    if surface == "pr":
        return bool(_MERGE_RE.search(relevant_text))
    if surface == "main_branch":
        return bool(_MAIN_UNCHANGED_CLAIM_RE.search(relevant_text))
    return bool(_CLAIM_WORD_RE.search(relevant_text))


def _verification_check_claimed(text: str, item: dict[str, Any]) -> bool:
    lines = [
        line
        for line in str(text or "").splitlines()
        if "verification downgrade" not in line.lower()
    ]
    if any(
        re.search(
            r"\b(?:verification|checks?)\b[^\n.!?]{0,40}\b(?:passed|green|successful)\b",
            line,
            flags=re.IGNORECASE,
        )
        for line in lines
    ):
        return True

    check = str(item.get("check_name") or "").lower()
    categories = (
        (("build",), r"\bbuild\b"),
        (("test", "pytest", "vitest"), r"\b(?:tests?|pytest|vitest)\b"),
        (("lint",), r"\blint(?:ed|ing)?\b"),
        (("type-check", "typecheck", "svelte-check", "pnpm check"), r"\b(?:type[ -]?check|svelte check)\b"),
        (("git diff --check",), r"\bgit\s+diff\s+--check\b"),
    )
    for markers, pattern in categories:
        if any(marker in check for marker in markers):
            return any(_CLAIM_WORD_RE.search(line) and re.search(pattern, line, re.IGNORECASE) for line in lines)
    return _surface_claimed(text, "verification")


def _surface_terms_present(text: str, surface: str) -> bool:
    lowered = text.lower()
    if surface == "production_browser":
        return bool(_PRODUCTION_RE.search(text) and (_BROWSER_RE.search(text) or "modal" in lowered))
    if surface == "browser":
        return bool(_BROWSER_RE.search(text) or "modal" in lowered or "visible" in lowered)
    if surface == "production":
        return bool(_PRODUCTION_RE.search(text))
    if surface == "deployment":
        return bool(_DEPLOY_RE.search(text))
    if surface == "ci":
        return bool(_CI_CLAIM_RE.search(text))
    if surface == "pr":
        return bool(_MERGE_RE.search(text))
    if surface == "main_branch":
        return bool(_MAIN_UNCHANGED_CLAIM_RE.search(text))
    return True


def _clause_mentions_blocked_surface(text: str, surface: str) -> bool:
    lowered = text.lower()
    if surface == "production_browser":
        return bool(_PRODUCTION_RE.search(text) or _BROWSER_RE.search(text) or "modal" in lowered or "visible" in lowered)
    return _surface_terms_present(text, surface)


def _surface_downgraded(text: str, surface: str, item: dict[str, Any]) -> bool:
    label = _SURFACE_LABELS.get(surface, surface.replace("_", " "))
    check = str(item.get("check_name") or "").strip()
    downgrade_lines = [line for line in str(text or "").splitlines() if "verification downgrade" in line.lower()]
    for line in downgrade_lines:
        lowered = line.lower()
        if label.lower() not in lowered and not _surface_terms_present(line, surface):
            continue
        if not _NEGATED_CLAIM_RE.search(line):
            continue
        if check and check[:80].lower() not in lowered:
            continue
        return True
    return False


def claim_constraints_for_text(final_text: str, evidence: Any) -> dict[str, Any]:
    latest = latest_evidence_by_surface(evidence)
    blocked = []
    explicit_pr_subjects = set()
    claim_sentences = [
        sentence
        for sentence in _SENTENCE_SPLIT_RE.split(str(final_text or ""))
        if _UNMERGED_PR_CLAIM_RE.search(sentence)
        or _MAIN_UNCHANGED_CLAIM_RE.search(sentence)
    ]
    for sentence in claim_sentences:
        for match in _EXPLICIT_PR_CLAIM_RE.finditer(sentence):
            repository = str(
                match.group("repo") or match.group("url_repo") or ""
            ).lower()
            number = str(match.group("number") or match.group("url_number") or "")
            if number:
                explicit_pr_subjects.add(
                    f"github:{repository + ':' if repository else ''}pr:{number}"
                )
    for surface, item in sorted(latest.items()):
        status = str(item.get("status") or "").lower()
        if status not in {"failure", "timeout", "pending"}:
            continue
        if (
            _verification_check_claimed(final_text, item)
            if surface == "verification"
            else _surface_claimed(final_text, surface)
        ):
            blocked.append(
                {
                    "surface": surface,
                    "status": status,
                    "check_name": str(item.get("check_name") or "verification"),
                    "detail": str(item.get("detail") or "")[:240],
                }
            )
    if _UNMERGED_PR_CLAIM_RE.search(final_text):
        item = latest.get("pr") or {}
        evidence_subject = str(item.get("subject") or "")
        subject_matches_claim = bool(
            not explicit_pr_subjects
            or all(
                claimed == evidence_subject
                or (
                    claimed.startswith("github:pr:")
                    and evidence_subject.endswith(claimed.removeprefix("github:"))
                )
                for claimed in explicit_pr_subjects
            )
        )
        try:
            detail = json.loads(str(item.get("detail") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            detail = {}
        if (
            str(item.get("status") or "").lower() != "success"
            or item.get("unmerged_confirmed", detail.get("unmerged_confirmed")) is not True
            or not subject_matches_claim
        ):
            if not any(entry.get("surface") == "pr" for entry in blocked):
                blocked.append(
                    {
                        "surface": "pr",
                        "status": str(item.get("status") or "missing"),
                        "check_name": str(item.get("check_name") or "closed-without-merge proof"),
                        "detail": str(
                            "PR evidence target does not match the explicit claim"
                            if not subject_matches_claim
                            else item.get("detail") or "explicit unmerged proof missing"
                        )[:240],
                    }
                )
    if _MAIN_UNCHANGED_CLAIM_RE.search(final_text):
        item = latest.get("main_branch") or {}
        pr_item = latest.get("pr") or {}
        branch_subject = str(item.get("subject") or "")
        pr_subject = str(pr_item.get("subject") or "")
        branch_match = re.fullmatch(r"github:(.+):pr:(\d+)", branch_subject)
        pr_match = re.fullmatch(r"github:(?:(.+):)?pr:\d+", pr_subject)
        branch_head_sha = str(item.get("head_sha") or "").lower()
        pr_head_sha = str(pr_item.get("head_sha") or "").lower()
        if not pr_head_sha:
            try:
                pr_detail = json.loads(str(pr_item.get("detail") or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                pr_detail = {}
            pr_head_sha = str(pr_detail.get("head_sha") or "").lower()
        repository_matches = bool(
            not pr_subject
            or (
                branch_match
                and pr_match
                and pr_match.group(1)
                and branch_match.group(1) == pr_match.group(1)
                and branch_subject == pr_subject
                and _SHA_RE.fullmatch(branch_head_sha)
                and branch_head_sha == pr_head_sha
            )
        )
        explicit_target_matches = bool(
            not explicit_pr_subjects
            or all(
                claimed == branch_subject
                or (
                    claimed.startswith("github:pr:")
                    and branch_subject.endswith(claimed.removeprefix("github:"))
                )
                for claimed in explicit_pr_subjects
            )
        )
        if (
            (
                str(item.get("status") or "").lower() != "success"
                or not repository_matches
                or not explicit_target_matches
            )
            and not any(entry.get("surface") == "main_branch" for entry in blocked)
        ):
            blocked.append(
                {
                    "surface": "main_branch",
                    "status": str(item.get("status") or "missing"),
                    "check_name": str(item.get("check_name") or "main branch SHA comparison"),
                    "detail": str(
                        item.get("detail")
                        or (
                            "main branch proof does not match the explicitly claimed PR"
                            if not explicit_target_matches
                            else "main branch proof does not match the PR repository and head"
                            if not repository_matches
                            else "main branch SHA proof missing"
                        )
                    )[:240],
                }
            )
    return {
        "allowed": not blocked,
        "blocked_surfaces": blocked,
        "latest_by_surface": latest,
    }


def _blocked_surface_clause(item: dict[str, Any]) -> str:
    surface = str(item.get("surface") or "verification")
    label = _SURFACE_LABELS.get(surface, surface.replace("_", " ") + " verification")
    status = str(item.get("status") or "failed").lower()
    check = str(item.get("check_name") or "verification")
    if len(check) > 180:
        check = check[:177].rstrip() + "..."
    return f"{label} is not verified: latest check `{check}` {status}."


def _rewrite_blocked_surface_claims(final_text: str, blocked: list[dict[str, Any]], downgrade: str) -> str:
    sentences = [part for part in _SENTENCE_SPLIT_RE.split(str(final_text or "")) if part.strip()]
    if not sentences:
        return downgrade

    rewritten: list[str] = []
    inserted = False
    for sentence in sentences:
        sentence = sentence.strip()
        blocked_surfaces = [
            str(item.get("surface") or "")
            for item in blocked
            if isinstance(item, dict) and _surface_claimed(sentence, str(item.get("surface") or ""))
        ]
        if not blocked_surfaces:
            rewritten.append(sentence)
            continue

        clauses = [part.strip() for part in _CLAUSE_SPLIT_RE.split(sentence) if part.strip()]
        kept = []
        for clause in clauses:
            if any(_clause_mentions_blocked_surface(clause, surface) for surface in blocked_surfaces):
                continue
            kept.append(clause.rstrip(".!?"))
        if kept:
            rewritten.append(". ".join(kept) + ".")
        if not inserted:
            rewritten.append(downgrade)
            inserted = True

    if not inserted:
        rewritten.append(downgrade)
    return "\n\n".join(part for part in rewritten if part.strip())


def downgrade_final_response_for_evidence(final_text: str, evidence: Any) -> tuple[str, dict[str, Any]]:
    """Downgrade final-answer success claims contradicted by latest evidence.

    This runs after model synthesis and before host delivery. The evidence ledger
    remains the source of truth; this helper only adds user-visible qualifiers so
    a streamed/returned final answer cannot overclaim a failed or timed-out check.
    """
    text = str(final_text or "")
    constraints = claim_constraints_for_text(text, evidence)
    blocked = constraints.get("blocked_surfaces")
    if not text.strip() or not isinstance(blocked, list) or not blocked:
        return text, constraints

    clauses = [_blocked_surface_clause(item) for item in blocked if isinstance(item, dict)]
    seen: set[str] = set()
    unique_clauses = []
    for clause in clauses:
        if clause not in seen:
            seen.add(clause)
            unique_clauses.append(clause)
    if not unique_clauses:
        return text, constraints

    downgrade = "Verification downgrade: " + " ".join(unique_clauses)
    if downgrade.lower() in text.lower():
        return text, constraints
    return _rewrite_blocked_surface_claims(text, blocked, downgrade), constraints


def metadata_has_verified_claim(value: Any) -> bool:
    if isinstance(value, dict):
        return any(metadata_has_verified_claim(v) for v in value.values())
    if isinstance(value, list):
        return any(metadata_has_verified_claim(v) for v in value)
    if isinstance(value, str):
        return bool(re.search(r"\b(verified|shipped)\b", value, flags=re.IGNORECASE))
    return False


def downgrade_verified_metadata(value: Any, blocked_surfaces: list[dict[str, Any]]) -> Any:
    """Return metadata with explicit verified/shipped strings downgraded."""
    if not blocked_surfaces:
        return value
    if isinstance(value, dict):
        updated = {str(k): downgrade_verified_metadata(v, blocked_surfaces) for k, v in value.items()}
        if metadata_has_verified_claim(value):
            updated["verification_guard"] = {
                "status": "not_verified",
                "blocked_surfaces": blocked_surfaces,
            }
        return updated
    if isinstance(value, list):
        return [downgrade_verified_metadata(item, blocked_surfaces) for item in value]
    if isinstance(value, str):
        return re.sub(r"\b(verified|shipped)\b", "not_verified", value, flags=re.IGNORECASE)
    return value
