"""Codex worker launcher for Discord Kanban role lanes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli.discord_worker_boards import ROLE_ASSIGNEES, ROLE_DEV, ROLE_PLANNER

_OPENCODE_ROLES = {ROLE_PLANNER, ROLE_DEV}
_SENSITIVE_ENV_FRAGMENTS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "ACCESS_KEY")

_ROLE_DEFAULT_REASONING = {
    "planner": "high",
    "dev": "medium",
    "reviewer": "high",
}


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


def _coding_backend(cfg: dict[str, Any]) -> str:
    try:
        from agent.opencode_worker import load_coding_worker_backend

        return load_coding_worker_backend(worker_config=cfg)
    except Exception:
        return "codex"


def _role_backend(role: str, configured_backend: str) -> str:
    if configured_backend == "opencode" and role in _OPENCODE_ROLES:
        return "opencode"
    return "codex"


def _role_runtime_settings(role: str, cfg: dict[str, Any]) -> dict[str, str]:
    roles = cfg.get("roles") if isinstance(cfg.get("roles"), dict) else {}
    role_cfg = roles.get(role) if isinstance(roles.get(role), dict) else {}

    reasoning = str(
        os.getenv("HERMES_CODEX_WORKER_REASONING")
        or role_cfg.get("reasoning")
        or _ROLE_DEFAULT_REASONING.get(role)
        or "medium"
    ).strip().lower()
    if reasoning not in {"minimal", "low", "medium", "high", "xhigh"}:
        reasoning = _ROLE_DEFAULT_REASONING.get(role, "medium")

    raw_tier = (
        os.getenv("HERMES_CODEX_WORKER_SERVICE_TIER")
        or role_cfg.get("service_tier")
        or role_cfg.get("mode")
        or role_cfg.get("fast")
        or cfg.get("service_tier")
        or cfg.get("mode")
        or cfg.get("fast")
        or "normal"
    )
    tier = _normalize_service_tier(raw_tier)
    return {"reasoning": reasoning, "service_tier": tier, "mode": tier}


def _role_log_settings(
    role: str,
    cfg: dict[str, Any],
    *,
    backend: str,
    settings: dict[str, str],
) -> dict[str, str]:
    if backend != "opencode":
        return settings
    try:
        from agent.opencode_worker import load_coding_worker_pass_config

        pass_cfg = load_coding_worker_pass_config(worker_config=cfg)
    except Exception:
        return settings
    if role == ROLE_PLANNER:
        reasoning = pass_cfg["complex_plan_reasoning_level"]
    else:
        reasoning = (
            f"simple={pass_cfg['simple_build_reasoning_level']},"
            f"plan={pass_cfg['complex_plan_reasoning_level']},"
            f"build={pass_cfg['complex_build_reasoning_level']}"
        )
    return {
        "reasoning": reasoning,
        "service_tier": settings.get("service_tier", "normal"),
        "mode": settings.get("mode", "normal"),
    }


def _normalize_service_tier(value: Any) -> str:
    if isinstance(value, bool):
        return "fast" if value else "normal"
    raw = str(value or "").strip().lower()
    if raw in {"fast", "priority", "on", "true", "1", "yes"}:
        return "fast"
    return "normal"


def _github_cli_config_dir(env: dict[str, str]) -> Optional[str]:
    try:
        from hermes_constants import get_github_cli_config_dir

        return get_github_cli_config_dir(env)
    except Exception:
        return None


def _strip_discord_credentials(env: dict[str, str]) -> None:
    for key in list(env):
        if key.startswith("DISCORD_"):
            env.pop(key, None)


def _configure_discord_read_broker(env: dict[str, str]) -> None:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return
    from hermes_cli.discord_worker_read import start_read_broker

    base_url, access_token = start_read_broker(token)
    env["HERMES_DISCORD_WORKER_READ_URL"] = base_url
    env["HERMES_DISCORD_WORKER_READ_TOKEN"] = access_token


def _worker_env() -> dict[str, str]:
    env = os.environ.copy()
    _strip_discord_credentials(env)
    _configure_discord_read_broker(env)
    return env


def _is_sensitive_env_key(key: str) -> bool:
    upper = key.upper()
    return any(fragment in upper for fragment in _SENSITIVE_ENV_FRAGMENTS)


def _redacted_command(cmd: list[str], env: dict[str, str]) -> str:
    secret_values = {
        value
        for key, value in env.items()
        if value and _is_sensitive_env_key(key) and len(value) >= 4
    }
    rendered = []
    for item in cmd:
        redacted = item
        for value in secret_values:
            if value in redacted:
                redacted = redacted.replace(value, "[REDACTED]")
        rendered.append(redacted)
    return " ".join(rendered)


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
    configured_backend = _coding_backend(cfg)
    backend = _role_backend(role, configured_backend)
    settings = _role_runtime_settings(role, cfg)
    log_settings = _role_log_settings(role, cfg, backend=backend, settings=settings)
    if backend == "opencode":
        try:
            from agent.opencode_worker import check_opencode_binary

            ok, detail = check_opencode_binary()
        except Exception as exc:
            ok, detail = False, str(exc)
        if not ok:
            raise RuntimeError(detail)
    if backend == "opencode" and _runner_kind(cfg) == "docker":
        raise RuntimeError(
            "OpenCode worker backend currently supports only host runner; "
            "set kanban.discord_worker.runner=host or coding_worker.backend=codex."
        )
    if _runner_kind(cfg) == "docker":
        return _spawn_docker_worker(
            task,
            workspace,
            cfg=cfg,
            settings=settings,
            log_settings=log_settings,
            backend=backend,
            board=board,
        )
    return _spawn_host_worker(
        task,
        workspace,
        cfg=cfg,
        settings=settings,
        log_settings=log_settings,
        backend=backend,
        board=board,
    )


def _spawn_host_worker(
    task: Any,
    workspace: str,
    *,
    cfg: dict[str, Any],
    settings: dict[str, str],
    log_settings: dict[str, str],
    backend: str,
    board: Optional[str],
) -> Optional[int]:
    workspace_path = Path(workspace).expanduser().resolve()
    workspace_path.mkdir(parents=True, exist_ok=True)

    codex_home = None
    inherited_credential_id = None
    if backend == "codex":
        codex_home = _codex_home(task, cfg)
        inherited_credential_id = _write_minimal_codex_home(codex_home)

    from hermes_cli import kanban_db

    board_db = str(kanban_db.kanban_db_path(board=board))
    env = _worker_env()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env.update(
        {
            "HERMES_KANBAN_TASK": str(getattr(task, "id", "")),
            "HERMES_KANBAN_BOARD": str(board or ""),
            "HERMES_KANBAN_DB": board_db,
            "HERMES_KANBAN_WORKSPACE": str(workspace_path),
            "HERMES_KANBAN_WORKSPACES_ROOT": str(kanban_db.workspaces_root(board=board)),
            "HERMES_CODEX_WORKER_ROLE": str(getattr(task, "assignee", "") or "").strip().lower(),
            "HERMES_CODEX_WORKER_REASONING": settings["reasoning"],
            "HERMES_CODEX_WORKER_SERVICE_TIER": settings["service_tier"],
            "HERMES_CODING_WORKER_BACKEND": backend,
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
    if codex_home is not None:
        env["CODEX_HOME"] = str(codex_home)
    if inherited_credential_id:
        env["HERMES_CODEX_WORKER_CREDENTIAL_ID"] = inherited_credential_id
    gh_config_dir = _github_cli_config_dir(env)
    if gh_config_dir:
        env["GH_CONFIG_DIR"] = gh_config_dir

    cmd = [sys.executable, "-m", "hermes_cli.kanban_codex_worker"]
    return _spawn_logged_process(
        task,
        cmd,
        str(workspace_path),
        env,
        settings=log_settings,
        backend=backend,
        board=board,
    )


def _spawn_docker_worker(
    task: Any,
    workspace: str,
    *,
    cfg: dict[str, Any],
    settings: dict[str, str],
    log_settings: dict[str, str],
    backend: str,
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
    inherited_credential_id = _write_minimal_codex_home(codex_home)

    env = _worker_env()
    env.update(
        {
            "HERMES_KANBAN_TASK": str(getattr(task, "id", "")),
            "HERMES_KANBAN_BOARD": str(board or ""),
            "HERMES_KANBAN_WORKSPACE": str(workspace_path),
            "HERMES_CODEX_WORKER_ROLE": role,
            "HERMES_CODEX_WORKER_REASONING": settings["reasoning"],
            "HERMES_CODEX_WORKER_SERVICE_TIER": settings["service_tier"],
            "HERMES_CODING_WORKER_BACKEND": backend,
            "CODEX_HOME": "/codex-home",
            "PYTHONPATH": "/hermes",
        }
    )
    if getattr(task, "claim_lock", None):
        env["HERMES_KANBAN_CLAIM_LOCK"] = str(task.claim_lock)
    if getattr(task, "current_run_id", None) is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if inherited_credential_id:
        env["HERMES_CODEX_WORKER_CREDENTIAL_ID"] = inherited_credential_id
    host_gh_config_dir = _github_cli_config_dir(env)

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
    if host_gh_config_dir and Path(host_gh_config_dir).is_dir():
        env["GH_CONFIG_DIR"] = "/gh-config"
        cmd.extend(["-v", f"{Path(host_gh_config_dir).resolve()}:/gh-config:ro"])
    for key in env:
        if key.startswith(
            ("HERMES_", "CODEX_", "OPENAI_", "ANTHROPIC_", "GH_", "GITHUB_")
        ) or key in {"PYTHONPATH", "HOME"}:
            cmd.extend(["-e", key])
    cmd.extend([image, "python", "-m", "hermes_cli.kanban_codex_worker"])

    return _spawn_logged_process(
        task,
        cmd,
        str(workspace_path),
        env,
        settings=log_settings,
        backend=backend,
        board=board,
    )


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
    settings: dict[str, str],
    backend: str,
    board: Optional[str],
) -> Optional[int]:
    from hermes_cli import kanban_db

    log_path = kanban_db.worker_log_path(str(getattr(task, "id", "")), board=board)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    role = str(getattr(task, "assignee", "") or "").strip().lower()
    label = "OpenCode" if backend == "opencode" else "Codex"
    kanban_db._append_worker_log_line(
        log_path,
        f"[kanban dispatcher] scheduled {label} role worker: "
        f"role={role or '-'} reasoning={settings['reasoning']} mode={settings['mode']}",
    )
    kanban_db._append_worker_log_line(
        log_path,
        f"[kanban dispatcher] spawning {label} role worker: {_redacted_command(cmd, env)}",
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
                f"{label} role worker exited immediately with code {exit_code}; "
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


def _write_minimal_codex_home(path: Path) -> Optional[str]:
    """Create a Codex home with auth but no Hermes MCP/tool bridge."""
    from agent.codex_worker_auth import prepare_codex_worker_home

    return prepare_codex_worker_home(path)


def spawn_or_default(task: Any, workspace: str, *, board: Optional[str] = None) -> Optional[int]:
    """Dispatch role-lane tasks to Codex, everything else to legacy Kanban."""
    role = str(getattr(task, "assignee", "") or "").strip().lower()
    if role in ROLE_ASSIGNEES:
        return spawn_codex_worker(task, workspace, board=board)
    from hermes_cli import kanban_db

    return kanban_db._default_spawn(task, workspace, board=board)
