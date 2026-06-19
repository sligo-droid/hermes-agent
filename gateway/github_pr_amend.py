"""GitHub mention-triggered PR amendment helpers.

This module is deliberately policy-heavy and side-effect-light.  The webhook
adapter owns HTTP/HMAC/rate-limit/idempotency; this module extracts GitHub
comment/review events, verifies that they are allowed to route to worker-board
intake, and builds the Discord/Kanban worker-board artifact.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any, Sequence

from hermes_constants import get_hermes_home
from hermes_cli.github_remote import github_cli_env
from utils import atomic_json_write

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
    source_node_id: str = ""
    source_url: str = ""
    review_state: str = ""
    review_id: str = ""
    pr_author: str = ""
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
    """Legacy route settings still parsed from existing PR-amend config."""

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
    allowed_base_repos: tuple[str, ...] = ("reserve-protocol/*",)
    allowed_head_repos: tuple[str, ...] = ("sligo-droid/*", "reserve-protocol/*")
    canary_prs: dict[str, tuple[int, ...]] = field(default_factory=dict)
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


def _matches_repo_allowlist(repo: str, patterns: Sequence[str]) -> bool:
    return bool(repo and patterns and any(fnmatchcase(repo, pattern) for pattern in patterns))


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
        return {}
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
            raw.get("allowed_base_repos"), ("reserve-protocol/*",)
        ),
        allowed_head_repos=_as_tuple(
            raw.get("allowed_head_repos"), ("sligo-droid/*", "reserve-protocol/*")
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
        source_node_id = str(_dig(payload, "comment", "node_id", default="") or "")
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
            source_node_id=source_node_id,
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
            source_node_id=str(_dig(payload, "comment", "node_id", default="") or ""),
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
            source_node_id=str(_dig(payload, "review", "node_id", default="") or ""),
            source_url=str(_dig(payload, "review", "html_url", default="") or ""),
            review_id=str(_dig(payload, "review", "id", default="") or ""),
            review_state=str(_dig(payload, "review", "state", default="") or ""),
            pr_author=str(_dig(payload, "pull_request", "user", "login", default="") or ""),
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

    if not _matches_repo_allowlist(request.repo, policy.allowed_base_repos):
        return f"base repo '{request.repo}' is not allowlisted"

    if policy.canary_prs:
        allowed_numbers = policy.canary_prs.get(request.repo, ())
        if request.pr_number not in allowed_numbers:
            return f"PR #{request.pr_number} is outside canary allowlist"

    if policy.mention.lower() not in request.body.lower():
        expected_pr_author = policy.mention.lstrip("@").lower()
        if request.event_type != "pull_request_review":
            return f"missing mention {policy.mention}"
        if request.pr_author and request.pr_author.lower() != expected_pr_author:
            return f"missing mention {policy.mention}; PR author '{request.pr_author}' is not {expected_pr_author}"

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

    if policy.mention.lower() not in request.body.lower():
        expected_pr_author = policy.mention.lstrip("@").lower()
        pr_author = str(_dig(pr_info, "user", "login", default="") or request.pr_author or "")
        if request.event_type != "pull_request_review" or pr_author.lower() != expected_pr_author:
            if request.event_type == "pull_request_review" and pr_author:
                return GitHubPrAmendDecision(
                    False,
                    f"missing mention {policy.mention}; PR author '{pr_author}' is not {expected_pr_author}",
                )
            return GitHubPrAmendDecision(False, f"missing mention {policy.mention}")

    if not _matches_repo_allowlist(base_repo, policy.allowed_base_repos):
        return GitHubPrAmendDecision(False, f"base repo '{base_repo}' is not allowlisted")

    if policy.canary_prs:
        allowed_numbers = policy.canary_prs.get(base_repo, ())
        if request.pr_number not in allowed_numbers:
            return GitHubPrAmendDecision(False, f"PR #{request.pr_number} is outside canary allowlist")

    if not _matches_repo_allowlist(head_repo, policy.allowed_head_repos):
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
        env=github_cli_env(),
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


def _parse_gh_json_documents(stdout: str) -> list[Any]:
    """Parse one or more JSON documents emitted by ``gh api --paginate``."""

    decoder = json.JSONDecoder()
    documents: list[Any] = []
    index = 0
    while index < len(stdout):
        while index < len(stdout) and stdout[index].isspace():
            index += 1
        if index >= len(stdout):
            break
        try:
            document, index = decoder.raw_decode(stdout, index)
        except json.JSONDecodeError as exc:
            raise RuntimeError("gh api returned invalid JSON") from exc
        documents.append(document)
    return documents


def _fetch_pr_api_json(
    repo: str,
    path: str,
    *,
    gh_command: str = "gh",
    list_response: bool = False,
) -> Any:
    endpoint = f"repos/{repo}/{path.lstrip('/')}"
    cmd = [gh_command, "api", endpoint]
    if list_response:
        separator = "&" if "?" in endpoint else "?"
        cmd = [
            gh_command,
            "api",
            "--paginate",
            f"{endpoint}{separator}per_page=100",
        ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=30,
        env=github_cli_env(),
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"gh api failed with {result.returncode}")
    if list_response:
        pages = _parse_gh_json_documents(result.stdout)
        if len(pages) == 1 and isinstance(pages[0], list) and all(isinstance(page, list) for page in pages[0]):
            pages = pages[0]
        if all(isinstance(page, list) for page in pages):
            return [item for page in pages for item in page]
    else:
        pages = _parse_gh_json_documents(result.stdout)
        if len(pages) == 1:
            return pages[0]
    raise RuntimeError("gh api returned invalid JSON")


def fetch_pr_related_context(
    repo: str,
    pr_number: int,
    *,
    gh_command: str = "gh",
) -> dict[str, Any]:
    """Fetch review/comment context needed by worker-board PR amendment."""

    context = {
        "reviews": _fetch_pr_api_json(
            repo,
            f"pulls/{pr_number}/reviews",
            gh_command=gh_command,
            list_response=True,
        ),
        "review_comments": _fetch_pr_api_json(
            repo,
            f"pulls/{pr_number}/comments",
            gh_command=gh_command,
            list_response=True,
        ),
        "issue_comments": _fetch_pr_api_json(
            repo,
            f"issues/{pr_number}/comments",
            gh_command=gh_command,
            list_response=True,
        ),
    }
    for key, value in context.items():
        if not isinstance(value, list):
            raise RuntimeError(f"gh api returned non-list {key}")
    return context


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-._")
    return slug[:120] or "github-pr"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_payload_subset(payload: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "action",
        "repository",
        "sender",
        "issue",
        "pull_request",
        "comment",
        "review",
    )
    return {key: payload[key] for key in keys if key in payload}


def _repo_info(payload: dict[str, Any], request: GitHubPrAmendRequest) -> dict[str, Any]:
    repo = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    owner = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
    return {
        "full_name": str(repo.get("full_name") or request.repo),
        "name": str(repo.get("name") or request.repo.rsplit("/", 1)[-1]),
        "owner": str(owner.get("login") or request.repo.split("/", 1)[0]),
        "html_url": str(repo.get("html_url") or f"https://github.com/{request.repo}"),
    }


def _source_info(payload: dict[str, Any], request: GitHubPrAmendRequest) -> dict[str, Any]:
    raw = {}
    if request.source_kind == "issue_comment":
        raw = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}
    elif request.source_kind == "review_comment":
        raw = payload.get("comment") if isinstance(payload.get("comment"), dict) else {}
    elif request.source_kind == "review":
        raw = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    return {
        "kind": request.source_kind,
        "id": request.source_id,
        "node_id": str(raw.get("node_id") or ""),
        "url": str(raw.get("url") or ""),
        "html_url": request.source_url or str(raw.get("html_url") or ""),
        "body": request.body,
        "path": request.path or str(raw.get("path") or ""),
        "line": request.line,
        "original_line": raw.get("original_line"),
        "diff_hunk": request.diff_hunk,
        "review_id": request.review_id,
        "review_state": request.review_state,
    }


def build_pr_amend_operational_instructions(
    request: GitHubPrAmendRequest,
    decision: GitHubPrAmendDecision,
) -> str:
    target_repo = decision.head_repo or "the PR head repo"
    target_ref = decision.head_ref or "the PR head branch"
    upstream_repo = decision.base_repo or request.repo
    return f"""GitHub PR-amend operational instructions:
- Do not post GitHub text comments, replies, review comments, or reviews.
- Use GitHub reactions for seen/in-progress/done/error status when available.
- Accepted intake must be handled through the Command Center/Discord worker-board path and produce the corresponding worker-board embed/thread, like an approved Command Center job.
- Implement the requested amendment in a worker worktree using the Discord/Kanban worker-board flow.
- Target checkout/repo for implementation and PR lifecycle: `{target_repo}`.
- Target base branch for the worker PR: `{target_ref}`.
- The upstream `{upstream_repo}` PR #{request.pr_number} is review context only; do not open, close, review, or merge that upstream PR as part of amendment work.
- Open and merge a PR in `{target_repo}` with base `{target_ref}` so the existing upstream PR head branch advances.
- Final public GitHub output is pushed commits/PRs plus reactions only.
- Preserve unrelated user changes and report blockers in the worker-board thread, not on GitHub.
""".strip()


def build_pr_amend_intake_artifact(
    request: GitHubPrAmendRequest,
    decision: GitHubPrAmendDecision,
    policy: GitHubPrAmendPolicy,
    pr_info: dict[str, Any],
    payload: dict[str, Any],
    fetched_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _utc_now_iso()
    fetched_context = fetched_context or {}
    return {
        "artifact_version": 1,
        "created_at": now,
        "updated_at": now,
        "delivery_id": request.delivery_id,
        "event": {"type": request.event_type, "action": request.action},
        "sender": {
            "login": request.sender,
            "id": _dig(payload, "sender", "id", default=None),
        },
        "repository": _repo_info(payload, request),
        "pull_request": {
            "number": request.pr_number,
            "title": str(pr_info.get("title") or ""),
            "url": str(pr_info.get("url") or ""),
            "html_url": decision.pr_url,
            "state": str(pr_info.get("state") or ""),
            "body": str(pr_info.get("body") or ""),
            "head": {
                "repo": decision.head_repo,
                "ref": decision.head_ref,
                "sha": decision.head_sha,
                "owner": decision.head_repo.split("/", 1)[0] if decision.head_repo else "",
            },
            "base": {
                "repo": decision.base_repo,
                "ref": decision.base_ref,
                "owner": decision.base_repo.split("/", 1)[0] if decision.base_repo else "",
            },
        },
        "source": _source_info(payload, request),
        "fetched_context": {
            "pull_request": pr_info,
            "reviews": fetched_context.get("reviews") or [],
            "review_comments": fetched_context.get("review_comments") or [],
            "issue_comments": fetched_context.get("issue_comments") or [],
        },
        "normalized_payload": _safe_payload_subset(payload),
        "policy_decision": {
            "accepted": decision.accepted,
            "reason": decision.reason,
            "lock_key": decision.lock_key,
            "matched_gates": {
                "mention": policy.mention,
                "allowed_sender": request.sender in policy.allowed_senders,
                "allowed_base_repos": policy.allowed_base_repos,
                "allowed_head_repos": policy.allowed_head_repos,
                "allowed_actions": policy.allowed_actions,
                "canary_prs": policy.canary_prs,
            },
        },
        "operational_instructions": build_pr_amend_operational_instructions(request, decision),
    }


def _pr_amend_source_key(artifact: dict[str, Any]) -> str:
    kind = str(_dig(artifact, "source", "kind", default="") or "").strip()
    source_id = str(_dig(artifact, "source", "id", default="") or "").strip()
    if kind and source_id:
        return f"github-pr-amend:{kind}:{source_id}"
    delivery_id = str(artifact.get("delivery_id") or "").strip()
    if delivery_id:
        return f"github-pr-amend:delivery:{delivery_id}"
    return ""


def _pr_amend_requires_head_sha_advance(artifact: dict[str, Any]) -> bool:
    source = artifact.get("source") if isinstance(artifact.get("source"), dict) else {}
    source_kind = str(source.get("kind") or "").strip()
    review_state = str(source.get("review_state") or "").strip().upper()
    if source_kind == "review_comment":
        return True
    if source_kind == "review" and review_state == "CHANGES_REQUESTED":
        return True
    return False


def _compact_pr_amend_text(value: Any, *, limit: int | None = None) -> str:
    text = str(value or "").strip()
    if limit is None:
        return text
    if len(text) <= limit:
        return text
    return text[: limit - 15].rstrip() + " ... [truncated]"


def _line_value(comment: dict[str, Any]) -> str:
    line = comment.get("line") or comment.get("original_line") or comment.get("position")
    return str(line or "").strip()


def _comment_review_id(comment: dict[str, Any]) -> str:
    return str(comment.get("pull_request_review_id") or comment.get("review_id") or "").strip()


def _format_review_comment(comment: dict[str, Any], index: int) -> list[str]:
    comment_id = str(comment.get("id") or "").strip()
    review_id = _comment_review_id(comment)
    path = str(comment.get("path") or "").strip()
    line = _line_value(comment)
    url = str(comment.get("html_url") or comment.get("url") or "").strip()
    header_parts = [f"{index}."]
    if path:
        header_parts.append(path)
    if line:
        header_parts.append(f"line {line}")
    if comment_id:
        header_parts.append(f"comment {comment_id}")
    if review_id:
        header_parts.append(f"review {review_id}")
    lines = [" ".join(header_parts).strip()]
    if url:
        lines.append(f"   URL: {url}")
    body = _compact_pr_amend_text(comment.get("body"))
    if body:
        lines.append(f"   Body: {body}")
    diff_hunk = _compact_pr_amend_text(comment.get("diff_hunk"), limit=800)
    if diff_hunk:
        lines.append(f"   Diff hunk: {diff_hunk}")
    return lines


def _github_review_context_block(artifact: dict[str, Any]) -> str:
    source = artifact.get("source") if isinstance(artifact.get("source"), dict) else {}
    fetched = artifact.get("fetched_context") if isinstance(artifact.get("fetched_context"), dict) else {}
    reviews = [item for item in fetched.get("reviews") or [] if isinstance(item, dict)]
    comments = [item for item in fetched.get("review_comments") or [] if isinstance(item, dict)]
    source_kind = str(source.get("kind") or "").strip()
    source_id = str(source.get("id") or "").strip()
    source_review_id = str(source.get("review_id") or "").strip()
    source_url = str(source.get("html_url") or "").strip()

    lines = ["GitHub review context:"]
    trigger = " ".join(
        part
        for part in (
            f"kind={source_kind}" if source_kind else "",
            f"id={source_id}" if source_id else "",
            f"review_id={source_review_id}" if source_review_id else "",
        )
        if part
    )
    if trigger:
        lines.append(f"Trigger: {trigger}")
    if source_url:
        lines.append(f"Trigger URL: {source_url}")
    if source.get("path"):
        line = str(source.get("line") or source.get("original_line") or "").strip()
        lines.append(f"Trigger location: {source.get('path')}{f' line {line}' if line else ''}")

    matching_review = None
    review_match_id = source_review_id if source_kind == "review_comment" else source_id or source_review_id
    if review_match_id:
        matching_review = next((review for review in reviews if str(review.get("id") or "") == review_match_id), None)
    if matching_review:
        state = str(matching_review.get("state") or "").strip()
        body = _compact_pr_amend_text(matching_review.get("body"))
        lines.append(f"Review {review_match_id}{f' ({state})' if state else ''}:")
        if body:
            lines.append(f"Review body: {body}")

    selected_comments = comments
    if source_kind == "review":
        selected_comments = [comment for comment in comments if _comment_review_id(comment) == source_id]
    elif source_kind == "review_comment":
        selected_comments = [comment for comment in comments if str(comment.get("id") or "") == source_id]
        if source_review_id:
            siblings = [comment for comment in comments if _comment_review_id(comment) == source_review_id]
            selected_comments = selected_comments + [
                comment for comment in siblings if str(comment.get("id") or "") != source_id
            ]
    if not selected_comments and comments:
        selected_comments = comments

    if selected_comments:
        lines.append(f"Inline review comments ({len(selected_comments)}):")
        for index, comment in enumerate(selected_comments, start=1):
            lines.extend(_format_review_comment(comment, index))
    elif source_kind in {"review", "review_comment"}:
        lines.append("Inline review comments: none found in fetched context; see artifact for raw payload.")

    return "\n".join(line for line in lines if line).strip()


def write_pr_amend_intake_artifact(artifact: dict[str, Any]) -> Path:
    repo = str(_dig(artifact, "repository", "full_name", default="github-pr-amend"))
    pr_number = str(_dig(artifact, "pull_request", "number", default="unknown"))
    delivery_id = str(artifact.get("delivery_id") or _dig(artifact, "source", "id", default="event"))
    root = get_hermes_home() / "gateway" / "github_pr_amend" / "intakes" / safe_slug(repo) / safe_slug(pr_number)
    path = root / f"{safe_slug(delivery_id)}.json"
    atomic_json_write(path, artifact, indent=2, mode=0o600)
    return path


def resolve_pr_amend_discord_channel(route_config: dict[str, Any], request: GitHubPrAmendRequest) -> str:
    raw = dict(route_config)
    nested = route_config.get("github_pr_amend")
    if isinstance(nested, dict):
        raw.update(nested)
    for key in ("discord_channel_id", "channel_id"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    project_candidates = [
        raw.get("project"),
        raw.get("project_key"),
        request.repo,
        request.repo.rsplit("/", 1)[-1] if request.repo else "",
    ]
    try:
        from self_improvement.discord_publish import configured_project_channel_id
    except Exception as exc:
        logger.debug("[github-pr-amend] Discord channel resolver unavailable: %s", exc)
        return ""
    for candidate in project_candidates:
        channel_id = configured_project_channel_id(candidate)
        if channel_id:
            return channel_id
    return ""


def build_pr_amend_discord_card(
    artifact: dict[str, Any],
    *,
    artifact_path: Path,
) -> dict[str, Any]:
    repo = str(_dig(artifact, "repository", "full_name", default=""))
    pr_number = str(_dig(artifact, "pull_request", "number", default=""))
    pr_title = str(_dig(artifact, "pull_request", "title", default=""))
    source_url = str(_dig(artifact, "source", "html_url", default=""))
    source_kind = str(_dig(artifact, "source", "kind", default=""))
    source_id = str(_dig(artifact, "source", "id", default=""))
    source_node_id = str(_dig(artifact, "source", "node_id", default=""))
    source_review_state = str(_dig(artifact, "source", "review_state", default=""))
    sender = str(_dig(artifact, "sender", "login", default=""))
    body = str(_dig(artifact, "source", "body", default=""))
    review_context = _github_review_context_block(artifact)
    instructions = str(artifact.get("operational_instructions") or "")
    head_repo = str(_dig(artifact, "pull_request", "head", "repo", default=""))
    head_ref = str(_dig(artifact, "pull_request", "head", "ref", default=""))
    head_sha = str(_dig(artifact, "pull_request", "head", "sha", default=""))
    base_repo = str(_dig(artifact, "pull_request", "base", "repo", default=""))
    base_ref = str(_dig(artifact, "pull_request", "base", "ref", default=""))
    title = f"GitHub PR amend: {repo}#{pr_number}"
    summary = f"{sender} requested an amendment on `{repo}` PR #{pr_number}: {pr_title}"
    requires_head_sha_advance = _pr_amend_requires_head_sha_advance(artifact)
    source_body = _compact_pr_amend_text(body, limit=1000)
    source_criterion = (
        f"Address the triggering {source_kind or 'GitHub'} request verbatim: {source_body}"
        if source_body
        else "Address the triggering GitHub PR-amend request."
    )
    request_body = "\n\n".join(
        part
        for part in (
            f"Source: {source_url}" if source_url else "",
            f"Artifact: {artifact_path}",
            "Requested change:",
            body,
            review_context,
            instructions,
        )
        if part
    )
    return {
        "kind": "github_pr_amend",
        "title": title,
        "summary": summary,
        "body": request_body,
        "project": repo.rsplit("/", 1)[-1] or repo or "github-pr-amend",
        "prong": "github-pr-amend",
        "priority": "high",
        "proposal_id": str(artifact.get("delivery_id") or ""),
        "rationale": "Accepted signed GitHub PR-amend webhook routed through the Command Center/Discord worker-board path.",
        "acceptance_criteria": [
            source_criterion,
            "Do not post GitHub text comments or reviews.",
            "Accepted intake must produce and use the corresponding Discord worker-board embed/thread, like an approved Command Center job.",
            f"Target repo for checkout/PR lifecycle is `{head_repo}`; upstream `{base_repo}` is review context only.",
            f"Open and merge a PR in `{head_repo}` with base `{head_ref}`.",
            f"Do not merge the upstream `{base_repo}` PR #{pr_number} as part of amendment work.",
            "When code changes are requested, completion requires evidence that the upstream PR head SHA advanced beyond the triggering source commit.",
            "Final public GitHub output is pushed commits/PRs plus reactions only.",
            f"Preserve and follow intake artifact: {artifact_path}",
        ],
        "github_pr_amend": {
            "artifact_path": str(artifact_path),
            "delivery_id": artifact.get("delivery_id"),
            "repo": repo,
            "pr_number": pr_number,
            "source_kind": source_kind,
            "source_id": source_id,
            "source_node_id": source_node_id,
            "source_url": source_url,
            "base_repo": base_repo,
            "base_ref": base_ref,
            "head_repo": head_repo,
            "head_ref": head_ref,
            "head_sha": head_sha,
            "lock_key": str(_dig(artifact, "policy_decision", "lock_key", default="")),
        },
        "project_context": {
            "github_pr_target_repo": head_repo,
            "github_pr_target_url": f"https://github.com/{head_repo}.git" if head_repo else "",
            "base_branch": head_ref,
            **_reserve_solidity_skill_hint(base_repo, head_repo),
            "github_pr_amend": {
                "artifact_path": str(artifact_path),
                "delivery_id": artifact.get("delivery_id"),
                "upstream_repo": base_repo,
                "upstream_pr_number": pr_number,
                "upstream_pr_url": str(_dig(artifact, "pull_request", "html_url", default="")),
                "upstream_base_ref": base_ref,
                "head_repo": head_repo,
                "head_ref": head_ref,
                "head_sha": head_sha,
                "source_kind": source_kind,
                "source_id": source_id,
                "source_node_id": source_node_id,
                "review_state": source_review_state,
                "source_url": source_url,
                "source_key": _pr_amend_source_key(artifact),
                "requires_head_sha_advance": requires_head_sha_advance,
            },
        },
        "created_by": "github-pr-amend",
        "dispatch_dirty_reason": "github-pr-amend-accepted",
    }


def _reserve_solidity_skill_hint(base_repo: str, head_repo: str) -> dict[str, Any]:
    repos = {str(base_repo or "").lower(), str(head_repo or "").lower()}
    if not any(repo.startswith("reserve-protocol/") for repo in repos):
        return {}
    if not any(
        token in repo
        for repo in repos
        for token in ("solidity", "protocol", "contracts", "dtf")
    ):
        return {}
    return {
        "worker_skill_hints": ["reserve-solidity-style"],
        "worker_context_hints": [
            "For reserve-protocol Solidity work, load and follow the reserve-solidity-style skill before implementation."
        ],
    }


def resolve_pr_amend_existing_discord_route(artifact: dict[str, Any]) -> dict[str, Any]:
    """Find the Discord worker route that originally produced this PR, if any."""

    pr_url = str(_dig(artifact, "pull_request", "html_url", default="") or "").strip()
    head_repo = str(_dig(artifact, "pull_request", "head", "repo", default="") or "").strip()
    head_ref = str(_dig(artifact, "pull_request", "head", "ref", default="") or "").strip()
    if not pr_url and not (head_repo and head_ref):
        return {}
    try:
        from hermes_cli import kanban_db
        from hermes_cli.discord_worker_roles import DISCORD_WORKER_META_KEY
    except Exception as exc:
        logger.debug("[github-pr-amend] Discord worker-board resolver unavailable: %s", exc)
        return {}

    try:
        boards = kanban_db.list_boards(include_archived=True)
    except Exception as exc:
        logger.debug("[github-pr-amend] Discord worker-board list failed: %s", exc)
        return {}

    for board_meta in boards:
        if not isinstance(board_meta, dict):
            continue
        board = str(board_meta.get("slug") or board_meta.get("id") or board_meta.get("name") or "").strip()
        metadata = board_meta
        if board:
            try:
                metadata = kanban_db.read_board_metadata(board)
            except Exception:
                metadata = board_meta
        worker = metadata.get(DISCORD_WORKER_META_KEY) if isinstance(metadata, dict) else None
        if not isinstance(worker, dict):
            continue
        context = worker.get("project_context") if isinstance(worker.get("project_context"), dict) else {}
        github_context = context.get("github_pr_amend") if isinstance(context.get("github_pr_amend"), dict) else {}
        candidates = {
            str(worker.get("pr_url") or "").strip(),
            str(worker.get("github_pr_url") or "").strip(),
            str(context.get("pr_url") or "").strip(),
            str(github_context.get("upstream_pr_url") or "").strip(),
        }
        repo_matches = head_repo and head_repo in {
            str(worker.get("github_pr_target_repo") or "").strip(),
            str(context.get("github_pr_target_repo") or "").strip(),
            str(github_context.get("head_repo") or "").strip(),
        }
        ref_matches = head_ref and head_ref in {
            str(worker.get("base_branch") or "").strip(),
            str(context.get("base_branch") or "").strip(),
            str(github_context.get("head_ref") or "").strip(),
        }
        if pr_url not in candidates and not (repo_matches and ref_matches):
            continue
        thread_id = str(worker.get("thread_id") or worker.get("discord_thread_id") or "").strip()
        top_level_message_id = str(
            worker.get("discord_top_level_message_id")
            or worker.get("source_message_id")
            or worker.get("request_id")
            or ""
        ).strip()
        summary_message_id = str(worker.get("summary_message_id") or "").strip()
        board_slug = str(board or worker.get("discord_board") or "").strip()
        channel_id = str(worker.get("parent_channel_id") or worker.get("chat_id") or "").strip()
        if thread_id and top_level_message_id and board_slug and channel_id:
            return {
                "discord_channel_id": channel_id,
                "discord_top_level_message_id": top_level_message_id,
                "discord_thread_id": thread_id,
                "discord_thread_url": str(worker.get("thread_url") or worker.get("discord_thread_url") or ""),
                "discord_board": board_slug,
                "discord_board_public_url": str(worker.get("public_url") or worker.get("discord_board_public_url") or ""),
                "discord_guild_id": str(worker.get("guild_id") or worker.get("discord_guild_id") or ""),
                "discord_summary_message_id": summary_message_id,
            }
    return {}


def publish_and_activate_pr_amend_intake(
    card: dict[str, Any],
    *,
    channel_id: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from self_improvement.discord_publish import activate_approved_proposal, publish_approved_proposal

    route = publish_approved_proposal(card, channel_id=channel_id, existing=existing)
    if route is None:
        raise RuntimeError("Discord publish returned no route")
    if route.error:
        raise RuntimeError(route.error)
    route = activate_approved_proposal(card, route)
    if route is None:
        raise RuntimeError("Discord activation returned no route")
    if route.error:
        raise RuntimeError(route.error)
    return route.metadata()
