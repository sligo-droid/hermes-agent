#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli.pr_body_format import (
    check_project_state_requirement,
    has_escaped_markdown_newlines,
    has_project_state_not_needed_justification,
    has_project_state_update,
    is_operational_path,
    is_project_state_file,
    requires_project_state_evidence,
)

__all__ = [
    "check_project_state_requirement",
    "has_escaped_markdown_newlines",
    "has_project_state_not_needed_justification",
    "has_project_state_update",
    "is_operational_path",
    "is_project_state_file",
    "requires_project_state_evidence",
    "load_body_from_event",
    "load_changed_files",
    "main",
]


def load_body_from_event(path: Path) -> str:
    event = json.loads(path.read_text(encoding="utf-8"))
    pull_request = event.get("pull_request") or {}
    return str(pull_request.get("body") or "")


def load_changed_files(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


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
    parser.add_argument("--changed-files", type=Path, help="Newline-delimited changed file paths")
    args = parser.parse_args(argv)

    body = _read_body(args)
    changed_files = load_changed_files(args.changed_files) if args.changed_files else []
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
    ok, message = check_project_state_requirement(body, changed_files)
    if not ok:
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
