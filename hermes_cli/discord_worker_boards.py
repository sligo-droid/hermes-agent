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
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote, urlsplit, urlunsplit

from hermes_cli import kanban_db
from utils import atomic_json_write


logger = logging.getLogger(__name__)
DISCORD_WORKER_META_KEY = "discord_worker"
DISCORD_WORKER_DISPATCH_DIRTY_FILENAME = "discord-worker-dispatch.dirty.json"
PUBLIC_TOKEN_BYTES = 24
REVIEW_LOOP_LIMIT_BLOCKED_REASON = "review loop limit reached"
REVIEW_LOOP_CONTINUE_EXTRA_LOOPS = 5
ROLE_PLANNER = "planner"
ROLE_DEV = "dev"
ROLE_REVIEWER = "reviewer"
ROLE_ASSIGNEES = frozenset({ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER})
CODEX_STATE_MAX_EVENTS = 200
CODEX_STATE_MAX_TEXT_BYTES = 24_000
CODEX_STATE_LOG_TAIL_BYTES = 64_000
GOAL_CONTROL_COMMANDS = frozenset({"status", "pause", "resume", "clear", "stop", "done"})
TERMINAL_GOAL_STATUSES = frozenset({"done", "blocked", "cancelled"})
_DISCORD_MESSAGE_URL_RE = re.compile(
    r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d+)/(?P<channel>\d+)/(?P<message>\d+)"
)
_DISCORD_MESSAGE_ID_RE = re.compile(
    r"\b(?:message|msg)\s+(?P<message>\d{16,24})\b",
    re.IGNORECASE,
)
PUBLIC_BOARD_COLUMNS = ("triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done")
_POSIX_PATH_RE = re.compile(
    r"(?<![\w:/.-])/(?:home|Users|tmp|var|etc|opt|private|workspace|workspaces|mnt|srv|repo|root)"
    r"(?:/[^\s\"'<>),;{}\[\]]*)?"
)
_WINDOWS_PATH_RE = re.compile(r"(?<![\w:/.-])[A-Za-z]:\\[^\s\"'<>),;{}\[\]]+")


class TicketMoveConflict(RuntimeError):
    """Raised when a ticket status move is valid syntax but refused."""


def board_slug_for_discord_thread(thread_id: str) -> str:
    """Return the canonical board slug for a Discord thread id."""
    cleaned = re.sub(r"[^0-9a-zA-Z_-]+", "-", str(thread_id or "").strip()).strip("-_")
    if not cleaned:
        raise ValueError("Discord thread id is required")
    return f"discord-{cleaned.lower()}"[:64]


def _now() -> int:
    return int(time.time())


def _metadata_path(board: str) -> Path:
    return kanban_db.board_metadata_path(board)


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


def codex_worker_state_path(task_id: str, *, board: Optional[str] = None) -> Path:
    """Return the per-ticket Codex app-server state sidecar path."""
    log_path = kanban_db.worker_log_path(str(task_id or ""), board=board)
    return log_path.with_name(f"{log_path.stem}.codex-state.json")


def _read_codex_worker_state(task_id: str, *, board: Optional[str] = None) -> dict[str, Any]:
    path = codex_worker_state_path(task_id, board=board)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"error": "codex state sidecar could not be read"}
    return data if isinstance(data, dict) else {}


def _cap_state_value(value: Any, *, max_text: int = CODEX_STATE_MAX_TEXT_BYTES) -> Any:
    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="replace")
        if len(encoded) <= max_text:
            return value
        clipped = encoded[:max_text].decode("utf-8", errors="replace")
        return f"{clipped}\n...[truncated {len(encoded) - max_text} bytes]"
    if isinstance(value, list):
        return [_cap_state_value(item, max_text=max_text) for item in value[:80]]
    if isinstance(value, dict):
        return {
            str(key): _cap_state_value(item, max_text=max_text)
            for key, item in list(value.items())[:80]
        }
    return value


def _write_codex_worker_state(
    task_id: str,
    *,
    board: Optional[str],
    update: dict[str, Any],
) -> dict[str, Any]:
    path = codex_worker_state_path(task_id, board=board)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _read_codex_worker_state(task_id, board=board)
    current.update(update)
    current["task_id"] = str(task_id)
    current["board"] = str(board or "")
    current["updated_at"] = _now()
    atomic_json_write(path, current, indent=2)
    return current


def record_codex_worker_event(
    task_id: str,
    *,
    board: Optional[str],
    event: dict[str, Any],
) -> None:
    """Append one raw Codex app-server notification to a bounded sidecar."""
    current = _read_codex_worker_state(task_id, board=board)
    events = current.get("events") if isinstance(current.get("events"), list) else []
    item = ((event.get("params") or {}).get("item") or {}) if isinstance(event, dict) else {}
    events.append(
        {
            "ts": _now(),
            "method": event.get("method") if isinstance(event, dict) else "",
            "item_type": item.get("type") if isinstance(item, dict) else "",
            "payload": _cap_state_value(event),
        }
    )
    truncated = int(current.get("truncated_events") or 0)
    if len(events) > CODEX_STATE_MAX_EVENTS:
        truncated += len(events) - CODEX_STATE_MAX_EVENTS
        events = events[-CODEX_STATE_MAX_EVENTS:]
    _write_codex_worker_state(
        task_id,
        board=board,
        update={"events": events, "truncated_events": truncated},
    )


def record_codex_worker_result(
    task_id: str,
    *,
    board: Optional[str],
    result: Any,
) -> None:
    payload = {
        "backend": getattr(result, "backend", "codex"),
        "final_text": getattr(result, "final_text", ""),
        "error": getattr(result, "error", None),
        "interrupted": bool(getattr(result, "interrupted", False)),
        "timed_out": bool(getattr(result, "timed_out", False)),
        "should_retire": bool(getattr(result, "should_retire", False)),
        "tool_iterations": int(getattr(result, "tool_iterations", 0) or 0),
        "turn_id": getattr(result, "turn_id", None),
        "thread_id": getattr(result, "thread_id", None),
        "agents": getattr(result, "agents", []),
        "plan_text": getattr(result, "plan_text", ""),
        "exit_code": getattr(result, "exit_code", None),
        "duration_seconds": getattr(result, "duration_seconds", None),
        "run_profile": getattr(result, "run_profile", {}),
        "service_tier": getattr(result, "service_tier", None),
        "fast_mode": getattr(result, "fast_mode", None),
    }
    _write_codex_worker_state(
        task_id,
        board=board,
        update={"result": _cap_state_value(payload)},
    )


def _read_worker_meta(board: str) -> dict[str, Any]:
    metadata = kanban_db.read_board_metadata(board)
    raw = metadata.get(DISCORD_WORKER_META_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def _update_worker_meta(board: str, updates: dict[str, Any]) -> dict[str, Any]:
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    worker.update(updates)
    worker["updated_at"] = _now()
    metadata[DISCORD_WORKER_META_KEY] = worker
    return _write_metadata(board, metadata)


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
) -> DiscordBoard:
    """Create or update the board backing a Discord thread."""
    started = time.time()
    slug = board_slug_for_discord_thread(thread_id)
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
    branch = f"discord/{thread_id}"
    metadata = kanban_db.create_board(
        slug,
        name=f"Discord {thread_id}",
        description=(initial_request or "")[:500],
        icon="",
        color="#22c55e",
    )
    previous_worktree_path = str(worker.get("worktree_path") or "").strip()
    project_path_changed = bool(project_path and project_path != existing_project_path)
    worktree_path = previous_worktree_path
    if not worktree_path or (project_path_changed and not worker.get("code_island_ready")):
        worktree_path = _default_worktree_path(project_path, str(thread_id))
    worker.update(
        {
            "kind": "discord_worker_board",
            "thread_id": str(thread_id),
            "chat_id": str(chat_id or ""),
            "guild_id": str(guild_id or ""),
            "parent_channel_id": str(parent_channel_id or ""),
            "initial_request": str(initial_request or worker.get("initial_request") or ""),
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
            "review_loop_limit": int(worker.get("review_loop_limit") or _review_loop_limit()),
            "share_token": token,
            "public_url": public_session_board_url(str(thread_id)),
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
    if os.path.isdir(worktree_path):
        if _is_git_worktree(worktree_path):
            worker["code_island_ready"] = True
            worker["code_island_pending"] = False
            worker.pop("code_island_error", None)
        else:
            worker["code_island_ready"] = False
            worker["code_island_pending"] = False
            worker["code_island_error"] = f"worktree path is not a git repository: {worktree_path}"
        return
    worker["code_island_ready"] = False
    worker["code_island_pending"] = True
    try:
        root = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if root.returncode != 0:
            worker["code_island_error"] = (root.stderr or root.stdout or "not a git repository").strip()
            return
        repo_root = root.stdout.strip() or project_path
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
    board = board_slug_for_discord_thread(session_id)
    if not kanban_db.board_exists(board):
        raise KeyError("unknown board session")
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board":
        raise KeyError("unknown board session")
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
    return {
        "board": board,
        "name": _worker_board_name(worker, metadata, board),
        "description": metadata.get("description") or "",
        "session_id": str(worker.get("thread_id") or ""),
        "worker": _public_worker_meta(worker),
        "counts": counts,
        "running": running,
        "runtime": runtime,
        "tasks": rows,
    }


def ticket_state_for_session(session_id: str, task_id: str) -> dict[str, Any]:
    board = board_slug_for_discord_thread(session_id)
    if not kanban_db.board_exists(board):
        raise KeyError("unknown board session")
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board":
        raise KeyError("unknown board session")
    return _ticket_state_for_board(board, task_id, worker=worker)


def ticket_terminal_feed_for_session(session_id: str, task_id: str) -> dict[str, Any]:
    board = board_slug_for_discord_thread(session_id)
    if not kanban_db.board_exists(board):
        raise KeyError("unknown board session")
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board":
        raise KeyError("unknown board session")
    return _ticket_terminal_feed_for_board(board, task_id, worker=worker)


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
    board = board_slug_for_discord_thread(session_id)
    if not kanban_db.board_exists(board):
        raise KeyError("unknown board session")
    worker = _read_worker_meta(board)
    if worker.get("kind") != "discord_worker_board":
        raise KeyError("unknown board session")

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


def _public_terminal_run(run: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if not run:
        return None
    keys = ("id", "status", "outcome", "worker_pid", "started_at", "last_heartbeat_at", "ended_at")
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
        return f"[{created}] {kind.replace('_', ' ')}"
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


def _public_review_text(worker: dict[str, Any]) -> str:
    count = worker.get("review_loop_count")
    limit = worker.get("review_loop_limit")
    if limit not in (None, ""):
        return f"{count or 0}/{limit}"
    return str(count or 0)


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
        conn = kanban_db.connect(board=board)
        try:
            total += len(_running_ticket_snapshot(conn))
        finally:
            conn.close()
    return total


def board_thread_state(board: str) -> str:
    """Return the Discord thread-facing state for a worker board."""
    worker = _read_worker_meta(board)
    if worker.get("cancelled") or worker.get("goal_status") == "cancelled":
        return "errored"
    if str(worker.get("blocked_reason") or "").strip() or worker.get("goal_status") == "blocked":
        return "blocked"

    conn = kanban_db.connect(board=board)
    try:
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
            if (
                all(task.status == "done" for task in tasks)
                and (worker.get("goal_status") == "done" or worker.get("phase") == "complete")
            ):
                return "done"
            if any(task.status == "running" for task in tasks):
                return "running"
            return "active"
    finally:
        conn.close()

    if worker.get("goal_status") == "done" or worker.get("phase") == "complete":
        return "done"
    return "active"


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


def feature_summary_snapshot(board: str) -> dict[str, Any]:
    """Return the current feature-summary values for a Discord worker board."""
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    if worker.get("kind") != "discord_worker_board":
        raise KeyError("unknown Discord worker board")

    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        summaries = kanban_db.latest_summaries(conn, [t.id for t in tasks])
        counts = kanban_db.board_stats(conn).get("by_status", {})
        running = _running_ticket_snapshot(conn)
    finally:
        conn.close()

    state = board_thread_state(board)
    title = _worker_generated_title(worker)
    outcome = _feature_summary_outcome(
        worker,
        state=state,
        tasks=tasks,
        summaries=summaries,
        counts=counts,
        running=running,
    )
    snapshot = {
        "board": board,
        "thread_id": str(worker.get("thread_id") or ""),
        "chat_id": str(worker.get("chat_id") or worker.get("thread_id") or ""),
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
        "updated_at": worker.get("updated_at"),
    }
    key_payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    snapshot["sync_key"] = hashlib.sha256(key_payload.encode("utf-8")).hexdigest()
    return snapshot


def thread_status_targets() -> list[dict[str, Any]]:
    """Return Discord thread targets with their current board state."""
    targets: list[dict[str, Any]] = []
    for board_meta in kanban_db.list_boards(include_archived=False):
        board = str(board_meta.get("slug") or kanban_db.DEFAULT_BOARD)
        worker = _read_worker_meta(board)
        if worker.get("kind") != "discord_worker_board":
            continue
        thread_id = str(worker.get("thread_id") or "").strip()
        if not thread_id:
            continue
        try:
            summary = feature_summary_snapshot(board)
        except Exception:
            summary = {"state": board_thread_state(board)}
        state = summary.get("state") or board_thread_state(board)
        terminal_sync_pending = bool(
            worker.get("terminal_reaction_sync_pending")
            or worker.get("terminal_summary_sync_pending")
        )
        if state not in {"active", "running"} and not (state in {"done", "blocked", "errored"} and terminal_sync_pending):
            continue
        targets.append(
            {
                "board": board,
                "thread_id": thread_id,
                "chat_id": str(worker.get("chat_id") or thread_id),
                "guild_id": summary.get("guild_id") or str(worker.get("guild_id") or ""),
                "parent_channel_id": summary.get("parent_channel_id") or str(worker.get("parent_channel_id") or ""),
                "state": state,
                "title": summary.get("title") or "",
                "fallback_title": summary.get("fallback_title") or "",
                "outcome": summary.get("outcome") or "",
                "branch": summary.get("branch") or "",
                "pr_url": summary.get("pr_url") or "",
                "pr_number": summary.get("pr_number") or "",
                "public_url": summary.get("public_url") or "",
                "sync_key": summary.get("sync_key") or "",
                "terminal_reaction_sync_pending": bool(worker.get("terminal_reaction_sync_pending")),
                "terminal_summary_sync_pending": bool(worker.get("terminal_summary_sync_pending")),
            }
        )
    return targets


def mark_thread_status_synced(
    board: str,
    *,
    reaction: bool = False,
    summary: bool = False,
) -> None:
    """Clear one-shot terminal Discord thread sync flags for a board."""
    if not board or not (reaction or summary):
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
        max_per_board = int(worker_cfg.get("max_workers_per_board") or 1)
    except (TypeError, ValueError):
        max_per_board = 1
    try:
        max_global = int(worker_cfg.get("max_global_workers") or 4)
    except (TypeError, ValueError):
        max_global = 4
    if max_per_board > 0 and running_count >= max_per_board:
        return "board worker limit reached"
    if max_global > 0:
        try:
            if _active_role_count_across_boards() >= max_global:
                return "global worker limit reached"
        except Exception:
            pass

    if not kanban_db.has_spawnable_ready(conn, additional_spawnable_assignees=ROLE_ASSIGNEES):
        return "ready tickets are assigned to non-spawnable lanes"
    return "awaiting next dispatcher tick"


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
    .ticket { appearance: none; background: transparent; border: 0; color: inherit; cursor: pointer; display: block; font: inherit; padding: 0; text-align: left; width: 100%; }
    .ticket:hover strong { color: var(--link); text-decoration: underline; }
    .ticket:focus-visible { outline: 2px solid var(--code); outline-offset: 3px; }
    .ticket strong { display: block; font-size: 14px; line-height: 1.25; }
    .ticket p { color: var(--muted); font-size: 13px; margin: 8px 0 0; }
    .move-error { background: #fef2f2; border: 1px solid #fecaca; border-radius: 6px; color: #991b1b; font-size: 13px; margin-bottom: 12px; padding: 10px 12px; }
    .move-error[hidden] { display: none; }
    .criteria { margin-bottom: 18px; padding: 14px; }
    .criteria strong { display: block; font-size: 14px; margin-bottom: 8px; }
    .criteria ol { margin: 0; padding-left: 20px; }
    .criteria li { color: var(--text); margin: 4px 0; }
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
            "<button type=\"button\" class=\"ticket\" data-ticket-id=\"{id}\" "
            "data-ticket-title=\"{title}\" data-ticket-url=\"{ticket_url}\" "
            "data-ticket-state-url=\"{state_url}\" "
            "data-ticket-terminal-page-url=\"{terminal_page_url}\" "
            "data-ticket-terminal-url=\"{terminal_url}\">"
            "<strong>{title}</strong><br><code>{id}</code> {assignee}{brief}{summary}"
            "</button></li>".format(
                title=esc(item["title"]),
                id=esc(ticket_id),
                status=esc(status),
                ticket_url=esc(ticket_url),
                state_url=esc(f"{ticket_url}/state"),
                terminal_page_url=esc(f"{ticket_url}/terminal"),
                terminal_url=esc(f"{ticket_url}/terminal.json"),
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
    session_text = (
        f'<a href="{esc(discord_thread_url)}" target="_blank" rel="noopener noreferrer">'
        f"<code>{esc(session_id)}</code></a>"
        if discord_thread_url
        else f"<code>{esc(session_id)}</code>"
    )
    runtime_action = _runtime_action_form_html(
        session_id,
        runtime,
        return_to=board_url,
    )
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
        session_id = str(worker.get("thread_id") or "")
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
        session_text = (
            f'<a href="{esc(discord_thread_url)}" target="_blank" rel="noopener noreferrer">'
            f"<code>{esc(session_id)}</code></a>"
            if discord_thread_url
            else f"<code>{esc(session_id)}</code>"
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
    )
    worker = board.worker
    worker.update(
        {
            "root_goal": raw_goal,
            "goal_status": "active",
            "phase": "dev",
            "execution_mode": "kanban_pipeline",
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
) -> DiscordBoard:
    raw_request = _canonical_planner_request_text(request)
    board = ensure_discord_thread_board(
        thread_id=thread_id,
        chat_id=chat_id,
        guild_id=guild_id,
        parent_channel_id=parent_channel_id,
        initial_request=raw_request,
        project_context=project_context,
    )
    worker = board.worker
    previous_goal_status = str(worker.get("goal_status") or "").strip().lower()
    starts_new_goal_run = previous_goal_status in TERMINAL_GOAL_STATUSES
    planner_key = _planner_request_key(
        raw_request,
        request_id=request_id,
        include_request_id=starts_new_goal_run,
    )
    thread_context_text = str(thread_context or "").strip()
    worker.update(
        {
            "root_goal": raw_request,
            "latest_planner_request": raw_request,
            "latest_planner_request_key": planner_key,
            "goal_status": "active",
            "phase": "planning",
            "execution_mode": "kanban_pipeline",
            "paused": False,
            "cancelled": False,
        }
    )
    if thread_context_text:
        worker["latest_goal_thread_context"] = thread_context_text
    else:
        worker.pop("latest_goal_thread_context", None)
    metadata = _update_worker_meta(board.slug, worker)
    _ensure_planner_task(
        board.slug,
        metadata[DISCORD_WORKER_META_KEY],
        request=raw_request,
        request_key=planner_key,
        created_by=created_by,
        thread_context=thread_context_text,
        allow_existing=not starts_new_goal_run,
    )
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
        "Create tickets for the dev role; do not implement the work yourself.",
        "When you call kanban_create for a dev ticket, pass that brief in the kanban_create body argument; do not rely on the title, parent ticket, comments, or acceptance_criteria metadata alone.",
        "Each dev ticket body must be a detailed, self-contained implementation brief with labeled sections: Goal, Scope, Implementation notes, Ticket-specific acceptance criteria, Likely files/subsystems, Dependencies or handoffs, Verification, and Out of scope.",
        "Write acceptance criteria for the specific slice owned by that dev ticket; do not copy the whole board-level list into every task unless that ticket owns the whole outcome.",
        "Include enough surrounding context from the overall request for a fresh dev worker to execute the ticket without guessing, but keep the scope tight to the ticket.",
        "Do not create standalone discovery, audit, polish, or verification tickets unless that work is the user's explicit request or it blocks multiple implementation tickets; fold normal inspection and verification into the relevant implementation ticket.",
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
                _set_planner_thread_context(conn, existing, thread_context_text)
                return existing
        body = json.dumps(
            {
                "role": ROLE_PLANNER,
                "root_goal": worker.get("root_goal") or worker.get("initial_request") or "",
                "request": planner_request,
                "discord_thread_context": thread_context_text,
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
            title="Plan Discord implementation work",
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


def _set_planner_thread_context(conn, task_id: str, thread_context: str) -> None:
    if not thread_context:
        return
    row = conn.execute("SELECT body FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return
    try:
        payload = json.loads(row["body"] or "{}")
    except Exception:
        return
    if not isinstance(payload, dict) or str(payload.get("discord_thread_context") or "").strip():
        return
    payload["discord_thread_context"] = thread_context
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
        WHERE title = 'Plan Discord implementation work'
          AND assignee = ?
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
        return int(value or 5)
    except Exception:
        return 5


def _is_review_loop_limit_blocker(worker: dict[str, Any]) -> bool:
    reason = str(worker.get("blocked_reason") or "").strip().lower()
    return (
        reason == REVIEW_LOOP_LIMIT_BLOCKED_REASON
        and worker.get("goal_status") == "blocked"
    )


def status_line(board: str) -> str:
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
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


def pause_board(board: str, *, reason: str = "user-paused") -> None:
    worker = _read_worker_meta(board)
    phase = str(worker.get("phase") or "").strip()
    if phase and phase != "paused":
        worker["phase_before_pause"] = phase
    worker.update({"goal_status": "paused", "phase": "paused", "paused": True, "paused_reason": reason})
    _update_worker_meta(board, worker)


def resume_board(board: str) -> None:
    worker = _read_worker_meta(board)
    worker.update({"goal_status": "active", "phase": worker.get("phase_before_pause") or "planning", "paused": False})
    _update_worker_meta(board, worker)


def start_board(board: str) -> None:
    worker = _read_worker_meta(board)
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
    try:
        loops = max(0, int(worker.get("review_loop_count") or 0))
    except (TypeError, ValueError):
        loops = 0
    try:
        limit = max(0, int(worker.get("review_loop_limit") or _review_loop_limit()))
    except (TypeError, ValueError):
        limit = _review_loop_limit()
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
    worker = _read_worker_meta(board)
    worker.update({"goal_status": "cancelled", "phase": "cancelled", "cancelled": True, "paused": True})
    _update_worker_meta(board, worker)


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
            title=f"User subgoal {idx}",
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
        if loops >= int(worker.get("review_loop_limit") or 5):
            worker.update({
                "phase": "blocked",
                "goal_status": "blocked",
                "blocked_reason": "review loop limit reached",
                "terminal_reaction_sync_pending": True,
                "terminal_summary_sync_pending": True,
            })
            _update_worker_meta(board, worker)
            return "blocked_review_loop_limit"
        loops += 1
        worker["review_loop_count"] = loops
        worker["phase"] = "reviewing"
        _update_worker_meta(board, worker)
        kanban_db.create_task(
            conn,
            title=f"Review Discord implementation (loop {loops})",
            body=json.dumps(
                {
                    "role": ROLE_REVIEWER,
                    "root_goal": worker.get("root_goal") or worker.get("initial_request") or "",
                    "acceptance_criteria": worker.get("criteria") or [],
                    "review_loop": loops,
                    "loop_limit": int(worker.get("review_loop_limit") or 5),
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
    )


def is_paused_or_cancelled(board: str) -> bool:
    worker = _read_worker_meta(board)
    return bool(worker.get("paused") or worker.get("cancelled"))


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
        conn = kanban_db.connect(board=board)
        try:
            placeholders = ",".join("?" for _ in ROLE_ASSIGNEES)
            row = conn.execute(
                "SELECT COUNT(*) FROM tasks "
                "WHERE status = 'running' AND lower(assignee) IN "
                f"({placeholders})",
                tuple(sorted(ROLE_ASSIGNEES)),
            ).fetchone()
            running = int(row[0] or 0) if row else 0
        finally:
            conn.close()
        if running <= 0:
            continue
        targets.append(
            {
                "board": board,
                "thread_id": thread_id,
                "chat_id": str(worker.get("chat_id") or thread_id),
                "running": running,
            }
        )
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
    slug = board_slug_for_discord_thread(thread_id)
    if not create and not kanban_db.board_exists(slug):
        return None
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
        )
    return DiscordBoard(slug=slug, metadata=kanban_db.read_board_metadata(slug))
