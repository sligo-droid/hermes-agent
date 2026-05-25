#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


_COMMON_PR_HEADINGS = {
    "bug description",
    "changes made",
    "checklist",
    "fix",
    "how to test",
    "how to verify",
    "related issue",
    "risk assessment",
    "root cause",
    "summary",
    "test plan",
    "tests",
    "type of change",
    "verification",
    "what does this pr do?",
}

_ESCAPED_MARKDOWN_BREAK = re.compile(r"\\n(?:\\n)?(?:#{1,6}\s+\S|[-*]\s+\S|\d+\.\s+\S)")


def _normalize_heading(value: str) -> str:
    heading = value.strip()
    heading = heading.lstrip("#").strip()
    heading = heading.strip("*").strip()
    return heading.rstrip(":").strip().lower()


def has_escaped_markdown_newlines(body: str | None) -> bool:
    """Return True when a PR body looks like Markdown with literal ``\\n`` separators."""

    if not body or "\\n" not in body:
        return False

    first_escaped_line = body.strip().split("\\n", 1)[0]
    if _normalize_heading(first_escaped_line) in _COMMON_PR_HEADINGS:
        return True

    first_real_line = body.strip().splitlines()[0]
    if _ESCAPED_MARKDOWN_BREAK.search(first_real_line):
        normalized = _normalize_heading(first_real_line.split("\\n", 1)[0])
        return normalized in _COMMON_PR_HEADINGS or first_real_line.lstrip().startswith("#")

    escaped_markdown_breaks = _ESCAPED_MARKDOWN_BREAK.findall(body)
    return len(escaped_markdown_breaks) >= 2


def load_body_from_event(path: Path) -> str:
    event = json.loads(path.read_text(encoding="utf-8"))
    pull_request = event.get("pull_request") or {}
    return str(pull_request.get("body") or "")


def _read_body(args: argparse.Namespace) -> str:
    if args.body is not None:
        return args.body
    if args.body_file is not None:
        return args.body_file.read_text(encoding="utf-8")
    if args.event_path is not None:
        return load_body_from_event(args.event_path)
    return sys.stdin.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check PR body Markdown formatting.")
    parser.add_argument("--body", help="PR body text to validate")
    parser.add_argument("--body-file", type=Path, help="File containing a PR body")
    parser.add_argument("--event-path", type=Path, help="GitHub event JSON path")
    args = parser.parse_args(argv)

    body = _read_body(args)
    if has_escaped_markdown_newlines(body):
        print(
            "PR body appears to contain literal escaped newlines (\\\\n) instead of real line breaks.",
            file=sys.stderr,
        )
        print(
            "Write Markdown to a file and use `gh pr create --body-file <file>`; do not pass JSON-escaped Markdown to `--body`.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
