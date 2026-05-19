"""Codex worker launcher for Discord Kanban role lanes."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli.discord_worker_boards import ROLE_ASSIGNEES


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _worker_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        return dict(((cfg.get("kanban") or {}).get("discord_worker") or {}))
    except Exception:
        return {}


def _runner_kind(cfg: dict[str, Any]) -> str:
    raw = os.getenv("HERMES_CODEX_WORKER_RUNNER") or cfg.get("runner") or "host"
    runner = str(raw).strip().lower()
    return runner if runner in {"host", "docker"} else "host"


def spawn_codex_worker(task: Any, workspace: str, *, board: Optional[str] = None) -> Optional[int]:
    """Spawn a Codex worker for planner/dev/reviewer tasks.

    Returns the host-side subprocess pid so the existing Kanban crash detector
    can observe the worker lifecycle. Durable state lives in the board DB and
    mounted worktree.
    """
    role = str(getattr(task, "assignee", "") or "").strip().lower()
    if role not in ROLE_ASSIGNEES:
        return None
    cfg = _worker_config()
    if _runner_kind(cfg) == "docker":
        return _spawn_docker_worker(task, workspace, cfg=cfg, board=board)
    return _spawn_host_worker(task, workspace, cfg=cfg, board=board)


def _spawn_host_worker(
    task: Any,
    workspace: str,
    *,
    cfg: dict[str, Any],
    board: Optional[str],
) -> Optional[int]:
    workspace_path = Path(workspace).expanduser().resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)

    codex_home = _codex_home(task, cfg)
    _write_minimal_codex_home(codex_home)

    from hermes_cli import kanban_db

    board_db = str(kanban_db.kanban_db_path(board=board))
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env.update(
        {
            "HERMES_KANBAN_TASK": str(getattr(task, "id", "")),
            "HERMES_KANBAN_BOARD": str(board or ""),
            "HERMES_KANBAN_DB": board_db,
            "HERMES_KANBAN_WORKSPACE": str(workspace_path),
            "HERMES_KANBAN_WORKSPACES_ROOT": str(kanban_db.workspaces_root(board=board)),
            "HERMES_CODEX_WORKER_ROLE": str(getattr(task, "assignee", "") or "").strip().lower(),
            "CODEX_HOME": str(codex_home),
            "PYTHONPATH": (
                f"{_repo_root()}{os.pathsep}{existing_pythonpath}"
                if existing_pythonpath else str(_repo_root())
            ),
        }
    )
    if getattr(task, "claim_lock", None):
        env["HERMES_KANBAN_CLAIM_LOCK"] = str(task.claim_lock)
    if getattr(task, "current_run_id", None) is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)

    cmd = [sys.executable, "-m", "hermes_cli.kanban_codex_worker"]
    return _spawn_logged_process(task, cmd, str(workspace_path), env, board=board)


def _spawn_docker_worker(
    task: Any,
    workspace: str,
    *,
    cfg: dict[str, Any],
    board: Optional[str],
) -> Optional[int]:
    role = str(getattr(task, "assignee", "") or "").strip().lower()
    image = str(
        cfg.get("docker_image")
        or os.getenv("HERMES_CODEX_WORKER_IMAGE")
        or "ghcr.io/nousresearch/hermes-codex-worker:latest"
    )
    docker_bin = str(cfg.get("docker_bin") or "docker")
    workspace_path = Path(workspace).expanduser().resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)

    codex_home = _codex_home(task, cfg)
    _write_minimal_codex_home(codex_home)

    env = os.environ.copy()
    env.update(
        {
            "HERMES_KANBAN_TASK": str(getattr(task, "id", "")),
            "HERMES_KANBAN_BOARD": str(board or ""),
            "HERMES_KANBAN_WORKSPACE": str(workspace_path),
            "HERMES_CODEX_WORKER_ROLE": role,
            "CODEX_HOME": "/codex-home",
            "PYTHONPATH": "/hermes",
        }
    )
    if getattr(task, "claim_lock", None):
        env["HERMES_KANBAN_CLAIM_LOCK"] = str(task.claim_lock)
    if getattr(task, "current_run_id", None) is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)

    from hermes_cli import kanban_db

    board_db = str(kanban_db.kanban_db_path(board=board))
    env["HERMES_KANBAN_DB"] = board_db
    env["HERMES_KANBAN_WORKSPACES_ROOT"] = str(kanban_db.workspaces_root(board=board))

    cmd = [
        docker_bin,
        "run",
        "--rm",
        "--network=host",
        "-v",
        f"{workspace_path}:/workspace",
        "-v",
        f"{Path(board_db).parent.resolve()}:{Path(board_db).parent.resolve()}",
        "-v",
        f"{_repo_root()}:/hermes:ro",
        "-v",
        f"{codex_home}:/codex-home",
        "-w",
        "/workspace",
    ]
    for key, value in env.items():
        if key.startswith(
            ("HERMES_", "CODEX_", "OPENAI_", "ANTHROPIC_", "GH_", "GITHUB_")
        ) or key in {"PYTHONPATH", "HOME"}:
            cmd.extend(["-e", f"{key}={value}"])
    cmd.extend([image, "python", "-m", "hermes_cli.kanban_codex_worker"])

    return _spawn_logged_process(task, cmd, str(workspace_path), env, board=board)


def _codex_home(task: Any, cfg: dict[str, Any]) -> Path:
    root = Path(
        cfg.get("codex_home_root")
        or (Path.home() / ".hermes" / "codex-worker-homes")
    )
    path = root / str(getattr(task, "id", "task"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _spawn_logged_process(
    task: Any,
    cmd: list[str],
    workspace: str,
    env: dict[str, str],
    *,
    board: Optional[str],
) -> Optional[int]:
    from hermes_cli import kanban_db

    log_path = kanban_db.worker_log_path(str(getattr(task, "id", "")), board=board)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    kanban_db._append_worker_log_line(
        log_path,
        f"[kanban dispatcher] spawning Codex role worker: {' '.join(cmd)}",
    )
    log_f = open(log_path, "ab")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workspace,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            env=env,
            start_new_session=True,
        )
    finally:
        log_f.close()

    time.sleep(0.2)
    exit_code = proc.poll()
    if exit_code not in (None, 0):
        if _task_still_running(str(getattr(task, "id", "")), board=board):
            raise RuntimeError(
                f"Codex role worker exited immediately with code {exit_code}; "
                f"check log: {log_path}"
            )
    return int(proc.pid)


def _task_still_running(task_id: str, *, board: Optional[str]) -> bool:
    if not task_id:
        return False
    try:
        from hermes_cli import kanban_db

        conn = kanban_db.connect(board=board)
        try:
            task = kanban_db.get_task(conn, task_id)
            return task is not None and task.status == "running"
        finally:
            conn.close()
    except Exception:
        return True


def _write_minimal_codex_home(path: Path) -> None:
    """Create a Codex home with auth but no Hermes MCP/tool bridge."""
    config = path / "config.toml"
    source_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()
    for name in ("auth.json", "credentials.json"):
        src = source_home / name
        dst = path / name
        if src.exists() and not dst.exists():
            try:
                shutil.copy2(src, dst)
            except OSError:
                pass
    if config.exists():
        return
    config.write_text(
        "\n".join(
            [
                'sandbox_mode = "workspace-write"',
                'approval_policy = "never"',
                "",
            ]
        ),
        encoding="utf-8",
    )


def spawn_or_default(task: Any, workspace: str, *, board: Optional[str] = None) -> Optional[int]:
    """Dispatch role-lane tasks to Codex, everything else to legacy Kanban."""
    role = str(getattr(task, "assignee", "") or "").strip().lower()
    if role in ROLE_ASSIGNEES:
        return spawn_codex_worker(task, workspace, board=board)
    from hermes_cli import kanban_db

    return kanban_db._default_spawn(task, workspace, board=board)
