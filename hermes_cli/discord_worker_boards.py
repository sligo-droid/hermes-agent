"""Discord thread boards for durable Codex worker sessions.

This module is deliberately control-plane only. It creates and mutates Kanban
state for Discord project threads, but it does not call Hermes inference and it
does not expose Hermes tools to workers.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db
from utils import atomic_json_write


DISCORD_WORKER_META_KEY = "discord_worker"
PUBLIC_TOKEN_BYTES = 24
ROLE_PLANNER = "planner"
ROLE_DEV = "dev"
ROLE_REVIEWER = "reviewer"
ROLE_ASSIGNEES = frozenset({ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER})


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
    for key in (
        "HERMES_PUBLIC_KANBAN_BASE_URL",
        "HERMES_DASHBOARD_PUBLIC_URL",
        "HERMES_DASHBOARD_URL",
        "PUBLIC_URL",
    ):
        value = str(os.getenv(key) or "").strip()
        if value:
            return value.rstrip("/")
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        value = (
            ((cfg.get("kanban") or {}).get("discord_worker") or {}).get("public_base_url")
            or ((cfg.get("dashboard") or {}).get("public_base_url"))
            or ""
        )
        if value:
            return str(value).strip().rstrip("/")
    except Exception:
        pass
    return ""


def public_board_url(token: str) -> str:
    path = f"/public/kanban/{token}"
    base = _public_base_url()
    return f"{base}{path}" if base else path


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
            "public_url": public_board_url(str(token)),
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
    metadata = kanban_db.read_board_metadata(board)
    worker = dict(metadata.get(DISCORD_WORKER_META_KEY) or {})
    conn = kanban_db.connect(board=board)
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        summaries = kanban_db.latest_summaries(conn, [t.id for t in tasks])
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
        "worker": _public_worker_meta(worker),
        "tasks": rows,
    }


def _public_worker_meta(worker: dict[str, Any]) -> dict[str, Any]:
    safe = dict(worker)
    safe.pop("share_token", None)
    return safe


def render_public_board_html(token: str) -> str:
    snapshot = public_board_snapshot(token)
    worker = snapshot["worker"]
    tasks = snapshot["tasks"]
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
            "<li><strong>{title}</strong><br><code>{id}</code> {assignee}<p>{summary}</p></li>".format(
                title=esc(item["title"]),
                id=esc(item["id"]),
                assignee=esc(item["assignee"] or ""),
                summary=esc(item["latest_summary"] or ""),
            )
            for item in items
        )
        cards.append(f"<section><h2>{esc(status)} <span>{len(items)}</span></h2><ul>{body}</ul></section>")
    criteria = "\n".join(
        f"<li>{esc(c.get('text') if isinstance(c, dict) else c)}</li>"
        for c in (worker.get("criteria") or [])
        if (c.get("active", True) if isinstance(c, dict) else True)
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(snapshot["name"])}</title>
  <style>
    body {{ margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #f7f7f5; color: #1f2933; }}
    header {{ padding: 24px 28px; border-bottom: 1px solid #d7d7d2; background: #ffffff; }}
    h1 {{ margin: 0 0 8px; font-size: 24px; }}
    .meta {{ display: flex; flex-wrap: wrap; gap: 12px; color: #52606d; font-size: 14px; }}
    main {{ padding: 20px; }}
    .criteria {{ margin-bottom: 18px; }}
    .board {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }}
    section {{ background: #fff; border: 1px solid #d7d7d2; border-radius: 8px; min-height: 180px; }}
    h2 {{ margin: 0; padding: 12px; font-size: 14px; text-transform: uppercase; border-bottom: 1px solid #e6e6e2; display: flex; justify-content: space-between; }}
    ul {{ list-style: none; margin: 0; padding: 10px; }}
    li {{ border: 1px solid #e6e6e2; border-radius: 6px; padding: 10px; margin-bottom: 8px; background: #fbfbfa; }}
    code {{ color: #5965f2; }}
    p {{ margin: 8px 0 0; color: #52606d; font-size: 13px; }}
  </style>
</head>
<body>
  <header>
    <h1>{esc(snapshot["name"])}</h1>
    <div class="meta">
      <span>Status: {esc(worker.get("goal_status") or worker.get("phase") or "pending")}</span>
      <span>Branch: {esc(worker.get("worker_branch") or "")}</span>
      <span>PR: {esc(worker.get("pr_url") or "not opened")}</span>
      <span>Review loops: {esc(worker.get("review_loop_count") or 0)}</span>
    </div>
  </header>
  <main>
    <div class="criteria"><strong>Acceptance Criteria</strong><ol>{criteria}</ol></div>
    <div class="board">{''.join(cards)}</div>
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
) -> DiscordBoard:
    board = ensure_discord_thread_board(
        thread_id=thread_id,
        chat_id=chat_id,
        guild_id=guild_id,
        parent_channel_id=parent_channel_id,
        initial_request=goal,
        project_context=project_context,
    )
    worker = board.worker
    worker.update(
        {
            "root_goal": goal.strip(),
            "goal_status": "active",
            "phase": "planning",
            "execution_mode": "kanban_pipeline",
            "paused": False,
            "cancelled": False,
        }
    )
    metadata = _update_worker_meta(board.slug, worker)
    _ensure_planner_task(board.slug, metadata[DISCORD_WORKER_META_KEY])
    return DiscordBoard(slug=board.slug, metadata=metadata)


def _ensure_planner_task(board: str, worker: dict[str, Any]) -> str:
    conn = kanban_db.connect(board=board)
    try:
        body = json.dumps(
            {
                "role": ROLE_PLANNER,
                "root_goal": worker.get("root_goal") or worker.get("initial_request") or "",
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
            created_by="discord-goal",
            workspace_kind="dir",
            workspace_path=str(worker.get("worktree_path") or ""),
            tenant=board,
            priority=100,
            idempotency_key=f"{board}:planner:root",
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
    worker.update({"goal_status": "paused", "phase": "paused", "paused": True, "paused_reason": reason})
    _update_worker_meta(board, worker)


def resume_board(board: str) -> None:
    worker = _read_worker_meta(board)
    worker.update({"goal_status": "active", "phase": worker.get("phase_before_pause") or "planning", "paused": False})
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


def is_paused_or_cancelled(board: str) -> bool:
    worker = _read_worker_meta(board)
    return bool(worker.get("paused") or worker.get("cancelled"))


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
    thread_id = str(getattr(source, "thread_id", "") or getattr(source, "chat_id", "") or "")
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
