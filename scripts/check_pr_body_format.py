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
_PROJECT_STATE_MARKER = "project-state: not needed"

_PROJECT_STATE_FILES = {
    "docs/project-state.md",
}

_PROJECT_STATE_RELATED_DOCS = {
    "docs/context.md",
    "docs/sligo-command-center.md",
}

_OPERATIONAL_PREFIXES = (
    "cron/",
    "gateway/",
    "plugins/kanban/",
    "tui_gateway/",
)

_OPERATIONAL_DOC_PREFIXES = (
    "docs/decisions/",
    "docs/plans/",
)

_OPERATIONAL_WEB_PATHS = {
    "web/src/pages/CommandCenterPage.tsx",
}

_OPERATIONAL_FILE_PATTERNS = (
    re.compile(r"^hermes_cli/(?:command_center(?:_|\.)|kanban(?:_|\.))"),
)


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


def _normalize_changed_path(path: str) -> str:
    return path.strip().lstrip("./")


def is_project_state_file(path: str) -> bool:
    return _normalize_changed_path(path) in _PROJECT_STATE_FILES


def is_operational_path(path: str) -> bool:
    normalized = _normalize_changed_path(path)
    if not normalized:
        return False
    if normalized in _PROJECT_STATE_FILES | _PROJECT_STATE_RELATED_DOCS | _OPERATIONAL_WEB_PATHS:
        return True
    if normalized.startswith(_OPERATIONAL_PREFIXES):
        return True
    if normalized.startswith(_OPERATIONAL_DOC_PREFIXES):
        return True
    return any(pattern.search(normalized) for pattern in _OPERATIONAL_FILE_PATTERNS)


def requires_project_state_evidence(changed_files: list[str]) -> bool:
    return any(is_operational_path(path) for path in changed_files)


def has_project_state_update(changed_files: list[str]) -> bool:
    return any(is_project_state_file(path) for path in changed_files)


def has_project_state_not_needed_justification(body: str | None) -> bool:
    if not body:
        return False

    lines = body.splitlines()
    for index, line in enumerate(lines):
        marker_start = line.lower().find(_PROJECT_STATE_MARKER)
        if marker_start == -1:
            continue
        after_marker = line[marker_start + len(_PROJECT_STATE_MARKER) :]
        inline_reason = after_marker.lstrip(" :-\t")
        if inline_reason.strip() and not inline_reason.strip().startswith("<!--"):
            return True
        for next_line in lines[index + 1 : index + 4]:
            stripped = next_line.strip()
            if not stripped:
                continue
            if stripped.startswith("<!--") or stripped.startswith("#"):
                return False
            return True
    return False


def check_project_state_requirement(
    body: str | None,
    changed_files: list[str],
) -> tuple[bool, str]:
    if not requires_project_state_evidence(changed_files):
        return True, "No operational changed paths require project-state evidence."
    if has_project_state_update(changed_files):
        return True, "Operational changes include docs/project-state.md."
    if has_project_state_not_needed_justification(body):
        return True, "Operational changes include Project-state: not needed justification."
    return (
        False,
        "Operational PRs must update docs/project-state.md or include "
        "`Project-state: not needed` with a short justification in the PR body. "
        "Update docs/project-state.md when current focus, blockers, live runtime state, "
        "Command Center behavior, or worker/gateway operational truth changed.",
    )


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
