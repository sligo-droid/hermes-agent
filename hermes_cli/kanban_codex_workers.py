"""Codex worker launcher for Discord Kanban role lanes."""

from __future__ import annotations

import os
import shutil
import subprocess
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


def spawn_codex_worker(task: Any, workspace: str, *, board: Optional[str] = None) -> Optional[int]:
    """Spawn a Docker-backed Codex worker for planner/dev/reviewer tasks.

    Returns the host-side docker subprocess pid so the existing Kanban crash
    detector can observe the worker lifecycle. The container process itself is
    disposable; durable state lives in the board DB and mounted worktree.
    """
    role = str(getattr(task, "assignee", "") or "").strip().lower()
    if role not in ROLE_ASSIGNEES:
        return None
    cfg = _worker_config()
    image = str(
        cfg.get("docker_image")
        or os.getenv("HERMES_CODEX_WORKER_IMAGE")
        or "ghcr.io/nousresearch/hermes-codex-worker:latest"
    )
    docker_bin = str(cfg.get("docker_bin") or "docker")
    workspace_path = Path(workspace).expanduser().resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)

    codex_home = (
        Path(cfg.get("codex_home_root") or (Path.home() / ".hermes" / "codex-worker-homes"))
        / str(getattr(task, "id", "task"))
    )
    codex_home.mkdir(parents=True, exist_ok=True)
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

    board_db = os.environ.get("HERMES_KANBAN_DB")
    if not board_db:
        from hermes_cli import kanban_db

        board_db = str(kanban_db.kanban_db_path(board=board))
    env["HERMES_KANBAN_DB"] = board_db

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

    proc = subprocess.Popen(
        cmd,
        cwd=str(workspace_path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return int(proc.pid)


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
