"""Discord thread boards for durable Codex worker sessions.

This module is deliberately control-plane only. It creates and mutates Kanban
state for Discord project threads, but it does not call Hermes inference and it
does not expose Hermes tools to workers.
"""

from __future__ import annotations

import html
import hashlib
import json
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


DISCORD_WORKER_META_KEY = "discord_worker"
PUBLIC_TOKEN_BYTES = 24
ROLE_PLANNER = "planner"
ROLE_DEV = "dev"
ROLE_REVIEWER = "reviewer"
ROLE_ASSIGNEES = frozenset({ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER})
CODEX_STATE_MAX_EVENTS = 200
CODEX_STATE_MAX_TEXT_BYTES = 24_000
CODEX_STATE_LOG_TAIL_BYTES = 64_000
_POSIX_PATH_RE = re.compile(
    r"(?<![\w:/.-])/(?:home|Users|tmp|var|etc|opt|private|workspace|workspaces|mnt|srv|repo|root)"
    r"(?:/[^\s\"'<>),;{}\[\]]*)?"
)
_WINDOWS_PATH_RE = re.compile(r"(?<![\w:/.-])[A-Za-z]:\\[^\s\"'<>),;{}\[\]]+")


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
        "final_text": getattr(result, "final_text", ""),
        "error": getattr(result, "error", None),
        "interrupted": bool(getattr(result, "interrupted", False)),
        "timed_out": bool(getattr(result, "timed_out", False)),
        "should_retire": bool(getattr(result, "should_retire", False)),
        "tool_iterations": int(getattr(result, "tool_iterations", 0) or 0),
        "turn_id": getattr(result, "turn_id", None),
        "thread_id": getattr(result, "thread_id", None),
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
    slug = board_slug_for_discord_thread(thread_id)
    project_context = dict(project_context or {})
    project_path = str(project_context.get("project_path") or "").strip() or None
    token = _read_worker_meta(slug).get("share_token") or secrets.token_urlsafe(PUBLIC_TOKEN_BYTES)
    branch = f"discord/{thread_id}"
    metadata = kanban_db.create_board(
        slug,
        name=f"Discord {thread_id}",
        description=(initial_request or "")[:500],
        icon="",
        color="#22c55e",
    )
    worker = _read_worker_meta(slug)
    worker.update(
        {
            "kind": "discord_worker_board",
            "thread_id": str(thread_id),
            "chat_id": str(chat_id or ""),
            "guild_id": str(guild_id or ""),
            "parent_channel_id": str(parent_channel_id or ""),
            "initial_request": str(initial_request or worker.get("initial_request") or ""),
            "project_context": project_context,
            "project_path": project_path,
            "base_branch": worker.get("base_branch") or project_context.get("base_branch") or "main",
            "worker_branch": worker.get("worker_branch") or branch,
            "worktree_path": worker.get("worktree_path") or _default_worktree_path(project_path, str(thread_id)),
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
    _ensure_code_island(worker)
    metadata[DISCORD_WORKER_META_KEY] = worker
    metadata = _write_metadata(slug, metadata)
    return DiscordBoard(slug=slug, metadata=metadata)


def _ensure_code_island(worker: dict[str, Any]) -> None:
    project_path = str(worker.get("project_path") or "").strip()
    worktree_path = str(worker.get("worktree_path") or "").strip()
    branch = str(worker.get("worker_branch") or "").strip()
    base_branch = str(worker.get("base_branch") or "main").strip() or "main"
    if not project_path or not worktree_path or not branch or not os.path.isdir(project_path):
        return
    if os.path.isdir(worktree_path):
        worker["code_island_ready"] = True
        return
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
            worker.pop("code_island_error", None)
        else:
            worker["code_island_error"] = (result.stderr or result.stdout or "git worktree add failed").strip()
    except Exception as exc:
        worker["code_island_error"] = str(exc)


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
                    "latest_summary": summaries.get(task.id),
                }
            )
    finally:
        conn.close()
    return {
        "board": board,
        "name": metadata.get("name") or board,
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
        payload = {
            "board": board,
            "worker": _public_worker_meta(worker or _read_worker_meta(board)),
            "task": _task_state_dict(task),
            "current_run": _current_run_state(task, runs),
            "runs": [_run_state_dict(run) for run in runs],
            "events": [_event_state_dict(event) for event in events[-200:]],
            "worker_log_tail": kanban_db.read_worker_log(
                task_id,
                tail_bytes=CODEX_STATE_LOG_TAIL_BYTES,
                board=board,
            ),
            "codex_state": _ticket_codex_state(task_id, board=board),
        }
    finally:
        conn.close()
    return _redact_public_state(payload)


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
    ordered = ["triage", "todo", "ready", "running", "blocked", "done"]
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
    elif worker.get("paused") or worker.get("goal_status") == "paused" or worker.get("phase") == "paused":
        state = "paused"
        reason = str(worker.get("paused_reason") or "queue paused")
    elif running_count > 0:
        state = "running"
        reason = _running_status_text(running)
    elif int(counts.get("running") or 0) > 0:
        state = "stale"
        reason = "running ticket has no live worker"
    elif worker.get("goal_status") == "done" or worker.get("phase") == "complete":
        state = "done"
        reason = "complete"
    elif str(worker.get("blocked_reason") or "").strip() or worker.get("goal_status") == "blocked":
        state = "blocked"
        reason = str(worker.get("blocked_reason") or "blocked")
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


def render_public_session_board_html(session_id: str) -> str:
    snapshot = public_board_snapshot_for_session(session_id)
    return _render_public_board_html(snapshot)


def _workers_page_css() -> str:
    return """
    :root { color-scheme: dark; --bg: #041c1c; --panel: rgba(255, 230, 203, 0.055); --panel-strong: rgba(255, 230, 203, 0.11); --line: rgba(255, 230, 203, 0.18); --text: #ffe6cb; --muted: rgba(255, 230, 203, 0.62); --faint: rgba(255, 230, 203, 0.38); --green: #4ade80; --yellow: #ffbd38; --red: #fb7185; --blue: #7dd3fc; }
    * { box-sizing: border-box; }
    body { margin: 0; min-height: 100vh; font-family: ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    body::after { content: ""; position: fixed; inset: 0; pointer-events: none; opacity: 0.16; mix-blend-mode: color-dodge; background-image: url("data:image/svg+xml,%3Csvg viewBox='0 0 512 512' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' fill='%23eaeaea' filter='url(%23n)' opacity='0.45'/%3E%3C/svg%3E"); background-size: 512px 512px; }
    header { border-bottom: 1px solid var(--line); background: rgba(4, 28, 28, 0.88); backdrop-filter: blur(10px); padding: 18px clamp(16px, 3vw, 34px); position: sticky; top: 0; z-index: 5; }
    .brand { color: var(--text); display: inline-block; font-size: 12px; font-weight: 800; letter-spacing: 0.18em; line-height: 1.05; text-decoration: none; text-transform: uppercase; }
    .brand:hover { text-decoration: underline; text-underline-offset: 4px; }
    .hero { align-items: end; display: flex; flex-wrap: wrap; gap: 14px 24px; justify-content: space-between; margin-top: 18px; }
    h1 { font-size: clamp(28px, 5vw, 56px); line-height: 0.95; margin: 0; max-width: 1000px; text-transform: uppercase; }
    .subtle { color: var(--muted); font-size: 13px; }
    main { margin: 0 auto; max-width: 1480px; padding: 22px clamp(14px, 3vw, 34px) 38px; position: relative; z-index: 1; }
    a { color: inherit; }
    code { color: var(--blue); font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 0.88em; }
    .metrics { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); margin-bottom: 18px; }
    .metric, .board-card, .column, .criteria { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; }
    .metric { padding: 12px; }
    .metric strong { display: block; font-size: 24px; line-height: 1; }
    .metric span { color: var(--muted); display: block; font-size: 11px; letter-spacing: 0.12em; margin-top: 6px; text-transform: uppercase; }
    .board-list { display: grid; gap: 12px; list-style: none; margin: 0; padding: 0; }
    .board-card { overflow: hidden; padding: 0; }
    .board-card-head { align-items: start; display: grid; gap: 12px; grid-template-columns: 1fr auto; padding: 14px; }
    .board-title { font-size: 17px; font-weight: 760; line-height: 1.25; text-decoration: none; }
    .board-title:hover { text-decoration: underline; text-underline-offset: 3px; }
    .board-meta, .meta { color: var(--muted); display: flex; flex-wrap: wrap; gap: 8px 14px; font-size: 12px; margin-top: 8px; }
    .chips { display: flex; flex-wrap: wrap; gap: 7px; }
    .chip { border: 1px solid var(--line); border-radius: 999px; color: var(--muted); font-size: 11px; letter-spacing: 0.08em; padding: 4px 8px; text-transform: uppercase; white-space: nowrap; }
    .runtime { border-color: currentColor; color: var(--muted); }
    .runtime-running { color: var(--green); }
    .runtime-queued { color: var(--yellow); }
    .runtime-paused { color: var(--yellow); }
    .runtime-blocked, .runtime-cancelled, .runtime-stale { color: var(--red); }
    .runtime-done { color: var(--green); }
    .board-card-body { border-top: 1px solid var(--line); display: grid; gap: 12px; grid-template-columns: minmax(0, 1fr) auto; padding: 14px; }
    .status-grid { display: grid; gap: 8px; grid-template-columns: repeat(auto-fit, minmax(72px, 1fr)); }
    .status-cell { background: rgba(255, 230, 203, 0.04); border: 1px solid rgba(255, 230, 203, 0.09); border-radius: 6px; padding: 8px; }
    .status-cell b { display: block; font-size: 18px; line-height: 1; }
    .status-cell span { color: var(--faint); display: block; font-size: 10px; letter-spacing: 0.11em; margin-top: 5px; text-transform: uppercase; }
    .reason { color: var(--muted); font-size: 13px; margin-top: 10px; }
    .actions { align-self: start; display: flex; justify-content: end; }
    form { display: inline; margin: 0; }
    button, .button-link { align-items: center; background: var(--text); border: 1px solid var(--text); border-radius: 6px; color: var(--bg); cursor: pointer; display: inline-flex; font: inherit; font-size: 12px; font-weight: 720; gap: 6px; min-height: 34px; padding: 7px 11px; text-decoration: none; text-transform: uppercase; }
    button:hover, .button-link:hover { background: transparent; color: var(--text); }
    .board { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); }
    .column { min-height: 190px; overflow: hidden; }
    .column h2 { align-items: center; border-bottom: 1px solid var(--line); display: flex; font-size: 12px; justify-content: space-between; letter-spacing: 0.13em; margin: 0; padding: 11px 12px; text-transform: uppercase; }
    .column ul { list-style: none; margin: 0; padding: 10px; }
    .column li { margin-bottom: 8px; }
    .ticket { appearance: none; background: rgba(255, 230, 203, 0.055); border: 1px solid rgba(255, 230, 203, 0.12); border-radius: 6px; color: inherit; cursor: pointer; display: block; font: inherit; padding: 10px; text-align: left; width: 100%; }
    .ticket:hover { border-color: rgba(255, 230, 203, 0.38); }
    .ticket strong { display: block; font-size: 13px; line-height: 1.25; }
    .ticket p { color: var(--muted); font-size: 12px; margin: 7px 0 0; }
    .criteria { margin-bottom: 16px; padding: 14px; }
    .criteria strong { display: block; font-size: 12px; letter-spacing: 0.13em; margin-bottom: 8px; text-transform: uppercase; }
    .criteria ol { margin: 0; padding-left: 20px; }
    .criteria li { color: var(--muted); margin: 4px 0; }
    .modal { align-items: center; background: rgba(0, 0, 0, 0.68); display: none; inset: 0; justify-content: center; padding: 18px; position: fixed; z-index: 20; }
    .modal[aria-hidden="false"] { display: flex; }
    .modal-panel { background: #071f1f; border: 1px solid var(--line); border-radius: 8px; box-shadow: 0 24px 70px rgba(0, 0, 0, 0.46); max-height: min(88vh, 920px); max-width: min(980px, 96vw); min-width: min(760px, 96vw); overflow: hidden; }
    .modal-head { align-items: center; border-bottom: 1px solid var(--line); display: flex; gap: 16px; justify-content: space-between; padding: 14px 16px; }
    .modal-head h2 { font-size: 15px; margin: 0; text-transform: uppercase; }
    .modal-body { color: var(--text); font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; margin: 0; max-height: calc(min(88vh, 920px) - 58px); overflow: auto; padding: 14px; white-space: pre-wrap; word-break: break-word; }
    @media (max-width: 760px) { .board-card-head, .board-card-body { grid-template-columns: 1fr; } .actions { justify-content: start; } }
    """


def _render_public_board_html(snapshot: dict[str, Any]) -> str:
    worker = snapshot["worker"]
    tasks = snapshot["tasks"]
    counts = snapshot.get("counts") or {}
    runtime = snapshot.get("runtime") or {"state": "idle", "reason": "no active tickets"}
    session_id = str(snapshot.get("session_id") or worker.get("thread_id") or "")
    columns = ["triage", "todo", "ready", "running", "blocked", "done"]
    by_status = {status: [] for status in columns}
    for task in tasks:
        by_status.setdefault(task["status"], []).append(task)

    def esc(value: Any) -> str:
        return html.escape(str(value or ""))

    cards = []
    for status in columns:
        items = by_status.get(status, [])
        body = "\n".join(
            "<li><button type=\"button\" class=\"ticket\" data-ticket-id=\"{id}\" "
            "data-ticket-title=\"{title}\" data-ticket-state-url=\"{url}\">"
            "<strong>{title}</strong><br><code>{id}</code> {assignee}<p>{summary}</p>"
            "</button></li>".format(
                title=esc(item["title"]),
                id=esc(item["id"]),
                url=esc(
                    f"/workers/{quote(session_id, safe='')}/tickets/"
                    f"{quote(str(item['id']), safe='')}/state"
                ),
                assignee=esc(item["assignee"] or ""),
                summary=esc(item["latest_summary"] or ""),
            )
            for item in items
        )
        cards.append(f"<section class=\"column\"><h2>{esc(status)} <span>{len(items)}</span></h2><ul>{body}</ul></section>")
    criteria = "\n".join(
        f"<li>{esc(c.get('text') if isinstance(c, dict) else c)}</li>"
        for c in (worker.get("criteria") or [])
        if (c.get("active", True) if isinstance(c, dict) else True)
    )
    metric_cards = "".join(
        f'<div class="metric"><strong>{int(counts.get(status) or 0)}</strong><span>{esc(status)}</span></div>'
        for status in ("ready", "running", "blocked", "done")
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="15">
  <title>{esc(snapshot["name"])}</title>
  <style>
{_workers_page_css()}
  </style>
</head>
<body>
  <header>
    <a class="brand" href="/">Hermes<br>Kanban</a>
    <div class="hero">
      <div>
        <h1>{esc(snapshot["name"])}</h1>
        <div class="meta">
          <span>Status: {esc(_public_status_text(worker))}</span>
          <span>Branch: {esc(worker.get("worker_branch") or "")}</span>
          <span>PR: {esc(worker.get("pr_url") or "not opened")}</span>
          <span>Review: {esc(_public_review_text(worker))}</span>
          <span>Updated: {esc(_format_public_timestamp(worker.get("updated_at")) or "never")}</span>
        </div>
      </div>
      <div class="chips">
        <span class="chip runtime runtime-{esc(runtime.get("state"))}">{esc(runtime.get("state"))}</span>
        <span class="chip">{esc(runtime.get("reason"))}</span>
      </div>
    </div>
  </header>
  <main>
    <div class="metrics">{metric_cards}</div>
    <div class="criteria"><strong>Acceptance Criteria</strong><ol>{criteria}</ol></div>
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
      const emptyCodexMessage = "No Codex app-server internals captured for this ticket yet.";
      const hide = () => {{
        modal.setAttribute("aria-hidden", "true");
        body.textContent = "";
      }};
      const show = (label) => {{
        title.textContent = label ? label + " - Worker Internals" : "Worker Internals";
        body.textContent = "Loading...";
        modal.setAttribute("aria-hidden", "false");
      }};
      const value = (v) => {{
        if (v === null || v === undefined || v === "") return "-";
        if (typeof v === "object") return Object.entries(v).map(([k, item]) => `${{k}}=${{value(item)}}`).join(", ");
        return String(v);
      }};
      const time = (v) => {{
        if (!v) return "-";
        const d = new Date(Number(v) * 1000);
        return Number.isNaN(d.getTime()) ? value(v) : d.toISOString().replace("T", " ").replace(".000Z", " UTC");
      }};
      const lines = [];
      const push = (label, v) => lines.push(`${{label}}: ${{value(v)}}`);
      const section = (label) => {{
        if (lines.length) lines.push("");
        lines.push(label.toUpperCase());
        lines.push("-".repeat(label.length));
      }};
      const interestingText = (payload) => {{
        const item = payload?.params?.item || payload?.item || payload || {{}};
        return item.aggregatedOutput || item.output || item.text || item.content || item.summary || item.message || "";
      }};
      const renderState = (state) => {{
        lines.length = 0;
        const task = state?.task || {{}};
        const run = state?.current_run || {{}};
        section("Ticket");
        push("id", task.id);
        push("title", task.title);
        push("status", task.status);
        push("assignee", task.assignee);
        push("failure", task.last_failure_error);
        push("result", task.result);

        section("Current run");
        push("run", run.id);
        push("status", run.status || run.outcome);
        push("pid", run.worker_pid || task.worker_pid);
        push("started", time(run.started_at || task.started_at));
        push("heartbeat", time(run.last_heartbeat_at || task.last_heartbeat_at));
        push("ended", time(run.ended_at));
        push("summary", run.summary);
        push("error", run.error);

        const codex = state?.codex_state || {{}};
        section("Codex result");
        if (codex.available === false) {{
          lines.push(codex.message || emptyCodexMessage);
        }} else {{
          const result = codex.result || {{}};
          push("final_text", result.final_text);
          push("error", result.error);
          push("interrupted", result.interrupted);
          push("timed_out", result.timed_out);
          push("tool_iterations", result.tool_iterations);
          push("turn_id", result.turn_id);
          push("thread_id", result.thread_id);
        }}

        const events = Array.isArray(codex.events) ? codex.events.slice(-20) : [];
        section("Recent internals");
        if (!events.length) {{
          lines.push("No captured Codex events.");
        }} else {{
          for (const event of events) {{
            const text = interestingText(event.payload);
            lines.push(`[${{time(event.ts)}}] ${{value(event.method)}} ${{value(event.item_type)}}`);
            if (text) lines.push(text);
          }}
        }}

        section("Worker log");
        lines.push(state?.worker_log_tail || "No worker log captured.");
        return lines.join("\\n");
      }};
      document.querySelectorAll("[data-ticket-state-url]").forEach((button) => {{
        button.addEventListener("click", async () => {{
          show(button.dataset.ticketTitle || button.dataset.ticketId || "Ticket State");
          try {{
            const response = await fetch(button.dataset.ticketStateUrl, {{
              headers: {{ "Accept": "application/json" }},
            }});
            if (!response.ok) {{
              throw new Error(`HTTP ${{response.status}}`);
            }}
            const state = await response.json();
            body.textContent = renderState(state);
          }} catch (error) {{
            body.textContent = `Unable to load ticket state: ${{error}}`;
          }}
        }});
      }});
      close.addEventListener("click", hide);
      modal.addEventListener("click", (event) => {{
        if (event.target === modal) hide();
      }});
      document.addEventListener("keydown", (event) => {{
        if (event.key === "Escape") hide();
      }});
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
                "name": board.get("name") or slug,
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

    def status_grid(counts: dict[str, Any]) -> str:
        return "".join(
            '<div class="status-cell"><b>{count}</b><span>{label}</span></div>'.format(
                count=int(counts.get(status) or 0),
                label=esc(status),
            )
            for status in ("ready", "running", "blocked", "done")
        )

    items = []
    for board in snapshot["boards"]:
        worker = board.get("worker") or {}
        session_id = str(board.get("session_id") or "")
        href = f"/workers/{quote(session_id, safe='')}" if session_id else ""
        counts = board.get("counts") or {}
        running = board.get("running") if isinstance(board.get("running"), list) else []
        runtime = board.get("runtime") if isinstance(board.get("runtime"), dict) else {}
        running_text = _running_status_text(running)
        runtime_state = str(runtime.get("state") or "idle")
        runtime_reason = str(runtime.get("reason") or running_text)
        runtime_label = "Queue" if runtime_state == "queued" else "Status"
        title = esc(worker.get("root_goal") or worker.get("initial_request") or board.get("name"))
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
        control = str(runtime.get("control") or "none")
        control_label = str(runtime.get("control_label") or "")
        if control == "resume":
            primary_action = (
                f'<form method="post" action="/workers/{quote(session_id, safe="")}/start">'
                f'<button type="submit">{esc(control_label or "Resume")}</button></form>'
            )
        elif control == "start":
            primary_action = (
                f'<form method="post" action="/workers/{quote(session_id, safe="")}/start">'
                f'<button type="submit">{esc(control_label or "Start")}</button></form>'
            )
        elif control == "pause":
            primary_action = (
                f'<form method="post" action="/workers/{quote(session_id, safe="")}/pause">'
                f'<button type="submit">{esc(control_label or "Pause")}</button></form>'
            )
        else:
            primary_action = ""
        items.append(
            '<li class="board-card">'
            '<div class="board-card-head">'
            '<div><a class="board-title" href="{href}">{link_text}</a>'
            '<div class="board-meta">{session}<span>{status}</span><span>{branch}</span><span>{mode}</span><span>PR: {pr}</span><span>Review: {review}</span></div>'
            '</div>'
            '<div class="chips"><span class="chip runtime runtime-{runtime_class}">{runtime}</span></div>'
            '</div>'
            '<div class="board-card-body">'
            '<div><div class="status-grid">{status_grid}</div>'
            '<div class="reason">{reason_label}: {reason}</div>'
            '<div class="reason">Running: {running}</div>'
            '<div class="reason">{timestamps}</div>'
            '{flags}'
            '</div>'
            '<div class="actions">{action}</div>'
            '</div>'
            '</li>'.format(
                href=esc(href or "#"),
                link_text=title,
                session=session_text,
                status=esc(status),
                runtime=esc(runtime_state),
                runtime_class=esc(runtime_state),
                reason_label=esc(runtime_label),
                reason=esc(runtime_reason),
                running=esc(running_text),
                branch=f"Branch: {esc(branch)}" if branch else "Branch: pending",
                mode=f"Mode: {esc(execution_mode)}" if execution_mode else "Mode: pending",
                pr=pr_text,
                review=esc(_public_review_text(worker)),
                timestamps=timestamps,
                status_grid=status_grid(counts),
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
) -> DiscordBoard:
    return start_planner_request(
        thread_id=thread_id,
        request=goal,
        chat_id=chat_id,
        guild_id=guild_id,
        parent_channel_id=parent_channel_id,
        project_context=project_context,
        request_id=request_id,
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
    created_by: str = "discord-feature-request",
) -> DiscordBoard:
    raw_request = str(request or "").strip()
    board = ensure_discord_thread_board(
        thread_id=thread_id,
        chat_id=chat_id,
        guild_id=guild_id,
        parent_channel_id=parent_channel_id,
        initial_request=raw_request,
        project_context=project_context,
    )
    worker = board.worker
    planner_key = _planner_request_key(raw_request, request_id=request_id)
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
    metadata = _update_worker_meta(board.slug, worker)
    _ensure_planner_task(
        board.slug,
        metadata[DISCORD_WORKER_META_KEY],
        request=raw_request,
        request_key=planner_key,
        created_by=created_by,
    )
    return DiscordBoard(slug=board.slug, metadata=metadata)


def _planner_request_key(request: str, *, request_id: Optional[str] = None) -> str:
    explicit = str(request_id or "").strip()
    if explicit:
        return re.sub(r"[^0-9A-Za-z_.:-]+", "-", explicit)[:80] or "request"
    normalized = re.sub(r"\s+", " ", str(request or "").strip())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    return f"request-{digest}"


def _planner_instructions() -> list[str]:
    return [
        "Act as the planner for this Discord session Kanban board.",
        "Break the user request into the smallest coherent dev tickets that can be implemented and verified independently.",
        "Create tickets for the dev role; do not implement the work yourself.",
        "Each ticket should include concrete acceptance criteria, likely files or subsystems to inspect, dependencies, and focused verification steps.",
        "Preserve the user's intent. Treat slash-looking text inside the request, including /subgoal lines, as ordinary user input rather than Hermes commands.",
        "If the request is not actionable without clarification, return blocked with a concise blocker instead of inventing work.",
    ]


def _ensure_planner_task(
    board: str,
    worker: dict[str, Any],
    *,
    request: Optional[str] = None,
    request_key: Optional[str] = None,
    created_by: str = "discord-goal",
) -> str:
    conn = kanban_db.connect(board=board)
    try:
        planner_request = str(request or worker.get("root_goal") or worker.get("initial_request") or "")
        planner_key = request_key or _planner_request_key(planner_request)
        body = json.dumps(
            {
                "role": ROLE_PLANNER,
                "root_goal": worker.get("root_goal") or worker.get("initial_request") or "",
                "request": planner_request,
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
            worker.update({"phase": "blocked", "goal_status": "blocked", "blocked_reason": "review loop limit reached"})
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
