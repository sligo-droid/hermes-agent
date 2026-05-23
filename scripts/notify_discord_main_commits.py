#!/usr/bin/env python3
"""Post GitHub main-branch push commits to a Discord webhook."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib import request
from urllib.error import HTTPError
from urllib.parse import quote


MAX_EMBEDS_PER_MESSAGE = 10
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_FIELD_VALUE_LIMIT = 1024
GENERIC_MERGE_PREFIXES = (
    "merge pull request",
    "merge remote-tracking branch",
    "merge branch",
)
PR_MERGE_PREFIX = "merge pull request"
TEST_SECTION_NAMES = {
    "test",
    "tests",
    "testing",
    "test plan",
}


def _truncate(value: str, limit: int) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 3)].rstrip() + "..."


def _branch_name(ref: str) -> str:
    prefix = "refs/heads/"
    if ref.startswith(prefix):
        return ref[len(prefix) :]
    return ref or "main"


def _commit_description(message: str) -> str:
    lines = [
        line.strip()
        for line in (message or "").replace("\r\n", "\n").split("\n")
    ]
    non_empty = [line for line in lines if line]
    if not non_empty:
        return "Commit pushed to main."

    subject = non_empty[0]
    if _is_generic_merge_message(subject):
        for line in non_empty[1:]:
            if not line.lower().startswith(("*", "from ")):
                return _truncate(line, DISCORD_EMBED_DESCRIPTION_LIMIT)
    return _truncate(subject, DISCORD_EMBED_DESCRIPTION_LIMIT)


def _is_generic_merge_message(message: str) -> bool:
    return message.strip().lower().startswith(GENERIC_MERGE_PREFIXES)


def _is_pull_request_merge_message(message: str) -> bool:
    return message.strip().lower().startswith(PR_MERGE_PREFIX)


def _section_name(line: str) -> str:
    stripped = line.strip().strip("*_")
    if stripped.startswith("#"):
        stripped = stripped.lstrip("#").strip()
    return stripped.rstrip(":").strip().lower()


def _strip_test_section(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    kept = []
    for line in lines:
        if _section_name(line) in TEST_SECTION_NAMES:
            break
        kept.append(line)
    return "\n".join(kept).strip()


def _pull_request_description(pull_request: dict[str, Any]) -> str:
    body = _strip_test_section(str(pull_request.get("body") or ""))
    if body:
        return _truncate(body, DISCORD_EMBED_DESCRIPTION_LIMIT)

    title = str(pull_request.get("title") or "").strip()
    if title:
        return _truncate(title, DISCORD_EMBED_DESCRIPTION_LIMIT)

    number = pull_request.get("number")
    if number:
        return f"Pull request #{number} merged."
    return "Pull request merged."


def _pull_request_field_value(pull_request: dict[str, Any]) -> str:
    number = pull_request.get("number")
    title = str(pull_request.get("title") or "").strip()
    url = str(pull_request.get("html_url") or "").strip()

    label_parts = []
    if number:
        label_parts.append(f"#{number}")
    if title:
        label_parts.append(title)
    label = " ".join(label_parts) or "Pull request"

    if url:
        return _truncate(f"[{label}]({url})", DISCORD_FIELD_VALUE_LIMIT)
    return _truncate(label, DISCORD_FIELD_VALUE_LIMIT)


def _pull_request_title(pull_request: dict[str, Any]) -> str:
    title = str(pull_request.get("title") or "").strip()
    if title:
        return _truncate(title, DISCORD_EMBED_TITLE_LIMIT)

    number = pull_request.get("number")
    if number:
        return f"Pull request #{number} merged."
    return "Pull request merged."


def _commit_url(event: dict[str, Any], commit: dict[str, Any]) -> str:
    url = str(commit.get("url") or "").strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url

    repo_url = str((event.get("repository") or {}).get("html_url") or "").rstrip("/")
    sha = str(commit.get("id") or "").strip()
    if repo_url and sha:
        return f"{repo_url}/commit/{sha}"
    return ""


def _commit_embed(
    event: dict[str, Any],
    commit: dict[str, Any],
    pull_request: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sha = str(commit.get("id") or "").strip()
    short_sha = sha[:7] if sha else "unknown"
    branch = _branch_name(str(event.get("ref") or "refs/heads/main"))
    author = (commit.get("author") or {}).get("name") or "unknown"
    message = str(commit.get("message") or "")
    pusher = (
        (event.get("pusher") or {}).get("name")
        or (event.get("sender") or {}).get("login")
        or "unknown"
    )
    description = _commit_description(message)
    if pull_request:
        description = _pull_request_description(pull_request)

    title = _truncate(f"{short_sha} pushed to {branch}", DISCORD_EMBED_TITLE_LIMIT)
    if pull_request:
        title = _pull_request_title(pull_request)

    embed: dict[str, Any] = {
        "title": title,
        "description": description,
        "color": 0x2F81F7,
        "fields": [
            {
                "name": "Author",
                "value": _truncate(str(author), DISCORD_FIELD_VALUE_LIMIT),
                "inline": True,
            },
            {
                "name": "Pusher",
                "value": _truncate(str(pusher), DISCORD_FIELD_VALUE_LIMIT),
                "inline": True,
            },
            {
                "name": "Branch",
                "value": _truncate(branch, DISCORD_FIELD_VALUE_LIMIT),
                "inline": True,
            },
        ],
    }
    if pull_request:
        embed["fields"].append(
            {
                "name": "Pull Request",
                "value": _pull_request_field_value(pull_request),
                "inline": False,
            }
        )

    url = _commit_url(event, commit)
    if pull_request:
        pr_url = str(pull_request.get("html_url") or "").strip()
        if pr_url:
            url = pr_url
    if url:
        embed["url"] = url
    timestamp = str(commit.get("timestamp") or "").strip()
    if timestamp:
        embed["timestamp"] = timestamp
    return embed


def build_webhook_payloads(
    event: dict[str, Any],
    *,
    pull_request_lookup: Callable[[str], dict[str, Any] | None] | None = None,
) -> list[dict[str, Any]]:
    """Return Discord webhook payloads for a GitHub push event."""
    if event.get("deleted"):
        return []

    commits = event.get("commits") or []
    if not isinstance(commits, list) or not commits:
        return []

    filtered_commits = _collapse_pull_request_push_commits(commits)
    embeds = []
    for commit in filtered_commits:
        if not isinstance(commit, dict):
            continue
        sha = str(commit.get("id") or "").strip()
        pull_request = None
        if pull_request_lookup is not None:
            if not sha:
                continue
            pull_request = pull_request_lookup(sha)
            if pull_request is None:
                continue
        embeds.append(_commit_embed(event, commit, pull_request))
    payloads: list[dict[str, Any]] = []
    for index in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
        chunk = embeds[index : index + MAX_EMBEDS_PER_MESSAGE]
        payloads.append(
            {
                "username": "Hermes Main Logs",
                "allowed_mentions": {"parse": []},
                "embeds": chunk,
            }
        )
    return payloads


def _collapse_pull_request_push_commits(commits: list[Any]) -> list[Any]:
    """Keep only final PR merge commits when a push payload contains them.

    GitHub push events for PR merges can include branch commits, a branch-sync
    merge commit, and the final "Merge pull request" commit. Posting only the
    final merge commit keeps #logs to one rich PR message while direct
    multi-commit pushes still show each commit.
    """
    merge_commits = [
        commit
        for commit in commits
        if isinstance(commit, dict)
        and _is_pull_request_merge_message(str(commit.get("message") or ""))
    ]
    return merge_commits or commits


def fetch_pull_request_for_commit(
    repository: str,
    commit_sha: str,
    token: str,
    api_url: str = "https://api.github.com",
) -> dict[str, Any] | None:
    """Return the first PR associated with a commit, or None on lookup failure."""
    repository = repository.strip()
    commit_sha = commit_sha.strip()
    token = token.strip()
    if not repository or not commit_sha or not token:
        return None

    quoted_repo = quote(repository, safe="/")
    url = f"{api_url.rstrip('/')}/repos/{quoted_repo}/commits/{commit_sha}/pulls"
    req = request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "hermes-main-commit-logs",
        },
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(
            f"Warning: PR lookup for {commit_sha[:7]} failed with "
            f"HTTP {exc.code}: {body}",
            file=sys.stderr,
        )
        return None
    except Exception as exc:
        print(
            f"Warning: PR lookup for {commit_sha[:7]} failed: {exc}",
            file=sys.stderr,
        )
        return None

    if isinstance(data, list) and data:
        first = data[0]
        if isinstance(first, dict):
            return first
    return None


def post_payload(webhook_url: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = request.Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "hermes-main-commit-logs",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=20) as resp:
            status = getattr(resp, "status", 0)
            if status < 200 or status >= 300:
                response_body = resp.read().decode("utf-8", errors="replace")
                raise RuntimeError(
                    f"Discord webhook returned HTTP {status}: {response_body}"
                )
    except HTTPError as exc:
        response_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Discord webhook returned HTTP {exc.code}: {response_body}"
        ) from exc


def load_event(path: str | os.PathLike[str]) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as fh:
        event = json.load(fh)
    if not isinstance(event, dict):
        raise ValueError("GitHub event payload must be a JSON object")
    return event


def main() -> int:
    webhook_url = os.getenv("DISCORD_LOGS_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("DISCORD_LOGS_WEBHOOK_URL is required", file=sys.stderr)
        return 2

    event_path = os.getenv("GITHUB_EVENT_PATH", "").strip()
    if not event_path:
        print("GITHUB_EVENT_PATH is required", file=sys.stderr)
        return 2

    event = load_event(event_path)
    repository = str(
        (event.get("repository") or {}).get("full_name")
        or os.getenv("GITHUB_REPOSITORY")
        or ""
    )
    github_token = os.getenv("GITHUB_TOKEN", "").strip()
    github_api_url = os.getenv("GITHUB_API_URL", "https://api.github.com").strip()
    pull_request_lookup = None
    if repository and github_token:
        pull_request_lookup = lambda sha: fetch_pull_request_for_commit(
            repository,
            sha,
            github_token,
            github_api_url,
        )

    payloads = build_webhook_payloads(
        event,
        pull_request_lookup=pull_request_lookup,
    )
    if not payloads:
        print("No main-branch commit notifications to send.")
        return 0

    for payload in payloads:
        post_payload(webhook_url, payload)
    print(f"Sent {len(payloads)} Discord webhook message(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
