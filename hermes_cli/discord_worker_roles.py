"""Shared Discord worker board constants and role helpers."""

from __future__ import annotations

import re
from typing import Any, Optional

DISCORD_WORKER_META_KEY = "discord_worker"
DISCORD_WORKER_DISPATCH_DIRTY_FILENAME = "discord-worker-dispatch.dirty.json"
BOARD_RUN_SUMMARY_FILENAME = "run-summary.json"
PUBLIC_TOKEN_BYTES = 24
REVIEW_LOOP_LIMIT_BLOCKED_REASON = "review loop limit reached"
REVIEW_LOOP_CONTINUE_EXTRA_LOOPS = 5
ROLE_PLANNER = "planner"
ROLE_DEV = "dev"
ROLE_REVIEWER = "reviewer"
ROLE_FOREMAN = "foreman"
ROLE_ASSIGNEES = frozenset({ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER, ROLE_FOREMAN})
DEV_TICKET_BODY_GUIDANCE = (
    "Each dev ticket body must be a detailed, self-contained implementation brief "
    "that opens with Goal, Success means, and Stop when, followed by Scope, "
    "Implementation notes, Likely files/subsystems, Dependencies or handoffs, "
    "Verification, and Out of scope. If the ticket changes a live entrypoint, "
    "cron job, profile-scoped script, deployment path, generated artifact, or "
    "any file whose active runtime path can differ from the repo path, include a "
    "pre-review readiness checklist with closeout evidence for the active path, "
    "source of truth, provenance, and live pickup/deployment verification."
)
GOAL_CONTROL_COMMANDS = frozenset({"status", "pause", "resume", "clear", "stop", "done"})
TERMINAL_GOAL_STATUSES = frozenset({"done", "blocked", "cancelled"})
PUBLIC_BOARD_COLUMNS = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done")

_ROLE_ROUND_TITLE_RE = re.compile(r"^R\d+:\s*")


def _discord_slug_part(value: str) -> str:
    return re.sub(r"[^0-9a-zA-Z_-]+", "-", str(value or "").strip()).strip("-_")


def board_slug_for_discord_thread(thread_id: str) -> str:
    """Return the canonical legacy board slug for a Discord thread id."""
    cleaned = _discord_slug_part(thread_id)
    if not cleaned:
        raise ValueError("Discord thread id is required")
    return f"discord-{cleaned.lower()}"[:64]


def board_slug_for_discord_request(thread_id: str, request_id: Optional[str] = None) -> str:
    """Return the board slug for one Discord work request within a thread."""
    base = board_slug_for_discord_thread(thread_id)
    request = _discord_slug_part(str(request_id or ""))
    if not request:
        return base
    return f"{base}-m-{request.lower()}"[:64]


def format_role_round_title(title: str, round_number: int) -> str:
    """Return a role-lane ticket title with one normalized round prefix."""
    try:
        round_value = max(1, int(round_number))
    except (TypeError, ValueError):
        round_value = 1
    clean_title = _ROLE_ROUND_TITLE_RE.sub("", str(title or "").strip()).strip()
    return f"R{round_value}: {clean_title}"


def active_dev_round(worker: Optional[dict[str, Any]]) -> int:
    """Return the dev round that should receive newly-created dev tickets."""
    try:
        return max(1, int((worker or {}).get("review_loop_count") or 0) + 1)
    except (TypeError, ValueError):
        return 1
