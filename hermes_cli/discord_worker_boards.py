"""Discord thread boards for durable Codex worker sessions.

This module is deliberately control-plane only. It creates and mutates Kanban
state for Discord project threads, but it does not call Hermes inference and it
does not expose Hermes tools to workers.
"""

from __future__ import annotations

import html
import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from hermes_cli import kanban_db
from hermes_cli.discord_time import discord_message_exceeds_age_limit
from hermes_cli.discord_thread_context import (
    expand_discord_thread_references,
    format_discord_thread_expansions,
    has_discord_thread_reference,
)
from hermes_cli.discord_worker_roles import (
    BOARD_RUN_SUMMARY_FILENAME,
    DEV_TICKET_BODY_GUIDANCE,
    DISCORD_WORKER_DISPATCH_DIRTY_FILENAME,
    DISCORD_WORKER_META_KEY,
    GOAL_CONTROL_COMMANDS,
    PUBLIC_BOARD_COLUMNS,
    PUBLIC_TOKEN_BYTES,
    REVIEW_LOOP_CONTINUE_EXTRA_LOOPS,
    REVIEW_LOOP_LIMIT_BLOCKED_REASON,
    ROLE_ASSIGNEES,
    ROLE_DEV,
    ROLE_FOREMAN,
    ROLE_PLANNER,
    ROLE_REVIEWER,
    TERMINAL_GOAL_STATUSES,
    active_dev_round,
    board_slug_for_discord_request,
    board_slug_for_discord_thread,
    format_role_round_title,
)
from hermes_cli.discord_worker_state import (
    CODEX_STATE_LOG_TAIL_BYTES,
    cap_state_value as _cap_state_value,
    codex_worker_state_path,
    read_codex_worker_state as _read_codex_worker_state,
    record_codex_worker_event,
    record_codex_worker_result,
)
from agent.runtime_breakdown import render_runtime_breakdown_text
from utils import atomic_json_write


logger = logging.getLogger(__name__)
DEFAULT_REVIEW_LOOP_LIMIT = 5
FOREMAN_REVIEW_LOOP_LIMIT = 3
PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL = "after_review_approval"
MERGE_POLICY_AUTO = "auto"
MERGE_POLICY_MANUAL = "manual"
MERGE_POLICY_NEVER = "never"
VALID_MERGE_POLICIES = frozenset({MERGE_POLICY_AUTO, MERGE_POLICY_MANUAL, MERGE_POLICY_NEVER})
_DISCORD_MESSAGE_URL_RE = re.compile(
    r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d+)/(?P<channel>\d+)/(?P<message>\d+)"
)
_DISCORD_MESSAGE_ID_RE = re.compile(
    r"\b(?:message|msg)\s+(?P<message>\d{16,24})\b",
    re.IGNORECASE,
)
_SOURCE_TASK_ERROR_OUTCOMES = frozenset({"spawn_failed", "crashed", "timed_out", "gave_up"})
_DELETE_META = object()
_POSIX_PATH_RE = re.compile(
    r"(?<![\w:/.-])/(?:home|Users|tmp|var|etc|opt|private|workspace|workspaces|mnt|srv|repo|root)"
    r"(?:/[^\s\"'<>),;{}\[\]]*)?"
)
_WINDOWS_PATH_RE = re.compile(r"(?<![\w:/.-])[A-Za-z]:\\[^\s\"'<>),;{}\[\]]+")
_CONTEXT_PACK_JSON_FILENAME = "context-pack.json"
_CONTEXT_PACK_MARKDOWN_FILENAME = "context-pack.md"


class TicketMoveConflict(RuntimeError):
    """Raised when a ticket status move is valid syntax but refused."""


def _now() -> int:
    return int(time.time())


def _is_foreman_generated_request(request: object) -> bool:
    text = str(request or "").lstrip()
    return text.startswith("Foreman escalation:") or text.startswith("/goal Foreman escalation:")


def pr_policy_for_request(request: object) -> dict[str, str]:
    """Infer board-level PR policy from the user's request.

    Ambiguous implementation requests should take the normal Sligo path:
    open a PR after reviewer approval, wait for checks, then merge. Explicit
    review-only / do-not-merge wording overrides that default.
    """
    text = re.sub(r"\s+", " ", str(request or "")).strip().casefold()
    merge_policy = MERGE_POLICY_AUTO
    if _request_forbids_merge(text):
        merge_policy = MERGE_POLICY_NEVER
    elif _request_requires_manual_merge(text):
        merge_policy = MERGE_POLICY_MANUAL
    return {
        "pr_open_policy": PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL,
        "merge_policy": merge_policy,
    }


def _request_forbids_merge(text: str) -> bool:
    if not text:
        return False
    patterns = (
        r"\bdo\s+not\s+merge\b",
        r"\bdon['’]?t\s+merge\b",
        r"\bdont\s+merge\b",
        r"\bwithout\s+merging\b",
        r"\bunmerged\b",
        r"\bopen\s+(?:a\s+)?(?:pull\s+request|pr)\s+only\b",
        r"\b(?:pull\s+request|pr)\s+only\b",
        r"\bleave\s+(?:the\s+)?(?:pull\s+request|pr)\s+open\b",
        r"\bkeep\s+(?:the\s+)?(?:pull\s+request|pr)\s+open\b",
        r"\bopen\s+(?:a\s+)?(?:pull\s+request|pr)\b.{0,80}\b(?:do\s+not|don['’]?t|dont)\s+merge\b",
        r"\b(?:do\s+not|don['’]?t|dont)\s+land\b",
        r"\breview[-\s]?only\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _request_requires_manual_merge(text: str) -> bool:
    if not text:
        return False
    patterns = (
        r"\bmanual\s+merge\b",
        r"\bmerge\s+manually\b",
        r"\bdo\s+not\s+auto[-\s]?merge\b",
        r"\bno\s+auto[-\s]?merge\b",
        r"\bwait\s+for\s+(?:human\s+)?(?:approval|review)\s+before\s+merg",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def active_dev_round_for_board(board: Optional[str]) -> int:
    if not board:
        return 1
    return active_dev_round(_read_worker_meta(board))


def _metadata_path(board: str) -> Path:
    return kanban_db.board_metadata_path(board)


def board_run_summary_path(board: str) -> Path:
    """Return the deterministic terminal summary sidecar path for a board."""
    return kanban_db.board_dir(board) / BOARD_RUN_SUMMARY_FILENAME


def context_pack_path(board: str) -> Path:
    return kanban_db.board_dir(board) / _CONTEXT_PACK_JSON_FILENAME


def context_pack_markdown_path(board: str) -> Path:
    return kanban_db.board_dir(board) / _CONTEXT_PACK_MARKDOWN_FILENAME


def _context_pack_digest(root_goal: str, request: str, thread_context: str) -> str:
    payload = json.dumps(
        {
            "root_goal": str(root_goal or ""),
            "request": str(request or ""),
            "discord_thread_context": str(thread_context or ""),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _context_pack_message_count(thread_context: str) -> int:
    lines = [line for line in str(thread_context or "").splitlines() if line.strip()]
    return sum(1 for line in lines if line.lstrip().startswith("[") and not line.startswith("[Goal thread context"))


def _context_pack_source_message_ids(text: str) -> list[str]:
    seen: set[str] = set()
    ids: list[str] = []
    for match in re.finditer(r"\b\d{16,24}\b", str(text or "")):
        value = match.group(0)
        if value not in seen:
            seen.add(value)
            ids.append(value)
    return ids


def render_context_pack_markdown(pack: dict[str, Any]) -> str:
    warnings = [str(item) for item in pack.get("warnings") or [] if str(item).strip()]
    source_ids = [str(item) for item in pack.get("source_message_ids") or [] if str(item).strip()]
    lines = [
        "# Discord Goal Context Pack",
        "",
        f"Version: {pack.get('version') or 1}",
        f"Updated at: {pack.get('updated_at') or ''}",
        f"Truncated: {bool(pack.get('truncated'))}",
        f"Message count: {int(pack.get('message_count') or 0)}",
        "",
        "## Root Goal",
        str(pack.get("root_goal") or ""),
        "",
        "## Request",
        str(pack.get("request") or ""),
        "",
        "## Source Message IDs",
        ", ".join(source_ids) if source_ids else "None detected.",
        "",
        "## Warnings",
        "\n".join(f"- {item}" for item in warnings) if warnings else "None.",
        "",
        "## Discord Thread Context",
        str(pack.get("discord_thread_context") or ""),
        "",
    ]
    return "\n".join(lines)


def read_context_pack(board: str) -> dict[str, Any]:
    try:
        with context_pack_path(board).open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}
    except OSError:
        return {}
    except json.JSONDecodeError:
        return {}


def _context_pack_summary(board: str, pack: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    data = dict(pack or read_context_pack(board) or {})
    if not data and not context_pack_path(board).exists():
        return {}
    return {
        "json_path": str(context_pack_path(board)),
        "markdown_path": str(context_pack_markdown_path(board)),
        "version": int(data.get("version") or 1),
        "truncated": bool(data.get("truncated")),
        "warnings": [str(item) for item in data.get("warnings") or [] if str(item).strip()],
        "message_count": int(data.get("message_count") or 0),
        "source_message_ids": [
            str(item) for item in data.get("source_message_ids") or [] if str(item).strip()
        ],
    }


def write_context_pack(board: str, *, root_goal: str, request: str, thread_context: str) -> dict[str, Any]:
    existing = read_context_pack(board)
    digest = _context_pack_digest(root_goal, request, thread_context)
    version = int(existing.get("version") or 0) if existing else 0
    if existing.get("content_digest") != digest:
        version += 1
    elif version <= 0:
        version = 1
    truncated = str(thread_context or "").lstrip().startswith("[Goal thread context truncated")
    warnings = ["Discord thread context was truncated to recent messages."] if truncated else []
    updated_at = int(existing.get("updated_at") or _now()) if existing.get("content_digest") == digest else _now()
    pack: dict[str, Any] = {
        "version": version,
        "updated_at": updated_at,
        "root_goal": str(root_goal or ""),
        "request": str(request or ""),
        "discord_thread_context": str(thread_context or ""),
        "message_count": _context_pack_message_count(thread_context),
        "truncated": truncated,
        "source_message_ids": _context_pack_source_message_ids("\n".join([root_goal, request, thread_context])),
        "warnings": warnings,
        "content_digest": digest,
    }
    pack["markdown"] = render_context_pack_markdown(pack)
    json_path = context_pack_path(board)
    md_path = context_pack_markdown_path(board)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(json_path, pack, indent=2)
    md_path.write_text(pack["markdown"], encoding="utf-8")
    return pack


def _write_metadata(board: str, metadata: dict[str, Any]) -> dict[str, Any]:
    path = _metadata_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(metadata)
    payload.pop("db_path", None)
    atomic_json_write(path, payload, indent=2)
    payload["db_path"] = str(kanban_db.kanban_db_path(board))
    return payload


def dispatch_dirty_marker_path() -> Path:
    """Return the cross-process marker used to wake gateway dispatch."""
    return kanban_db.kanban_home() / "kanban" / DISCORD_WORKER_DISPATCH_DIRTY_FILENAME


def mark_dispatch_dirty(*, board: Optional[str] = None, reason: str = "") -> Path:
    """Signal that Discord worker dispatch should run soon.

    Gateway-created work can use an in-process event, but role workers are
    subprocesses. This tiny marker gives those subprocesses a safe way to wake
    the embedded dispatcher without sharing an event loop.
    """
    path = dispatch_dirty_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(
        path,
        {
            "updated_at": _now(),
            "board": str(board or ""),
            "reason": str(reason or ""),
        },
        indent=2,
    )
    return path


def dispatch_dirty_marker_mtime_ns() -> int:
    try:
        return dispatch_dirty_marker_path().stat().st_mtime_ns
    except OSError:
        return 0


def _read_worker_meta(board: str) -> dict[str, Any]:
    metadata = kanban_db.read_board_metadata(board)
    raw = metadata.get(DISCORD_WORKER_META_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _worker_source_message_id(worker: dict[str, Any]) -> str:
    return str(
        worker.get("source_message_id")
        or worker.get("request_id")
        or worker.get("thread_id")
        or ""
    ).strip()


def _worker_source_message_too_old(worker: dict[str, Any]) -> bool:
    return discord_message_exceeds_age_limit(_worker_source_message_id(worker))


def _clear_stale_terminal_sync_flags(board: str, worker: dict[str, Any]) -> None:
    if not (
        worker.get("terminal_reaction_sync_pending")
        or worker.get("terminal_summary_sync_pending")
        or worker.get("terminal_completion_message_pending")
    ):
        return
    mark_thread_status_synced(
        board,
        reaction=True,
        summary=True,
        completion_message=True,
    )


_TERMINAL_SUMMARY_SYNC_FIELDS = frozenset(
    {
        "goal_status",
        "phase",
        "concise_outcome",
        "deployment_status",
        "final_discord_response",
        "final_discord_response_at",
        "final_discord_session_id",
        "final_discord_work_item_id",
        "final_discord_message_id",
        "blocked_reason",
        "pr_url",
        "pr_number",
        "pr_error",
        "pr_status_error",
        "pr_state",
        "pr_merge_state",
        "pr_mergeable",
        "pr_is_draft",
        "pr_review_decision",
        "pr_merged_at",
        "pr_merge_commit",
        "pr_checks_status",
        "pr_checks_total",
        "pr_checks_failed",
        "pr_blocker",
    }
)


def _terminal_worker_reaction_state(worker: dict[str, Any]) -> str:
    status = str(worker.get("goal_status") or "").strip().lower()
    phase = str(worker.get("phase") or "").strip().lower()
    if worker.get("cancelled") or status == "cancelled":
        return "errored"
    if status == "done" or phase == "complete":
        return "done"
    if status == "blocked":
        return "blocked"
    return ""


def _is_terminal_worker_meta(worker: dict[str, Any]) -> bool:
    status = str(worker.get("goal_status") or "").strip().lower()
    phase = str(worker.get("phase") or "").strip().lower()
    return bool(worker.get("cancelled") or status in TERMINAL_GOAL_STATUSES or phase == "complete")


def _update_worker_meta(board: str, updates: dict[str, Any]) -> dict[str, Any]:
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    previous = dict(worker)
    for key, value in updates.items():
        if value is _DELETE_META:
            worker.pop(key, None)
        else:
            worker[key] = value
    changed_keys = {key for key in set(previous) | set(worker) if previous.get(key) != worker.get(key)}
    if not changed_keys:
        metadata[DISCORD_WORKER_META_KEY] = worker
        return metadata
    terminal_summary_changed = bool(changed_keys & _TERMINAL_SUMMARY_SYNC_FIELDS)
    became_terminal = not _is_terminal_worker_meta(previous) and _is_terminal_worker_meta(worker)
    terminal_reaction_changed = _terminal_worker_reaction_state(previous) != _terminal_worker_reaction_state(worker)
    if worker.get("kind") == "discord_worker_board" and _is_terminal_worker_meta(worker):
        if terminal_summary_changed:
            worker["terminal_summary_sync_pending"] = True
        if became_terminal or terminal_reaction_changed:
            worker["terminal_reaction_sync_pending"] = True
    worker["updated_at"] = _now()
    metadata[DISCORD_WORKER_META_KEY] = worker
    written = _write_metadata(board, metadata)
    if worker.get("kind") == "discord_worker_board" and _is_terminal_worker_meta(worker) and terminal_summary_changed:
        try:
            persist_board_run_summary(board)
        except Exception:
            logger.debug("Failed to refresh Discord board run summary for %s", board, exc_info=True)
        try:
            mark_dispatch_dirty(board=board, reason="terminal-summary-metadata-updated")
        except Exception:
            logger.debug("Failed to mark Discord worker dispatch dirty for %s", board, exc_info=True)
        written = kanban_db.read_board_metadata(board)
    return written


def _clear_terminal_summary_fields(worker: dict[str, Any]) -> None:
    for key in (
        "board_summary",
        "board_summary_path",
        "board_summary_updated_at",
        "terminal_completion_message_pending",
        "terminal_completion_message_sent_at",
        "terminal_summary_message_sent_at",
        "terminal_summary_sync_pending",
        "terminal_reaction_sync_pending",
    ):
        worker[key] = _DELETE_META


def _clear_board_run_summary(board: str, worker: dict[str, Any]) -> None:
    _clear_terminal_summary_fields(worker)
    try:
        board_run_summary_path(board).unlink(missing_ok=True)
    except OSError:
        pass


def _clear_pr_summary_fields(worker: dict[str, Any]) -> None:
    for key in (
        "pr_url",
        "pr_number",
        "pr_error",
        "pr_status_error",
        "pr_state",
        "pr_merge_state",
        "pr_mergeable",
        "pr_is_draft",
        "pr_review_decision",
        "pr_merged_at",
        "pr_merge_commit",
        "pr_checks_status",
        "pr_checks_total",
        "pr_checks_failed",
        "pr_blocker",
    ):
        worker[key] = _DELETE_META


def _clear_generated_summary_title(worker: dict[str, Any]) -> None:
    worker["summary_title"] = _DELETE_META


def _public_base_url() -> str:
    value = str(os.getenv("HERMES_PUBLIC_KANBAN_BASE_URL") or "").strip()
    if value:
        return value.rstrip("/")
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        value = ((cfg.get("kanban") or {}).get("discord_worker") or {}).get("public_base_url") or ""
        if value:
            return str(value).strip().rstrip("/")
    except Exception:
        pass
    return ""


def _absolute_public_base_url() -> str:
    base = _public_base_url()
    if base.startswith(("http://", "https://")):
        return base
    return ""


def _public_workers_base_url() -> str:
    base = _absolute_public_base_url()
    if not base:
        return ""
    parts = urlsplit(base)
    path = parts.path.rstrip("/")
    if path.endswith("/kanban"):
        path = path[: -len("/kanban")]
    if not path.endswith("/workers"):
        path = f"{path}/workers"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def public_session_board_url(session_id: str) -> str:
    base = _public_workers_base_url()
    session = quote(str(session_id or "").strip(), safe="")
    return f"{base}/{session}" if base and session else ""


def _starter_message_thread_id_from_slug(session_id: str) -> str:
    raw = str(session_id or "").strip()
    if not raw.startswith("discord-") or "-m-" not in raw:
        return ""
    thread_part, request_part = raw[len("discord-") :].rsplit("-m-", 1)
    return thread_part if thread_part and thread_part == request_part else ""


def _public_session_id_for_board(board: str, worker: dict[str, Any]) -> str:
    thread_id = str(worker.get("thread_id") or "").strip()
    if not thread_id:
        return str(board or "")
    try:
        legacy = board_slug_for_discord_thread(thread_id)
    except ValueError:
        legacy = ""
    request_id = str(
        worker.get("request_id") or worker.get("source_message_id") or ""
    ).strip()
    starter = ""
    if request_id and request_id == thread_id:
        try:
            starter = board_slug_for_discord_request(thread_id, request_id)
        except ValueError:
            starter = ""
    return thread_id if board in {legacy, starter} else str(board or thread_id)


def resolve_public_session_board(session_id: str) -> str:
    raw = str(session_id or "").strip()
    if not raw:
        raise KeyError("unknown board session")
    candidates = [raw]
    starter_thread_id = _starter_message_thread_id_from_slug(raw)
    if starter_thread_id:
        candidates.append(board_slug_for_discord_thread(starter_thread_id))
    elif not raw.startswith("discord-"):
        candidates.append(board_slug_for_discord_thread(raw))
        candidates.append(board_slug_for_discord_request(raw, raw))
    for board in candidates:
        if not kanban_db.board_exists(board):
            continue
        worker = _read_worker_meta(board)
        if worker.get("kind") == "discord_worker_board":
            return board
    raise KeyError("unknown board session")


def public_board_url(token: str) -> str:
    """Return the token URL when an absolute public base is configured."""
    path = f"/public/kanban/{token}"
    base = _public_workers_base_url()
    return f"{base}{path}" if base else ""


def _discord_thread_url(worker: dict[str, Any]) -> str:
    guild_id = str(worker.get("guild_id") or "").strip()
    thread_id = str(worker.get("thread_id") or "").strip()
    if not guild_id or not thread_id:
        return ""
    return (
        "https://discord.com/channels/"
        f"{quote(guild_id, safe='')}/{quote(thread_id, safe='')}"
    )


def _default_worktree_path(project_path: Optional[str], thread_id: str) -> str:
    repo_name = Path(project_path or os.getcwd()).resolve().name or "project"
    safe_repo = re.sub(r"[^a-zA-Z0-9_.-]+", "-", repo_name).strip("-") or "project"
    return str(Path("/home/droid/workspaces") / f"{safe_repo}-discord-{thread_id}")


@dataclass
class DiscordBoard:
    slug: str
    metadata: dict[str, Any]

    @property
    def worker(self) -> dict[str, Any]:
        raw = self.metadata.get(DISCORD_WORKER_META_KEY)
        return dict(raw) if isinstance(raw, dict) else {}

    @property
    def public_url(self) -> str:
        return str(self.worker.get("public_url") or "")


def ensure_discord_thread_board(
    *,
    thread_id: str,
    chat_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    parent_channel_id: Optional[str] = None,
    initial_request: str = "",
    project_context: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
    board_slug: Optional[str] = None,
    source_message_id: Optional[str] = None,
    summary_message_id: Optional[str] = None,
) -> DiscordBoard:
    """Create or update the board backing a Discord thread/request."""
    started = time.time()
    thread_id = str(thread_id or "").strip()
    request_id = str(source_message_id or request_id or "").strip()
    legacy_slug = board_slug_for_discord_thread(thread_id)
    starter_slug = ""
    is_starter_message = bool(request_id and request_id == thread_id)
    if is_starter_message:
        starter_slug = board_slug_for_discord_request(thread_id, request_id)
    default_slug = board_slug_for_discord_request(
        thread_id,
        "" if is_starter_message else request_id,
    )
    slug = str(board_slug or "").strip()
    if not slug:
        slug = starter_slug if starter_slug and kanban_db.board_exists(starter_slug) else default_slug
    route_id = str(thread_id) if slug in {legacy_slug, starter_slug} else slug
    worker = _read_worker_meta(slug)
    existing_context = worker.get("project_context")
    merged_project_context = dict(existing_context) if isinstance(existing_context, dict) else {}
    incoming_project_context = dict(project_context or {})
    merged_project_context.update(
        {k: v for k, v in incoming_project_context.items() if v is not None}
    )
    incoming_project_path = str(incoming_project_context.get("project_path") or "").strip() or None
    existing_project_path = str(worker.get("project_path") or "").strip() or None
    project_path = incoming_project_path or existing_project_path
    if project_path:
        merged_project_context["project_path"] = project_path
    token = worker.get("share_token") or secrets.token_urlsafe(PUBLIC_TOKEN_BYTES)
    request_suffix = slug.removeprefix("discord-")
    branch = f"discord/{request_suffix or thread_id}"
    metadata = kanban_db.create_board(
        slug,
        name=f"Discord {thread_id}" if route_id == str(thread_id) else f"Discord {thread_id} / {request_id}",
        description=(initial_request or "")[:500],
        icon="",
        color="#22c55e",
    )
    previous_worktree_path = str(worker.get("worktree_path") or "").strip()
    project_path_changed = bool(project_path and project_path != existing_project_path)
    worktree_path = previous_worktree_path
    if not worktree_path or (project_path_changed and not worker.get("code_island_ready")):
        worktree_path = _default_worktree_path(project_path, request_suffix or str(thread_id))
    request_text = str(initial_request or worker.get("initial_request") or "")
    pr_policy = pr_policy_for_request(request_text)
    worker.update(
        {
            "kind": "discord_worker_board",
            "thread_id": str(thread_id),
            "request_id": request_id or worker.get("request_id") or "",
            "source_message_id": str(source_message_id or worker.get("source_message_id") or request_id or ""),
            "summary_message_id": str(summary_message_id or worker.get("summary_message_id") or ""),
            "chat_id": str(chat_id or ""),
            "guild_id": str(guild_id or ""),
            "parent_channel_id": str(parent_channel_id or ""),
            "initial_request": request_text,
            "project_context": merged_project_context,
            "project_path": project_path,
            "base_branch": worker.get("base_branch") or merged_project_context.get("base_branch") or "main",
            "worker_branch": worker.get("worker_branch") or branch,
            "worktree_path": worktree_path,
            "execution_mode": worker.get("execution_mode") or "pending",
            "phase": worker.get("phase") or "intake",
            "goal_status": worker.get("goal_status") or "unset",
            "criteria": worker.get("criteria") or [],
            "review_loop_count": int(worker.get("review_loop_count") or 0),
            "review_loop_limit": int(worker.get("review_loop_limit") or _review_loop_limit_for_request(request_text)),
            "pr_open_policy": pr_policy["pr_open_policy"],
            "merge_policy": pr_policy["merge_policy"],
            "share_token": token,
            "public_url": public_session_board_url(route_id),
            "created_at": worker.get("created_at") or _now(),
        }
    )
    _mark_code_island_deferred(worker)
    metadata[DISCORD_WORKER_META_KEY] = worker
    metadata = _write_metadata(slug, metadata)
    if previous_worktree_path != str(worker.get("worktree_path") or ""):
        _sync_role_task_workspaces(
            slug,
            old_path=previous_worktree_path,
            new_path=str(worker.get("worktree_path") or ""),
        )
    elapsed_ms = int((time.time() - started) * 1000)
    logger.info(
        "discord_worker_board_setup board=%s deferred_code_island=%s ready=%s total_ms=%d",
        slug,
        bool(worker.get("code_island_pending")),
        bool(worker.get("code_island_ready")),
        elapsed_ms,
    )
    return DiscordBoard(slug=slug, metadata=metadata)


def set_feature_summary_handle(
    board: str,
    *,
    message_id: Optional[str] = None,
    source_message_id: Optional[str] = None,
) -> None:
    """Attach the Discord summary/source message ids to a worker board."""
    updates: dict[str, Any] = {}
    if message_id:
        updates["summary_message_id"] = str(message_id)
    if source_message_id:
        updates["source_message_id"] = str(source_message_id)
        updates["request_id"] = str(source_message_id)
    if updates:
        _update_worker_meta(board, updates)


def _sync_role_task_workspaces(board: str, *, old_path: str, new_path: str) -> None:
    """Move queued role-lane tasks when board project context is repaired."""
    if not new_path:
        return
    statuses = ("triage", "todo", "ready", "blocked", "review")
    assignees = tuple(sorted(ROLE_ASSIGNEES))
    placeholders = ",".join("?" for _ in assignees)
    status_placeholders = ",".join("?" for _ in statuses)
    params: list[Any] = [new_path, *assignees, *statuses]
    path_clause = "workspace_path = ?"
    if old_path:
        params.append(old_path)
    else:
        path_clause = "(workspace_path IS NULL OR workspace_path = '')"
    conn = kanban_db.connect(board=board)
    try:
        conn.execute(
            "UPDATE tasks SET workspace_path = ? "
            "WHERE workspace_kind = 'dir' "
            f"AND lower(assignee) IN ({placeholders}) "
            f"AND status IN ({status_placeholders}) "
            f"AND {path_clause}",
            tuple(params),
        )
        conn.commit()
    finally:
        conn.close()


def _code_island_configured(worker: dict[str, Any]) -> bool:
    project_path = str(worker.get("project_path") or "").strip()
    worktree_path = str(worker.get("worktree_path") or "").strip()
    branch = str(worker.get("worker_branch") or "").strip()
    return bool(project_path and worktree_path and branch and os.path.isdir(project_path))


def _is_git_worktree(path: str) -> bool:
    if not path or not os.path.isdir(path):
        return False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return False
    return result.returncode == 0 and (result.stdout or "").strip().lower() == "true"


def _git_text(cwd: str, args: list[str], *, timeout: int = 10) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return (result.stdout or "").strip()


def _current_worktree_branch(worktree_path: str) -> str:
    return _git_text(worktree_path, ["branch", "--show-current"]) or ""


def _quarantine_generated_plan_artifacts(worktree_path: str, worker: dict[str, Any]) -> None:
    plans_dir = Path(worktree_path) / ".hermes" / "plans"
    if not plans_dir.exists():
        return
    try:
        if plans_dir.is_dir() and not any(plans_dir.iterdir()):
            plans_dir.rmdir()
            return
    except OSError:
        return
    try:
        from hermes_constants import get_hermes_home

        safe_name = re.sub(r"[^a-zA-Z0-9_.-]+", "-", Path(worktree_path).name).strip("-") or "worktree"
        dest = get_hermes_home() / "gateway" / "discord-plan-quarantine" / safe_name / str(_now())
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(plans_dir), str(dest))
        worker["generated_plan_quarantine_path"] = str(dest)
        worker["generated_plan_quarantined_at"] = _now()
        try:
            (Path(worktree_path) / ".hermes").rmdir()
        except OSError:
            pass
    except Exception as exc:
        worker["generated_plan_quarantine_error"] = str(exc)


def _is_generated_plan_status_line(line: str) -> bool:
    path = line[3:].strip() if len(line) > 3 else line.strip()
    return path == ".hermes/plans" or path.startswith(".hermes/plans/")


def _meaningful_worktree_status(worktree_path: str) -> list[str]:
    status = _git_text(
        worktree_path,
        ["status", "--porcelain", "--untracked-files=all"],
    )
    if not status:
        return []
    return [line for line in status.splitlines() if not _is_generated_plan_status_line(line)]


def _worktree_head_merged_into(worktree_path: str, base_branch: str) -> bool:
    refs = [base_branch]
    if base_branch and "/" not in base_branch:
        refs.append(f"origin/{base_branch}")
    for ref in [r for r in refs if r]:
        try:
            verify = subprocess.run(
                ["git", "rev-parse", "--verify", "--quiet", ref],
                cwd=worktree_path,
                timeout=10,
            )
            if verify.returncode != 0:
                continue
            merged = subprocess.run(
                ["git", "merge-base", "--is-ancestor", "HEAD", ref],
                cwd=worktree_path,
                timeout=20,
            )
        except Exception:
            continue
        if merged.returncode == 0:
            return True
    return False


def _remove_clean_merged_worktree(repo_root: str, worktree_path: str) -> Optional[str]:
    cwd = repo_root if repo_root and os.path.isdir(repo_root) else worktree_path
    try:
        result = subprocess.run(
            ["git", "worktree", "remove", worktree_path],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        return str(exc)
    if result.returncode == 0:
        return None
    return (result.stderr or result.stdout or "git worktree remove failed").strip()


def _prepare_existing_code_island_worktree(
    worker: dict[str, Any],
    *,
    repo_root: str,
    branch: str,
    base_branch: str,
) -> bool:
    """Return True when the existing worktree path has been fully handled."""
    worktree_path = str(worker.get("worktree_path") or "").strip()
    if not os.path.isdir(worktree_path):
        return False
    if not _is_git_worktree(worktree_path):
        worker["code_island_ready"] = False
        worker["code_island_pending"] = False
        worker["code_island_error"] = f"worktree path is not a git repository: {worktree_path}"
        return True

    _quarantine_generated_plan_artifacts(worktree_path, worker)
    current_branch = _current_worktree_branch(worktree_path)
    if branch and current_branch and current_branch != branch:
        status = _meaningful_worktree_status(worktree_path)
        if status:
            worker["code_island_ready"] = False
            worker["code_island_pending"] = False
            worker["code_island_error"] = (
                f"worker checkout is on {current_branch!r}, expected {branch!r}, "
                f"and has local changes: {'; '.join(status[:5])}"
            )
            return True
        if _worktree_head_merged_into(worktree_path, base_branch):
            removed_error = _remove_clean_merged_worktree(repo_root, worktree_path)
            if removed_error is None:
                worker["stale_worktree_removed_at"] = _now()
                worker["stale_worktree_previous_branch"] = current_branch
                worker["code_island_ready"] = False
                worker["code_island_pending"] = True
                worker.pop("code_island_error", None)
                return False
            worker["code_island_ready"] = False
            worker["code_island_pending"] = False
            worker["code_island_error"] = f"could not remove stale worker checkout: {removed_error}"
            return True
        worker["code_island_ready"] = False
        worker["code_island_pending"] = False
        worker["code_island_error"] = (
            f"worker checkout is on {current_branch!r}, expected {branch!r}; "
            f"not removing because HEAD is not merged into {base_branch!r}"
        )
        return True

    worker["code_island_ready"] = True
    worker["code_island_pending"] = False
    worker.pop("code_island_error", None)
    return True


def _code_island_blocker(worker: dict[str, Any]) -> str:
    project_path = str(worker.get("project_path") or "").strip()
    worktree_path = str(worker.get("worktree_path") or "").strip()
    branch = str(worker.get("worker_branch") or "").strip()
    context = worker.get("project_context") if isinstance(worker.get("project_context"), dict) else {}
    channel = str(context.get("project_channel_id") or worker.get("parent_channel_id") or "").strip()
    if not project_path:
        suffix = f" for Discord channel {channel}" if channel else ""
        return f"No project checkout is mapped{suffix}; configure discord.channel_cwds before running workers."
    if not os.path.isdir(project_path):
        return f"Mapped project checkout does not exist: {project_path}"
    if not branch:
        return "Discord worker branch is not configured."
    if not worktree_path:
        return "Discord worker worktree path is not configured."
    if os.path.isdir(worktree_path) and not _is_git_worktree(worktree_path):
        return f"Worker checkout exists but is not a git repository: {worktree_path}"
    error = str(worker.get("code_island_error") or "").strip()
    if error:
        return f"Could not prepare worker checkout: {error}"
    return ""


def _block_worker_board_for_code_island(board: str, worker: dict[str, Any], reason: str) -> None:
    worker.update(
        {
            "phase": "blocked",
            "goal_status": "blocked",
            "blocked_reason": reason,
            "code_island_pending": False,
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
            "updated_at": _now(),
        }
    )
    _update_worker_meta(board, worker)
    persist_board_run_summary(board)


def _mark_code_island_deferred(worker: dict[str, Any]) -> None:
    if not _code_island_configured(worker):
        worker["code_island_pending"] = False
        return
    worktree_path = str(worker.get("worktree_path") or "").strip()
    if os.path.isdir(worktree_path):
        if _is_git_worktree(worktree_path):
            worker["code_island_ready"] = True
            worker["code_island_pending"] = False
            worker.pop("code_island_error", None)
        else:
            worker["code_island_ready"] = False
            worker["code_island_pending"] = True
            worker["code_island_error"] = f"worktree path is not a git repository: {worktree_path}"
        return
    worker["code_island_ready"] = False
    worker["code_island_pending"] = True
    worker["code_island_requested_at"] = worker.get("code_island_requested_at") or _now()


def _ensure_code_island(worker: dict[str, Any]) -> None:
    project_path = str(worker.get("project_path") or "").strip()
    worktree_path = str(worker.get("worktree_path") or "").strip()
    branch = str(worker.get("worker_branch") or "").strip()
    base_branch = str(worker.get("base_branch") or "main").strip() or "main"
    if not project_path or not worktree_path or not branch or not os.path.isdir(project_path):
        worker["code_island_pending"] = False
        return

    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as exc:
        worker["code_island_ready"] = False
        worker["code_island_pending"] = True
        worker["code_island_error"] = str(exc)
        return
    if root.returncode != 0:
        worker["code_island_error"] = (root.stderr or root.stdout or "not a git repository").strip()
        return
    repo_root = root.stdout.strip() or project_path

    if os.path.isdir(worktree_path):
        handled = _prepare_existing_code_island_worktree(
            worker,
            repo_root=repo_root,
            branch=branch,
            base_branch=base_branch,
        )
        if handled:
            return

    if os.path.isdir(worktree_path):
        return
    worker["code_island_ready"] = False
    worker["code_island_pending"] = True
    try:
        Path(worktree_path).parent.mkdir(parents=True, exist_ok=True)
        branch_exists = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", branch],
            cwd=repo_root,
            timeout=10,
        ).returncode == 0
        if branch_exists:
            cmd = ["git", "worktree", "add", worktree_path, branch]
        else:
            cmd = ["git", "worktree", "add", "-b", branch, worktree_path, base_branch]
        result = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            worker["code_island_ready"] = True
            worker["code_island_pending"] = False
            worker.pop("code_island_error", None)
        else:
            worker["code_island_ready"] = False
            worker["code_island_pending"] = True
            worker["code_island_error"] = (result.stderr or result.stdout or "git worktree add failed").strip()
    except Exception as exc:
        worker["code_island_ready"] = False
        worker["code_island_pending"] = True
        worker["code_island_error"] = str(exc)


def ensure_code_island_for_board(board: str) -> bool:
    """Prepare a Discord worker board workspace before dispatch spawns roles."""
    started = time.time()
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    if worker.get("kind") != "discord_worker_board":
        return True
    active_pipeline = (
        worker.get("execution_mode") == "kanban_pipeline"
        and worker.get("goal_status") == "active"
    )
    previous_worktree_path = str(worker.get("worktree_path") or "").strip()
    _ensure_code_island(worker)
    metadata[DISCORD_WORKER_META_KEY] = worker
    _write_metadata(board, metadata)
    if previous_worktree_path != str(worker.get("worktree_path") or ""):
        _sync_role_task_workspaces(
            board,
            old_path=previous_worktree_path,
            new_path=str(worker.get("worktree_path") or ""),
        )
    elapsed_ms = int((time.time() - started) * 1000)
    blocker = _code_island_blocker(worker) if active_pipeline else ""
    if blocker:
        _block_worker_board_for_code_island(board, worker, blocker)
    logger.info(
        "discord_worker_code_island board=%s ready=%s pending=%s elapsed_ms=%d error=%s",
        board,
        bool(worker.get("code_island_ready")),
        bool(worker.get("code_island_pending")),
        elapsed_ms,
        bool(worker.get("code_island_error")),
    )
    if blocker:
        return False
    return bool(worker.get("code_island_ready") or not _code_island_configured(worker))


def find_board_by_share_token(token: str) -> Optional[str]:
    needle = str(token or "").strip()
    if not needle:
        return None
    for board in kanban_db.list_boards(include_archived=True):
        slug = str(board.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = _read_worker_meta(slug)
        if secrets.compare_digest(str(worker.get("share_token") or ""), needle):
            return slug
    return None


def public_board_snapshot(token: str) -> dict[str, Any]:
    board = find_board_by_share_token(token)
    if not board:
        raise KeyError("unknown board token")
    return _public_board_snapshot_for_board(board)


def public_board_snapshot_for_session(session_id: str) -> dict[str, Any]:
    board = resolve_public_session_board(session_id)
    return _public_board_snapshot_for_board(board)


def _public_board_snapshot_for_board(board: str) -> dict[str, Any]:
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        summaries = kanban_db.latest_summaries(conn, [t.id for t in tasks])
        counts = kanban_db.board_stats(conn).get("by_status", {})
        running = _running_ticket_snapshot(conn)
        runtime = _board_runtime_snapshot(
            worker,
            counts=counts,
            running=running,
            conn=conn,
        )
        rows = []
        for task in tasks:
            rows.append(
                {
                    "id": task.id,
                    "title": task.title,
                    "status": task.status,
                    "assignee": task.assignee,
                    "priority": task.priority,
                    "created_at": task.created_at,
                    "completed_at": task.completed_at,
                    "body_preview": _public_task_body_preview(task.body),
                    "latest_summary": summaries.get(task.id),
                }
            )
    finally:
        conn.close()
    session_id = _public_session_id_for_board(board, worker)
    public_worker = _public_worker_meta(worker)
    public_url = public_session_board_url(session_id)
    if public_url:
        public_worker["public_url"] = public_url
    return {
        "board": board,
        "name": _worker_board_name(worker, metadata, board),
        "description": metadata.get("description") or "",
        "session_id": session_id,
        "thread_id": str(worker.get("thread_id") or ""),
        "worker": public_worker,
        "counts": counts,
        "running": running,
        "runtime": runtime,
        "board_summary": read_board_run_summary(board),
        "tasks": rows,
    }


def ticket_state_for_session(session_id: str, task_id: str) -> dict[str, Any]:
    board = resolve_public_session_board(session_id)
    worker = _read_worker_meta(board)
    return _ticket_state_for_board(board, task_id, worker=worker)


def ticket_terminal_feed_for_session(session_id: str, task_id: str) -> dict[str, Any]:
    board = resolve_public_session_board(session_id)
    worker = _read_worker_meta(board)
    return _ticket_terminal_feed_for_board(board, task_id, worker=worker)


def worker_ticket_console_for_session(session_id: str, task_id: str) -> dict[str, Any]:
    """Return authenticated operator-console state for one worker ticket."""
    board = resolve_public_session_board(session_id)
    worker = _read_worker_meta(board)
    try:
        return _worker_ticket_console_for_board(board, task_id, worker=worker)
    except KeyError:
        return _worker_ticket_console_log_only_payload(board, task_id, worker=worker)


def worker_ticket_console_log_for_session(session_id: str, task_id: str) -> dict[str, Any]:
    """Return paths and snapshot data for the authenticated operator log."""
    board = resolve_public_session_board(session_id)
    worker = _read_worker_meta(board)
    try:
        task, runs, _events, current_run, codex_state = _ticket_console_parts(board, task_id)
        task_id = str(task.id)
        snapshot = _worker_ticket_console_payload(
            board,
            task,
            runs=runs,
            events=_events,
            current_run=current_run,
            codex_state=codex_state,
            worker=worker,
        )
    except KeyError:
        task_id = str(task_id or "").strip()
        snapshot = _worker_ticket_console_log_only_payload(board, task_id, worker=worker)
    return {
        "board": board,
        "task_id": task_id,
        "log_path": str(kanban_db.worker_log_path(task_id, board=board)),
        "state_path": str(codex_worker_state_path(task_id, board=board)),
        "snapshot": snapshot,
    }


def render_public_session_ticket_terminal_html(session_id: str, task_id: str) -> str:
    """Render a shareable, sanitized terminal page for one worker ticket."""
    feed = ticket_terminal_feed_for_session(session_id, task_id)
    worker = feed.get("worker") if isinstance(feed.get("worker"), dict) else {}
    task = feed.get("task") if isinstance(feed.get("task"), dict) else {}

    def esc(value: Any) -> str:
        return html.escape(str(value or ""))

    quoted_session = quote(str(session_id or ""), safe="")
    quoted_task = quote(str(task_id or ""), safe="")
    board_url = f"/workers/{quoted_session}"
    ticket_url = f"{board_url}/tickets/{quoted_task}"
    json_url = f"{ticket_url}/terminal.json"
    title = task.get("title") or task_id
    lines = feed.get("lines") if isinstance(feed.get("lines"), list) else []
    body = "\n".join(str(line) for line in lines) if lines else "(no terminal activity yet)"
    updated = _format_public_timestamp(feed.get("updated_at")) or "now"
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)} - Terminal</title>
  <style>
{_workers_page_css()}
    .terminal-page {{ margin: 0 auto; max-width: 1040px; }}
    .terminal-head {{ align-items: flex-start; display: flex; flex-wrap: wrap; gap: 12px; justify-content: space-between; margin-bottom: 12px; }}
    .terminal-log {{ background: #111827; border: 1px solid #374151; border-radius: 8px; color: #f9fafb; font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; overflow: auto; padding: 16px; white-space: pre-wrap; word-break: break-word; }}
  </style>
</head>
<body>
  <header>
    <div class="top-nav">
      <a class="brand" href="/workers">Hermes<br>Kanban</a>
      <a class="back-link" href="{esc(ticket_url)}">Back to Ticket</a>
    </div>
    <div class="hero">
      <h1>{esc(title)}</h1>
      <div class="meta">
        <span>Ticket: <code>{esc(task.get("id") or task_id)}</code></span>
        <span>Status: {esc(task.get("status") or "unknown")}</span>
        <span>Assignee: {esc(task.get("assignee") or "unassigned")}</span>
        <span>Session: <code>{esc(worker.get("thread_id") or session_id)}</code></span>
        <span>Updated: {esc(updated)}</span>
      </div>
    </div>
  </header>
  <main class="terminal-page">
    <div class="terminal-head">
      <a class="button-link" href="{esc(board_url)}">Board</a>
      <a class="button-link" href="{esc(ticket_url)}">Ticket Modal</a>
      <a class="button-link" href="{esc(json_url)}">JSON Feed</a>
    </div>
    <pre class="terminal-log">{esc(body)}</pre>
  </main>
</body>
</html>"""


def move_ticket_for_session(session_id: str, task_id: str, status: str) -> dict[str, Any]:
    """Move one public worker-board ticket to another visible status column."""
    board = resolve_public_session_board(session_id)
    worker = _read_worker_meta(board)

    task_id = str(task_id or "").strip()
    new_status = str(status or "").strip().lower()
    if not task_id:
        raise KeyError("unknown ticket")
    if new_status not in PUBLIC_BOARD_COLUMNS:
        raise ValueError(f"unknown status: {new_status}")

    conn = kanban_db.connect(board=board)
    try:
        if kanban_db.get_task(conn, task_id) is None:
            raise KeyError("unknown ticket")
        ok = kanban_db.move_task_status(
            conn,
            task_id,
            new_status,
            source="workers-page",
        )
        if not ok:
            if new_status == "ready":
                blockers = kanban_db.parents_blocking_ready(conn, task_id)
                if blockers:
                    names = ", ".join(
                        f"{p['title']!r} ({p['id']}, status={p['status']})"
                        for p in blockers
                    )
                    raise TicketMoveConflict(
                        "Cannot move to 'ready': blocked by parent(s) "
                        f"not done - {names}"
                    )
            raise TicketMoveConflict(
                f"status transition to {new_status!r} not valid from current state"
            )
    finally:
        conn.close()

    snapshot = _public_board_snapshot_for_board(board)
    updated = next(
        (task for task in snapshot.get("tasks", []) if task.get("id") == task_id),
        None,
    )
    return {"ok": True, "task": updated, "snapshot": snapshot}


def _ticket_state_for_board(
    board: str,
    task_id: str,
    *,
    worker: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise KeyError("unknown ticket")
    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise KeyError("unknown ticket")
        runs = kanban_db.list_runs(conn, task_id)
        events = kanban_db.list_events(conn, task_id)
        codex_state = _ticket_codex_state(task_id, board=board)
        payload = {
            "board": board,
            "worker": _public_worker_meta(worker or _read_worker_meta(board)),
            "task": _task_state_dict(task),
            "worker_run": _worker_run_profile_state(
                codex_state.get("result") if isinstance(codex_state, dict) else None
            ),
            "current_run": _current_run_state(task, runs),
            "runs": [_run_state_dict(run) for run in runs],
            "events": [_event_state_dict(event) for event in events[-200:]],
            "worker_log_tail": kanban_db.read_worker_log(
                task_id,
                tail_bytes=CODEX_STATE_LOG_TAIL_BYTES,
                board=board,
            ),
            "codex_state": codex_state,
        }
    finally:
        conn.close()
    return _redact_public_state(payload)


def _ticket_terminal_feed_for_board(
    board: str,
    task_id: str,
    *,
    worker: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise KeyError("unknown ticket")
    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise KeyError("unknown ticket")
        runs = kanban_db.list_runs(conn, task_id)
        events = kanban_db.list_events(conn, task_id)
        current_run = _current_run_state(task, runs)
        log_text = kanban_db.read_worker_log(task_id, board=board)
        codex_state = _read_codex_worker_state(task_id, board=board)
    finally:
        conn.close()

    lines = _terminal_feed_lines(
        task,
        current_run=current_run,
        events=events,
        log_text=log_text,
        codex_state=codex_state,
    )
    payload = {
        "board": board,
        "worker": _public_worker_meta(worker or _read_worker_meta(board)),
        "task": {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "assignee": task.assignee,
        },
        "current_run": _public_terminal_run(current_run),
        "updated_at": _now(),
        "lines": lines,
    }
    return _redact_terminal_state(payload)


def _ticket_console_parts(board: str, task_id: str) -> tuple[Any, list[Any], list[Any], Optional[dict[str, Any]], dict[str, Any]]:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise KeyError("unknown ticket")
    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            raise KeyError("unknown ticket")
        runs = kanban_db.list_runs(conn, task_id)
        events = kanban_db.list_events(conn, task_id)
        current_run = _current_run_state(task, runs)
        codex_state = _read_codex_worker_state(task_id, board=board)
    finally:
        conn.close()
    return task, runs, events, current_run, codex_state


def _worker_ticket_console_for_board(
    board: str,
    task_id: str,
    *,
    worker: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    task, runs, events, current_run, codex_state = _ticket_console_parts(board, task_id)
    return _worker_ticket_console_payload(
        board,
        task,
        runs=runs,
        events=events,
        current_run=current_run,
        codex_state=codex_state,
        worker=worker,
    )


def _worker_ticket_console_payload(
    board: str,
    task: Any,
    *,
    runs: list[Any],
    events: list[Any],
    current_run: Optional[dict[str, Any]],
    codex_state: dict[str, Any],
    worker: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    workspace_path = _ticket_workspace_path(task, board=board)
    workspace_available = bool(workspace_path and Path(workspace_path).expanduser().is_dir())
    log_text = kanban_db.read_worker_log(
        task.id,
        tail_bytes=CODEX_STATE_LOG_TAIL_BYTES,
        board=board,
    )
    return {
        "board": board,
        "worker": _public_worker_meta(worker or _read_worker_meta(board)),
        "task": _task_state_dict(task),
        "workspace": {
            "path": workspace_path,
            "kind": task.workspace_kind,
            "available": workspace_available,
        },
        "backend": _worker_state_backend(codex_state),
        "current_run": _public_terminal_run(current_run),
        "runs": [_run_state_dict(run) for run in runs[-20:]],
        "events": [_event_state_dict(event) for event in events[-200:]],
        "worker_log_path": str(kanban_db.worker_log_path(task.id, board=board)),
        "worker_log_tail": log_text or "",
        "codex_state": codex_state if isinstance(codex_state, dict) else {},
        "updated_at": _now(),
    }


def _worker_ticket_console_log_only_payload(
    board: str,
    task_id: str,
    *,
    worker: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    task_id = str(task_id or "").strip()
    if not task_id:
        raise KeyError("unknown ticket")
    log_path = kanban_db.worker_log_path(task_id, board=board)
    state_path = codex_worker_state_path(task_id, board=board)
    if not log_path.exists() and not state_path.exists():
        raise KeyError("unknown ticket")
    codex_state = _ticket_codex_state(task_id, board=board)
    log_text = kanban_db.read_worker_log(
        task_id,
        tail_bytes=CODEX_STATE_LOG_TAIL_BYTES,
        board=board,
    )
    return {
        "board": board,
        "worker": _public_worker_meta(worker or _read_worker_meta(board)),
        "task": {
            "id": task_id,
            "title": f"Retained worker activity for {task_id}",
            "body": "Task metadata is no longer present; showing retained worker log/state artifacts.",
            "assignee": None,
            "status": "log-only",
            "priority": 0,
            "created_by": None,
            "created_at": None,
            "started_at": None,
            "completed_at": None,
            "workspace_kind": "unknown",
            "workspace_path": "",
            "branch_name": None,
            "result": None,
            "claim_lock": None,
            "claim_expires": None,
            "worker_pid": None,
            "last_failure_error": None,
            "max_runtime_seconds": None,
            "last_heartbeat_at": None,
            "current_run_id": None,
            "model_override": None,
            "session_id": None,
        },
        "workspace": {
            "path": "",
            "kind": "unknown",
            "available": False,
        },
        "backend": _worker_state_backend(codex_state),
        "current_run": None,
        "runs": [],
        "events": [],
        "worker_log_path": str(log_path),
        "worker_log_tail": log_text or "",
        "codex_state": codex_state if isinstance(codex_state, dict) else {},
        "updated_at": _now(),
        "log_only": True,
    }


def _ticket_workspace_path(task: Any, *, board: str) -> str:
    raw_path = str(getattr(task, "workspace_path", "") or "").strip()
    if raw_path:
        return str(Path(raw_path).expanduser())
    kind = str(getattr(task, "workspace_kind", "") or "scratch")
    if kind == "scratch":
        return str(kanban_db.workspaces_root(board=board) / str(task.id))
    if kind == "worktree":
        return str(Path.cwd() / ".worktrees" / str(task.id))
    return ""


def _public_terminal_run(run: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not run:
        return None
    keys = (
        "id",
        "status",
        "outcome",
        "worker_pid",
        "started_at",
        "last_heartbeat_at",
        "ended_at",
        "summary",
        "error",
        "metadata",
    )
    return {key: run.get(key) for key in keys}


def _safe_terminal_text(value: Any, *, max_chars: int = 240) -> str:
    text = str(value or "").strip().replace("\r", " ").replace("\n", " ")
    if len(text) > max_chars:
        return f"{text[:max_chars].rstrip()}..."
    return text


def _redact_terminal_state(value: Any) -> Any:
    """Redact credentials for authenticated operator terminal views."""
    if isinstance(value, str):
        from agent.redact import redact_sensitive_text

        return redact_sensitive_text(value, force=True)
    if isinstance(value, list):
        return [_redact_terminal_state(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_terminal_state(item) for key, item in value.items()}
    return value


def _terminal_feed_lines(
    task: Any,
    *,
    current_run: Optional[dict[str, Any]],
    events: list[Any],
    log_text: Optional[str],
    codex_state: Optional[dict[str, Any]],
) -> list[str]:
    lines = [
        f"$ ticket {task.id}",
        f"title: {_safe_terminal_text(task.title)}",
        f"status: {_safe_terminal_text(task.status)}",
    ]
    if task.assignee:
        lines.append(f"assignee: {_safe_terminal_text(task.assignee)}")
    worker_run = _worker_run_profile_line(
        (codex_state or {}).get("result") if isinstance(codex_state, dict) else None
    )
    if worker_run:
        lines.append(f"worker_run: {worker_run}")
    if current_run:
        run_bits = [
            f"run={current_run.get('id') or '-'}",
            f"status={current_run.get('status') or current_run.get('outcome') or '-'}",
        ]
        if current_run.get("worker_pid"):
            run_bits.append(f"pid={current_run.get('worker_pid')}")
        started = _format_public_timestamp(current_run.get("started_at"))
        heartbeat = _format_public_timestamp(current_run.get("last_heartbeat_at"))
        ended = _format_public_timestamp(current_run.get("ended_at"))
        if started:
            run_bits.append(f"started={started}")
        if heartbeat:
            run_bits.append(f"heartbeat={heartbeat}")
        if ended:
            run_bits.append(f"ended={ended}")
        lines.append("run: " + " ".join(run_bits))
    else:
        lines.append("run: not started")

    event_lines: list[str] = []
    for event in events[-40:]:
        line = _terminal_event_line(event)
        if line:
            event_lines.append(line)
    if event_lines:
        lines.append("")
        lines.append("# lifecycle")
        lines.extend(event_lines)

    public_log_lines = _public_worker_log_lines(log_text)
    if public_log_lines:
        lines.append("")
        lines.append("# worker terminal")
        lines.extend(public_log_lines)
    elif log_text:
        lines.append("")
        lines.append("# worker terminal")
        lines.append("(worker process output hidden)")
    codex_log_lines = _codex_app_worker_log_lines(codex_state)
    if codex_log_lines:
        lines.append("")
        lines.append(f"# {_worker_state_log_label(codex_state)}")
        lines.extend(codex_log_lines)
    else:
        diagnostics: list[str] = []
        if not current_run:
            diagnostics.append("worker run has not started yet")
        if not public_log_lines and not log_text:
            diagnostics.append("no worker stdout/stderr log has been captured yet")
        diagnostics.append(
            "no Codex app-server internals, state, or event log has been captured yet"
        )
        if diagnostics:
            lines.append("")
            lines.append("# diagnostics")
            lines.extend(f"- {line}" for line in diagnostics)
    return lines


def _terminal_event_line(event: Any) -> str:
    kind = str(getattr(event, "kind", "") or "").strip()
    if not kind:
        return ""
    created = _format_public_timestamp(getattr(event, "created_at", None)) or "time unknown"
    payload = getattr(event, "payload", None) if isinstance(getattr(event, "payload", None), dict) else {}
    if kind == "claimed":
        return f"[{created}] claimed by worker"
    if kind == "spawned":
        pid = payload.get("pid") if isinstance(payload, dict) else None
        return f"[{created}] spawned worker pid={pid}" if pid else f"[{created}] spawned worker"
    if kind == "completed":
        summary = _safe_terminal_text(payload.get("summary") if isinstance(payload, dict) else "")
        return f"[{created}] completed: {summary}" if summary else f"[{created}] completed"
    if kind == "blocked":
        reason = _safe_terminal_text(payload.get("reason") if isinstance(payload, dict) else "")
        return f"[{created}] blocked: {reason}" if reason else f"[{created}] blocked"
    if kind in {"reclaimed", "archived", "scheduled", "promoted", "assigned"}:
        return f"[{created}] {kind}"
    if kind in {"spawn_failed", "crashed", "timed_out", "gave_up"}:
        reason = _safe_terminal_text(payload.get("error") if isinstance(payload, dict) else "")
        label = kind.replace("_", " ")
        return f"[{created}] {label}: {reason}" if reason else f"[{created}] {label}"
    return ""


def _public_worker_log_lines(log_text: Optional[str]) -> list[str]:
    if not log_text:
        return []
    lines: list[str] = []
    for raw in str(log_text).splitlines():
        line = raw.rstrip()
        if not line.strip():
            lines.append("")
            continue
        for label in ("Codex", "OpenCode"):
            marker = f"spawning {label} role worker"
            if marker in line:
                prefix = line.split(marker, 1)[0]
                line = prefix + f"{marker}: [command hidden]"
                break
        lines.append(_safe_terminal_text(line, max_chars=1200))
    return lines


def _worker_state_backend(state: Optional[dict[str, Any]]) -> str:
    if not isinstance(state, dict):
        return "codex"
    result = state.get("result") if isinstance(state.get("result"), dict) else {}
    backend = str(result.get("backend") or "").strip().lower()
    if backend in {"codex", "opencode"}:
        return backend
    events = state.get("events") if isinstance(state.get("events"), list) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        method = str(event.get("method") or "").strip().lower()
        if method.startswith("opencode/"):
            return "opencode"
    return "codex"


def _worker_state_log_label(state: Optional[dict[str, Any]]) -> str:
    if _worker_state_backend(state) == "opencode":
        return "opencode worker log"
    return "codex app worker log"


def _codex_app_worker_log_lines(state: Optional[dict[str, Any]]) -> list[str]:
    if not isinstance(state, dict) or not state:
        return []
    lines: list[str] = []
    backend = _worker_state_backend(state)
    backend_label = "opencode" if backend == "opencode" else "codex app"
    truncated = int(state.get("truncated_events") or 0)
    if truncated > 0:
        lines.append(f"... {truncated} older {backend_label} event(s) truncated by retention")
    events = state.get("events") if isinstance(state.get("events"), list) else []
    for event in events:
        line = _codex_app_event_line(event, backend=backend)
        if line:
            lines.append(line)
    result_line = _codex_app_result_line(state.get("result"))
    if result_line:
        lines.append(result_line)
    return lines


def _codex_app_event_line(event: Any, *, backend: str = "codex") -> str:
    if not isinstance(event, dict):
        return ""
    method = str(event.get("method") or "").strip()
    if not method:
        return ""
    created = _format_public_timestamp(event.get("ts")) or "time unknown"
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    item_type = str(event.get("item_type") or item.get("type") or "").strip()
    item_label = item_type or _codex_method_item_label(method)
    backend_label = "opencode" if backend == "opencode" else "codex"
    parts = [f"[{created}] {backend_label} {method}"]
    if item_label:
        parts.append(item_label)

    if method.endswith("/outputDelta") or method.endswith("/delta"):
        parts.append("output hidden")
        return _codex_log_line(parts)

    turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
    status = item.get("status") or turn.get("status")
    if status:
        parts.append(f"status={_safe_terminal_text(status, max_chars=80)}")
    if item_type == "commandExecution":
        cwd = item.get("cwd")
        exit_code = item.get("exitCode")
        duration = item.get("durationMs")
        if cwd:
            parts.append(f"cwd={_safe_terminal_text(cwd, max_chars=260)}")
        if exit_code is not None:
            parts.append(f"exit={exit_code}")
        if duration is not None:
            parts.append(f"duration={duration}ms")
        if item.get("aggregatedOutput"):
            parts.append("output hidden")
    elif item_type == "fileChange":
        summary = _codex_file_change_summary(item)
        if summary:
            parts.append(summary)
    elif item_type in {"agentMessage", "reasoning", "userMessage"}:
        parts.append("content hidden")
    elif item_type in {"mcpToolCall", "dynamicToolCall"}:
        tool = item.get("tool") or item.get("name")
        server = item.get("server")
        if server:
            parts.append(f"server={_safe_terminal_text(server, max_chars=80)}")
        if tool:
            parts.append(f"tool={_safe_terminal_text(tool, max_chars=120)}")
        parts.append("result hidden")

    error_obj = turn.get("error")
    if isinstance(error_obj, dict):
        message = error_obj.get("message") or error_obj.get("code")
        if message:
            parts.append(f"error={_safe_terminal_text(message, max_chars=260)}")
    return _codex_log_line(parts)


def _codex_log_line(parts: list[Any]) -> str:
    if len(parts) <= 1:
        return str(parts[0]) if parts else ""
    return ": ".join([str(parts[0]), " ".join(str(part) for part in parts[1:])])


def _codex_method_item_label(method: str) -> str:
    if method.startswith("item/"):
        bits = [bit for bit in method.split("/") if bit]
        if len(bits) >= 2:
            return bits[1]
    return ""


def _codex_file_change_summary(item: dict[str, Any]) -> str:
    changes = item.get("changes") if isinstance(item.get("changes"), list) else []
    if not changes:
        return "changes=0"
    labels: list[str] = []
    for change in changes[:12]:
        if not isinstance(change, dict):
            continue
        kind_obj = change.get("kind") if isinstance(change.get("kind"), dict) else {}
        kind = str(kind_obj.get("type") or change.get("type") or "update")
        path = str(change.get("path") or "")
        labels.append(f"{kind}:{path}" if path else kind)
    if len(changes) > len(labels):
        labels.append(f"+{len(changes) - len(labels)} more")
    return _safe_terminal_text(
        f"changes={len(changes)} " + ", ".join(labels),
        max_chars=1200,
    )


def _codex_app_result_line(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    backend = str(result.get("backend") or "codex").strip().lower()
    label = "opencode" if backend == "opencode" else "codex"
    bits = [f"{label} result"]
    if result.get("error"):
        bits.append(f"error={_safe_terminal_text(result.get('error'), max_chars=260)}")
    if result.get("interrupted"):
        bits.append("interrupted=true")
    if result.get("timed_out"):
        bits.append("timed_out=true")
    if result.get("tool_iterations") not in (None, ""):
        bits.append(f"tool_iterations={result.get('tool_iterations')}")
    if result.get("turn_id"):
        bits.append(f"turn={_safe_terminal_text(result.get('turn_id'), max_chars=80)}")
    if result.get("thread_id"):
        bits.append(f"thread={_safe_terminal_text(result.get('thread_id'), max_chars=80)}")
    return " ".join(bits) if len(bits) > 1 else ""


def _worker_run_profile_state(result: Any) -> Optional[dict[str, Any]]:
    if not isinstance(result, dict):
        return None
    summary = _worker_run_profile_line(result)
    if not summary:
        return None
    return {
        "summary": summary,
        "profile": result.get("run_profile") if isinstance(result.get("run_profile"), dict) else {},
        "service_tier": result.get("service_tier"),
        "fast_mode": result.get("fast_mode"),
    }


def _worker_run_profile_line(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    profile = result.get("run_profile") if isinstance(result.get("run_profile"), dict) else {}
    passes = profile.get("passes") if isinstance(profile.get("passes"), list) else []
    label = str(profile.get("label") or "").strip()
    if not label and passes:
        names = [str(item.get("name") or "").strip() for item in passes if isinstance(item, dict)]
        names = [name for name in names if name]
        if len(names) == 2 and names == ["plan", "build"]:
            label = "2-pass plan+build"
        elif len(names) == 1 and names[0] == "build":
            label = "1-pass simple build"
        elif len(names) == 1:
            label = f"1-pass {names[0]}"
    if not label:
        return ""

    bits = [label]
    for item in passes:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("agent") or "").strip()
        reasoning = _format_worker_reasoning(item.get("reasoning"))
        if name and reasoning:
            bits.append(f"{name} reasoning={reasoning}")

    service_tier = str(result.get("service_tier") or "").strip().lower()
    fast_raw = result.get("fast_mode")
    fast_mode: Optional[bool]
    if isinstance(fast_raw, bool):
        fast_mode = fast_raw
    elif service_tier:
        fast_mode = service_tier == "fast"
    else:
        fast_mode = None
    if fast_mode is not None:
        bits.append(f"fast mode={'on' if fast_mode else 'off'}")
    return _safe_terminal_text("; ".join(bits), max_chars=400)


def _format_worker_reasoning(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw == "xhigh":
        return "x-high"
    return raw


def _public_task_body_preview(value: Any, *, max_chars: int = 420) -> str:
    """Return a compact, public-safe ticket brief for board cards."""
    text = str(value or "").strip()
    if not text:
        return ""
    redacted = _redact_public_state(text)
    text = str(redacted or "").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > max_chars:
        return f"{text[:max_chars].rstrip()}..."
    return text


def _task_state_dict(task: Any) -> dict[str, Any]:
    return {
        "id": task.id,
        "title": task.title,
        "body": task.body,
        "assignee": task.assignee,
        "status": task.status,
        "priority": task.priority,
        "created_by": task.created_by,
        "created_at": task.created_at,
        "started_at": task.started_at,
        "completed_at": task.completed_at,
        "workspace_kind": task.workspace_kind,
        "workspace_path": task.workspace_path,
        "branch_name": task.branch_name,
        "result": task.result,
        "claim_lock": task.claim_lock,
        "claim_expires": task.claim_expires,
        "worker_pid": task.worker_pid,
        "last_failure_error": task.last_failure_error,
        "max_runtime_seconds": task.max_runtime_seconds,
        "last_heartbeat_at": task.last_heartbeat_at,
        "current_run_id": task.current_run_id,
        "model_override": task.model_override,
        "session_id": task.session_id,
    }


def _run_state_dict(run: Any) -> dict[str, Any]:
    return {
        "id": run.id,
        "task_id": run.task_id,
        "profile": run.profile,
        "step_key": run.step_key,
        "status": run.status,
        "claim_lock": run.claim_lock,
        "claim_expires": run.claim_expires,
        "worker_pid": run.worker_pid,
        "max_runtime_seconds": run.max_runtime_seconds,
        "last_heartbeat_at": run.last_heartbeat_at,
        "started_at": run.started_at,
        "ended_at": run.ended_at,
        "outcome": run.outcome,
        "summary": run.summary,
        "metadata": run.metadata,
        "error": run.error,
    }


def _event_state_dict(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "run_id": event.run_id,
        "kind": event.kind,
        "payload": event.payload,
        "created_at": event.created_at,
    }


def _redact_public_state(value: Any) -> Any:
    if isinstance(value, str):
        from agent.redact import redact_sensitive_text

        text = _WINDOWS_PATH_RE.sub("[REDACTED_PATH]", value)
        text = _POSIX_PATH_RE.sub("[REDACTED_PATH]", text)
        return redact_sensitive_text(text, force=True)
    if isinstance(value, list):
        return [_redact_public_state(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_public_state(item) for key, item in value.items()}
    return value


def _public_worker_meta(worker: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "kind",
        "thread_id",
        "initial_request",
        "root_goal",
        "summary_title",
        "concise_outcome",
        "goal_status",
        "phase",
        "execution_mode",
        "criteria",
        "worker_branch",
        "review_loop_count",
        "review_loop_limit",
        "public_url",
        "pr_url",
        "pr_number",
        "paused",
        "cancelled",
        "blocked_reason",
        "created_at",
        "updated_at",
    }
    public = {key: value for key, value in worker.items() if key in allowed}
    discord_url = _discord_thread_url(worker)
    if discord_url:
        public["discord_thread_url"] = discord_url
    return public


def _clean_feature_summary_text(
    value: Any,
    *,
    max_chars: int,
    default: str = "",
) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip()) or default
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _worker_generated_title(worker: dict[str, Any]) -> str:
    return _clean_feature_summary_text(
        worker.get("summary_title"),
        max_chars=80,
        default="",
    )


def _fallback_feature_title(worker: dict[str, Any]) -> str:
    source = str(worker.get("root_goal") or worker.get("initial_request") or "").strip()
    match = re.match(r"^/goal(?:\s+(.*))?$", source, re.IGNORECASE | re.DOTALL)
    if match and (match.group(1) or "").strip().lower() not in GOAL_CONTROL_COMMANDS:
        source = (match.group(1) or "").strip()
    return _clean_feature_summary_text(
        source,
        max_chars=80,
        default="Discord Worker Feature",
    )


def _worker_index_title(worker: dict[str, Any], fallback: Any) -> str:
    return (
        _worker_generated_title(worker)
        or _clean_feature_summary_text(
            worker.get("root_goal") or worker.get("initial_request") or fallback,
            max_chars=160,
            default="Discord Worker Board",
        )
    )


def _worker_board_name(worker: dict[str, Any], metadata: dict[str, Any], board: str) -> str:
    return _worker_generated_title(worker) or str(metadata.get("name") or board)


def set_feature_summary_title(board: str, title: str) -> str:
    """Persist the generated feature title used by embeds and /workers."""
    cleaned = _clean_feature_summary_text(title, max_chars=80, default="")
    if not board or not cleaned:
        return ""
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board":
        return ""
    if _worker_generated_title(worker) == cleaned:
        return cleaned
    worker["summary_title"] = cleaned
    _update_worker_meta(board, worker)
    return cleaned


def _format_public_timestamp(value: Any) -> str:
    try:
        ts = int(value)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(ts))


def _public_status_text(worker: dict[str, Any]) -> str:
    status = str(worker.get("goal_status") or "").strip()
    phase = str(worker.get("phase") or "").strip()
    if status and phase and status != phase:
        return f"{status} / {phase}"
    return status or phase or "pending"


def _public_board_summary_html(summary: dict[str, Any]) -> str:
    if not summary:
        return ""
    text = render_board_run_summary_text(summary)
    return (
        '<section class="criteria board-summary">'
        '<strong>Terminal Summary</strong>'
        f'<pre>{html.escape(text)}</pre>'
        '</section>'
    )


def _public_review_text(worker: dict[str, Any]) -> str:
    count = worker.get("review_loop_count")
    limit = _review_loop_limit_for_worker(worker)
    return f"{count or 0}/{limit}"


def _public_count_text(counts: dict[str, Any]) -> str:
    if not counts:
        return "none"
    ordered = ["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done"]
    keys = [key for key in ordered if key in counts]
    keys.extend(sorted(key for key in counts if key not in ordered))
    return " ".join(f"{key}:{counts.get(key) or 0}" for key in keys)


def _worker_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        return dict((cfg.get("kanban") or {}).get("discord_worker") or {})
    except Exception:
        return {}


def _kanban_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        return dict((load_config() or {}).get("kanban") or {})
    except Exception:
        return {}


def _active_role_count_across_boards() -> int:
    total = 0
    for board_meta in kanban_db.list_boards(include_archived=False):
        board = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = _read_worker_meta(board)
        if worker.get("kind") != "discord_worker_board":
            continue
        with kanban_db.connect_closing(board=board) as conn:
            total += len(_running_ticket_snapshot(conn))
    return total


def board_thread_state(board: str) -> str:
    """Return the Discord thread-facing state for a worker board."""
    worker = _read_worker_meta(board)
    if worker.get("cancelled") or worker.get("goal_status") == "cancelled":
        return "errored"
    is_terminal = worker.get("goal_status") == "done" or worker.get("phase") == "complete"
    has_worker_blocker = (
        bool(str(worker.get("blocked_reason") or "").strip())
        or worker.get("goal_status") == "blocked"
    )

    with kanban_db.connect_closing(board=board) as conn:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        if tasks:
            blocked_tasks = [task for task in tasks if task.status == "blocked"]
            for task in blocked_tasks:
                latest = kanban_db.latest_run(conn, task.id)
                if (
                    (latest and latest.outcome in {"spawn_failed", "crashed", "timed_out", "gave_up"})
                    or (latest is None and task.last_failure_error)
                ):
                    return "errored"
            if blocked_tasks:
                return "blocked"
            if all(task.status == "done" for task in tasks):
                return "done"
            if any(task.status == "running" for task in tasks):
                return "running"
            return "active"

    if is_terminal:
        return "done"
    if has_worker_blocker:
        return "blocked"
    return "active"


def board_thread_reaction_state(board: str) -> str:
    """Return the Discord reaction state for a worker board.

    ``active`` covers both never-started queues and boards that are between
    worker claims. Reactions need to distinguish those: once any ticket has
    started or completed, keep the visible marker in the working state instead
    of falling back to the pickup/queued eyes marker.
    """
    state = board_thread_state(board)
    if state != "active":
        return state

    with kanban_db.connect_closing(board=board) as conn:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        if any(getattr(task, "started_at", None) or getattr(task, "completed_at", None) for task in tasks):
            return "running"
    return "active"


def source_task_reaction_state(board: str, task_id: str) -> Optional[str]:
    """Return Discord reaction/status state for a source Kanban task."""
    board = str(board or "").strip() or kanban_db.DEFAULT_BOARD
    task_id = str(task_id or "").strip()
    if not task_id:
        return None
    with kanban_db.connect_closing(board=board) as conn:
        task = kanban_db.get_task(conn, task_id)
        if task is None:
            return None
        latest = kanban_db.latest_run(conn, task_id)

    status = str(getattr(task, "status", "") or "").strip().lower()
    latest_status = str(getattr(latest, "status", "") or "").strip().lower() if latest else ""
    latest_outcome = str(getattr(latest, "outcome", "") or "").strip().lower() if latest else ""
    latest_error_like = latest_status in _SOURCE_TASK_ERROR_OUTCOMES or latest_outcome in _SOURCE_TASK_ERROR_OUTCOMES

    if status == "done":
        return "done"
    if latest_error_like:
        return "errored"
    if status == "blocked":
        if str(getattr(task, "last_failure_error", "") or "").strip():
            return "errored"
        return "blocked"
    if status == "running" or (latest_status == "running" and getattr(latest, "ended_at", None) is None):
        return "running"
    if status in {"triage", "todo", "scheduled", "ready", "review"}:
        return "active"
    if status == "archived":
        return "blocked"
    return "active"


def _foreman_source_task_from_request(text: str) -> dict[str, str]:
    source: dict[str, str] = {}
    mapping = {
        "board": "source_board",
        "task": "source_task_id",
    }
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip().lstrip("- ").strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        target = mapping.get(key.strip().casefold())
        if target and value.strip():
            source[target] = value.strip()[:300]
    return source


def _worker_source_task_context(worker: dict[str, Any]) -> dict[str, str]:
    context = {
        "source_board": str(worker.get("source_board") or "").strip(),
        "source_task_id": str(worker.get("source_task_id") or "").strip(),
    }
    request_text = str(worker.get("root_goal") or worker.get("initial_request") or "")
    if (
        (not context["source_board"] or not context["source_task_id"])
        and _is_foreman_generated_request(request_text)
    ):
        parsed = _foreman_source_task_from_request(request_text)
        context["source_board"] = context["source_board"] or parsed.get("source_board", "")
        context["source_task_id"] = context["source_task_id"] or parsed.get("source_task_id", "")
    return {key: value for key, value in context.items() if value}


def _is_foreman_generated_worker(worker: dict[str, Any]) -> bool:
    request = str(
        worker.get("initial_request")
        or worker.get("root_goal")
        or worker.get("latest_planner_request")
        or ""
    )
    return _is_foreman_generated_request(request)


def _foreman_source_board_from_request(text: str) -> str:
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip().lstrip("- ").strip()
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().casefold() == "board":
            return value.strip()[:120]
    return ""


def _active_foreman_source_boards() -> set[str]:
    sources: set[str] = set()
    for board_meta in kanban_db.list_boards(include_archived=False):
        board = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = _read_worker_meta(board)
        if worker.get("kind") != "discord_worker_board" or not _is_foreman_generated_worker(worker):
            continue
        if worker.get("cancelled") or str(worker.get("goal_status") or "").strip().lower() in TERMINAL_GOAL_STATUSES:
            continue
        source = _foreman_source_board_from_request(
            str(worker.get("initial_request") or worker.get("root_goal") or "")
        )
        if source:
            sources.add(source)
    return sources


def _latest_task_summaries(tasks: list[Any], summaries: dict[str, str]) -> list[str]:
    ordered = sorted(
        tasks,
        key=lambda task: (
            getattr(task, "completed_at", None)
            or getattr(task, "started_at", None)
            or getattr(task, "created_at", 0)
            or 0
        ),
        reverse=True,
    )
    out: list[str] = []
    for task in ordered:
        summary = _clean_feature_summary_text(
            summaries.get(getattr(task, "id", "")),
            max_chars=220,
            default="",
        )
        if summary:
            out.append(summary)
        if len(out) >= 2:
            break
    return out


def _feature_summary_outcome(
    worker: dict[str, Any],
    *,
    state: str,
    tasks: list[Any],
    summaries: dict[str, str],
    counts: dict[str, Any],
    running: list[dict[str, Any]],
) -> str:
    stored = _clean_feature_summary_text(
        worker.get("concise_outcome"),
        max_chars=420,
        default="",
    )
    if stored:
        return stored

    latest = _latest_task_summaries(tasks, summaries)
    blocked_tasks = [task for task in tasks if getattr(task, "status", "") == "blocked"]
    blocker = _clean_feature_summary_text(
        worker.get("blocked_reason"),
        max_chars=320,
        default="",
    )
    if not blocker and blocked_tasks:
        task = blocked_tasks[0]
        blocker = _clean_feature_summary_text(
            getattr(task, "last_failure_error", None)
            or summaries.get(getattr(task, "id", ""))
            or getattr(task, "title", ""),
            max_chars=320,
            default="",
        )

    if state == "errored":
        return _clean_feature_summary_text(
            f"Failed. {blocker}" if blocker else "Failed. A Kanban worker hit an unrecoverable error.",
            max_chars=420,
        )
    if state == "blocked":
        return _clean_feature_summary_text(
            f"Blocked. {blocker}" if blocker else "Blocked. Kanban is waiting on input or a failed ticket.",
            max_chars=420,
        )
    if state == "done":
        detail = " ".join(latest)
        return _clean_feature_summary_text(
            f"Done. {detail}" if detail else "Done. Kanban work completed.",
            max_chars=420,
        )

    running_text = _running_status_text(running)
    if running and running_text != "idle":
        return _clean_feature_summary_text(
            f"In progress. {running_text}",
            max_chars=420,
        )
    if latest:
        return _clean_feature_summary_text(
            "In progress. " + " ".join(latest),
            max_chars=420,
        )

    queued = int(counts.get("ready") or 0) + int(counts.get("todo") or 0) + int(counts.get("review") or 0)
    if queued:
        return f"In progress. {queued} Kanban ticket{'s' if queued != 1 else ''} queued."
    if int(counts.get("done") or 0):
        return "In progress. Completed tickets are awaiting final board reconciliation."
    return "In progress. Kanban pipeline is preparing work."


def _status_counts(counts: dict[str, Any]) -> dict[str, int]:
    out: dict[str, int] = {}
    for status in PUBLIC_BOARD_COLUMNS:
        out[status] = int(counts.get(status) or 0)
    for key, value in counts.items():
        if key not in out:
            out[str(key)] = int(value or 0)
    out["total"] = sum(value for key, value in out.items() if key != "total")
    return out


def _count_values(values: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        label = str(value or "unknown").strip().lower() or "unknown"
        counts[label] = counts.get(label, 0) + 1
    return counts


def _clean_summary_value(value: Any, *, default: str = "unknown") -> str:
    text = str(value or "").strip()
    return text if text else default


def _task_sort_timestamp(task: Any) -> int:
    return int(
        getattr(task, "completed_at", None)
        or getattr(task, "started_at", None)
        or getattr(task, "created_at", None)
        or 0
    )


def _run_sort_timestamp(run: Any) -> int:
    return int(
        getattr(run, "ended_at", None)
        or getattr(run, "started_at", None)
        or 0
    )


def _final_reviewer_verdict(tasks: list[Any], runs_by_task: dict[str, list[Any]]) -> dict[str, str]:
    reviewer_tasks = [
        task for task in tasks
        if str(getattr(task, "assignee", "") or "").strip().lower() == ROLE_REVIEWER
    ]
    for task in sorted(reviewer_tasks, key=_task_sort_timestamp, reverse=True):
        for run in sorted(runs_by_task.get(getattr(task, "id", ""), []), key=_run_sort_timestamp, reverse=True):
            metadata = getattr(run, "metadata", None) if isinstance(getattr(run, "metadata", None), dict) else {}
            raw = metadata.get("raw") if isinstance(metadata.get("raw"), dict) else {}
            status = str(raw.get("status") or "").strip().lower()
            if status:
                return {
                    "status": status,
                    "task_id": str(getattr(task, "id", "") or ""),
                    "run_id": str(getattr(run, "id", "") or ""),
                    "summary": str(getattr(run, "summary", "") or ""),
                }
            summary = str(getattr(run, "summary", "") or "").strip()
            if summary:
                return {
                    "status": "unknown",
                    "task_id": str(getattr(task, "id", "") or ""),
                    "run_id": str(getattr(run, "id", "") or ""),
                    "summary": summary,
                }
    return {"status": "unknown", "task_id": "", "run_id": "", "summary": ""}


def _verification_commands(tasks: list[Any], runs_by_task: dict[str, list[Any]]) -> list[dict[str, Any]]:
    commands: list[dict[str, Any]] = []
    task_by_id = {str(getattr(task, "id", "") or ""): task for task in tasks}
    for task_id, runs in runs_by_task.items():
        task = task_by_id.get(task_id)
        assignee = str(getattr(task, "assignee", "") or "").strip().lower() if task else ""
        if assignee != ROLE_DEV:
            continue
        for run in sorted(runs, key=_run_sort_timestamp, reverse=True):
            metadata = getattr(run, "metadata", None) if isinstance(getattr(run, "metadata", None), dict) else {}
            tests = metadata.get("tests")
            if not isinstance(tests, list):
                raw = metadata.get("raw") if isinstance(metadata.get("raw"), dict) else {}
                tests = raw.get("tests") if isinstance(raw.get("tests"), list) else []
            for item in tests:
                if not isinstance(item, dict):
                    continue
                command = str(item.get("command") or "").strip()
                if not command:
                    continue
                commands.append(
                    {
                        "task_id": task_id,
                        "task_title": str(getattr(task, "title", "") or "") if task else "",
                        "run_id": getattr(run, "id", None),
                        "command": command,
                        "result": str(item.get("result") or "unknown").strip() or "unknown",
                        "output": _cap_state_value(str(item.get("output") or ""), max_text=4000),
                    }
                )
                if len(commands) >= 20:
                    return commands
    return commands


def _latest_failure_or_blocker(worker: dict[str, Any], tasks: list[Any], runs_by_task: dict[str, list[Any]]) -> str:
    reason = str(worker.get("blocked_reason") or "").strip()
    if reason:
        return reason
    blocked = [task for task in tasks if str(getattr(task, "status", "") or "") == "blocked"]
    for task in sorted(blocked, key=_task_sort_timestamp, reverse=True):
        error = str(getattr(task, "last_failure_error", "") or "").strip()
        if error:
            return error
        for run in sorted(runs_by_task.get(getattr(task, "id", ""), []), key=_run_sort_timestamp, reverse=True):
            run_error = str(getattr(run, "error", "") or "").strip()
            if run_error:
                return run_error
            summary = str(getattr(run, "summary", "") or "").strip()
            if summary:
                return summary
    pr_blocker = str(worker.get("pr_blocker") or worker.get("pr_error") or "").strip()
    return pr_blocker or ""


def _summary_timestamps(worker: dict[str, Any], tasks: list[Any], runs: list[Any]) -> tuple[Optional[int], Optional[int], Optional[int]]:
    starts = [worker.get("created_at")]
    starts.extend(getattr(task, "created_at", None) for task in tasks)
    starts.extend(getattr(run, "started_at", None) for run in runs)
    started = min((int(value) for value in starts if value), default=None)
    terminal = str(worker.get("goal_status") or "").strip().lower() in TERMINAL_GOAL_STATUSES
    completed = int(worker.get("updated_at") or _now()) if terminal else None
    duration = (completed - started) if started and completed and completed >= started else None
    return started, completed, duration


def _run_phase_name(task: Any, run: Any) -> str:
    assignee = str(getattr(task, "assignee", "") or "").strip().lower()
    unit = str(getattr(run, "worker_unit", "") or "").strip().lower()
    step = str(getattr(run, "step_key", "") or "").strip().lower()
    joined = " ".join(part for part in (assignee, unit, step) if part)
    if assignee == ROLE_PLANNER or "planner" in joined or "plan" in joined:
        return "Plan"
    if assignee == ROLE_REVIEWER or "reviewer" in joined or "review" in joined:
        return "Review"
    if "ci" in joined or "check" in joined or "test" in joined:
        return "CI"
    if "deploy" in joined or "release" in joined:
        return "Deploy"
    if assignee == ROLE_DEV or "dev" in joined or "build" in joined:
        return "Build"
    return "Work"


def _board_runtime_breakdown(
    worker: dict[str, Any],
    tasks: list[Any],
    runs_by_task: dict[str, list[Any]],
) -> dict[str, Any]:
    runs = [run for run_list in runs_by_task.values() for run in run_list]
    started_at, completed_at, duration_seconds = _summary_timestamps(worker, tasks, runs)
    if started_at and completed_at is None:
        duration_seconds = max(0, _now() - int(started_at))
    task_by_id = {str(getattr(task, "id", "") or ""): task for task in tasks}
    phases: dict[str, dict[str, Any]] = {}
    active = 0.0
    now = _now()
    for run in runs:
        start = getattr(run, "started_at", None)
        if start is None:
            continue
        try:
            start_i = int(start)
        except (TypeError, ValueError):
            continue
        end_raw = getattr(run, "ended_at", None)
        try:
            end_i = int(end_raw) if end_raw is not None else now
        except (TypeError, ValueError):
            end_i = now
        if end_i < start_i:
            continue
        duration = float(end_i - start_i)
        task = task_by_id.get(str(getattr(run, "task_id", "") or ""))
        phase = _run_phase_name(task, run)
        slot = phases.setdefault(phase, {"name": phase, "duration_s": 0.0, "count": 0})
        slot["duration_s"] += duration
        slot["count"] += 1
        active += duration
    wall = float(duration_seconds or 0)
    queue = max(0.0, wall - active) if wall else 0.0
    rows = sorted(phases.values(), key=lambda item: item["duration_s"], reverse=True)
    if queue:
        rows.append({"name": "Queue", "duration_s": queue, "count": 0})
    return {
        "schema_version": 1,
        "scope": "discord_worker_board",
        "wall_s": wall,
        "model_s": 0.0,
        "tools_s": active,
        "overhead_s": queue,
        "active_s": active,
        "api_calls": 0,
        "tool_calls": len(runs),
        "phases": rows[:6],
        "active_exceeds_wall": bool(wall and active > wall + 0.25),
    }


def _pr_summary(worker: dict[str, Any]) -> dict[str, Any]:
    checks_status = str(worker.get("pr_checks_status") or "").strip() or "not checked"
    merge_state = str(worker.get("pr_merge_state") or "").strip() or "unknown"
    return {
        "url": str(worker.get("pr_url") or "").strip(),
        "number": str(worker.get("pr_number") or "").strip(),
        "error": str(worker.get("pr_error") or "").strip(),
        "status_error": str(worker.get("pr_status_error") or "").strip(),
        "state": str(worker.get("pr_state") or "").strip() or "unknown",
        "merged_at": str(worker.get("pr_merged_at") or "").strip(),
        "merge_commit": str(worker.get("pr_merge_commit") or "").strip(),
        "merge_state": merge_state,
        "mergeable": worker.get("pr_mergeable") if worker.get("pr_mergeable") is not None else "unknown",
        "is_draft": worker.get("pr_is_draft") if worker.get("pr_is_draft") is not None else "unknown",
        "review_decision": str(worker.get("pr_review_decision") or "").strip() or "unknown",
        "checks_status": checks_status,
        "checks_total": int(worker.get("pr_checks_total") or 0),
        "checks_failed": list(worker.get("pr_checks_failed") or []),
        "blocker": str(worker.get("pr_blocker") or "").strip(),
    }


def build_board_run_summary(board: str) -> dict[str, Any]:
    """Build a deterministic summary of a Discord worker board's terminal state."""
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    if worker.get("kind") != "discord_worker_board":
        raise KeyError("unknown Discord worker board")

    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        counts = _status_counts(kanban_db.board_stats(conn).get("by_status", {}))
        summaries = kanban_db.latest_summaries(conn, [t.id for t in tasks])
        runs_by_task = {t.id: kanban_db.list_runs(conn, t.id) for t in tasks}
    finally:
        conn.close()

    runs = [run for run_list in runs_by_task.values() for run in run_list]
    started_at, completed_at, duration_seconds = _summary_timestamps(worker, tasks, runs)
    runtime_breakdown = _board_runtime_breakdown(worker, tasks, runs_by_task)
    task_rows = [
        {
            "id": str(getattr(task, "id", "") or ""),
            "title": str(getattr(task, "title", "") or ""),
            "assignee": str(getattr(task, "assignee", "") or "unassigned"),
            "status": str(getattr(task, "status", "") or "unknown"),
            "last_failure_error": str(getattr(task, "last_failure_error", "") or ""),
            "latest_summary": str(summaries.get(getattr(task, "id", "")) or ""),
        }
        for task in sorted(tasks, key=_task_sort_timestamp, reverse=True)[:20]
    ]
    blocker = _latest_failure_or_blocker(worker, tasks, runs_by_task)
    summary = {
        "schema_version": 1,
        "board": board,
        "generated_at": _now(),
        "thread_id": str(worker.get("thread_id") or ""),
        "chat_id": str(worker.get("chat_id") or worker.get("thread_id") or ""),
        "title": _worker_generated_title(worker) or _fallback_feature_title(worker),
        "root_goal": str(worker.get("root_goal") or worker.get("initial_request") or ""),
        "goal_status": _clean_summary_value(worker.get("goal_status")),
        "phase": _clean_summary_value(worker.get("phase")),
        "thread_state": board_thread_state(board),
        "blocked_reason": blocker,
        "task_counts": counts,
        "run_counts": {
            "total": len(runs),
            "by_status": _count_values([getattr(run, "status", None) for run in runs]),
            "by_outcome": _count_values([getattr(run, "outcome", None) for run in runs]),
        },
        "review": {
            "loop_count": int(worker.get("review_loop_count") or 0),
            "loop_limit": _review_loop_limit_for_worker(worker),
            "final_verdict": _final_reviewer_verdict(tasks, runs_by_task),
        },
        "pr": _pr_summary(worker),
        "deployment_status": str(worker.get("deployment_status") or "").strip() or "not checked",
        "verification_commands": _verification_commands(tasks, runs_by_task),
        "final_response": {
            "text": str(worker.get("final_discord_response") or ""),
            "recorded_at": worker.get("final_discord_response_at"),
            "session_id": str(worker.get("final_discord_session_id") or ""),
            "work_item_id": str(worker.get("final_discord_work_item_id") or ""),
            "message_id": str(worker.get("final_discord_message_id") or ""),
        },
        "branch": str(worker.get("worker_branch") or "").strip(),
        "public_url": str(worker.get("public_url") or "").strip(),
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": duration_seconds,
        "runtime_breakdown": runtime_breakdown,
        "latest_tasks": task_rows,
    }
    return summary


def persist_board_run_summary(board: str) -> dict[str, Any]:
    """Persist and index the deterministic terminal summary for a board."""
    summary = build_board_run_summary(board)
    path = board_run_summary_path(board)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_json_write(path, summary, indent=2)

    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    if worker.get("kind") == "discord_worker_board":
        worker["board_summary"] = summary
        worker["board_summary_path"] = str(path)
        worker["board_summary_updated_at"] = summary["generated_at"]
        metadata[DISCORD_WORKER_META_KEY] = worker
        metadata.pop("db_path", None)
        atomic_json_write(kanban_db.board_metadata_path(board), metadata, indent=2)
    return summary


def record_final_discord_response(
    board: str,
    *,
    final_response: str,
    session_id: Optional[str] = None,
    work_item_id: Optional[str] = None,
    result_message_id: Optional[str] = None,
) -> None:
    """Persist final Discord-response provenance on a worker board."""
    if not board:
        return
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    if worker.get("kind") != "discord_worker_board":
        return
    worker["final_discord_response"] = _cap_state_value(str(final_response or ""), max_text=12000)
    worker["final_discord_response_at"] = _now()
    if session_id:
        worker["final_discord_session_id"] = str(session_id)
    if work_item_id:
        worker["final_discord_work_item_id"] = str(work_item_id)
    if result_message_id:
        worker["final_discord_message_id"] = str(result_message_id)
    worker["terminal_summary_sync_pending"] = True
    metadata[DISCORD_WORKER_META_KEY] = worker
    metadata.pop("db_path", None)
    atomic_json_write(kanban_db.board_metadata_path(board), metadata, indent=2)
    try:
        persist_board_run_summary(board)
    except Exception:
        logger.debug("Failed to refresh Discord board run summary for %s", board, exc_info=True)


def read_board_run_summary(board: str) -> dict[str, Any]:
    """Return the indexed persisted run summary, if the current run has one."""
    worker = _read_worker_meta(board)
    indexed_at = worker.get("board_summary_updated_at")
    if not indexed_at:
        return {}
    embedded = worker.get("board_summary") if isinstance(worker.get("board_summary"), dict) else {}
    if embedded and embedded.get("board") == board and embedded.get("generated_at") == indexed_at:
        return dict(embedded)
    path = Path(str(worker.get("board_summary_path") or board_run_summary_path(board)))
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(embedded) if embedded else {}
    if isinstance(loaded, dict) and loaded.get("board") == board:
        return loaded
    return {}


def board_run_summary_for_session(session_id: str) -> dict[str, Any]:
    board = resolve_public_session_board(session_id)
    return read_board_run_summary(board) or build_board_run_summary(board)


def _format_summary_counts(counts: dict[str, Any]) -> str:
    bits = [
        f"{status}:{counts.get(status) or 0}"
        for status in PUBLIC_BOARD_COLUMNS
        if int(counts.get(status) or 0)
    ]
    return " ".join(bits) or "none"


def _format_run_outcomes(run_counts: dict[str, Any]) -> str:
    outcomes = run_counts.get("by_outcome") if isinstance(run_counts.get("by_outcome"), dict) else {}
    bits = [f"{key}:{value}" for key, value in sorted(outcomes.items()) if value]
    return ", ".join(bits) or "none"


def render_board_run_summary_text(summary: dict[str, Any]) -> str:
    """Render deterministic terminal-board facts for Discord and diagnostics."""
    pr = summary.get("pr") if isinstance(summary.get("pr"), dict) else {}
    review = summary.get("review") if isinstance(summary.get("review"), dict) else {}
    verdict = review.get("final_verdict") if isinstance(review.get("final_verdict"), dict) else {}
    pr_ref = pr.get("url") or pr.get("error") or "not opened"
    checks = pr.get("checks_status") or "not checked"
    merge = pr.get("merge_state") or "unknown"
    merge_commit = pr.get("merge_commit") or ""
    lines = [
        f"Kanban goal: {summary.get('goal_status') or 'unknown'} / {summary.get('phase') or 'unknown'}",
        f"Board: {summary.get('public_url') or summary.get('board') or 'unknown'}",
        f"Branch: {summary.get('branch') or 'unknown'}",
        f"PR: {pr_ref}",
        f"PR merge: {merge}; checks: {checks}",
        f"Deployment: {summary.get('deployment_status') or 'not checked'}",
        f"Tasks: {_format_summary_counts(summary.get('task_counts') if isinstance(summary.get('task_counts'), dict) else {})}",
        f"Runs: total={(summary.get('run_counts') or {}).get('total', 0) if isinstance(summary.get('run_counts'), dict) else 0}; outcomes: {_format_run_outcomes(summary.get('run_counts') if isinstance(summary.get('run_counts'), dict) else {})}",
        f"Review: {review.get('loop_count') or 0}/{review.get('loop_limit') or 'unknown'}; final verdict: {verdict.get('status') or 'unknown'}",
    ]
    blocker = str(summary.get("blocked_reason") or pr.get("blocker") or "").strip()
    if blocker:
        lines.append(f"Blocker: {blocker}")
    if merge_commit:
        lines.append(f"Merge commit: {merge_commit}")
    commands = summary.get("verification_commands") if isinstance(summary.get("verification_commands"), list) else []
    if commands:
        rendered = "; ".join(
            f"{item.get('command')} [{item.get('result') or 'unknown'}]"
            for item in commands[:5]
            if isinstance(item, dict) and item.get("command")
        )
        if rendered:
            lines.append(f"Verification: {rendered}")
    started = _format_public_timestamp(summary.get("started_at")) or "unknown"
    completed = _format_public_timestamp(summary.get("completed_at")) or "unknown"
    duration = summary.get("duration_seconds")
    duration_text = f"{duration}s" if duration is not None else "unknown"
    lines.append(f"Timing: started {started}; completed {completed}; duration {duration_text}")
    runtime_text = render_runtime_breakdown_text(summary.get("runtime_breakdown"), compact=True)
    if runtime_text:
        lines.append(f"Runtime: {runtime_text.replace(chr(10), '; ')}")
    return "\n".join(lines)


def _terminal_summary_outcome(summary: dict[str, Any]) -> str:
    status = str(summary.get("goal_status") or summary.get("thread_state") or "unknown").strip()
    pr = summary.get("pr") if isinstance(summary.get("pr"), dict) else {}
    task_counts = summary.get("task_counts") if isinstance(summary.get("task_counts"), dict) else {}
    bits = [f"{status.title()}. Tasks: {_format_summary_counts(task_counts)}."]
    if pr.get("url"):
        bits.append(f"PR: {pr.get('url')}.")
    elif pr.get("error"):
        bits.append(f"PR blocked: {pr.get('error')}.")
    bits.append(f"Checks: {pr.get('checks_status') or 'not checked'}.")
    deployment = summary.get("deployment_status") or "not checked"
    bits.append(f"Deployment: {deployment}.")
    blocker = str(summary.get("blocked_reason") or pr.get("blocker") or "").strip()
    if blocker:
        bits.append(f"Blocker: {blocker}.")
    return _clean_feature_summary_text(" ".join(bits), max_chars=420)


def feature_summary_snapshot(board: str) -> dict[str, Any]:
    """Return the current feature-summary values for a Discord worker board."""
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    if worker.get("kind") != "discord_worker_board":
        raise KeyError("unknown Discord worker board")

    with kanban_db.connect_closing(board=board) as conn:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        summaries = kanban_db.latest_summaries(conn, [t.id for t in tasks])
        counts = kanban_db.board_stats(conn).get("by_status", {})
        running = _running_ticket_snapshot(conn)
        runs_by_task = {t.id: kanban_db.list_runs(conn, t.id) for t in tasks}

    state = board_thread_state(board)
    terminal_summary = read_board_run_summary(board)
    if state in {"done", "blocked", "errored"} and not terminal_summary:
        try:
            terminal_summary = persist_board_run_summary(board)
        except Exception:
            terminal_summary = {}
    title = _worker_generated_title(worker)
    outcome = _feature_summary_outcome(
        worker,
        state=state,
        tasks=tasks,
        summaries=summaries,
        counts=counts,
        running=running,
    )
    if terminal_summary:
        outcome = _terminal_summary_outcome(terminal_summary)
        runtime_breakdown = terminal_summary.get("runtime_breakdown") if isinstance(terminal_summary.get("runtime_breakdown"), dict) else {}
    else:
        runtime_breakdown = _board_runtime_breakdown(
            worker,
            tasks,
            runs_by_task,
        )
    snapshot = {
        "board": board,
        "thread_id": str(worker.get("thread_id") or ""),
        "chat_id": str(worker.get("chat_id") or worker.get("thread_id") or ""),
        "message_id": str(worker.get("summary_message_id") or "").strip(),
        "source_message_id": str(worker.get("source_message_id") or "").strip(),
        "guild_id": str(worker.get("guild_id") or "").strip(),
        "parent_channel_id": str(worker.get("parent_channel_id") or "").strip(),
        "state": state,
        "title": title,
        "fallback_title": _fallback_feature_title(worker),
        "outcome": outcome,
        "branch": str(worker.get("worker_branch") or "").strip(),
        "pr_url": str(worker.get("pr_url") or "").strip(),
        "pr_number": str(worker.get("pr_number") or "").strip(),
        "public_url": str(worker.get("public_url") or "").strip(),
        "runtime_breakdown": runtime_breakdown,
        "terminal_summary_updated_at": terminal_summary.get("generated_at") if terminal_summary else None,
        "updated_at": worker.get("updated_at"),
    }
    key_payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    snapshot["sync_key"] = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
    return snapshot


def thread_status_targets() -> list[dict[str, Any]]:
    """Return Discord thread targets with their current board state."""
    targets: list[dict[str, Any]] = []
    active_foreman_sources = _active_foreman_source_boards()
    try:
        from hermes_cli.discord_worker_foreman import active_master_foreman_source_boards

        active_foreman_sources.update(active_master_foreman_source_boards())
    except Exception:
        pass
    for board_meta in kanban_db.list_boards(include_archived=False):
        board = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = _read_worker_meta(board)
        if worker.get("kind") != "discord_worker_board":
            continue
        thread_id = str(worker.get("thread_id") or "").strip()
        if not thread_id:
            continue
        if _worker_source_message_too_old(worker):
            _clear_stale_terminal_sync_flags(board, worker)
            continue
        if _paused_corrupt_incident(board):
            logger.debug(
                "discord thread status targets: board %s paused for unchanged DB corruption; skipping",
                board,
            )
            continue
        try:
            summary = feature_summary_snapshot(board)
        except Exception as exc:
            if _is_skippable_board_db_error(exc):
                _log_skipped_board_target(board, exc, source="discord thread status feature summary")
                continue
            try:
                summary = {"state": board_thread_state(board)}
            except Exception as state_exc:
                if _is_skippable_board_db_error(state_exc):
                    _log_skipped_board_target(board, state_exc, source="discord thread status state fallback")
                    continue
                raise
        state = summary.get("state")
        if not state:
            try:
                state = board_thread_state(board)
            except Exception as exc:
                if _is_skippable_board_db_error(exc):
                    _log_skipped_board_target(board, exc, source="discord thread status board state")
                    continue
                raise
        source_context = _worker_source_task_context(worker)
        source_state = None
        if source_context.get("source_board") and source_context.get("source_task_id"):
            source_board = source_context["source_board"]
            try:
                source_state = source_task_reaction_state(
                    source_board,
                    source_context["source_task_id"],
                )
            except Exception as exc:
                if _is_skippable_board_db_error(exc):
                    _log_skipped_board_target(source_board, exc, source="discord thread status source task")
                    continue
                raise
        visible_state = state if state in {"done", "blocked", "errored"} else source_state or state
        terminal_completion_message_pending = bool(worker.get("terminal_completion_message_pending"))
        terminal_sync_pending = bool(
            worker.get("terminal_reaction_sync_pending")
            or worker.get("terminal_summary_sync_pending")
            or terminal_completion_message_pending
        )
        if (
            visible_state not in {"active", "running"}
            and not source_state
            and not (state in {"done", "blocked", "errored"} and terminal_sync_pending)
            and board not in active_foreman_sources
        ):
            continue
        if source_state and state not in {"done", "blocked", "errored"}:
            reaction_state = source_state
        else:
            try:
                reaction_state = board_thread_reaction_state(board)
            except Exception as exc:
                if _is_skippable_board_db_error(exc):
                    _log_skipped_board_target(board, exc, source="discord thread status reaction state")
                    continue
                raise
        target = {
            "board": board,
            "thread_id": thread_id,
            "chat_id": str(worker.get("chat_id") or thread_id),
            "message_id": summary.get("message_id") or str(worker.get("summary_message_id") or ""),
            "source_message_id": summary.get("source_message_id") or str(worker.get("source_message_id") or ""),
            "guild_id": summary.get("guild_id") or str(worker.get("guild_id") or ""),
            "parent_channel_id": summary.get("parent_channel_id") or str(worker.get("parent_channel_id") or ""),
            "state": visible_state,
            "title": summary.get("title") or "",
            "fallback_title": summary.get("fallback_title") or "",
            "outcome": summary.get("outcome") or "",
            "branch": summary.get("branch") or "",
            "pr_url": summary.get("pr_url") or "",
            "pr_number": summary.get("pr_number") or "",
            "public_url": summary.get("public_url") or "",
            "runtime_breakdown": summary.get("runtime_breakdown") or {},
            "sync_key": summary.get("sync_key") or "",
            "terminal_reaction_sync_pending": bool(worker.get("terminal_reaction_sync_pending")),
            "terminal_summary_sync_pending": bool(worker.get("terminal_summary_sync_pending")),
            "terminal_completion_message_pending": terminal_completion_message_pending,
        }
        if source_context:
            target.update(source_context)
        if source_state:
            target["source_state"] = source_state
        if reaction_state != visible_state:
            target["reaction_state"] = reaction_state
        if board in active_foreman_sources and not source_state:
            target["reaction_state"] = "foreman"
        if _is_foreman_generated_worker(worker):
            target["hide_source_links"] = True
            target["foreman_generated"] = True
        targets.append(target)
    return targets


def mark_thread_status_synced(
    board: str,
    *,
    reaction: bool = False,
    summary: bool = False,
    completion_message: bool = False,
) -> None:
    """Clear one-shot terminal Discord thread sync flags for a board."""
    if not board or not (reaction or summary or completion_message):
        return
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    if worker.get("kind") != "discord_worker_board":
        return
    changed = False
    if reaction and worker.pop("terminal_reaction_sync_pending", None) is not None:
        changed = True
    if summary and worker.pop("terminal_summary_sync_pending", None) is not None:
        changed = True
    if completion_message and worker.pop("terminal_completion_message_pending", None) is not None:
        changed = True
    if not changed:
        return
    worker["updated_at"] = _now()
    metadata[DISCORD_WORKER_META_KEY] = worker
    metadata.pop("db_path", None)
    atomic_json_write(kanban_db.board_metadata_path(board), metadata, indent=2)


def _board_runtime_snapshot(
    worker: dict[str, Any],
    *,
    counts: dict[str, Any],
    running: list[dict[str, Any]],
    conn: Any,
) -> dict[str, Any]:
    queued_ready = int(counts.get("ready") or 0)
    queued_todo = int(counts.get("todo") or 0)
    queued_review = int(counts.get("review") or 0)
    queued_total = queued_ready + queued_todo + queued_review
    running_count = len(running)

    if worker.get("cancelled") or worker.get("goal_status") == "cancelled":
        state = "cancelled"
        reason = "cancelled"
    elif running_count > 0:
        state = "running"
        reason = _running_status_text(running)
    elif (
        str(worker.get("blocked_reason") or "").strip()
        or worker.get("goal_status") == "blocked"
        or int(counts.get("blocked") or 0) > 0
    ):
        state = "blocked"
        reason = _blocked_runtime_reason(worker, conn=conn)
    elif int(counts.get("running") or 0) > 0:
        state = "stalled"
        reason = "running ticket has no live worker"
    elif (
        worker.get("paused")
        or worker.get("goal_status") == "paused"
        or worker.get("phase") == "paused"
    ):
        state = "paused"
        reason = str(worker.get("paused_reason") or "queue paused")
    elif worker.get("goal_status") == "done" or worker.get("phase") == "complete":
        state = "done"
        reason = "complete"
    elif queued_total > 0:
        state = "queued"
        reason = _queue_reason(worker, counts=counts, running_count=running_count, conn=conn)
    elif int(counts.get("done") or 0) > 0 and not any(
        int(counts.get(status) or 0)
        for status in ("triage", "todo", "ready", "running", "blocked", "review")
    ):
        state = "done"
        reason = "all tickets done"
    else:
        state = "idle"
        reason = "no active tickets"

    if state == "paused":
        control = "resume"
        control_label = "Resume"
    elif state == "running":
        control = "pause"
        control_label = "Pause"
    elif state == "queued":
        control = "pause"
        control_label = "Pause Queue"
    elif state == "blocked" and _is_review_loop_limit_blocker(worker):
        control = "continue"
        control_label = f"Continue (+{REVIEW_LOOP_CONTINUE_EXTRA_LOOPS} loops)"
    elif state in {"idle"} and worker.get("goal_status") in {"unset", None, ""}:
        control = "start"
        control_label = "Start"
    else:
        control = "none"
        control_label = ""

    return {
        "state": state,
        "reason": reason,
        "running_count": running_count,
        "queued_count": queued_total,
        "control": control,
        "control_label": control_label,
    }


def _blocked_runtime_reason(worker: dict[str, Any], *, conn: Any) -> str:
    reason = _public_runtime_reason(
        worker.get("blocked_reason"),
        max_chars=240,
    )
    if reason:
        return reason
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    except Exception:
        tasks = []
    for task in tasks:
        if getattr(task, "status", "") != "blocked":
            continue
        detail = _public_runtime_reason(
            getattr(task, "last_failure_error", None) or getattr(task, "title", ""),
            max_chars=240,
        )
        return detail or "blocked ticket needs attention"
    return "blocked"


def _public_runtime_reason(value: Any, *, max_chars: int = 240) -> str:
    text = _clean_feature_summary_text(value, max_chars=max_chars, default="")
    return str(_redact_public_state(text) or "").strip() if text else ""


def _runtime_action_form_html(
    session_id: str,
    runtime: dict[str, Any],
    *,
    return_to: str = "",
) -> str:
    control = str(runtime.get("control") or "none")
    control_label = str(runtime.get("control_label") or "")
    if control in {"resume", "start"}:
        action = "start"
        label = control_label or ("Resume" if control == "resume" else "Start")
    elif control == "continue":
        action = "continue"
        label = control_label or "Continue"
    elif control == "pause":
        action = "pause"
        label = control_label or "Pause"
    else:
        return ""

    action_url = f"/workers/{quote(session_id, safe='')}/{action}"
    if return_to:
        action_url = f"{action_url}?return_to={quote(return_to, safe='/')}"
    return (
        f'<form method="post" action="{html.escape(action_url)}">'
        f'<button type="submit">{html.escape(label)}</button></form>'
    )


def _queue_reason(
    worker: dict[str, Any],
    *,
    counts: dict[str, Any],
    running_count: int,
    conn: Any,
) -> str:
    if worker.get("paused") or worker.get("goal_status") == "paused" or worker.get("phase") == "paused":
        return "queue paused"
    if worker.get("cancelled") or worker.get("goal_status") == "cancelled":
        return "cancelled"
    if worker.get("execution_mode") != "kanban_pipeline" or worker.get("goal_status") != "active":
        return "board is not active"
    if int(counts.get("ready") or 0) <= 0 and int(counts.get("review") or 0) <= 0:
        if int(counts.get("todo") or 0) > 0:
            return "waiting on dependencies"
        return "waiting for dispatchable work"

    cfg = _kanban_config()
    if cfg.get("dispatch_in_gateway") is False:
        return "dispatcher disabled in config"

    worker_cfg = _worker_config()
    try:
        max_per_board = int(worker_cfg.get("max_workers_per_board") or 2)
    except (TypeError, ValueError):
        max_per_board = 2
    try:
        max_dev_per_board = int(worker_cfg.get("max_dev_workers_per_board") or 1)
    except (TypeError, ValueError):
        max_dev_per_board = 1
    try:
        max_global = int(worker_cfg.get("max_global_workers") or 8)
    except (TypeError, ValueError):
        max_global = 8
    if max_per_board > 0 and running_count >= max_per_board:
        return "board worker limit reached"
    if max_dev_per_board > 0:
        try:
            running_dev = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status = 'running' AND lower(assignee) = ?",
                (ROLE_DEV,),
            ).fetchone()[0]
        except Exception:
            running_dev = 0
        if int(running_dev or 0) >= max_dev_per_board:
            return "dev worker limit reached"
        if max_dev_per_board > 1 and _shared_dev_workspace_limited(conn):
            return "shared worktree dev worker limit reached"
    if max_global > 0:
        try:
            if _active_role_count_across_boards() >= max_global:
                return "global worker limit reached"
        except Exception:
            pass

    if not kanban_db.has_spawnable_ready(conn, additional_spawnable_assignees=ROLE_ASSIGNEES):
        return "ready tickets are assigned to non-spawnable lanes"
    return "awaiting next dispatcher tick"


def _shared_dev_workspace_limited(conn: Any) -> bool:
    try:
        rows = conn.execute(
            "SELECT workspace_path FROM tasks "
            "WHERE status IN ('ready', 'running') AND lower(assignee) = ?",
            (ROLE_DEV,),
        ).fetchall()
    except Exception:
        return False
    paths = [str(row["workspace_path"] or "").strip() for row in rows]
    paths = [path for path in paths if path]
    return len(paths) > 1 and len(set(paths)) == 1


def _running_ticket_snapshot(conn: Any) -> list[dict[str, Any]]:
    running = [
        task for task in kanban_db.list_tasks(conn, include_archived=False)
        if task.status == "running"
    ]
    rows = []
    for task in running:
        run = kanban_db.active_run(conn, task.id)
        if task.worker_pid and not kanban_db._pid_alive(int(task.worker_pid)):
            continue
        if run is None and task.current_run_id is None and not task.worker_pid:
            continue
        rows.append(
            {
                "id": task.id,
                "title": task.title,
                "assignee": task.assignee,
                "worker_pid": task.worker_pid,
                "started_at": task.started_at,
                "last_heartbeat_at": task.last_heartbeat_at,
                "current_run_id": task.current_run_id,
                "run_id": getattr(run, "id", None),
                "run_started_at": getattr(run, "started_at", None),
            }
        )
        if len(rows) >= 5:
            break
    return rows


def _running_status_text(items: list[dict[str, Any]]) -> str:
    if not items:
        return "idle"
    parts = []
    for item in items:
        label = str(item.get("title") or item.get("id") or "ticket")
        assignee = str(item.get("assignee") or "").strip()
        pid = item.get("worker_pid")
        bits = [label]
        if assignee:
            bits.append(f"assignee={assignee}")
        if pid:
            bits.append(f"pid={pid}")
        heartbeat = _format_public_timestamp(item.get("last_heartbeat_at"))
        if heartbeat:
            bits.append(f"heartbeat={heartbeat}")
        parts.append(" (".join([bits[0], ", ".join(bits[1:]) + ")"]) if len(bits) > 1 else bits[0])
    if len(items) >= 5:
        parts.append("...")
    return "; ".join(parts)


def _current_run_state(task: Any, runs: list[Any]) -> Optional[dict[str, Any]]:
    current_run_id = getattr(task, "current_run_id", None)
    if current_run_id is not None:
        for run in runs:
            if getattr(run, "id", None) == current_run_id:
                return _run_state_dict(run)
    for run in reversed(runs):
        if getattr(run, "ended_at", None) is None or getattr(run, "status", "") == "running":
            return _run_state_dict(run)
    if runs:
        return _run_state_dict(runs[-1])
    return None


def _ticket_codex_state(task_id: str, *, board: str) -> dict[str, Any]:
    state = _read_codex_worker_state(task_id, board=board)
    if not state:
        return {
            "available": False,
            "message": "No Codex app-server internals captured for this ticket yet.",
        }
    state = dict(state)
    state["available"] = True
    return state


def render_public_board_html(token: str) -> str:
    snapshot = public_board_snapshot(token)
    return _render_public_board_html(snapshot)


def render_public_session_board_html(
    session_id: str,
    *,
    active_ticket_id: Optional[str] = None,
) -> str:
    snapshot = public_board_snapshot_for_session(session_id)
    return _render_public_board_html(snapshot, active_ticket_id=active_ticket_id)


def _workers_page_css() -> str:
    return """
    :root { color-scheme: light; --bg: #f7f7f5; --panel: #ffffff; --panel-soft: #fbfbfa; --line: #d7d7d2; --line-soft: #e6e6e2; --text: #1f2933; --muted: #52606d; --link: #1d4ed8; --code: #5965f2; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    header { border-bottom: 1px solid var(--line); background: var(--panel); padding: 24px 28px; }
    .brand { color: var(--link); display: inline-block; font-size: 13px; font-weight: 700; line-height: 1.05; text-decoration: none; }
    .brand:hover { text-decoration: underline; }
    .top-nav { align-items: flex-start; display: flex; gap: 18px; justify-content: space-between; }
    .back-link { color: var(--link); font-size: 14px; font-weight: 700; text-decoration: none; }
    .back-link:hover { text-decoration: underline; }
    .hero { margin-top: 14px; }
    h1 { font-size: 24px; line-height: 1.2; margin: 0 0 8px; text-transform: none; }
    .subtle { color: var(--muted); font-size: 14px; }
    main { padding: 20px; }
    a { color: var(--link); text-decoration: none; }
    a:hover { text-decoration: underline; }
    code { color: var(--code); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.9em; }
    p { color: var(--muted); font-size: 13px; margin: 8px 0 0; }
    .board-list { list-style: none; margin: 0; max-width: 900px; padding: 0; }
    .board-card, .column, .criteria { background: var(--panel); border: 1px solid var(--line-soft); border-radius: 6px; }
    .board-card { margin-bottom: 10px; padding: 12px; }
    .board-card-head { display: block; padding: 0; }
    .board-title { font-size: 16px; font-weight: 700; line-height: 1.25; text-decoration: none; }
    .board-meta, .meta { color: var(--muted); display: flex; flex-wrap: wrap; gap: 8px 12px; font-size: 14px; margin-top: 8px; }
    .chips { margin-top: 8px; }
    .chip { color: var(--muted); font-size: 13px; }
    .runtime { font-weight: 700; text-transform: uppercase; }
    .board-card-body { display: block; padding: 0; }
    .status-grid { color: var(--muted); font-size: 13px; margin-top: 8px; }
    .status-cell { display: inline; }
    .status-cell + .status-cell::before { content: " "; }
    .status-cell b { font-weight: 400; }
    .status-cell span::after { content: ":"; }
    .status-cell span { margin-right: 0; }
    .reason { color: var(--muted); font-size: 13px; margin-top: 8px; }
    .actions { margin-top: 10px; }
    form { display: inline; margin: 0; }
    button, .button-link { background: var(--text); border: 1px solid var(--text); border-radius: 6px; color: #ffffff; cursor: pointer; font: inherit; padding: 6px 10px; text-decoration: none; }
    button:hover, .button-link:hover { background: #374151; }
    .board { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
    .column { min-height: 190px; overflow: hidden; transition: border-color 120ms ease, box-shadow 120ms ease; }
    .column.column-drop { border-color: var(--code); box-shadow: 0 0 0 2px rgba(89, 101, 242, 0.16); }
    .column.column-disabled { opacity: 0.72; }
    .column h2 { align-items: center; border-bottom: 1px solid var(--line-soft); display: flex; font-size: 14px; justify-content: space-between; margin: 0; padding: 12px; text-transform: uppercase; }
    .column ul { list-style: none; margin: 0; min-height: 132px; padding: 10px; }
    .column li { background: var(--panel-soft); border: 1px solid var(--line-soft); border-radius: 6px; margin-bottom: 8px; padding: 10px; }
    .ticket-card { cursor: grab; touch-action: manipulation; }
    .ticket-card.dragging { opacity: 0.48; }
    .ticket-card:active { cursor: grabbing; }
    .ticket-shell { align-items: flex-start; display: grid; gap: 8px; grid-template-columns: minmax(0, 1fr) auto; }
    .ticket { appearance: none; background: transparent; border: 0; color: inherit; cursor: pointer; display: block; font: inherit; padding: 0; text-align: left; width: 100%; }
    .ticket:hover strong { color: var(--link); text-decoration: underline; }
    .ticket:focus-visible { outline: 2px solid var(--code); outline-offset: 3px; }
    .ticket strong { display: block; font-size: 14px; line-height: 1.25; }
    .ticket p { color: var(--muted); font-size: 13px; margin: 8px 0 0; }
    .ticket-console { align-items: center; background: #111827; border: 1px solid #374151; border-radius: 6px; color: #f9fafb; display: inline-flex; font: 700 12px/1 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; justify-content: center; min-height: 28px; min-width: 32px; padding: 6px 7px; text-decoration: none; }
    .ticket-console:hover { background: #1f2937; color: #ffffff; text-decoration: none; }
    .ticket-console:focus-visible { outline: 2px solid var(--code); outline-offset: 2px; }
    .move-error { background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; color: #991b1b; font-size: 13px; margin-bottom: 12px; padding: 10px 12px; }
    .move-error[hidden] { display: none; }
    .criteria { margin-bottom: 18px; padding: 14px; }
    .criteria strong { display: block; font-size: 14px; margin-bottom: 8px; }
    .criteria ol { margin: 0; padding-left: 20px; }
    .criteria li { color: var(--text); margin: 4px 0; }
    .board-summary pre { color: var(--text); font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; margin: 0; white-space: pre-wrap; word-break: break-word; }
    .modal { align-items: center; background: rgba(15, 23, 42, 0.54); display: none; inset: 0; justify-content: center; padding: 20px; position: fixed; z-index: 20; }
    .modal[aria-hidden="false"] { display: flex; }
    .modal-panel { background: #ffffff; border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 24px 60px rgba(15, 23, 42, 0.28); max-height: min(86vh, 900px); max-width: min(920px, 96vw); min-width: min(720px, 96vw); overflow: hidden; }
    .modal-head { align-items: center; border-bottom: 1px solid var(--line-soft); display: flex; gap: 16px; justify-content: space-between; padding: 14px 16px; }
    .modal-head h2 { font-size: 16px; margin: 0; }
    .modal-close { background: #f3f4f6; border: 1px solid var(--line); color: var(--text); }
    .modal-close:hover { background: #e5e7eb; }
    .modal-body { background: #111827; color: #f9fafb; font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; margin: 0; max-height: calc(min(86vh, 900px) - 58px); overflow: auto; padding: 14px; white-space: pre-wrap; word-break: break-word; }
    @media (max-width: 760px) { .modal-panel { min-width: min(100%, 96vw); } }
    """


def _render_public_board_html(
    snapshot: dict[str, Any],
    *,
    active_ticket_id: Optional[str] = None,
) -> str:
    worker = snapshot["worker"]
    tasks = snapshot["tasks"]
    runtime = snapshot.get("runtime") or {"state": "idle", "reason": "no active tickets"}
    session_id = str(snapshot.get("session_id") or worker.get("thread_id") or "")
    columns = list(PUBLIC_BOARD_COLUMNS)
    by_status = {status: [] for status in columns}
    for task in tasks:
        by_status.setdefault(task["status"], []).append(task)

    def esc(value: Any) -> str:
        return html.escape(str(value or ""))

    board_url = f"/workers/{quote(session_id, safe='')}"
    board_url_json = json.dumps(board_url).replace("</", "<\\/")
    active_ticket_json = json.dumps(str(active_ticket_id or "")).replace("</", "<\\/")

    def render_ticket_card(item: dict[str, Any], status: str) -> str:
        ticket_id = str(item["id"])
        quoted_ticket = quote(ticket_id, safe="")
        ticket_url = f"{board_url}/tickets/{quoted_ticket}"
        body_preview = str(item.get("body_preview") or "").strip()
        summary = str(item.get("latest_summary") or "").strip()
        brief_html = (
            f'<p class="ticket-brief"><b>Brief:</b> {esc(body_preview)}</p>'
            if body_preview else ""
        )
        summary_html = (
            f'<p class="ticket-summary"><b>Latest:</b> {esc(summary)}</p>'
            if summary else ""
        )
        return (
            "<li class=\"ticket-card\" draggable=\"true\" data-ticket-item "
            "data-ticket-id=\"{id}\" data-ticket-status=\"{status}\" "
            "data-ticket-move-url=\"{move_url}\">"
            "<div class=\"ticket-shell\">"
            "<button type=\"button\" class=\"ticket\" data-ticket-id=\"{id}\" "
            "data-ticket-title=\"{title}\" data-ticket-url=\"{ticket_url}\" "
            "data-ticket-state-url=\"{state_url}\" "
            "data-ticket-terminal-page-url=\"{terminal_page_url}\" "
            "data-ticket-terminal-url=\"{terminal_url}\">"
            "<strong>{title}</strong><br><code>{id}</code> {assignee}{brief}{summary}"
            "</button>"
            "<a class=\"ticket-console\" href=\"{console_url}\" "
            "data-ticket-console-url=\"{console_url}\" "
            "title=\"Open worker console\" aria-label=\"Open worker console for {title}\">"
            "<span aria-hidden=\"true\">&gt;_</span>"
            "</a></div></li>".format(
                title=esc(item["title"]),
                id=esc(ticket_id),
                status=esc(status),
                ticket_url=esc(ticket_url),
                state_url=esc(f"{ticket_url}/state"),
                terminal_page_url=esc(f"{ticket_url}/terminal"),
                terminal_url=esc(f"{ticket_url}/terminal.json"),
                console_url=esc(f"{ticket_url}/console"),
                move_url=esc(f"{ticket_url}/move"),
                assignee=esc(item["assignee"] or ""),
                brief=brief_html,
                summary=summary_html,
            )
        )

    cards = []
    for status in columns:
        items = by_status.get(status, [])
        body = "\n".join(render_ticket_card(item, status) for item in items)
        disabled = " column-disabled" if status == "running" else ""
        drop_disabled = "true" if status == "running" else "false"
        cards.append(
            f"<section class=\"column{disabled}\" data-column data-status=\"{esc(status)}\" "
            f"data-drop-disabled=\"{drop_disabled}\"><h2>{esc(status)} "
            f"<span data-column-count>{len(items)}</span></h2>"
            f"<ul data-ticket-list>{body}</ul></section>"
        )
    criteria = "\n".join(
        f"<li>{esc(c.get('text') if isinstance(c, dict) else c)}</li>"
        for c in (worker.get("criteria") or [])
        if (c.get("active", True) if isinstance(c, dict) else True)
    )
    discord_thread_url = str(worker.get("discord_thread_url") or "").strip()
    thread_display = str(worker.get("thread_id") or session_id)
    session_text = (
        f'<a href="{esc(discord_thread_url)}" target="_blank" rel="noopener noreferrer">'
        f"<code>{esc(thread_display)}</code></a>"
        if discord_thread_url
        else f"<code>{esc(thread_display)}</code>"
    )
    runtime_action = _runtime_action_form_html(
        session_id,
        runtime,
        return_to=board_url,
    )
    summary_html = _public_board_summary_html(snapshot.get("board_summary") or {})
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(snapshot["name"])}</title>
  <style>
{_workers_page_css()}
  </style>
</head>
<body>
  <header>
    <div class="top-nav">
      <a class="brand" href="/workers">Hermes<br>Kanban</a>
      <a class="back-link" href="/workers">Worker Boards</a>
    </div>
    <div class="hero">
      <div>
        <h1>{esc(snapshot["name"])}</h1>
        <div class="meta">
          <span>Discord: {session_text}</span>
          <span>Status: {esc(_public_status_text(worker))}</span>
          <span>Branch: {esc(worker.get("worker_branch") or "")}</span>
          <span>PR: {esc(worker.get("pr_url") or "not opened")}</span>
          <span>Review: {esc(_public_review_text(worker))}</span>
          <span>Updated: {esc(_format_public_timestamp(worker.get("updated_at")) or "never")}</span>
          <span>Runtime: <strong class="runtime runtime-{esc(runtime.get("state"))}">{esc(runtime.get("state"))}</strong></span>
          <span>{esc(runtime.get("reason"))}</span>
        </div>
        <div class="actions">{runtime_action}</div>
      </div>
    </div>
  </header>
  <main>
    {summary_html}
    <div class="criteria"><strong>Acceptance Criteria</strong><ol>{criteria}</ol></div>
    <div class="move-error" id="ticket-move-error" role="alert" hidden></div>
    <div class="board">{''.join(cards)}</div>
  </main>
  <div class="modal" id="ticket-modal" aria-hidden="true" role="dialog" aria-modal="true" aria-labelledby="ticket-modal-title">
    <div class="modal-panel">
      <div class="modal-head">
        <h2 id="ticket-modal-title">Ticket State</h2>
        <button class="modal-close" type="button" id="ticket-modal-close">Close</button>
      </div>
      <pre class="modal-body" id="ticket-modal-body">Loading...</pre>
    </div>
  </div>
  <script>
    (() => {{
      const modal = document.getElementById("ticket-modal");
      const title = document.getElementById("ticket-modal-title");
      const body = document.getElementById("ticket-modal-body");
      const close = document.getElementById("ticket-modal-close");
      const moveError = document.getElementById("ticket-move-error");
      let draggingItem = null;
      let touchState = null;
      let suppressClickUntil = 0;
      const boardUrl = {board_url_json};
      const initialTicketId = {active_ticket_json};
      const columns = Array.from(document.querySelectorAll("[data-column]"));
      const ticketUrlFor = (ticketId) => boardUrl + "/tickets/" + encodeURIComponent(ticketId || "");
      const stateUrlFor = (ticketId) => ticketUrlFor(ticketId) + "/state";
      const terminalPageUrlFor = (ticketId) => ticketUrlFor(ticketId) + "/terminal";
      const ticketIdFromPath = () => {{
        const prefix = boardUrl + "/tickets/";
        if (!window.location.pathname.startsWith(prefix)) return "";
        const encoded = window.location.pathname.slice(prefix.length).split("/")[0];
        if (!encoded) return "";
        try {{ return decodeURIComponent(encoded); }} catch (_error) {{ return encoded; }}
      }};
      const labelForTicket = (ticketId) => {{
        for (const button of document.querySelectorAll("[data-ticket-state-url]")) {{
          if (button.dataset.ticketId === ticketId) return button.dataset.ticketTitle || ticketId;
        }}
        return ticketId || "Ticket State";
      }};
      const setPageUrl = (url, replace) => {{
        if (!url || !window.history || typeof window.history.pushState !== "function") return;
        if (window.location.pathname === url && !window.location.search && !window.location.hash) return;
        const state = url === boardUrl ? {{}} : {{ ticketId: url.slice(url.lastIndexOf("/") + 1) }};
        if (replace && typeof window.history.replaceState === "function") window.history.replaceState(state, "", url);
        else window.history.pushState(state, "", url);
      }};
      const clearColumnDrops = () => {{
        columns.forEach((column) => column.classList.remove("column-drop"));
      }};
      const showMoveError = (message) => {{
        if (!moveError) return;
        moveError.textContent = message || "Move failed";
        moveError.hidden = false;
      }};
      const clearMoveError = () => {{
        if (!moveError) return;
        moveError.textContent = "";
        moveError.hidden = true;
      }};
      const updateCounts = () => {{
        columns.forEach((column) => {{
          const count = column.querySelector("[data-column-count]");
          const list = column.querySelector("[data-ticket-list]");
          if (count && list) count.textContent = String(list.querySelectorAll("[data-ticket-item]").length);
        }});
      }};
      const parseApiError = async (response) => {{
        try {{
          const payload = await response.json();
          return payload?.detail || `HTTP ${{response.status}}`;
        }} catch (_error) {{
          return `HTTP ${{response.status}}`;
        }}
      }};
      const moveTicket = async (item, targetColumn) => {{
        if (!item || !targetColumn) return;
        const status = targetColumn.dataset.status || "";
        if (targetColumn.dataset.dropDisabled === "true") {{
          showMoveError("Workers can only enter running through the dispatcher.");
          return;
        }}
        if (!status || item.dataset.ticketStatus === status) return;
        const targetList = targetColumn.querySelector("[data-ticket-list]");
        if (!targetList) return;

        clearMoveError();
        const originalParent = item.parentElement;
        const originalNext = item.nextSibling;
        const originalStatus = item.dataset.ticketStatus || "";
        targetList.appendChild(item);
        item.dataset.ticketStatus = status;
        updateCounts();
        try {{
          const response = await fetch(item.dataset.ticketMoveUrl, {{
            method: "POST",
            headers: {{
              "Accept": "application/json",
              "Content-Type": "application/json",
            }},
            body: JSON.stringify({{ status }}),
          }});
          if (!response.ok) {{
            throw new Error(await parseApiError(response));
          }}
          window.location.reload();
        }} catch (error) {{
          if (originalParent) originalParent.insertBefore(item, originalNext);
          item.dataset.ticketStatus = originalStatus;
          updateCounts();
          showMoveError(`Move failed: ${{error?.message || error}}`);
        }}
      }};
      const hide = (options = {{}}) => {{
        modal.setAttribute("aria-hidden", "true");
        body.textContent = "";
        if (options.updateUrl !== false) setPageUrl(boardUrl, options.replaceUrl === true);
      }};
      const show = (label) => {{
        title.textContent = label ? label + " - Details" : "Ticket Details";
        body.textContent = "Loading...";
        modal.setAttribute("aria-hidden", "false");
      }};
      const renderTicketState = (state, terminalPageUrl) => {{
        const task = state?.task || {{}};
        const workerRun = state?.worker_run || null;
        const currentRun = state?.current_run || null;
        const runs = Array.isArray(state?.runs) ? state.runs : [];
        const latestRun = runs.length ? runs[runs.length - 1] : null;
        const lines = [
          `$ ticket ${{task.id || ""}}`,
          `title: ${{task.title || ""}}`,
          `status: ${{task.status || "unknown"}}`,
          `assignee: ${{task.assignee || "unassigned"}}`,
          `terminal: ${{terminalPageUrl || ""}}`,
          "",
          "# brief",
          task.body || "(no ticket body provided)",
        ];
        if (workerRun?.summary) {{
          lines.splice(4, 0, `worker_run: ${{workerRun.summary}}`);
        }}
        if (currentRun) {{
          lines.push("", "# current run");
          lines.push(`run=${{currentRun.id || "-"}} status=${{currentRun.status || currentRun.outcome || "-"}}`);
        }}
        if (latestRun?.summary) {{
          lines.push("", "# latest worker summary", latestRun.summary);
        }}
        if (task.result) {{
          lines.push("", "# result", task.result);
        }}
        if (task.last_failure_error) {{
          lines.push("", "# last failure", task.last_failure_error);
        }}
        return lines.join("\\n");
      }};
      const loadTicketState = async (url, terminalPageUrl) => {{
        const response = await fetch(url, {{ headers: {{ "Accept": "application/json" }} }});
        if (!response.ok) {{
          throw new Error(`HTTP ${{response.status}}`);
        }}
        const state = await response.json();
        body.textContent = renderTicketState(state, terminalPageUrl);
        body.scrollTop = 0;
      }};
      const openTicket = async (ticketId, label, options = {{}}) => {{
        const cleanTicketId = ticketId || "";
        if (!cleanTicketId) return;
        const ticketUrl = options.ticketUrl || ticketUrlFor(cleanTicketId);
        show(label || labelForTicket(cleanTicketId));
        if (options.updateUrl !== false) setPageUrl(ticketUrl, options.replaceUrl === true);
        try {{
          await loadTicketState(
            options.stateUrl || stateUrlFor(cleanTicketId),
            options.terminalPageUrl || terminalPageUrlFor(cleanTicketId),
          );
        }} catch (error) {{
          body.textContent = `Unable to load ticket details: ${{error}}`;
        }}
      }};
      document.querySelectorAll("[data-ticket-item]").forEach((item) => {{
        item.addEventListener("dragstart", (event) => {{
          draggingItem = item;
          item.classList.add("dragging");
          if (event.dataTransfer) {{
            event.dataTransfer.effectAllowed = "move";
            event.dataTransfer.setData("text/plain", item.dataset.ticketId || "");
          }}
        }});
        item.addEventListener("dragend", () => {{
          item.classList.remove("dragging");
          draggingItem = null;
          clearColumnDrops();
        }});
        item.addEventListener("pointerdown", (event) => {{
          if (event.button !== 0) return;
          touchState = {{
            item,
            pointerId: event.pointerId,
            startX: event.clientX,
            startY: event.clientY,
            targetColumn: null,
            started: false,
          }};
          if (event.pointerType !== "mouse" && item.setPointerCapture) item.setPointerCapture(event.pointerId);
        }});
      }});
      columns.forEach((column) => {{
        column.addEventListener("dragover", (event) => {{
          if (!draggingItem || column.dataset.dropDisabled === "true") return;
          event.preventDefault();
          column.classList.add("column-drop");
          if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        }});
        column.addEventListener("dragleave", (event) => {{
          if (!column.contains(event.relatedTarget)) column.classList.remove("column-drop");
        }});
        column.addEventListener("drop", (event) => {{
          if (!draggingItem) return;
          event.preventDefault();
          clearColumnDrops();
          moveTicket(draggingItem, column);
        }});
      }});
      document.addEventListener("pointermove", (event) => {{
        if (!touchState || event.pointerId !== touchState.pointerId) return;
        const dx = Math.abs(event.clientX - touchState.startX);
        const dy = Math.abs(event.clientY - touchState.startY);
        if (!touchState.started && dx + dy < 10) return;
        touchState.started = true;
        draggingItem = touchState.item;
        touchState.item.classList.add("dragging");
        event.preventDefault();
        const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-column]");
        clearColumnDrops();
        if (target && target.dataset.dropDisabled !== "true") {{
          target.classList.add("column-drop");
          touchState.targetColumn = target;
        }} else {{
          touchState.targetColumn = null;
        }}
      }}, {{ passive: false }});
      document.addEventListener("pointerup", (event) => {{
        if (!touchState || event.pointerId !== touchState.pointerId) return;
        const state = touchState;
        touchState = null;
        clearColumnDrops();
        state.item.classList.remove("dragging");
        draggingItem = null;
        if (state.started) {{
          suppressClickUntil = Date.now() + 350;
          moveTicket(state.item, state.targetColumn);
        }}
      }});
      document.addEventListener("pointercancel", () => {{
        if (touchState?.item) touchState.item.classList.remove("dragging");
        touchState = null;
        draggingItem = null;
        clearColumnDrops();
      }});
      document.addEventListener("click", (event) => {{
        if (Date.now() < suppressClickUntil && event.target.closest("[data-ticket-item]")) {{
          event.preventDefault();
          event.stopPropagation();
        }}
      }}, true);
      document.querySelectorAll("[data-ticket-state-url]").forEach((button) => {{
        button.addEventListener("click", () => {{
          openTicket(
            button.dataset.ticketId || "",
            button.dataset.ticketTitle || button.dataset.ticketId || "Ticket State",
            {{
              ticketUrl: button.dataset.ticketUrl,
              stateUrl: button.dataset.ticketStateUrl,
              terminalPageUrl: button.dataset.ticketTerminalPageUrl,
            }},
          );
        }});
      }});
      close.addEventListener("click", hide);
      modal.addEventListener("click", (event) => {{
        if (event.target === modal) hide();
      }});
      document.addEventListener("keydown", (event) => {{
        if (event.key === "Escape") hide();
      }});
      window.addEventListener("popstate", () => {{
        const ticketId = ticketIdFromPath();
        if (ticketId) openTicket(ticketId, labelForTicket(ticketId), {{ updateUrl: false }});
        else hide({{ updateUrl: false }});
      }});
      const startupTicketId = initialTicketId || ticketIdFromPath();
      if (startupTicketId) openTicket(startupTicketId, labelForTicket(startupTicketId), {{ updateUrl: false }});
    }})();
  </script>
</body>
</html>"""


def public_board_index_snapshot() -> dict[str, Any]:
    def newest_sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
        worker = item.get("worker") or {}
        try:
            created_at = int(worker.get("created_at") or 0)
        except (TypeError, ValueError):
            created_at = 0
        try:
            session_id = int(item.get("session_id") or 0)
        except (TypeError, ValueError):
            session_id = 0
        return (created_at, session_id, str(item.get("board") or ""))

    boards = []
    for board in kanban_db.list_boards(include_archived=False):
        slug = str(board.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = _read_worker_meta(slug)
        if worker.get("kind") != "discord_worker_board":
            continue
        conn = kanban_db.connect(board=slug)
        try:
            counts = kanban_db.board_stats(conn).get("by_status", {})
            running = _running_ticket_snapshot(conn)
            runtime = _board_runtime_snapshot(
                worker,
                counts=counts,
                running=running,
                conn=conn,
            )
        finally:
            conn.close()
        session_id = _public_session_id_for_board(slug, worker)
        boards.append(
            {
                "board": slug,
                "name": _worker_board_name(worker, board, slug),
                "description": board.get("description") or "",
                "session_id": session_id,
                "public_url": public_session_board_url(session_id),
                "worker": _public_worker_meta(worker),
                "counts": counts,
                "running": running,
                "runtime": runtime,
            }
        )
    boards.sort(key=newest_sort_key, reverse=True)
    return {"boards": boards}


def render_public_board_index_html() -> str:
    snapshot = public_board_index_snapshot()

    def esc(value: Any) -> str:
        return html.escape(str(value or ""))

    items = []
    for board in snapshot["boards"]:
        worker = board.get("worker") or {}
        session_id = str(board.get("session_id") or "")
        href = f"/workers/{quote(session_id, safe='')}" if session_id else ""
        counts = board.get("counts") or {}
        running = board.get("running") if isinstance(board.get("running"), list) else []
        runtime = board.get("runtime") if isinstance(board.get("runtime"), dict) else {}
        count_text = _public_count_text(counts)
        running_text = _running_status_text(running)
        runtime_state = str(runtime.get("state") or "idle")
        runtime_reason = str(runtime.get("reason") or running_text)
        runtime_label = "Queue" if runtime_state == "queued" else "Status"
        title = esc(_worker_index_title(worker, board.get("name")))
        link = f'<a class="board-title" href="{esc(href)}">{title}</a>' if href else title
        discord_thread_url = str(worker.get("discord_thread_url") or "").strip()
        thread_display = str(worker.get("thread_id") or session_id)
        session_text = (
            f'<a href="{esc(discord_thread_url)}" target="_blank" rel="noopener noreferrer">'
            f"<code>{esc(thread_display)}</code></a>"
            if discord_thread_url
            else f"<code>{esc(thread_display)}</code>"
        )
        status = _public_status_text(worker)
        execution_mode = str(worker.get("execution_mode") or "").strip()
        branch = str(worker.get("worker_branch") or "").strip()
        pr_url = str(worker.get("pr_url") or "").strip()
        pr_number = str(worker.get("pr_number") or "").strip()
        if pr_url.startswith(("http://", "https://")):
            pr_label = f"#{pr_number}" if pr_number else pr_url
            pr_text = f'<a href="{esc(pr_url)}">{esc(pr_label)}</a>'
        else:
            pr_text = esc(pr_url or "not opened")
        created_at = _format_public_timestamp(worker.get("created_at"))
        updated_at = _format_public_timestamp(worker.get("updated_at"))
        timestamps = " ".join(
            bit for bit in (
                f"Created: {esc(created_at)}" if created_at else "",
                f"Updated: {esc(updated_at)}" if updated_at else "",
            )
            if bit
        )
        flags = []
        if worker.get("paused"):
            flags.append("paused")
        if worker.get("cancelled"):
            flags.append("cancelled")
        blocked_reason = str(worker.get("blocked_reason") or "").strip()
        if blocked_reason:
            flags.append(f"blocked: {blocked_reason}")
        flags_text = " ".join(flags)
        primary_action = _runtime_action_form_html(session_id, runtime)
        items.append(
            '<li class="board-card">'
            '<strong>{link}</strong><br>'
            '{session} {status}'
            '<p>Runtime: <strong class="runtime runtime-{runtime_class}">{runtime}</strong></p>'
            '<p>{counts}</p>'
            '<p>{reason_label}: {reason}</p>'
            '<p>Running: {running}</p>'
            '<p>{branch}{mode}{pr}{review}</p>'
            '<p>{timestamps}</p>'
            '{flags}'
            '<div class="actions">{action}</div>'
            '</li>'.format(
                link=link,
                session=session_text,
                status=esc(status),
                runtime=esc(runtime_state),
                runtime_class=esc(runtime_state),
                reason_label=esc(runtime_label),
                reason=esc(runtime_reason),
                counts=esc(count_text),
                running=esc(running_text),
                branch=f"Branch: {esc(branch)}" if branch else "Branch: pending",
                mode=f" Mode: {esc(execution_mode)}" if execution_mode else "",
                pr=f" PR: {pr_text}",
                review=f" Review: {esc(_public_review_text(worker))}",
                timestamps=timestamps,
                flags=f"<p>{esc(flags_text)}</p>" if flags_text else "",
                action=primary_action,
            )
        )
    body = "\n".join(items) or "<li>No public Discord Kanban boards yet.</li>"
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hermes Kanban</title>
  <style>
{_workers_page_css()}
  </style>
</head>
<body>
  <header>
    <a class="brand" href="/">Hermes<br>Kanban</a>
    <div class="hero">
      <h1>Worker Boards</h1>
      <div class="subtle">{len(snapshot["boards"])} public session boards</div>
    </div>
  </header>
  <main>
    <ul class="board-list">{body}</ul>
  </main>
</body>
</html>"""


def set_goal(
    *,
    thread_id: str,
    goal: str,
    chat_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    parent_channel_id: Optional[str] = None,
    project_context: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
    thread_context: Optional[str] = None,
    board_slug: Optional[str] = None,
) -> DiscordBoard:
    return start_planner_request(
        thread_id=thread_id,
        request=goal,
        chat_id=chat_id,
        guild_id=guild_id,
        parent_channel_id=parent_channel_id,
        project_context=project_context,
        request_id=request_id,
        thread_context=thread_context,
        board_slug=board_slug,
        created_by="discord-goal",
    )


def start_direct_goal(
    *,
    thread_id: str,
    goal: str,
    chat_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    parent_channel_id: Optional[str] = None,
    project_context: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
    board_slug: Optional[str] = None,
) -> DiscordBoard:
    """Activate a Discord worker board whose work items already exist."""
    raw_goal = str(goal or "").strip()
    board = ensure_discord_thread_board(
        thread_id=thread_id,
        chat_id=chat_id,
        guild_id=guild_id,
        parent_channel_id=parent_channel_id,
        initial_request=raw_goal,
        project_context=project_context,
        request_id=request_id,
        board_slug=board_slug,
    )
    worker = board.worker
    previous_goal = str(worker.get("root_goal") or worker.get("initial_request") or "")
    if _planner_request_fingerprint(previous_goal) != _planner_request_fingerprint(raw_goal):
        _clear_generated_summary_title(worker)
    _clear_board_run_summary(board.slug, worker)
    _clear_pr_summary_fields(worker)
    worker.update(
        {
            "root_goal": raw_goal,
            "goal_status": "active",
            "phase": "dev",
            "execution_mode": "kanban_pipeline",
            **pr_policy_for_request(raw_goal),
            "paused": False,
            "cancelled": False,
        }
    )
    metadata = _update_worker_meta(board.slug, worker)
    return DiscordBoard(slug=board.slug, metadata=metadata)


def start_planner_request(
    *,
    thread_id: str,
    request: str,
    chat_id: Optional[str] = None,
    guild_id: Optional[str] = None,
    parent_channel_id: Optional[str] = None,
    project_context: Optional[dict[str, Any]] = None,
    request_id: Optional[str] = None,
    thread_context: Optional[str] = None,
    created_by: str = "discord-feature-request",
    board_slug: Optional[str] = None,
    acceptance_criteria: Optional[list[Any]] = None,
) -> DiscordBoard:
    raw_request = _canonical_planner_request_text(request)
    board = ensure_discord_thread_board(
        thread_id=thread_id,
        chat_id=chat_id,
        guild_id=guild_id,
        parent_channel_id=parent_channel_id,
        initial_request=raw_request,
        project_context=project_context,
        request_id=request_id,
        board_slug=board_slug,
        source_message_id=request_id,
    )
    worker = board.worker
    previous_goal_status = str(worker.get("goal_status") or "").strip().lower()
    starts_new_goal_run = previous_goal_status in TERMINAL_GOAL_STATUSES
    previous_goal = str(
        worker.get("latest_planner_request")
        or worker.get("root_goal")
        or worker.get("initial_request")
        or ""
    )
    request_changed = _planner_request_fingerprint(previous_goal) != _planner_request_fingerprint(raw_request)
    planner_key = _planner_request_key(
        raw_request,
        request_id=request_id,
        include_request_id=starts_new_goal_run,
    )
    thread_context_text = _merge_expanded_discord_thread_context(
        raw_request,
        str(thread_context or ""),
    )
    if request_changed:
        _clear_generated_summary_title(worker)
    if starts_new_goal_run:
        _clear_board_run_summary(board.slug, worker)
        _clear_pr_summary_fields(worker)
    worker.update(
        {
            "root_goal": raw_request,
            "latest_planner_request": raw_request,
            "latest_planner_request_key": planner_key,
            "goal_status": "active",
            "phase": "planning",
            "execution_mode": "kanban_pipeline",
            **pr_policy_for_request(raw_request),
            "paused": False,
            "cancelled": False,
        }
    )
    if acceptance_criteria is not None:
        worker["criteria"] = acceptance_criteria
    if thread_context_text:
        worker["latest_goal_thread_context"] = thread_context_text
    else:
        worker.pop("latest_goal_thread_context", None)
    context_pack = write_context_pack(
        board.slug,
        root_goal=raw_request,
        request=raw_request,
        thread_context=thread_context_text,
    )
    worker.update(
        {
            "context_pack_path": str(context_pack_path(board.slug)),
            "context_pack_markdown_path": str(context_pack_markdown_path(board.slug)),
            "context_version": int(context_pack.get("version") or 1),
            "context_updated_at": int(context_pack.get("updated_at") or _now()),
            "context_truncated": bool(context_pack.get("truncated")),
        }
    )
    metadata = _update_worker_meta(board.slug, worker)
    planner_task_id = _ensure_planner_task(
        board.slug,
        metadata[DISCORD_WORKER_META_KEY],
        request=raw_request,
        request_key=planner_key,
        created_by=created_by,
        thread_context=thread_context_text,
        allow_existing=not starts_new_goal_run,
    )
    worker = dict(metadata[DISCORD_WORKER_META_KEY])
    worker["latest_planner_task_id"] = planner_task_id
    metadata = _update_worker_meta(board.slug, worker)
    return DiscordBoard(slug=board.slug, metadata=metadata)


def _canonical_planner_request_text(request: str) -> str:
    text = re.sub(r"\r\n?", "\n", str(request or "")).strip()
    match = re.match(r"^/goal(?:\s+(.*))?$", text, re.IGNORECASE | re.DOTALL)
    if not match:
        return text
    args = (match.group(1) or "").strip()
    if args.lower() in GOAL_CONTROL_COMMANDS:
        return text
    return args


def _merge_expanded_discord_thread_context(request: str, thread_context: str) -> str:
    base = str(thread_context or "").strip()
    if not has_discord_thread_reference(request):
        return base
    expanded = format_discord_thread_expansions(expand_discord_thread_references(request))
    if not expanded:
        return base
    if expanded in base:
        return base
    if base:
        return f"{base}\n\n{expanded}"
    return expanded


def _planner_request_fingerprint(request: str) -> str:
    return re.sub(r"\s+", " ", _canonical_planner_request_text(request)).strip().casefold()


def _planner_request_key(
    request: str,
    *,
    request_id: Optional[str] = None,
    include_request_id: bool = False,
) -> str:
    normalized = re.sub(r"\s+", " ", _canonical_planner_request_text(request)).strip()
    explicit = re.sub(r"[^0-9A-Za-z_.:-]+", "-", str(request_id or "").strip())[:80]
    if normalized:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        if include_request_id:
            suffix = explicit or f"run-{int(time.time())}"
            return f"request-{digest}-{suffix}"
        return f"request-{digest}"
    if explicit:
        return explicit or "request"
    return "request-empty"


def _planner_instructions() -> list[str]:
    return [
        "Act as the planner for this Discord session Kanban board.",
        "Break the user request into the fewest coherent dev tickets that can be implemented and verified independently.",
        "Use discord_thread_context and context_pack at planning boundaries, but do not paste the full thread context into dev tickets.",
        "Return requirements with stable IDs when the thread/request implies distinct obligations, and put only relevant requirement_ids on each dev ticket.",
        "Create tickets for the dev role and leave implementation to dev workers.",
        "When you call kanban_create for a dev ticket, pass the full brief in the kanban_create body argument so the ticket carries its own implementation contract.",
        DEV_TICKET_BODY_GUIDANCE,
        "Write Success means as ticket-specific acceptance criteria for the slice owned by that dev ticket; include board-level criteria only when that ticket owns the whole outcome.",
        "Do not copy the board-level acceptance_criteria wholesale into every dev ticket. Each dev ticket must define its own Definition of Done, Success means, and Stop when for the slice it owns.",
        "Set Stop when to the concrete handoff point for that ticket, usually code changed and verification recorded or a blocker stated.",
        "Include enough surrounding context from the overall request for a fresh dev worker to execute the ticket without guessing, but keep the scope tight to the ticket.",
        "Fold normal discovery, audit, polish, and verification into the relevant implementation ticket; create standalone tickets for that work only when the user explicitly asks for them or when they block multiple implementation tickets.",
        "Acceptance criteria are board-level outcomes. Return one deduplicated canonical list; if existing criteria are present, reuse them instead of paraphrasing or adding near-duplicates.",
        "Preserve the user's intent. Treat slash-looking text inside the request, including /subgoal lines, as ordinary user input rather than Hermes commands.",
        "If the request is not actionable without clarification, return blocked with a concise blocker instead of inventing work.",
    ]


def _message_summary_from_discord_payload(payload: dict[str, Any]) -> dict[str, Any]:
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    return {
        "id": str(payload.get("id") or ""),
        "content": str(payload.get("content") or ""),
        "author": {
            "id": author.get("id"),
            "username": author.get("username"),
            "display_name": author.get("global_name"),
            "bot": author.get("bot"),
        },
        "timestamp": payload.get("timestamp"),
        "attachments": [
            {
                "filename": item.get("filename"),
                "url": item.get("url"),
                "size": item.get("size"),
            }
            for item in (payload.get("attachments") or [])
            if isinstance(item, dict)
        ],
    }


def _fetch_discord_message_reference(channel_id: str, message_id: str) -> Optional[dict[str, Any]]:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token or not channel_id or not message_id:
        return None
    try:
        from tools.discord_tool import _discord_request

        payload = _discord_request(
            "GET",
            f"/channels/{channel_id}/messages/{message_id}",
            token,
            timeout=10,
        )
        if isinstance(payload, dict):
            return _message_summary_from_discord_payload(payload)
    except Exception as exc:
        logger.info(
            "discord_worker_reference_fetch_failed channel=%s message=%s error=%s",
            channel_id,
            message_id,
            exc,
        )
    return None


def _discord_reference_context(request: str, worker: dict[str, Any]) -> list[dict[str, Any]]:
    """Resolve Discord message links/ids into planner-visible read context."""
    text = str(request or "")
    exact_candidates: list[tuple[str, str]] = []
    fallback_candidates: list[tuple[str, list[str]]] = []
    seen_exact: set[tuple[str, str]] = set()
    seen_message_ids: set[str] = set()

    for match in _DISCORD_MESSAGE_URL_RE.finditer(text):
        pair = (match.group("channel"), match.group("message"))
        if pair not in seen_exact:
            seen_exact.add(pair)
            exact_candidates.append(pair)
            seen_message_ids.add(pair[1])

    fallback_channels = [
        str(worker.get("chat_id") or "").strip(),
        str(worker.get("parent_channel_id") or "").strip(),
    ]
    for match in _DISCORD_MESSAGE_ID_RE.finditer(text):
        message_id = match.group("message")
        if message_id in seen_message_ids:
            continue
        channels = [channel for channel in fallback_channels if channel]
        if channels:
            fallback_candidates.append((message_id, channels))
            seen_message_ids.add(message_id)

    references: list[dict[str, Any]] = []
    for channel_id, message_id in exact_candidates[:8]:
        fetched = _fetch_discord_message_reference(channel_id, message_id)
        if fetched:
            fetched["channel_id"] = channel_id
            references.append(fetched)
        else:
            references.append(
                {
                    "id": message_id,
                    "channel_id": channel_id,
                    "unresolved": True,
                    "content": "",
                }
            )
    for message_id, channels in fallback_candidates[: max(0, 8 - len(references))]:
        unresolved_channel = channels[0]
        for channel_id in channels:
            fetched = _fetch_discord_message_reference(channel_id, message_id)
            if fetched:
                fetched["channel_id"] = channel_id
                references.append(fetched)
                break
        else:
            references.append(
                {
                    "id": message_id,
                    "channel_id": unresolved_channel,
                    "unresolved": True,
                    "content": "",
                }
            )
    return references


def _ensure_planner_task(
    board: str,
    worker: dict[str, Any],
    *,
    request: Optional[str] = None,
    request_key: Optional[str] = None,
    thread_context: Optional[str] = None,
    allow_existing: bool = True,
    created_by: str = "discord-goal",
) -> str:
    conn = kanban_db.connect(board=board)
    try:
        planner_request = _canonical_planner_request_text(
            str(request or worker.get("root_goal") or worker.get("initial_request") or "")
        )
        planner_key = request_key or _planner_request_key(planner_request)
        thread_context_text = str(
            thread_context or worker.get("latest_goal_thread_context") or ""
        ).strip()
        if allow_existing:
            existing = _find_existing_planner_task(conn, planner_request)
            if existing:
                _set_planner_thread_context(
                    conn,
                    existing,
                    thread_context_text,
                    context_pack=_context_pack_summary(board),
                    acceptance_criteria=worker.get("criteria") or [],
                    planner_instructions=_planner_instructions(),
                )
                return existing
        body = json.dumps(
            {
                "role": ROLE_PLANNER,
                "root_goal": worker.get("root_goal") or worker.get("initial_request") or "",
                "request": planner_request,
                "discord_thread_context": thread_context_text,
                "context_pack": _context_pack_summary(board),
                "discord_references": _discord_reference_context(planner_request, worker),
                "planner_instructions": _planner_instructions(),
                "acceptance_criteria": worker.get("criteria") or [],
                "project_path": worker.get("project_path"),
                "worktree_path": worker.get("worktree_path"),
                "worker_branch": worker.get("worker_branch"),
            },
            indent=2,
            ensure_ascii=False,
        )
        return kanban_db.create_task(
            conn,
            title=format_role_round_title("Plan Discord implementation work", 1),
            body=body,
            assignee=ROLE_PLANNER,
            created_by=created_by,
            workspace_kind="dir",
            workspace_path=str(worker.get("worktree_path") or ""),
            tenant=board,
            priority=100,
            idempotency_key=f"{board}:planner:{planner_key}",
            max_runtime_seconds=_role_runtime_seconds(ROLE_PLANNER),
        )
    finally:
        conn.close()


def _set_planner_thread_context(
    conn,
    task_id: str,
    thread_context: str,
    *,
    context_pack: Optional[dict[str, Any]] = None,
    acceptance_criteria: Optional[list[Any]] = None,
    planner_instructions: Optional[list[str]] = None,
) -> None:
    row = conn.execute("SELECT body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return
    try:
        payload = json.loads(row["body"] or "{}")
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    changed = False
    if thread_context and str(payload.get("discord_thread_context") or "") != thread_context:
        payload["discord_thread_context"] = thread_context
        changed = True
    if context_pack:
        payload["context_pack"] = context_pack
        changed = True
    if acceptance_criteria is not None and payload.get("acceptance_criteria") != acceptance_criteria:
        payload["acceptance_criteria"] = acceptance_criteria
        changed = True
    if planner_instructions is not None and payload.get("planner_instructions") != planner_instructions:
        payload["planner_instructions"] = planner_instructions
        changed = True
    if not changed:
        return
    conn.execute(
        "UPDATE tasks SET body = ? WHERE id = ?",
        (json.dumps(payload, indent=2, ensure_ascii=False), task_id),
    )
    conn.commit()


def _find_existing_planner_task(conn, planner_request: str) -> Optional[str]:
    fingerprint = _planner_request_fingerprint(planner_request)
    if not fingerprint:
        return None
    rows = conn.execute(
        """
        SELECT id, body
        FROM tasks
        WHERE assignee = ?
          AND status != 'archived'
        ORDER BY created_at ASC, id ASC
        """,
        (ROLE_PLANNER,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["body"] or "{}")
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        existing_request = payload.get("request") or payload.get("root_goal") or ""
        if _planner_request_fingerprint(existing_request) == fingerprint:
            return row["id"]
    return None


def _role_runtime_seconds(role: str) -> Optional[int]:
    defaults = {ROLE_PLANNER: 1800, ROLE_DEV: 3600, ROLE_REVIEWER: 1800}
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        role_cfg = (((cfg.get("kanban") or {}).get("discord_worker") or {}).get("roles") or {}).get(role) or {}
        value = role_cfg.get("max_runtime_seconds")
        return int(value) if value else defaults.get(role)
    except Exception:
        return defaults.get(role)


def _review_loop_limit() -> int:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        value = ((cfg.get("kanban") or {}).get("discord_worker") or {}).get("review_loop_limit")
        return int(value or DEFAULT_REVIEW_LOOP_LIMIT)
    except Exception:
        return DEFAULT_REVIEW_LOOP_LIMIT


def _review_loop_limit_for_request(request: object) -> int:
    if _is_foreman_generated_request(request):
        return FOREMAN_REVIEW_LOOP_LIMIT
    return _review_loop_limit()


def _review_loop_limit_for_worker(worker: dict[str, Any]) -> int:
    value = worker.get("review_loop_limit")
    if value not in (None, ""):
        try:
            return int(value)
        except (TypeError, ValueError):
            pass
    return _review_loop_limit_for_request(
        worker.get("initial_request")
        or worker.get("root_goal")
        or worker.get("latest_planner_request")
        or ""
    )


def _is_review_loop_limit_blocker(worker: dict[str, Any]) -> bool:
    reason = str(worker.get("blocked_reason") or "").strip().lower()
    return (
        reason == REVIEW_LOOP_LIMIT_BLOCKED_REASON
        and worker.get("goal_status") == "blocked"
    )


def status_line(board: str) -> str:
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    persisted_summary = read_board_run_summary(board)
    if persisted_summary:
        return render_board_run_summary_text(persisted_summary)
    conn = kanban_db.connect(board=board)
    try:
        counts = kanban_db.board_stats(conn).get("by_status", {})
    finally:
        conn.close()
    count_bits = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()) if v)
    return (
        f"Kanban goal: {worker.get('goal_status') or worker.get('phase') or 'pending'}\n"
        f"Board: {worker.get('public_url') or board}\n"
        f"Branch: {worker.get('worker_branch') or 'pending'}\n"
        f"PR: {worker.get('pr_url') or 'not opened'}\n"
        f"Tasks: {count_bits or 'none'}"
    )


def _reclaim_running_role_workers(board: str, *, reason: str) -> list[str]:
    reclaimed: list[str] = []
    conn = kanban_db.connect(board=board)
    try:
        for task in kanban_db.list_tasks(conn, include_archived=False):
            if getattr(task, "status", None) != "running":
                continue
            assignee = str(getattr(task, "assignee", "") or "").strip().lower()
            if assignee not in ROLE_ASSIGNEES:
                continue
            if kanban_db.reclaim_task(conn, task.id, reason=reason):
                reclaimed.append(task.id)
    finally:
        conn.close()
    return reclaimed


def pause_board(board: str, *, reason: str = "user-paused") -> None:
    worker = _read_worker_meta(board)
    phase = str(worker.get("phase") or "").strip()
    if phase and phase != "paused":
        worker["phase_before_pause"] = phase
    worker.update({"goal_status": "paused", "phase": "paused", "paused": True, "paused_reason": reason})
    _update_worker_meta(board, worker)
    _reclaim_running_role_workers(board, reason=f"board-paused: {reason}")
    persist_board_run_summary(board)
    try:
        mark_dispatch_dirty(board=board, reason=reason)
    except Exception:
        pass


def resume_board(board: str) -> dict[str, Any]:
    """Replay a Discord worker board by moving blocked tickets back to dispatchable work."""
    worker = _read_worker_meta(board)
    replayed_task_ids: list[str] = []
    conn = kanban_db.connect(board=board)
    try:
        for task in kanban_db.list_tasks(conn, include_archived=False):
            if getattr(task, "status", None) not in {"blocked", "scheduled"}:
                continue
            before_status = str(getattr(task, "status", "") or "")
            if kanban_db.move_task_status(
                conn,
                task.id,
                "ready",
                source="command-center-replay",
            ):
                after = kanban_db.get_task(conn, task.id)
                if after and after.status != before_status:
                    replayed_task_ids.append(task.id)
    finally:
        conn.close()
    phase = worker.get("phase_before_pause") or worker.get("phase")
    if not phase or phase in {"paused", "cancelled", "intake"}:
        phase = "dev"
    worker.update(
        {
            "goal_status": "active",
            "phase": phase,
            "paused": False,
            "paused_reason": _DELETE_META,
        }
    )
    _update_worker_meta(board, worker)
    dispatch_dirty: str | None = None
    try:
        dispatch_dirty = str(mark_dispatch_dirty(board=board, reason="command-center-replay"))
    except Exception:
        pass
    return {
        "board": board,
        "resumed": True,
        "replayed_task_ids": replayed_task_ids,
        "dispatch_dirty": dispatch_dirty,
    }


def start_board(board: str) -> None:
    worker = _read_worker_meta(board)
    _clear_board_run_summary(board, worker)
    phase = worker.get("phase_before_pause") or worker.get("phase")
    if not phase or phase in {"paused", "cancelled", "intake"}:
        phase = "dev"
    worker.update(
        {
            "goal_status": "active",
            "phase": phase,
            "execution_mode": worker.get("execution_mode") or "kanban_pipeline",
            "root_goal": worker.get("root_goal") or worker.get("initial_request") or "",
            "paused": False,
            "cancelled": False,
        }
    )
    worker.pop("paused_reason", None)
    _update_worker_meta(board, worker)


def continue_board_after_review_loop_limit(
    board: str,
    *,
    extra_loops: int = REVIEW_LOOP_CONTINUE_EXTRA_LOOPS,
) -> dict[str, Any]:
    """Extend a Discord worker board that stopped at the review loop cap."""
    worker = _read_worker_meta(board)
    if not _is_review_loop_limit_blocker(worker):
        raise ValueError("board is not stopped at the review loop limit")
    _clear_board_run_summary(board, worker)
    try:
        loops = max(0, int(worker.get("review_loop_count") or 0))
    except (TypeError, ValueError):
        loops = 0
    try:
        limit = max(0, _review_loop_limit_for_worker(worker))
    except (TypeError, ValueError):
        limit = _review_loop_limit_for_worker(worker)
    try:
        extension = max(1, int(extra_loops))
    except (TypeError, ValueError):
        extension = REVIEW_LOOP_CONTINUE_EXTRA_LOOPS
    new_limit = max(limit, loops) + extension
    worker.update(
        {
            "goal_status": "active",
            "phase": "reviewing",
            "review_loop_limit": new_limit,
            "blocked_reason": "",
            "paused": False,
            "cancelled": False,
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
        }
    )
    worker.pop("paused_reason", None)
    _update_worker_meta(board, worker)
    reconcile_result = reconcile_board(board)
    marker_path = mark_dispatch_dirty(board=board, reason="continue-review-loop-limit")
    updated = _read_worker_meta(board)
    return {
        "review_loop_count": updated.get("review_loop_count"),
        "review_loop_limit": updated.get("review_loop_limit"),
        "goal_status": updated.get("goal_status"),
        "phase": updated.get("phase"),
        "reconcile_result": reconcile_result,
        "dispatch_dirty_marker": str(marker_path),
    }


def clear_board_goal(board: str) -> None:
    stop_board_execution(board, reason="user-cleared")


def stop_board_execution(board: str, *, reason: str = "user-stopped") -> dict[str, Any]:
    """Cancel a Discord worker board and reclaim live role workers."""
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board":
        return {"board": board, "reclaimed": []}
    worker.update(
        {
            "goal_status": "cancelled",
            "phase": "cancelled",
            "cancelled": True,
            "paused": True,
            "paused_reason": reason,
            "stop_reason": reason,
            "terminal_reaction_sync_pending": True,
            "terminal_summary_sync_pending": True,
        }
    )
    _update_worker_meta(board, worker)
    reclaimed = _reclaim_running_role_workers(board, reason=reason)
    persist_board_run_summary(board)
    try:
        mark_dispatch_dirty(board=board, reason=reason)
    except Exception:
        pass
    return {"board": board, "reclaimed": reclaimed}


def is_cancelled(board: str) -> bool:
    worker = _read_worker_meta(board)
    return bool(worker.get("cancelled") or worker.get("goal_status") == "cancelled")


def add_subgoal(board: str, text: str) -> tuple[int, str]:
    body = str(text or "").strip()
    if not body:
        raise ValueError("subgoal text is required")
    worker = _read_worker_meta(board)
    criteria = list(worker.get("criteria") or [])
    idx = len(criteria) + 1
    criterion = {
        "id": f"c{idx}",
        "text": body,
        "active": True,
        "created_at": _now(),
    }
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title=format_role_round_title(f"User subgoal {idx}", active_dev_round(worker)),
            body=body,
            assignee=ROLE_DEV,
            created_by="discord-subgoal",
            workspace_kind="dir",
            workspace_path=str(worker.get("worktree_path") or ""),
            tenant=board,
            priority=80,
            idempotency_key=f"{board}:subgoal:{idx}:{secrets.token_hex(4)}",
            max_runtime_seconds=_role_runtime_seconds(ROLE_DEV),
        )
    finally:
        conn.close()
    criterion["task_id"] = task_id
    criteria.append(criterion)
    worker["criteria"] = criteria
    worker["phase"] = "dev"
    worker["goal_status"] = "active"
    worker["execution_mode"] = "kanban_pipeline"
    _clear_board_run_summary(board, worker)
    _update_worker_meta(board, worker)
    return idx, body


def list_subgoals(board: str) -> str:
    criteria = _read_worker_meta(board).get("criteria") or []
    active = [c for c in criteria if not isinstance(c, dict) or c.get("active", True)]
    if not active:
        return "No active subgoals."
    lines = []
    for idx, item in enumerate(active, start=1):
        if isinstance(item, dict):
            suffix = f" ({item.get('task_id')})" if item.get("task_id") else ""
            lines.append(f"{idx}. {item.get('text')}{suffix}")
        else:
            lines.append(f"{idx}. {item}")
    return "\n".join(lines)


def deactivate_subgoal(board: str, index: int) -> str:
    worker = _read_worker_meta(board)
    criteria = list(worker.get("criteria") or [])
    active_positions = [i for i, c in enumerate(criteria) if not isinstance(c, dict) or c.get("active", True)]
    if index < 1 or index > len(active_positions):
        raise IndexError("subgoal index out of range")
    pos = active_positions[index - 1]
    item = criteria[pos]
    text = str(item.get("text") if isinstance(item, dict) else item)
    if isinstance(item, dict):
        item["active"] = False
        item["deactivated_at"] = _now()
        criteria[pos] = item
        _cancel_unstarted_task(board, str(item.get("task_id") or ""))
    else:
        criteria[pos] = {"text": text, "active": False, "deactivated_at": _now()}
    worker["criteria"] = criteria
    _update_worker_meta(board, worker)
    return text


def clear_subgoals(board: str) -> int:
    worker = _read_worker_meta(board)
    criteria = list(worker.get("criteria") or [])
    count = 0
    for idx, item in enumerate(criteria):
        if isinstance(item, dict):
            if item.get("active", True):
                count += 1
                item["active"] = False
                item["deactivated_at"] = _now()
                _cancel_unstarted_task(board, str(item.get("task_id") or ""))
                criteria[idx] = item
        else:
            count += 1
            criteria[idx] = {"text": str(item), "active": False, "deactivated_at": _now()}
    worker["criteria"] = criteria
    _update_worker_meta(board, worker)
    return count


def reconcile_board(board: str) -> Optional[str]:
    """Advance deterministic Discord worker board phases.

    This creates reviewer tasks when all planner/dev work has settled and
    enforces the reviewer loop cap. It intentionally does not call an LLM.
    """
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board":
        return None
    if not is_executable_worker_board(board):
        return None
    if worker.get("paused") or worker.get("cancelled") or worker.get("goal_status") in {"done", "blocked", "cancelled"}:
        return None

    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        active_roles = {
            str(t.assignee or "").lower()
            for t in tasks
            if t.status in {"triage", "todo", "ready", "running", "blocked"}
        }
        if ROLE_PLANNER in active_roles or ROLE_DEV in active_roles or ROLE_REVIEWER in active_roles:
            return None
        if not tasks:
            _ensure_planner_task(board, worker)
            return "planner_created"

        loops = int(worker.get("review_loop_count") or 0)
        loop_limit = _review_loop_limit_for_worker(worker)
        if loops >= loop_limit:
            worker.update({
                "phase": "blocked",
                "goal_status": "blocked",
                "blocked_reason": "review loop limit reached",
                "terminal_reaction_sync_pending": True,
                "terminal_summary_sync_pending": True,
            })
            _update_worker_meta(board, worker)
            persist_board_run_summary(board)
            return "blocked_review_loop_limit"
        loops += 1
        worker["review_loop_count"] = loops
        worker["phase"] = "reviewing"
        _update_worker_meta(board, worker)
        kanban_db.create_task(
            conn,
            title=format_role_round_title("Review Discord implementation", loops),
            body=json.dumps(
                {
                    "role": ROLE_REVIEWER,
                    "root_goal": worker.get("root_goal") or worker.get("initial_request") or "",
                    "acceptance_criteria": worker.get("criteria") or [],
                    "context_pack": _context_pack_summary(board),
                    "requirements": worker.get("requirements") or [],
                    "review_loop": loops,
                    "loop_limit": loop_limit,
                },
                indent=2,
                ensure_ascii=False,
            ),
            assignee=ROLE_REVIEWER,
            created_by="discord-worker-harness",
            workspace_kind="dir",
            workspace_path=str(worker.get("worktree_path") or ""),
            tenant=board,
            priority=95,
            idempotency_key=f"{board}:review:{loops}",
            max_runtime_seconds=_role_runtime_seconds(ROLE_REVIEWER),
        )
        return "reviewer_created"
    finally:
        conn.close()


def is_discord_worker_board(board: str) -> bool:
    return _read_worker_meta(board).get("kind") == "discord_worker_board"


def is_executable_worker_board(board: str) -> bool:
    worker = _read_worker_meta(board)
    return (
        worker.get("kind") == "discord_worker_board"
        and worker.get("execution_mode") == "kanban_pipeline"
        and worker.get("goal_status") == "active"
        and not _worker_source_message_too_old(worker)
    )


def is_paused_or_cancelled(board: str) -> bool:
    worker = _read_worker_meta(board)
    return bool(worker.get("paused") or worker.get("cancelled"))


def _paused_corrupt_incident(board: str) -> Optional[dict[str, Any]]:
    try:
        incident = kanban_db.is_board_paused_for_corruption(board)
    except Exception:
        return None
    if not incident:
        return None
    db_path = Path(str(incident.get("db_path") or kanban_db.kanban_db_path(board)))
    try:
        fingerprint = kanban_db._db_content_fingerprint(db_path)
    except Exception:
        fingerprint = None
    if incident.get("fingerprint") == fingerprint:
        return incident
    logger.info(
        "discord worker board: board %s database changed since corruption incident; retrying",
        board,
    )
    return None


def _is_skippable_board_db_error(exc: Exception) -> bool:
    return isinstance(exc, (kanban_db.KanbanDbCorruptError, sqlite3.DatabaseError, OSError))


def _log_skipped_board_target(board: str, exc: Exception, *, source: str) -> None:
    if isinstance(exc, kanban_db.KanbanDbCorruptError):
        logger.warning(
            "%s: skipping board %s after kanban DB corruption: %s",
            source,
            board,
            exc,
        )
    else:
        logger.debug("%s: skipping board %s after DB open/read failure: %s", source, board, exc)


def running_worker_thread_targets() -> list[dict[str, Any]]:
    """Return Discord thread targets whose worker board is actively running."""
    targets: list[dict[str, Any]] = []
    for board_meta in kanban_db.list_boards(include_archived=False):
        board = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = _read_worker_meta(board)
        if worker.get("kind") != "discord_worker_board":
            continue
        thread_id = str(worker.get("thread_id") or "").strip()
        if not thread_id:
            continue
        if _worker_source_message_too_old(worker):
            continue
        if _paused_corrupt_incident(board):
            logger.debug(
                "discord worker typing targets: board %s paused for unchanged DB corruption; skipping",
                board,
            )
            continue
        try:
            with kanban_db.connect_closing(board=board) as conn:
                placeholders = ",".join("?" for _ in ROLE_ASSIGNEES)
                row = conn.execute(
                    "SELECT COUNT(*) FROM tasks "
                    "WHERE status = 'running' AND lower(assignee) IN "
                    f"({placeholders})",
                    tuple(sorted(ROLE_ASSIGNEES)),
                ).fetchone()
                running = int(row[0] or 0) if row else 0
        except Exception as exc:
            if _is_skippable_board_db_error(exc):
                _log_skipped_board_target(board, exc, source="discord worker typing targets")
                continue
            raise
        source_context = _worker_source_task_context(worker)
        source_state = None
        if source_context.get("source_board") and source_context.get("source_task_id"):
            try:
                source_state = source_task_reaction_state(
                    source_context["source_board"],
                    source_context["source_task_id"],
                )
            except Exception as exc:
                if _is_skippable_board_db_error(exc):
                    _log_skipped_board_target(
                        source_context["source_board"],
                        exc,
                        source="discord worker source task state",
                    )
                else:
                    raise
        if running <= 0 and source_state != "running":
            continue
        targets.append(
            {
                "board": board,
                "thread_id": thread_id,
                "chat_id": str(worker.get("chat_id") or thread_id),
                "running": running,
                **source_context,
            }
        )
    return targets


def running_notify_thread_targets() -> list[dict[str, Any]]:
    """Return Discord notification thread targets for running Kanban tasks.

    Default/control-plane boards such as #dev intake do not always have a
    Discord worker-board metadata file, but their tasks can still carry a
    ``kanban_notify_subs`` row pointing back to the originating Discord thread.
    Those active tasks should pulse the same native typing indicator as normal
    project worker boards while the work is actually running.
    """
    targets: list[dict[str, Any]] = []
    for board_meta in kanban_db.list_boards(include_archived=False):
        board = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        if _paused_corrupt_incident(board):
            logger.debug(
                "discord notify typing targets: board %s paused for unchanged DB corruption; skipping",
                board,
            )
            continue
        try:
            with kanban_db.connect_closing(board=board) as conn:
                rows = conn.execute(
                    """
                    SELECT t.id AS task_id,
                           n.chat_id AS chat_id,
                           n.thread_id AS thread_id
                      FROM tasks t
                      JOIN kanban_notify_subs n ON n.task_id = t.id
                     WHERE t.status = 'running'
                       AND lower(n.platform) = 'discord'
                       AND COALESCE(n.thread_id, '') != ''
                    """
                ).fetchall()
        except Exception as exc:
            if _is_skippable_board_db_error(exc):
                _log_skipped_board_target(board, exc, source="discord notify typing targets")
                continue
            raise
        for row in rows:
            thread_id = str(row["thread_id"] or "").strip()
            if not thread_id:
                continue
            targets.append(
                {
                    "board": board,
                    "task_id": str(row["task_id"] or ""),
                    "thread_id": thread_id,
                    "chat_id": str(row["chat_id"] or thread_id),
                    "running": 1,
                    "source": "notify_sub",
                }
            )
    return targets


def running_discord_thread_typing_targets() -> list[dict[str, Any]]:
    """Return all Discord thread targets that should show native typing."""
    targets: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for target in [*running_worker_thread_targets(), *running_notify_thread_targets()]:
        thread_id = str(target.get("thread_id") or "").strip()
        if not thread_id:
            continue
        chat_id = str(target.get("chat_id") or thread_id).strip() or thread_id
        key = (chat_id, thread_id)
        if key in seen:
            continue
        seen.add(key)
        normalized = dict(target)
        normalized["chat_id"] = chat_id
        normalized["thread_id"] = thread_id
        targets.append(normalized)
    return targets


def _cancel_unstarted_task(board: str, task_id: str) -> None:
    if not task_id:
        return
    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        if task is not None and task.status in {"triage", "todo", "ready"}:
            kanban_db.archive_task(conn, task_id)
    finally:
        conn.close()


def board_for_gateway_event(event: Any, *, create: bool = False) -> Optional[DiscordBoard]:
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    platform_value = platform.value if hasattr(platform, "value") else str(platform or "")
    if platform_value.lower() != "discord":
        return None
    chat_type = str(getattr(source, "chat_type", "") or "").lower()
    source_thread_id = str(getattr(source, "thread_id", "") or "").strip()
    if not source_thread_id and chat_type != "thread":
        return None
    thread_id = str(source_thread_id or getattr(source, "chat_id", "") or "")
    if not thread_id:
        return None
    feature_summary = getattr(event, "feature_summary", None)
    feature_board = None
    if isinstance(feature_summary, dict):
        board_handle = feature_summary.get("kanban_board")
        if isinstance(board_handle, dict):
            feature_board = str(board_handle.get("slug") or "").strip() or None
    command_name = ""
    get_command = getattr(event, "get_command", None)
    if callable(get_command):
        try:
            command_name = str(get_command() or "").lstrip("/").lower()
        except Exception:
            command_name = ""
    request_id = str(
        (feature_summary or {}).get("source_message_id") if isinstance(feature_summary, dict) else ""
    ).strip()
    if not request_id and create and command_name == "goal":
        request_id = str(getattr(event, "message_id", "") or "").strip()
    if feature_board:
        slug = feature_board
    elif create:
        is_starter_message = bool(request_id and request_id == thread_id)
        starter_slug = board_slug_for_discord_request(thread_id, request_id) if is_starter_message else ""
        slug = (
            starter_slug
            if starter_slug and kanban_db.board_exists(starter_slug)
            else board_slug_for_discord_request(
                thread_id,
                "" if is_starter_message else request_id,
            )
        )
    else:
        slug = board_slug_for_discord_thread(thread_id)
    if not create and not kanban_db.board_exists(slug):
        return None
    if not create:
        metadata = kanban_db.read_board_metadata(slug)
        worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
        if _worker_source_message_too_old(worker):
            return None
        return DiscordBoard(slug=slug, metadata=metadata)
    if create:
        project_context = {
            "project_name": getattr(source, "project_name", None),
            "project_path": getattr(source, "project_path", None),
            "project_github_url": getattr(source, "project_github_url", None),
            "project_channel_id": getattr(source, "project_channel_id", None),
            "project_mapping_source": getattr(source, "project_mapping_source", None),
            "project_mapping_resolved": getattr(source, "project_mapping_resolved", None),
        }
        project_context = {k: v for k, v in project_context.items() if v is not None}
        return ensure_discord_thread_board(
            thread_id=thread_id,
            chat_id=str(getattr(source, "chat_id", "") or ""),
            guild_id=str(getattr(source, "guild_id", "") or ""),
            parent_channel_id=str(getattr(source, "parent_chat_id", "") or ""),
            initial_request=str(getattr(event, "text", "") or ""),
            project_context=project_context,
            request_id=request_id,
            board_slug=slug,
            source_message_id=request_id,
        )
    return DiscordBoard(slug=slug, metadata=kanban_db.read_board_metadata(slug))


def stoppable_boards_for_gateway_event(event: Any) -> list[DiscordBoard]:
    """Return Discord worker boards a plain /stop in this thread should halt."""
    source = getattr(event, "source", None)
    platform = getattr(source, "platform", None)
    platform_value = platform.value if hasattr(platform, "value") else str(platform or "")
    if platform_value.lower() != "discord":
        return []
    chat_type = str(getattr(source, "chat_type", "") or "").lower()
    source_thread_id = str(getattr(source, "thread_id", "") or "").strip()
    channel_id = str(getattr(source, "chat_id", "") or "").strip()
    thread_id = str(source_thread_id or (channel_id if chat_type == "thread" else "")).strip()
    if not thread_id and not channel_id:
        return []
    guild_id = str(getattr(source, "guild_id", "") or "").strip()

    boards: list[DiscordBoard] = []
    seen: set[str] = set()

    def add_board(slug: str) -> None:
        if not slug or slug in seen or not kanban_db.board_exists(slug):
            return
        worker = _read_worker_meta(slug)
        if worker.get("kind") != "discord_worker_board":
            return
        status = str(worker.get("goal_status") or "").strip().lower()
        phase = str(worker.get("phase") or "").strip().lower()
        if (
            worker.get("cancelled")
            or status == "cancelled"
            or status == "done"
            or phase == "complete"
        ):
            return
        if status not in {"active", "paused", "blocked"} and phase not in {
            "planning",
            "dev",
            "reviewing",
            "paused",
            "blocked",
        }:
            return
        seen.add(slug)
        boards.append(DiscordBoard(slug=slug, metadata=kanban_db.read_board_metadata(slug)))

    try:
        direct = board_for_gateway_event(event, create=False)
        if direct is not None:
            add_board(direct.slug)
    except Exception:
        pass

    for board_meta in kanban_db.list_boards(include_archived=False):
        slug = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = _read_worker_meta(slug)
        if guild_id and str(worker.get("guild_id") or "").strip() not in {"", guild_id}:
            continue
        worker_thread_id = str(worker.get("thread_id") or "").strip()
        if thread_id and worker_thread_id == thread_id:
            add_board(slug)
            continue
        if not thread_id and channel_id and channel_id in {
            str(worker.get("chat_id") or "").strip(),
            str(worker.get("parent_channel_id") or "").strip(),
        }:
            add_board(slug)
    return boards
