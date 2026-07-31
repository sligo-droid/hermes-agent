"""Codex worker launcher for Discord Kanban role lanes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli.discord_worker_boards import ROLE_ASSIGNEES, ROLE_DEV, ROLE_FOREMAN, ROLE_PLANNER, ROLE_REVIEWER

_OPENCODE_ROLES = {ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER}
_SENSITIVE_ENV_FRAGMENTS = ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "ACCESS_KEY")
_CODEX_WORKER_ENV_KEYS = (
    "CODEX_HOME",
    "HERMES_CODEX_WORKER_CREDENTIAL_ID",
    "HERMES_CODEX_WORKER_CLEANUP_HOME",
    "HERMES_CODEX_WORKER_CLEANUP_ROOT",
    "HERMES_CODEX_WORKER_CONTAINER_CODEX_HOME",
    "HERMES_CODEX_WORKER_TIER",
    "HERMES_CODEX_WORKER_TIER_SOURCE",
)
_WORKER_CONTAINER_ENV_PREFIXES = (
    "HERMES_",
    "CODEX_",
    "OPENAI_",
    "ANTHROPIC_",
    "GH_",
    "GITHUB_",
    "VITE_",
    "PUBLIC_",
    "NEXT_PUBLIC_",
)
_WORKER_CONTAINER_ENV_KEYS = {
    "PYTHONPATH",
    "HOME",
    "PID_QA_USERNAME",
    "PID_QA_PASSWORD",
    "PID_QA_EXPECT_READONLY",
    "PID_QA_BASE_URL",
    "PID_QA_PATH",
    "PID_QA_ENV_FILE",
}
_DASHBOARD_QA_USERNAME = "hermes_qa"

_ROLE_DEFAULT_REASONING = {
    "planner": "xhigh",
    "dev": "medium",
    "foreman": "xhigh",
    "reviewer": "xhigh",
}
_VALID_REASONING_LEVELS = {"minimal", "low", "medium", "high", "xhigh", "max"}
_AUTO_RUNTIME = "auto"
_WORKER_SCRIPT = Path("hermes_cli") / "kanban_codex_worker.py"
_CONTAINER_WORKER_SCRIPT = "/hermes/hermes_cli/kanban_codex_worker.py"


def _repo_root() -> Path:
    """Return the Hermes runtime source root for worker imports.

    Role workers execute with their project worktree as cwd. For Hermes
    self-improvement tasks that worktree is also a Hermes checkout, so Python's
    default import path can otherwise shadow the canonical runtime checkout.
    Prefer the source root that owns the current venv interpreter, then fall
    back to this module's location for non-worktree installs.
    """
    try:
        venv_dir = Path(sys.executable).resolve().parent.parent
        if venv_dir.name in {".venv", "venv"}:
            runtime_root = venv_dir.parent
            if (runtime_root / "hermes_cli").is_dir():
                return runtime_root
    except OSError:
        pass
    return Path(__file__).resolve().parent.parent


def _host_worker_cmd() -> list[str]:
    return [sys.executable, str(_repo_root() / _WORKER_SCRIPT)]


def _worker_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        worker_cfg = dict(((cfg.get("kanban") or {}).get("discord_worker") or {}))
        model_tiers = cfg.get("model_tiers") if isinstance(cfg, dict) else None
        if isinstance(model_tiers, dict):
            worker_cfg["model_tiers"] = model_tiers
        return worker_cfg
    except Exception:
        return {}


def _runner_kind(cfg: dict[str, Any]) -> str:
    raw = os.getenv("HERMES_CODEX_WORKER_RUNNER") or cfg.get("runner") or "host"
    runner = str(raw).strip().lower()
    return runner if runner in {"host", "docker"} else "host"


def _excluded_worker_workspaces(cfg: dict[str, Any]) -> set[Path]:
    raw = cfg.get("excluded_workspaces")
    if isinstance(raw, str):
        values = raw.split(os.pathsep)
    elif isinstance(raw, (list, tuple, set)):
        values = raw
    else:
        values = []
    excluded: set[Path] = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        excluded.add(Path(text).expanduser().resolve())
    return excluded


def _ensure_workspace_not_excluded(workspace_path: Path, cfg: dict[str, Any]) -> None:
    if workspace_path in _excluded_worker_workspaces(cfg):
        raise RuntimeError(f"Discord worker workspace is quarantined and not selectable: {workspace_path}")


def _coding_backend(cfg: dict[str, Any]) -> str:
    try:
        from agent.opencode_worker import load_coding_worker_backend

        return load_coding_worker_backend(worker_config=cfg)
    except Exception:
        return "opencode"


def _task_forces_opencode(task: Any = None) -> bool:
    """Return whether a task must bypass the configured coding backend.

    No current task type forces OpenCode. Command Center repair work follows the
    same configured coding-worker backend as every other role lane.
    """
    return False


def _role_backend(role: str, configured_backend: str, task: Any = None) -> str:
    if _task_forces_opencode(task):
        return "opencode"
    if configured_backend == "opencode":
        return "opencode"
    return "codex"


def _role_runtime_settings(
    role: str,
    cfg: dict[str, Any],
    task: Any = None,
) -> dict[str, Any]:
    backend = _role_backend(role, _coding_backend(cfg), task)
    roles = cfg.get("roles") if isinstance(cfg.get("roles"), dict) else {}
    role_cfg = roles.get(role) if isinstance(roles.get(role), dict) else {}
    try:
        from hermes_cli.model_tiers import (
            require_worker_model_tier,
            restrict_model_tier_for_task,
        )

        model_tier = require_worker_model_tier(cfg, role_cfg.get("model_tier"))
        purpose = "review" if role == "reviewer" else "implementation"
        model_tier = restrict_model_tier_for_task(
            cfg,
            model_tier,
            task,
            purpose=purpose,
        )
    except ValueError:
        raise
    except Exception:
        model_tier = None

    raw_reasoning = _config_value_with_auto_env(
        "HERMES_CODEX_WORKER_REASONING",
        model_tier.reasoning_effort if model_tier is not None else role_cfg.get("reasoning"),
    )
    reasoning_source = (
        "model_tier"
        if model_tier is not None
        else ("explicit" if raw_reasoning is not None else "adaptive")
    )
    if raw_reasoning is None or _is_auto(raw_reasoning):
        reasoning = _adaptive_reasoning(role, task)
    else:
        from hermes_constants import normalize_reasoning_effort

        reasoning = normalize_reasoning_effort(raw_reasoning)
        if backend == "opencode":
            if reasoning == "ultra":
                reasoning = "max"
        elif reasoning in {"max", "ultra"}:
            reasoning = "xhigh"
        if reasoning not in _VALID_REASONING_LEVELS:
            reasoning = _ROLE_DEFAULT_REASONING.get(role, "medium")
            reasoning_source = "default"
    if role != "reviewer" and reasoning == "xhigh":
        reasoning = "high"
        if reasoning_source != "model_tier":
            reasoning_source = "review_only_cap"

    configured_tier = _first_configured_value(
        role_cfg.get("service_tier"),
        role_cfg.get("mode"),
        role_cfg.get("fast"),
        cfg.get("service_tier"),
        cfg.get("mode"),
        cfg.get("fast"),
    )
    raw_tier = _config_value_with_auto_env(
        "HERMES_CODEX_WORKER_SERVICE_TIER",
        role_cfg.get("service_tier"),
        role_cfg.get("mode"),
        role_cfg.get("fast"),
        cfg.get("service_tier"),
        cfg.get("mode"),
        cfg.get("fast"),
    )
    if model_tier is not None and (
        configured_tier is None or _is_auto(configured_tier)
    ):
        tier = "fast" if model_tier.fast_mode else "normal"
        tier_source = "model_tier"
    else:
        tier_source = "explicit" if raw_tier is not None else "adaptive"
        tier = (
            _adaptive_service_tier(role, task)
            if raw_tier is None or _is_auto(raw_tier)
            else _normalize_service_tier(raw_tier)
        )
    return {
        "reasoning": reasoning,
        "reasoning_source": reasoning_source,
        "model_tier": model_tier.name if model_tier is not None else "",
        "model_tier_source": "role" if model_tier is not None else "none",
        "model": model_tier.model if model_tier is not None else "",
        "opencode_model": model_tier.opencode_model if model_tier is not None else "",
        "fast_mode": model_tier.fast_mode if model_tier is not None else tier == "fast",
        "service_tier": tier,
        "service_tier_source": tier_source,
        "mode": tier,
    }


def _first_configured_value(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _config_value_with_auto_env(env_key: str, *values: Any) -> Any:
    configured = _first_configured_value(*values)
    if configured is not None and not _is_auto(configured):
        return configured
    env_value = _first_configured_value(os.getenv(env_key))
    return env_value if env_value is not None else configured


def _is_auto(value: Any) -> bool:
    return str(value).strip().lower() == _AUTO_RUNTIME


def _adaptive_reasoning(role: str, task: Any = None) -> str:
    if role in {ROLE_PLANNER, ROLE_REVIEWER, ROLE_FOREMAN}:
        return "xhigh"
    if role != ROLE_DEV:
        return _ROLE_DEFAULT_REASONING.get(role, "medium")
    if _task_needs_escalation(task):
        return "xhigh"
    if _task_is_risky(task):
        return "high"
    return "medium"


def _adaptive_service_tier(role: str, task: Any = None) -> str:
    if role == ROLE_DEV and not _task_is_risky(task) and not _task_needs_escalation(task):
        return "fast"
    return "normal"


def _task_text(task: Any = None) -> str:
    if task is None:
        return ""
    return "\n".join(
        str(part or "")
        for part in (
            getattr(task, "title", ""),
            getattr(task, "body", ""),
            getattr(task, "result", ""),
            getattr(task, "last_failure_error", ""),
        )
    )


def _task_is_risky(task: Any = None) -> bool:
    try:
        from agent.opencode_worker import looks_complex_or_risky

        return looks_complex_or_risky(_task_text(task))
    except Exception:
        lower = _task_text(task).lower()
        return any(
            signal in lower
            for signal in (
                "security",
                "auth",
                "migration",
                "performance",
                "reviewer",
                "planner",
            )
        )


def _task_needs_escalation(task: Any = None) -> bool:
    if task is None:
        return False
    if int(getattr(task, "consecutive_failures", 0) or 0) > 0:
        return True
    if str(getattr(task, "last_failure_error", "") or "").strip():
        return True
    created_by = str(getattr(task, "created_by", "") or "").strip().lower()
    if created_by == "reviewer":
        return True
    lower = _task_text(task).lower()
    return any(
        phrase in lower
        for phrase in (
            "changes requested",
            "reviewer requested",
            "review feedback",
            "follow-up",
            "retry",
        )
    )


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
    if role == ROLE_PLANNER or settings.get("model_tier"):
        reasoning = settings.get("reasoning") or pass_cfg["complex_plan_reasoning_level"]
    elif settings.get("reasoning_source") == "adaptive":
        reasoning = settings.get("reasoning", pass_cfg["simple_build_reasoning_level"])
    else:
        reasoning = (
            f"simple={pass_cfg['simple_build_reasoning_level']},"
            f"plan={pass_cfg['complex_plan_reasoning_level']},"
            f"build={pass_cfg['complex_build_reasoning_level']}"
        )
    return {
        **settings,
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


def _strip_inherited_codex_worker_state(env: dict[str, str]) -> None:
    for key in _CODEX_WORKER_ENV_KEYS:
        env.pop(key, None)


def _configure_discord_read_broker(env: dict[str, str]) -> None:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        return
    from hermes_cli.discord_worker_read import start_read_broker

    base_url, access_token = start_read_broker(token)
    env["HERMES_DISCORD_WORKER_READ_URL"] = base_url
    env["HERMES_DISCORD_WORKER_READ_TOKEN"] = access_token
    env["HERMES_DISCORD_WORKER_CONTROL_URL"] = base_url
    env["HERMES_DISCORD_WORKER_CONTROL_TOKEN"] = access_token


def _config_env_value(key: str) -> str:
    try:
        from hermes_cli.config import get_env_value

        return (get_env_value(key) or "").strip()
    except Exception:
        return ""


def _configure_dashboard_qa_auth(env: dict[str, str]) -> None:
    username = (
        env.get("HERMES_DASHBOARD_USERNAME")
        or _config_env_value("HERMES_DASHBOARD_USERNAME")
    ).strip()
    env["HERMES_DASHBOARD_USERNAME"] = username or _DASHBOARD_QA_USERNAME
    if not env.get("HERMES_DASHBOARD_PASSWORD"):
        password = _config_env_value("HERMES_DASHBOARD_PASSWORD")
        if password:
            env["HERMES_DASHBOARD_PASSWORD"] = password


def _configure_pid_qa_auth(env: dict[str, str]) -> None:
    """Keep PID QA workers on the read-only account contract by default.

    PID's own ``qa:auth`` launcher loads the canonical read-only credential
    file when the two credential variables are absent.  Preserve explicitly
    forwarded credentials for that launcher, but never inherit an admin-mode
    switch from the long-lived gateway environment.
    """
    env.pop("PID_QA_EXPECT_ADMIN", None)
    env["PID_QA_EXPECT_READONLY"] = "true"


def _worker_env() -> dict[str, str]:
    env = os.environ.copy()
    _strip_discord_credentials(env)
    _strip_inherited_codex_worker_state(env)
    _configure_discord_read_broker(env)
    _configure_dashboard_qa_auth(env)
    _configure_pid_qa_auth(env)
    return env


def _codex_home_source_env() -> dict[str, str]:
    env = os.environ.copy()
    if env.get("HERMES_CODEX_WORKER_CREDENTIAL_ID"):
        _strip_inherited_codex_worker_state(env)
    return env


def _is_sensitive_env_key(key: str) -> bool:
    upper = key.upper()
    return any(fragment in upper for fragment in _SENSITIVE_ENV_FRAGMENTS)


def _forward_env_to_worker_container(key: str) -> bool:
    return (
        key.startswith(_WORKER_CONTAINER_ENV_PREFIXES)
        or key in _WORKER_CONTAINER_ENV_KEYS
    )


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


def spawn_codex_worker(task: Any, workspace: str, *, board: Optional[str] = None) -> Optional[Any]:
    """Spawn a coding worker for planner/dev/reviewer/foreman tasks.

    Returns the host-side subprocess pid or systemd unit handle so the existing
    Kanban crash detector can observe the worker lifecycle. Durable state lives
    in the board DB and mounted worktree.
    """
    role = str(getattr(task, "assignee", "") or "").strip().lower()
    if role not in ROLE_ASSIGNEES:
        return None
    cfg = _worker_config()
    _ensure_workspace_not_excluded(Path(workspace).expanduser().resolve(), cfg)
    configured_backend = _coding_backend(cfg)
    backend = _role_backend(role, configured_backend, task)
    settings = _role_runtime_settings(role, cfg, task)
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
            "set kanban.discord_worker.runner=host or explicitly use "
            "coding_worker.backend=codex for the legacy container path."
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
) -> Optional[Any]:
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
            "HERMES_CODEX_WORKER_REASONING_SOURCE": settings["reasoning_source"],
            "HERMES_CODEX_WORKER_MODEL_TIER": settings["model_tier"],
            "HERMES_CODEX_WORKER_MODEL_TIER_SOURCE": settings.get(
                "model_tier_source", "none"
            ),
            "HERMES_CODEX_WORKER_MODEL": settings["model"],
            "HERMES_OPENCODE_WORKER_MODEL": settings["opencode_model"],
            "HERMES_CODEX_WORKER_SERVICE_TIER": settings["service_tier"],
            "HERMES_CODEX_WORKER_SERVICE_TIER_SOURCE": settings["service_tier_source"],
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
        env["HERMES_CODEX_WORKER_CLEANUP_HOME"] = "1"
        env["HERMES_CODEX_WORKER_CLEANUP_ROOT"] = str(codex_home.parent)
    if inherited_credential_id:
        env["HERMES_CODEX_WORKER_CREDENTIAL_ID"] = inherited_credential_id
    gh_config_dir = _github_cli_config_dir(env)
    if gh_config_dir:
        env["GH_CONFIG_DIR"] = gh_config_dir

    cmd = _host_worker_cmd()
    return _spawn_logged_process(
        task,
        cmd,
        str(workspace_path),
        env,
        settings=log_settings,
        backend=backend,
        board=board,
        use_systemd=True,
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
            "HERMES_CODEX_WORKER_REASONING_SOURCE": settings["reasoning_source"],
            "HERMES_CODEX_WORKER_MODEL_TIER": settings["model_tier"],
            "HERMES_CODEX_WORKER_MODEL_TIER_SOURCE": settings.get(
                "model_tier_source", "none"
            ),
            "HERMES_CODEX_WORKER_MODEL": settings["model"],
            "HERMES_OPENCODE_WORKER_MODEL": settings["opencode_model"],
            "HERMES_CODEX_WORKER_SERVICE_TIER": settings["service_tier"],
            "HERMES_CODEX_WORKER_SERVICE_TIER_SOURCE": settings["service_tier_source"],
            "HERMES_CODING_WORKER_BACKEND": backend,
            "CODEX_HOME": "/codex-home",
            "HERMES_CODEX_WORKER_CONTAINER_CODEX_HOME": "1",
            "PYTHONPATH": "/hermes",
        }
    )
    if getattr(task, "claim_lock", None):
        env["HERMES_KANBAN_CLAIM_LOCK"] = str(task.claim_lock)
    if getattr(task, "current_run_id", None) is not None:
        env["HERMES_KANBAN_RUN_ID"] = str(task.current_run_id)
    if inherited_credential_id:
        env["HERMES_CODEX_WORKER_CREDENTIAL_ID"] = inherited_credential_id
    else:
        env["HERMES_CODEX_WORKER_CLEANUP_HOME"] = "1"
        env["HERMES_CODEX_WORKER_CLEANUP_ROOT"] = "/codex-home"
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
        if _forward_env_to_worker_container(key):
            cmd.extend(["-e", key])
    cmd.extend([image, "python", _CONTAINER_WORKER_SCRIPT])

    return _spawn_logged_process(
        task,
        cmd,
        str(workspace_path),
        env,
        settings=log_settings,
        backend=backend,
        board=board,
        use_systemd=False,
    )


def _codex_home(task: Any, cfg: dict[str, Any]) -> Path:
    from hermes_constants import get_hermes_home

    root = Path(
        cfg.get("codex_home_root")
        or (get_hermes_home() / "tmp" / "codex-worker-homes")
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
    use_systemd: bool,
) -> Optional[Any]:
    from hermes_cli import kanban_db

    log_path = kanban_db.worker_log_path(str(getattr(task, "id", "")), board=board)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    role = str(getattr(task, "assignee", "") or "").strip().lower()
    label = "OpenCode" if backend == "opencode" else "Codex"
    selected_model = (
        settings.get("opencode_model") if backend == "opencode" else settings.get("model")
    )
    kanban_db._append_worker_log_line(
        log_path,
        f"[kanban dispatcher] scheduled {label} role worker: "
        f"role={role or '-'} reasoning={settings['reasoning']} mode={settings['mode']} "
        f"model={selected_model or '-'} "
        f"tier={settings.get('model_tier') or '-'} "
        f"tier_source={settings.get('model_tier_source') or 'none'} "
        f"reasoning_source={settings.get('reasoning_source') or 'default'} "
        f"service_tier={settings.get('service_tier') or settings.get('mode') or 'normal'} "
        f"service_tier_source={settings.get('service_tier_source') or 'default'}",
    )
    kanban_db._append_worker_log_line(
        log_path,
        f"[kanban dispatcher] spawning {label} role worker: {_redacted_command(cmd, env)}",
    )
    if use_systemd and kanban_db._should_use_systemd_worker():
        unit_name = kanban_db._systemd_worker_unit_name(task, board=board)
        try:
            return kanban_db._spawn_systemd_worker(
                cmd=cmd,
                workspace=workspace,
                env=env,
                log_path=log_path,
                unit_name=unit_name,
            )
        except Exception as exc:
            kanban_db._append_worker_log_line(
                log_path,
                f"[kanban dispatcher] systemd-run role worker launch failed; "
                f"falling back to direct spawn: {exc}",
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

    return prepare_codex_worker_home(
        path,
        source_env=_codex_home_source_env(),
        prefer_pool=True,
    )


def spawn_or_default(task: Any, workspace: str, *, board: Optional[str] = None) -> Optional[int]:
    """Dispatch role-lane tasks to the configured coding worker backend."""
    role = str(getattr(task, "assignee", "") or "").strip().lower()
    if role in ROLE_ASSIGNEES:
        return spawn_codex_worker(task, workspace, board=board)
    from hermes_cli import kanban_db

    return kanban_db._default_spawn(task, workspace, board=board)
