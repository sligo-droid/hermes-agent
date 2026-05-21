#!/usr/bin/env python3
"""Post GitHub main-branch push commits to a Discord webhook."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib import request


MAX_EMBEDS_PER_MESSAGE = 10
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
DISCORD_EMBED_TITLE_LIMIT = 256
DISCORD_FIELD_VALUE_LIMIT = 1024


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
    if subject.lower().startswith(("merge pull request", "merge branch")):
        for line in non_empty[1:]:
            if not line.lower().startswith(("*", "from ")):
                return _truncate(line, DISCORD_EMBED_DESCRIPTION_LIMIT)
    return _truncate(subject, DISCORD_EMBED_DESCRIPTION_LIMIT)


def _commit_url(event: dict[str, Any], commit: dict[str, Any]) -> str:
    url = str(commit.get("url") or "").strip()
    if url.startswith("http://") or url.startswith("https://"):
        return url

    repo_url = str((event.get("repository") or {}).get("html_url") or "").rstrip("/")
    sha = str(commit.get("id") or "").strip()
    if repo_url and sha:
        return f"{repo_url}/commit/{sha}"
    return ""


def _commit_embed(event: dict[str, Any], commit: dict[str, Any]) -> dict[str, Any]:
    sha = str(commit.get("id") or "").strip()
    short_sha = sha[:7] if sha else "unknown"
    branch = _branch_name(str(event.get("ref") or "refs/heads/main"))
    author = (commit.get("author") or {}).get("name") or "unknown"
    pusher = (
        (event.get("pusher") or {}).get("name")
        or (event.get("sender") or {}).get("login")
        or "unknown"
    )

    embed: dict[str, Any] = {
        "title": _truncate(f"{short_sha} pushed to {branch}", DISCORD_EMBED_TITLE_LIMIT),
        "description": _commit_description(str(commit.get("message") or "")),
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

    url = _commit_url(event, commit)
    if url:
        embed["url"] = url
    timestamp = str(commit.get("timestamp") or "").strip()
    if timestamp:
        embed["timestamp"] = timestamp
    return embed


def build_webhook_payloads(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Return Discord webhook payloads for a GitHub push event."""
    if event.get("deleted"):
        return []

    commits = event.get("commits") or []
    if not isinstance(commits, list) or not commits:
        return []

    embeds = [
        _commit_embed(event, commit)
        for commit in commits
        if isinstance(commit, dict)
    ]
    payloads: list[dict[str, Any]] = []
    repo_name = str(
        (event.get("repository") or {}).get("full_name")
        or os.getenv("GITHUB_REPOSITORY")
        or "repository"
    )

    for index in range(0, len(embeds), MAX_EMBEDS_PER_MESSAGE):
        chunk = embeds[index : index + MAX_EMBEDS_PER_MESSAGE]
        payloads.append(
            {
                "username": "Hermes Main Logs",
                "content": f"{len(chunk)} commit(s) landed on `{repo_name}` `main`.",
                "allowed_mentions": {"parse": []},
                "embeds": chunk,
            }
        )
    return payloads


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
    payloads = build_webhook_payloads(event)
    if not payloads:
        print("No main-branch commit notifications to send.")
        return 0

    for payload in payloads:
        post_payload(webhook_url, payload)
    print(f"Sent {len(payloads)} Discord webhook message(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
