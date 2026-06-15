"""GitHub mention-triggered PR amendment helpers.

This module is deliberately policy-heavy and side-effect-light.  The webhook
adapter owns HTTP/HMAC/rate-limit/idempotency; this module extracts GitHub
comment/review events, verifies that they are allowed to trigger a coding job,
and builds the bounded worker prompt used by the job runner.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_ALLOWED_EVENTS_DEFAULT = {
    "issue_comment": {"created"},
    "pull_request_review_comment": {"created"},
    "pull_request_review": {"submitted"},
}


@dataclass(frozen=True)
class GitHubPrAmendRequest:
    """Normalized GitHub webhook event relevant to PR amendment."""

    event_type: str
    action: str
    repo: str
    pr_number: int
    sender: str
    body: str
    source_kind: str
    source_id: str
    source_url: str = ""
    review_state: str = ""
    review_id: str = ""
    path: str = ""
    line: int | None = None
    diff_hunk: str = ""
    delivery_id: str = ""


@dataclass(frozen=True)
class GitHubPrAmendDecision:
    """Policy decision for a normalized request."""

    accepted: bool
    reason: str
    lock_key: str = ""
    head_repo: str = ""
    head_ref: str = ""
    head_sha: str = ""
    base_repo: str = ""
    base_ref: str = ""
    pr_url: str = ""


@dataclass(frozen=True)
class GitHubPrAmendJobConfig:
    """Runtime settings for the coding job spawned after policy acceptance."""

    hermes_command: str = "hermes"
    toolsets: str = "terminal,file,web,session_search"
    timeout_seconds: int = 1800
    max_turns: int = 120
    workspace_root: str = "/home/droid/workspaces/github-pr-amend"
    source: str = "github-pr-amend"
    quiet: bool = True
    yolo: bool = True


@dataclass(frozen=True)
class GitHubPrAmendPolicy:
    """Deterministic allow policy for GitHub PR amendment triggers."""

    mention: str = "@sligo-droid"
    allowed_senders: tuple[str, ...] = ("tbrent",)
    allowed_base_repos: tuple[str, ...] = ("reserve-protocol/reserve-index-dtf",)
    allowed_head_repos: tuple[str, ...] = ("sligo-droid/reserve-index-dtf",)
    canary_prs: dict[str, tuple[int, ...]] = field(
        default_factory=lambda: {"reserve-protocol/reserve-index-dtf": (182,)}
    )
    allowed_actions: dict[str, tuple[str, ...]] = field(
        default_factory=lambda: {
            event: tuple(sorted(actions))
            for event, actions in _ALLOWED_EVENTS_DEFAULT.items()
        }
    )
    job: GitHubPrAmendJobConfig = field(default_factory=GitHubPrAmendJobConfig)


class GitHubPrAmendError(ValueError):
    """Raised when a webhook payload cannot be normalized."""


def _as_tuple(value: Any, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set)):
        return tuple(str(v) for v in value if str(v))
    return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return bool(value)


def _normalize_canary_prs(value: Any) -> dict[str, tuple[int, ...]]:
    if not isinstance(value, dict):
        return {"reserve-protocol/reserve-index-dtf": (182,)}
    result: dict[str, tuple[int, ...]] = {}
    for repo, prs in value.items():
        numbers: list[int] = []
        if isinstance(prs, int):
            numbers.append(prs)
        elif isinstance(prs, (list, tuple, set)):
            for pr in prs:
                try:
                    numbers.append(int(pr))
                except (TypeError, ValueError):
                    continue
        if numbers:
            result[str(repo)] = tuple(sorted(set(numbers)))
    return result


def _normalize_actions(value: Any) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, dict):
        return {
            event: tuple(sorted(actions))
            for event, actions in _ALLOWED_EVENTS_DEFAULT.items()
        }
    result: dict[str, tuple[str, ...]] = {}
    for event, actions in value.items():
        result[str(event)] = _as_tuple(actions)
    return result


def policy_from_route(route_config: dict[str, Any]) -> GitHubPrAmendPolicy:
    """Build a policy from a webhook route config.

    Settings may live either under ``github_pr_amend`` or directly on the route
    for short configs.  The nested block wins where present.
    """

    raw = dict(route_config)
    nested = route_config.get("github_pr_amend")
    if isinstance(nested, dict):
        raw.update(nested)

    raw_job = raw.get("job")
    job_raw: dict[str, Any] = raw_job if isinstance(raw_job, dict) else {}
    job = GitHubPrAmendJobConfig(
        hermes_command=str(job_raw.get("hermes_command") or raw.get("hermes_command") or "hermes"),
        toolsets=str(job_raw.get("toolsets") or raw.get("toolsets") or "terminal,file,web,session_search"),
        timeout_seconds=int(job_raw.get("timeout_seconds") or raw.get("timeout_seconds") or 1800),
        max_turns=int(job_raw.get("max_turns") or raw.get("max_turns") or 120),
        workspace_root=str(job_raw.get("workspace_root") or raw.get("workspace_root") or "/home/droid/workspaces/github-pr-amend"),
        source=str(job_raw.get("source") or raw.get("source") or "github-pr-amend"),
        quiet=_as_bool(job_raw.get("quiet", raw.get("quiet")), default=True),
        yolo=_as_bool(job_raw.get("yolo", raw.get("yolo")), default=True),
    )

    return GitHubPrAmendPolicy(
        mention=str(raw.get("mention") or "@sligo-droid"),
        allowed_senders=_as_tuple(raw.get("allowed_senders"), ("tbrent",)),
        allowed_base_repos=_as_tuple(
            raw.get("allowed_base_repos"), ("reserve-protocol/reserve-index-dtf",)
        ),
        allowed_head_repos=_as_tuple(
            raw.get("allowed_head_repos"), ("sligo-droid/reserve-index-dtf",)
        ),
        canary_prs=_normalize_canary_prs(raw.get("canary_prs")),
        allowed_actions=_normalize_actions(raw.get("allowed_actions")),
        job=job,
    )


def _dig(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return default
        value = value.get(key)
    return default if value is None else value


def _require_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise GitHubPrAmendError(f"Missing/invalid {field_name}") from None


def extract_request(
    event_type: str,
    payload: dict[str, Any],
    *,
    delivery_id: str = "",
) -> GitHubPrAmendRequest:
    """Normalize supported GitHub webhook payloads.

    Raises ``GitHubPrAmendError`` when the event is not a supported PR-amend
    trigger shape.
    """

    repo = str(_dig(payload, "repository", "full_name", default="") or "")
    sender = str(_dig(payload, "sender", "login", default="") or "")
    action = str(payload.get("action") or "")

    if event_type == "issue_comment":
        if not _dig(payload, "issue", "pull_request", default=None):
            raise GitHubPrAmendError("issue_comment is not on a pull request")
        pr_number = _require_int(_dig(payload, "issue", "number"), "issue.number")
        body = str(_dig(payload, "comment", "body", default="") or "")
        source_id = str(_dig(payload, "comment", "id", default="") or "")
        source_url = str(_dig(payload, "comment", "html_url", default="") or "")
        return GitHubPrAmendRequest(
            event_type=event_type,
            action=action,
            repo=repo,
            pr_number=pr_number,
            sender=sender,
            body=body,
            source_kind="issue_comment",
            source_id=source_id,
            source_url=source_url,
            delivery_id=delivery_id,
        )

    if event_type == "pull_request_review_comment":
        pr_number = _require_int(
            _dig(payload, "pull_request", "number"), "pull_request.number"
        )
        body = str(_dig(payload, "comment", "body", default="") or "")
        line_raw = _dig(payload, "comment", "line", default=None)
        try:
            line = int(line_raw) if line_raw is not None else None
        except (TypeError, ValueError):
            line = None
        return GitHubPrAmendRequest(
            event_type=event_type,
            action=action,
            repo=repo,
            pr_number=pr_number,
            sender=sender,
            body=body,
            source_kind="review_comment",
            source_id=str(_dig(payload, "comment", "id", default="") or ""),
            source_url=str(_dig(payload, "comment", "html_url", default="") or ""),
            review_id=str(_dig(payload, "comment", "pull_request_review_id", default="") or ""),
            path=str(_dig(payload, "comment", "path", default="") or ""),
            line=line,
            diff_hunk=str(_dig(payload, "comment", "diff_hunk", default="") or ""),
            delivery_id=delivery_id,
        )

    if event_type == "pull_request_review":
        pr_number = _require_int(
            _dig(payload, "pull_request", "number"), "pull_request.number"
        )
        body = str(_dig(payload, "review", "body", default="") or "")
        return GitHubPrAmendRequest(
            event_type=event_type,
            action=action,
            repo=repo,
            pr_number=pr_number,
            sender=sender,
            body=body,
            source_kind="review",
            source_id=str(_dig(payload, "review", "id", default="") or ""),
            source_url=str(_dig(payload, "review", "html_url", default="") or ""),
            review_id=str(_dig(payload, "review", "id", default="") or ""),
            review_state=str(_dig(payload, "review", "state", default="") or ""),
            delivery_id=delivery_id,
        )

    raise GitHubPrAmendError(f"Unsupported GitHub event for PR amendment: {event_type}")


def preflight_request(
    request: GitHubPrAmendRequest,
    policy: GitHubPrAmendPolicy,
) -> str | None:
    """Reject requests that do not need live PR metadata.

    This keeps arbitrary third-party mentions from causing even a GitHub API
    lookup. Full branch/head validation still happens in ``evaluate_request``
    after fetching trusted PR metadata.
    """

    allowed_actions = policy.allowed_actions.get(request.event_type, ())
    if request.action not in allowed_actions:
        return f"action '{request.action}' not allowed for {request.event_type}"

    if request.sender not in policy.allowed_senders:
        return f"sender '{request.sender}' is not allowlisted"

    if policy.mention.lower() not in request.body.lower():
        return f"missing mention {policy.mention}"

    if request.repo not in policy.allowed_base_repos:
        return f"base repo '{request.repo}' is not allowlisted"

    if policy.canary_prs:
        allowed_numbers = policy.canary_prs.get(request.repo, ())
        if request.pr_number not in allowed_numbers:
            return f"PR #{request.pr_number} is outside canary allowlist"

    return None


def _repo_full_name_from_pr(pr_info: dict[str, Any], side: str) -> str:
    return str(_dig(pr_info, side, "repo", "full_name", default="") or "")


def evaluate_request(
    request: GitHubPrAmendRequest,
    pr_info: dict[str, Any],
    policy: GitHubPrAmendPolicy,
) -> GitHubPrAmendDecision:
    """Return a deterministic allow/deny decision for a request."""

    preflight_reason = preflight_request(request, policy)
    if preflight_reason:
        return GitHubPrAmendDecision(False, preflight_reason)

    base_repo = _repo_full_name_from_pr(pr_info, "base") or request.repo
    head_repo = _repo_full_name_from_pr(pr_info, "head")
    base_ref = str(_dig(pr_info, "base", "ref", default="") or "")
    head_ref = str(_dig(pr_info, "head", "ref", default="") or "")
    head_sha = str(_dig(pr_info, "head", "sha", default="") or "")
    pr_url = str(pr_info.get("html_url") or request.source_url or "")
    state = str(pr_info.get("state") or "").lower()

    if state and state != "open":
        return GitHubPrAmendDecision(False, f"PR is not open (state={state})")

    if base_repo not in policy.allowed_base_repos:
        return GitHubPrAmendDecision(False, f"base repo '{base_repo}' is not allowlisted")

    if policy.canary_prs:
        allowed_numbers = policy.canary_prs.get(base_repo, ())
        if request.pr_number not in allowed_numbers:
            return GitHubPrAmendDecision(False, f"PR #{request.pr_number} is outside canary allowlist")

    if head_repo not in policy.allowed_head_repos:
        return GitHubPrAmendDecision(False, f"head repo '{head_repo}' is not allowlisted")

    if not head_ref:
        return GitHubPrAmendDecision(False, "PR head ref is missing")

    lock_key = f"{head_repo}:{head_ref}"
    return GitHubPrAmendDecision(
        True,
        "accepted",
        lock_key=lock_key,
        head_repo=head_repo,
        head_ref=head_ref,
        head_sha=head_sha,
        base_repo=base_repo,
        base_ref=base_ref,
        pr_url=pr_url,
    )


def fetch_pr_info(repo: str, pr_number: int, *, gh_command: str = "gh") -> dict[str, Any]:
    """Fetch PR metadata through GitHub CLI and return JSON."""

    result = subprocess.run(
        [gh_command, "api", f"repos/{repo}/pulls/{pr_number}"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh api failed with {result.returncode}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("gh api returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("gh api returned non-object PR metadata")
    return data


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-._")
    return slug[:120] or "github-pr"


def job_workspace(policy: GitHubPrAmendPolicy, decision: GitHubPrAmendDecision, request: GitHubPrAmendRequest) -> Path:
    repo_slug = safe_slug(request.repo.replace("/", "-"))
    return Path(policy.job.workspace_root).expanduser() / f"{repo_slug}-pr-{request.pr_number}"


def write_job_brief(
    request: GitHubPrAmendRequest,
    decision: GitHubPrAmendDecision,
    policy: GitHubPrAmendPolicy,
    pr_info: dict[str, Any],
) -> Path:
    """Persist a structured job brief under HERMES_HOME for auditability."""

    root = get_hermes_home() / "github-pr-amend" / safe_slug(request.repo) / str(request.pr_number)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{safe_slug(request.delivery_id or request.source_id or 'event')}.json"
    data = {
        "request": request.__dict__,
        "decision": decision.__dict__,
        "policy": {
            "mention": policy.mention,
            "allowed_senders": policy.allowed_senders,
            "allowed_base_repos": policy.allowed_base_repos,
            "allowed_head_repos": policy.allowed_head_repos,
            "canary_prs": policy.canary_prs,
            "allowed_actions": policy.allowed_actions,
            "job": policy.job.__dict__,
        },
        "pr": {
            "url": decision.pr_url,
            "base_repo": decision.base_repo,
            "base_ref": decision.base_ref,
            "head_repo": decision.head_repo,
            "head_ref": decision.head_ref,
            "head_sha": decision.head_sha,
            "title": pr_info.get("title"),
            "body": pr_info.get("body"),
        },
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def build_worker_prompt(
    request: GitHubPrAmendRequest,
    decision: GitHubPrAmendDecision,
    policy: GitHubPrAmendPolicy,
    pr_info: dict[str, Any],
    *,
    brief_path: Path,
) -> str:
    """Build the one-shot Hermes coding-worker prompt."""

    workspace = job_workspace(policy, decision, request)
    source_context = {
        "event_type": request.event_type,
        "action": request.action,
        "source_kind": request.source_kind,
        "source_id": request.source_id,
        "source_url": request.source_url,
        "review_state": request.review_state,
        "review_id": request.review_id,
        "path": request.path,
        "line": request.line,
        "diff_hunk": request.diff_hunk,
        "request_body": request.body,
    }
    return f"""You are the `sligo-droid` GitHub bot identity handling a trusted PR amendment request from `tbrent`.

Goal: amend the PR branch in response to the tagged GitHub request, verify the change, push a commit to the PR head branch, and comment the result back on the PR.

Hard policy:
- You MAY clone/fetch/checkout, edit files, run tests/builds, commit, push to the exact PR head branch, and comment the result.
- You MUST NOT merge, approve reviews, request reviews, deploy, publish releases, change repo settings, send/draft/reply/forward email, or push to any ref other than the exact PR head branch below.
- Preserve unrelated user changes if the workspace already exists. Start by inspecting git status.
- If the requested change is ambiguous, comment a concise clarification instead of guessing and do not push a commit.
- If a blocker prevents a safe commit, comment the blocker and do not push a partial/broken commit.

PR:
- URL: {decision.pr_url}
- Base: {decision.base_repo}:{decision.base_ref}
- Head: {decision.head_repo}:{decision.head_ref}
- Expected starting head SHA: {decision.head_sha}
- Workspace: {workspace}
- Audit brief: {brief_path}

Triggering GitHub context (trusted because only `tbrent` may trigger this route):
```json
{json.dumps(source_context, indent=2, ensure_ascii=False)}
```

Before editing, fetch complete current context, including Changes Requested / review feedback:
- `gh api repos/{decision.base_repo}/pulls/{request.pr_number}`
- `gh api repos/{decision.base_repo}/pulls/{request.pr_number}/reviews`
- `gh api repos/{decision.base_repo}/pulls/{request.pr_number}/comments`
- `gh api repos/{decision.base_repo}/issues/{request.pr_number}/comments`
- `gh pr diff {request.pr_number} --repo {decision.base_repo}`

Checkout/push constraints:
1. Use or create the workspace shown above under `/home/droid/workspaces/`.
2. Fetch the PR head repo `{decision.head_repo}` and branch `{decision.head_ref}`.
3. Reset to the expected starting head SHA `{decision.head_sha}` if it is still current; if the remote branch moved, inspect and continue only if safe.
4. Add/fetch upstream `{decision.base_repo}` for base context if needed.
5. Push only to `{decision.head_repo}` branch `{decision.head_ref}`.

Final GitHub comment requirements:
- Reply/comment on PR #{request.pr_number} in `{decision.base_repo}`.
- Include summary, commit SHA if pushed, commands/tests run, and caveats/blockers.
- If no commit was pushed, say that explicitly.

PR title: {pr_info.get('title') or ''}
PR body:
{pr_info.get('body') or ''}
"""


def build_hermes_command(prompt: str, policy: GitHubPrAmendPolicy) -> list[str]:
    """Return argv for a one-shot Hermes worker run."""

    argv = [
        policy.job.hermes_command,
        "chat",
        "--source",
        policy.job.source,
        "--toolsets",
        policy.job.toolsets,
        "--max-turns",
        str(policy.job.max_turns),
        "--query",
        prompt,
    ]
    if policy.job.quiet:
        argv.insert(2, "--quiet")
    if policy.job.yolo:
        argv.insert(2, "--yolo")
    return argv


def run_worker_command(prompt: str, policy: GitHubPrAmendPolicy) -> subprocess.CompletedProcess[str]:
    """Run the one-shot Hermes worker command."""

    argv = build_hermes_command(prompt, policy)
    logger.info("[github-pr-amend] starting worker command: %s", argv[:6] + ["..."])
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=policy.job.timeout_seconds,
    )
