"""OpenCode non-PTY coding worker backend."""

from __future__ import annotations

import copy
import json
import logging
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


BACKEND_CODEX = "codex"
BACKEND_OPENCODE = "opencode"
_VALID_BACKENDS = {BACKEND_CODEX, BACKEND_OPENCODE}
_VALID_REASONING_LEVELS = {"minimal", "low", "medium", "high", "xhigh", "max"}
_DEFAULT_STARTUP_TIMEOUT_SECONDS = 0.0
_DEFAULT_OPENCODE_MODEL = "hermes-codex/gpt-5.6-sol"
_CODING_WORKER_PASS_NAMES = ("simple_build", "complex_plan", "complex_build")

logger = logging.getLogger(__name__)


@dataclass
class OpenCodeRunResult:
    final_text: str = ""
    error: Optional[str] = None
    interrupted: bool = False
    timed_out: bool = False
    should_retire: bool = False
    tool_iterations: int = 0
    turn_id: Optional[str] = None
    thread_id: Optional[str] = None
    backend: str = BACKEND_OPENCODE
    agents: list[str] = field(default_factory=list)
    plan_text: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    exit_code: Optional[int] = None
    duration_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    run_profile: dict[str, Any] = field(default_factory=dict)
    export_status: dict[str, Any] = field(default_factory=dict)
    no_final_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class _OpenCodeProcessResult:
    returncode: Optional[int]
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    startup_timed_out: bool = False
    duration_seconds: float = 0.0


def normalize_coding_worker_backend(value: Any) -> str:
    raw = str(value or "").strip().lower()
    return raw if raw in _VALID_BACKENDS else BACKEND_CODEX


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _bool_config(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raw = str(value).strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def _normalize_service_tier(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if raw in {"fast", "priority", "on", "true", "1", "yes"}:
        return "fast"
    if raw in {"normal", "off", "false", "0", "no"}:
        return "normal"
    return None


def _direct_opencode_model(value: Any) -> str:
    model = str(value or "").strip()
    if not model:
        return _DEFAULT_OPENCODE_MODEL
    return model


def _inline_worker_brief(model: Any) -> bool:
    """Return True when the selected OpenCode provider rejects file parts.

    The local ``hermes-codex`` OpenCode provider proxies Codex-style text
    turns. Sending the worker brief through ``opencode run --file`` produces a
    server-side ``UnknownError`` before inference, while the same brief in the
    text message succeeds. Keep file attachments for providers that support
    them, but inline for this provider so the coding worker smoke is a real
    inference check instead of a file-upload compatibility check.
    """
    return str(model or "").strip().startswith("hermes-codex/")


def _worker_brief_message(prompt: str) -> str:
    return (
        "Follow this Hermes worker brief exactly. Return only your final "
        "answer unless the brief asks for a structured response.\n\n"
        "Hermes worker brief:\n"
        f"{prompt.rstrip()}"
    )


def _opencode_provider_id(model: Any) -> str:
    raw = str(model or "").strip()
    return raw.split("/", 1)[0] if "/" in raw else ""


def _read_opencode_user_config() -> dict[str, Any]:
    config_base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    config_dir = config_base / "opencode"
    for name in ("opencode.json", "opencode.jsonc"):
        path = config_dir / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            parsed = json.loads(_strip_jsonc(text) if path.suffix == ".jsonc" else text)
        except (OSError, json.JSONDecodeError) as exc:
            warnings.warn(
                f"Ignoring invalid OpenCode config {path}: {exc}",
                RuntimeWarning,
                stacklevel=2,
            )
            continue
        if isinstance(parsed, dict):
            return parsed
        warnings.warn(
            f"Ignoring unsupported OpenCode config {path}: expected JSON object.",
            RuntimeWarning,
            stacklevel=2,
        )
    return {}


def _strip_jsonc(text: str) -> str:
    """Strip JSONC comments and trailing commas without touching strings."""
    out: list[str] = []
    in_string = False
    escape = False

    def next_significant_index(index: int) -> int:
        while index < len(text):
            char = text[index]
            nxt = text[index + 1] if index + 1 < len(text) else ""
            if char.isspace():
                index += 1
                continue
            if char == "/" and nxt == "/":
                index += 2
                while index < len(text) and text[index] not in "\r\n":
                    index += 1
                continue
            if char == "/" and nxt == "*":
                index += 2
                while index + 1 < len(text) and not (
                    text[index] == "*" and text[index + 1] == "/"
                ):
                    index += 1
                index = min(index + 2, len(text))
                continue
            break
        return index

    i = 0
    while i < len(text):
        char = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if in_string:
            out.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            i += 1
            continue
        if char == "/" and nxt == "/":
            i += 2
            while i < len(text) and text[i] not in "\r\n":
                i += 1
            continue
        if char == "/" and nxt == "*":
            i += 2
            while i + 1 < len(text) and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i = min(i + 2, len(text))
            continue
        if char == "," and next_significant_index(i + 1) < len(text):
            if text[next_significant_index(i + 1)] in "}]":
                i += 1
                continue
        out.append(char)
        i += 1

    return "".join(out)


def _opencode_provider_config_for_model(model: Any) -> dict[str, Any]:
    provider_id = _opencode_provider_id(model)
    if not provider_id:
        return {}
    providers = _read_opencode_user_config().get("provider")
    if not isinstance(providers, dict):
        return {}
    provider = providers.get(provider_id)
    return provider if isinstance(provider, dict) else {}


def load_coding_worker_backend(
    config: Optional[dict[str, Any]] = None,
    *,
    worker_config: Optional[dict[str, Any]] = None,
) -> str:
    """Resolve coding worker backend.

    Precedence:
      1. HERMES_CODING_WORKER_BACKEND
      2. kanban.discord_worker.backend (passed as worker_config)
      3. coding_worker.backend
      4. codex
    """
    raw_env = os.getenv("HERMES_CODING_WORKER_BACKEND")
    if raw_env:
        return normalize_coding_worker_backend(raw_env)

    if worker_config and worker_config.get("backend"):
        return normalize_coding_worker_backend(worker_config.get("backend"))

    cfg = config
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except Exception:
            cfg = {}
    if isinstance(cfg, dict):
        coding_cfg = cfg.get("coding_worker") if isinstance(cfg.get("coding_worker"), dict) else {}
        if coding_cfg.get("backend"):
            return normalize_coding_worker_backend(coding_cfg.get("backend"))
    return BACKEND_CODEX


def load_coding_worker_model_tier(
    config: Optional[dict[str, Any]] = None,
    *,
    worker_config: Optional[dict[str, Any]] = None,
) -> Any:
    """Resolve the named tier for ordinary delegated coding work."""
    cfg = config
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except Exception:
            cfg = {}
    if not isinstance(cfg, dict):
        return None

    coding_cfg = cfg.get("coding_worker") if isinstance(cfg.get("coding_worker"), dict) else {}
    tier_name = coding_cfg.get("model_tier")
    if worker_config and "model_tier" in worker_config:
        tier_name = worker_config.get("model_tier")
    try:
        from hermes_cli.model_tiers import resolve_model_tier

        return resolve_model_tier(cfg, tier_name)
    except Exception:
        return None


def _coding_worker_model_tiers_disabled(
    coding_cfg: dict[str, Any], worker_config: Optional[dict[str, Any]]
) -> bool:
    source = worker_config if worker_config is not None and "model_tier" in worker_config else coding_cfg
    return str(source.get("model_tier") or "").strip().lower() in {"disabled", "off"}


def load_coding_worker_pass_profiles(
    config: Optional[dict[str, Any]] = None,
    *,
    worker_config: Optional[dict[str, Any]] = None,
    task: Any = "",
    context: Any = "",
) -> dict[str, dict[str, str]]:
    """Resolve each coding-worker pass to one atomic model tier or raw fallback."""
    cfg = config
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except Exception:
            cfg = {}
    cfg = cfg if isinstance(cfg, dict) else {}
    coding_cfg = cfg.get("coding_worker") if isinstance(cfg.get("coding_worker"), dict) else {}
    worker_cfg = worker_config or {}
    raw_opencode = coding_cfg.get("opencode") if isinstance(coding_cfg.get("opencode"), dict) else {}
    worker_opencode = worker_cfg.get("opencode") if isinstance(worker_cfg.get("opencode"), dict) else {}
    explicit_model = str(worker_opencode.get("model") or "").strip()
    legacy_model = str(explicit_model or raw_opencode.get("model") or _DEFAULT_OPENCODE_MODEL).strip()
    global_tier_name = worker_cfg.get("model_tier") if "model_tier" in worker_cfg else coding_cfg.get("model_tier")
    tiers_disabled = _coding_worker_model_tiers_disabled(coding_cfg, worker_config)

    from hermes_cli.model_tiers import (
        require_worker_model_tier,
        restrict_model_tier_for_task,
        restrict_reasoning_effort_for_task,
    )

    per_call_tier_keys = {
        "model_tier",
        "simple_build_reasoning_level",
        "complex_plan_reasoning_level",
        "complex_build_reasoning_level",
    }
    explicit_call_tier = bool(
        worker_config is not None
        and "model_tier" in worker_config
        and str(worker_config.get("model_tier") or "").strip()
        and set(worker_config).issubset(per_call_tier_keys)
    )

    profiles: dict[str, dict[str, str]] = {}
    legacy_efforts = {
        "simple_build": "medium",
        "complex_plan": "xhigh",
        "complex_build": "medium",
    }
    for pass_name in _CODING_WORKER_PASS_NAMES:
        tier = None
        if not tiers_disabled:
            pass_tier_key = f"{pass_name}_model_tier"
            tier_name = (
                worker_cfg.get(pass_tier_key)
                if pass_tier_key in worker_cfg
                else global_tier_name or coding_cfg.get(pass_tier_key)
            )
            tier = require_worker_model_tier(cfg, tier_name)
        reasoning_key = f"{pass_name}_reasoning_level"
        explicit_reasoning = worker_cfg.get(reasoning_key) if reasoning_key in worker_cfg else None
        configured_reasoning = coding_cfg.get(reasoning_key)
        reasoning = (
            explicit_reasoning
            if explicit_reasoning is not None
            else tier.reasoning_effort if tier is not None else configured_reasoning or legacy_efforts[pass_name]
        )
        reasoning = _normalize_reasoning_level(reasoning)
        safe_reasoning = (
            reasoning
            if explicit_call_tier
            else restrict_reasoning_effort_for_task(
                reasoning,
                task,
                context,
            )
        )
        if safe_reasoning != str(reasoning or "").strip().lower():
            tier = restrict_model_tier_for_task(
                cfg,
                tier,
                task,
                context,
            )
        reasoning = safe_reasoning
        model = explicit_model or (tier.opencode_model if tier is not None else legacy_model)
        profiles[pass_name] = {
            "model_tier": tier.name if tier is not None else "",
            "model": _direct_opencode_model(model),
            "codex_model": tier.model if tier is not None else "",
            "reasoning_level": _normalize_reasoning_level(reasoning),
        }
    return profiles


def load_coding_worker_pass_config(
    config: Optional[dict[str, Any]] = None,
    *,
    worker_config: Optional[dict[str, Any]] = None,
    task: Any = "",
    context: Any = "",
) -> dict[str, Any]:
    cfg = config
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except Exception:
            cfg = {}

    profiles = load_coding_worker_pass_profiles(
        cfg,
        worker_config=worker_config,
        task=task,
        context=context,
    )
    from hermes_cli.model_tiers import resolve_model_tier

    coding_cfg = cfg.get("coding_worker") if isinstance(cfg.get("coding_worker"), dict) else {}
    worker_cfg = worker_config or {}
    explicit_service_tier = (
        worker_cfg.get("service_tier")
        if "service_tier" in worker_cfg
        else coding_cfg.get("service_tier")
    )
    normalized_service_tier = _normalize_service_tier(explicit_service_tier)
    result: dict[str, Any] = {}
    for pass_name, profile in profiles.items():
        result[f"{pass_name}_reasoning_level"] = profile["reasoning_level"]
        result[f"{pass_name}_model"] = profile["model"]
        result[f"{pass_name}_model_tier"] = profile["model_tier"]
        tier = resolve_model_tier(cfg, profile["model_tier"])
        result[f"{pass_name}_fast_mode"] = (
            normalized_service_tier == "fast"
            if normalized_service_tier is not None
            else bool(tier is not None and tier.fast_mode)
        )
    return result


def load_opencode_config(
    config: Optional[dict[str, Any]] = None,
    *,
    worker_config: Optional[dict[str, Any]] = None,
    task: Any = "",
    context: Any = "",
) -> dict[str, Any]:
    cfg = config
    if cfg is None:
        try:
            from hermes_cli.config import load_config

            cfg = load_config() or {}
        except Exception:
            cfg = {}

    opencode_cfg: dict[str, Any] = {}
    if isinstance(cfg, dict):
        coding_cfg = cfg.get("coding_worker") if isinstance(cfg.get("coding_worker"), dict) else {}
        if isinstance(coding_cfg.get("opencode"), dict):
            opencode_cfg.update(coding_cfg["opencode"])
    if worker_config and isinstance(worker_config.get("opencode"), dict):
        opencode_cfg.update(worker_config["opencode"])

    pass_cfg = load_coding_worker_pass_config(
        cfg,
        worker_config=worker_config,
        task=task,
        context=context,
    )
    model_tier = load_coding_worker_model_tier(cfg, worker_config=worker_config)
    if model_tier is not None and not (
        worker_config
        and isinstance(worker_config.get("opencode"), dict)
        and worker_config["opencode"].get("model")
    ):
        opencode_cfg["model"] = model_tier.opencode_model
    return {
        "binary": str(opencode_cfg.get("binary") or "opencode"),
        "model": _direct_opencode_model(opencode_cfg.get("model")),
        "plan_agent": str(opencode_cfg.get("plan_agent") or "plan").strip() or "plan",
        "build_agent": str(opencode_cfg.get("build_agent") or "build").strip() or "build",
        "simple_build_reasoning_level": pass_cfg["simple_build_reasoning_level"],
        "complex_plan_reasoning_level": pass_cfg["complex_plan_reasoning_level"],
        "complex_build_reasoning_level": pass_cfg["complex_build_reasoning_level"],
        "simple_build_model": pass_cfg["simple_build_model"],
        "complex_plan_model": pass_cfg["complex_plan_model"],
        "complex_build_model": pass_cfg["complex_build_model"],
        "simple_build_model_tier": pass_cfg["simple_build_model_tier"],
        "complex_plan_model_tier": pass_cfg["complex_plan_model_tier"],
        "complex_build_model_tier": pass_cfg["complex_build_model_tier"],
        "simple_build_fast_mode": pass_cfg.get("simple_build_fast_mode", False),
        "complex_plan_fast_mode": pass_cfg.get("complex_plan_fast_mode", False),
        "complex_build_fast_mode": pass_cfg.get("complex_build_fast_mode", False),
        "dangerously_skip_permissions": bool(opencode_cfg.get("dangerously_skip_permissions", False)),
        "isolated_config": _bool_config(opencode_cfg.get("isolated_config"), True),
        "startup_timeout_seconds": _non_negative_float(
            os.getenv("HERMES_OPENCODE_STARTUP_TIMEOUT_SECONDS")
            or opencode_cfg.get("startup_timeout_seconds"),
            _DEFAULT_STARTUP_TIMEOUT_SECONDS,
        ),
    }


def check_opencode_binary(config: Optional[dict[str, Any]] = None) -> tuple[bool, str]:
    binary = load_opencode_config(config).get("binary") or "opencode"
    if os.path.isabs(str(binary)):
        path = Path(str(binary))
        if path.is_file() and os.access(path, os.X_OK):
            return True, str(path)
        return False, f"OpenCode binary is not executable: {path}"
    resolved = shutil.which(str(binary))
    if not resolved:
        return False, f"OpenCode CLI not found in PATH: {binary}"
    return True, resolved


def opencode_credentials_look_configured(config: Optional[dict[str, Any]] = None) -> tuple[bool, str]:
    ok, resolved = check_opencode_binary(config)
    if not ok:
        return False, resolved
    try:
        proc = subprocess.run(
            [resolved, "providers", "list"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        return False, f"OpenCode credentials check failed: {exc}"
    output = "\n".join(
        part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip()
    )
    if proc.returncode != 0:
        return False, output or f"OpenCode credentials check exited {proc.returncode}"
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output).strip()
    if not plain or re.search(r"\b(?:0|no) credentials\b", plain, flags=re.IGNORECASE):
        return False, "OpenCode is installed but has no configured credentials."
    return True, plain


def looks_complex_or_risky(task: str, context: str = "") -> bool:
    lower = f"{task}\n{context}".lower()
    if not lower.strip():
        return False
    explicit_plan = (
        "plan first",
        "first plan",
        "planning pass",
        "two phase",
        "two-phase",
        "design before",
    )
    if any(phrase in lower for phrase in explicit_plan):
        return True

    simple_signals = (
        "typo",
        "comment",
        "formatting",
        "small docs",
        "documentation",
        "readme",
        "changelog",
        "one-line",
        "one line",
        "trivial",
        "mechanical",
    )
    if any(_contains_signal(lower, signal) for signal in simple_signals):
        return False

    risky_signals = (
        "security",
        "auth",
        "permission",
        "sandbox",
        "secret",
        "credential",
        "payment",
        "wallet",
        "signing",
        "race",
        "deadlock",
        "concurrency",
        "data loss",
        "migration",
        "schema migration",
        "breaking change",
        "architecture",
        "design review",
        "audit",
        "incident",
        "production",
        "unsafe",
        "dangerous",
        "rewrite",
        "upgrade",
        "rebase",
        "merge conflict",
        "flaky",
        "intermittent",
        "root cause",
        "state machine",
        "async",
        "cache",
        "performance",
    )
    return any(_contains_signal(lower, signal) for signal in risky_signals)


def _prompt_with_repo_state_preflight(prompt: str, workspace: str) -> str:
    try:
        from agent.repo_state_guard import format_repo_state_preflight, repo_state_preflight

        notes = format_repo_state_preflight(repo_state_preflight(workspace)).strip()
    except Exception:
        notes = ""
    if not notes or "Repository state preflight:" in prompt:
        return prompt
    return f"{notes}\n\n{prompt}"


def run_opencode_task(
    prompt: str,
    workspace: str,
    *,
    timeout: float,
    context_for_classification: str = "",
    task_for_purpose: Any = None,
    force_plan: Optional[bool] = None,
    title: str = "",
    config: Optional[dict[str, Any]] = None,
    worker_config: Optional[dict[str, Any]] = None,
    env: Optional[dict[str, str]] = None,
    on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    scope_session_key: str = "",
) -> OpenCodeRunResult:
    cfg = load_opencode_config(
        config,
        worker_config=worker_config,
        task=prompt if task_for_purpose is None else task_for_purpose,
        context=context_for_classification,
    )
    needs_plan = (
        bool(force_plan)
        if force_plan is not None
        else looks_complex_or_risky(prompt, context_for_classification)
    )
    started = time.monotonic()
    events: list[dict[str, Any]] = []
    agents: list[str] = []
    run_profile = _task_run_profile(cfg, needs_plan)
    logger.info(
        "coding_worker_runtime %s",
        json.dumps(
            {
                "backend": BACKEND_OPENCODE,
                "runtime_route": "coding_worker",
                "runtime_profile": run_profile["kind"],
                "passes": run_profile["passes"],
            },
            sort_keys=True,
        ),
    )
    worker_prompt = _prompt_with_repo_state_preflight(prompt, workspace)
    git_before = _git_artifact_snapshot(workspace)

    def _capture(event: dict[str, Any]) -> None:
        events.append(event)
        if on_event is not None:
            on_event(event)

    plan_text = ""
    if needs_plan:
        agents.append(cfg["plan_agent"])
        plan = _run_opencode_once(
            prompt=_plan_prompt(worker_prompt),
            workspace=workspace,
            timeout=max(30.0, timeout),
            cfg=cfg,
            agent=cfg["plan_agent"],
            model=cfg["complex_plan_model"],
            reasoning_level=cfg["complex_plan_reasoning_level"],
            fast_mode=cfg["complex_plan_fast_mode"],
            title=title,
            env=env,
            on_event=_capture,
            scope_session_key=scope_session_key,
        )
        if plan.error:
            plan.backend = BACKEND_OPENCODE
            plan.agents = agents
            plan.events = events
            plan.plan_text = plan.final_text
            plan.duration_seconds = round(time.monotonic() - started, 2)
            plan.run_profile = run_profile
            return plan
        plan_text = plan.final_text.strip()

    agents.append(cfg["build_agent"])
    build_prompt = worker_prompt
    if plan_text:
        build_prompt = (
            f"{worker_prompt.rstrip()}\n\n"
            "OpenCode plan to follow:\n"
            f"{plan_text}\n"
        )
    build = _run_opencode_once(
        prompt=build_prompt,
        workspace=workspace,
        timeout=max(30.0, timeout),
        cfg=cfg,
        agent=cfg["build_agent"],
        model=cfg["complex_build_model"] if needs_plan else cfg["simple_build_model"],
        reasoning_level=(
            cfg["complex_build_reasoning_level"]
            if needs_plan
            else cfg["simple_build_reasoning_level"]
        ),
        fast_mode=(
            cfg["complex_build_fast_mode"]
            if needs_plan
            else cfg["simple_build_fast_mode"]
        ),
        title=title,
        env=env,
        on_event=_capture,
        scope_session_key=scope_session_key,
    )
    build.backend = BACKEND_OPENCODE
    build.agents = agents
    build.plan_text = plan_text
    build.events = events
    build.run_profile = run_profile
    build.tool_iterations = len(events)
    build.timed_out = bool(build.timed_out)
    if build.error is None and not build.final_text.strip():
        build.error = "OpenCode completed without producing final text."
    if build.error == "OpenCode completed without producing final text.":
        _attach_no_final_metadata(build, workspace=workspace, git_before=git_before)
    build.exit_code = build.exit_code
    if build.thread_id is None:
        build.thread_id = _last_session_id(events)
    build.turn_id = build.thread_id
    build.duration_seconds = round(time.monotonic() - started, 2)
    if build.stderr:
        build.stderr = build.stderr.strip()
    build.stdout = build.stdout.strip()
    return build


def run_opencode_single_pass(
    prompt: str,
    workspace: str,
    *,
    timeout: float,
    agent: str,
    reasoning_level: str,
    fast_mode: bool = False,
    title: str = "",
    config: Optional[dict[str, Any]] = None,
    worker_config: Optional[dict[str, Any]] = None,
    env: Optional[dict[str, str]] = None,
    on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    scope_session_key: str = "",
) -> OpenCodeRunResult:
    cfg = load_opencode_config(config, worker_config=worker_config)
    selected_agent = str(agent or cfg["build_agent"]).strip() or cfg["build_agent"]
    selected_reasoning = _normalize_reasoning_level(reasoning_level)
    started = time.monotonic()
    events: list[dict[str, Any]] = []
    git_before = _git_artifact_snapshot(workspace)

    def _capture(event: dict[str, Any]) -> None:
        events.append(event)
        if on_event is not None:
            on_event(event)

    result = _run_opencode_once(
        prompt=_prompt_with_repo_state_preflight(prompt, workspace),
        workspace=workspace,
        timeout=max(30.0, timeout),
        cfg=cfg,
        agent=selected_agent,
        model=cfg["model"],
        reasoning_level=selected_reasoning,
        fast_mode=fast_mode,
        title=title,
        env=env,
        on_event=_capture,
        scope_session_key=scope_session_key,
    )
    result.backend = BACKEND_OPENCODE
    result.agents = [selected_agent]
    result.run_profile = {
        "kind": "single_pass",
        "label": f"1-pass {selected_agent}",
        "pass_count": 1,
        "plan_used": False,
        "passes": [
            {
                "name": selected_agent,
                "agent": selected_agent,
                "reasoning": selected_reasoning,
                "model": cfg["model"],
            }
        ],
    }
    if events:
        result.events = events
    result.tool_iterations = len(result.events)
    result.duration_seconds = round(time.monotonic() - started, 2)
    if result.error is None and not result.final_text.strip():
        result.error = "OpenCode completed without producing final text."
    if result.error == "OpenCode completed without producing final text.":
        _attach_no_final_metadata(result, workspace=workspace, git_before=git_before)
    if result.thread_id is None:
        result.thread_id = _last_session_id(result.events)
    result.turn_id = result.thread_id
    result.stdout = result.stdout.strip()
    result.stderr = result.stderr.strip()
    return result


def _task_run_profile(cfg: dict[str, Any], needs_plan: bool) -> dict[str, Any]:
    if needs_plan:
        return {
            "kind": "two_pass_plan_build",
            "label": "2-pass plan+build",
            "pass_count": 2,
            "plan_used": True,
            "passes": [
                {
                    "name": "plan",
                    "agent": cfg["plan_agent"],
                    "reasoning": cfg["complex_plan_reasoning_level"],
                    "model": cfg["complex_plan_model"],
                    "model_tier": cfg["complex_plan_model_tier"],
                },
                {
                    "name": "build",
                    "agent": cfg["build_agent"],
                    "reasoning": cfg["complex_build_reasoning_level"],
                    "model": cfg["complex_build_model"],
                    "model_tier": cfg["complex_build_model_tier"],
                },
            ],
        }
    return {
        "kind": "one_pass_simple_build",
        "label": "1-pass simple build",
        "pass_count": 1,
        "plan_used": False,
        "passes": [
            {
                "name": "build",
                "agent": cfg["build_agent"],
                "reasoning": cfg["simple_build_reasoning_level"],
                "model": cfg["simple_build_model"],
                "model_tier": cfg["simple_build_model_tier"],
            }
        ],
    }


def _run_opencode_once(
    *,
    prompt: str,
    workspace: str,
    timeout: float,
    cfg: dict[str, Any],
    agent: str,
    model: str,
    reasoning_level: str,
    fast_mode: bool,
    title: str,
    env: Optional[dict[str, str]],
    on_event: Callable[[dict[str, Any]], None],
    scope_session_key: str = "",
) -> OpenCodeRunResult:
    ok, binary_or_error = check_opencode_binary({"coding_worker": {"opencode": cfg}})
    if not ok:
        return OpenCodeRunResult(error=binary_or_error)

    workdir_path = Path(workspace).expanduser().resolve()
    workdir = str(workdir_path)
    inline_brief = _inline_worker_brief(model)
    brief_path = None if inline_brief else _write_brief(prompt, workspace=workdir_path)
    try:
        config_home = (
            _write_worker_config(model, reasoning_level, fast_mode=fast_mode)
            if cfg.get("isolated_config")
            else None
        )
    except ValueError as exc:
        if brief_path is not None:
            try:
                brief_path.unlink()
            except OSError:
                pass
        return OpenCodeRunResult(error=f"OpenCode isolated configuration error: {exc}")
    run_nonce = f"hermes-{uuid.uuid4().hex[:12]}"
    run_title = f"{title} [{run_nonce}]" if title else f"Hermes worker [{run_nonce}]"
    cmd = [
        binary_or_error,
        "run",
        "--pure",
        _worker_brief_message(prompt)
        if inline_brief
        else "Read the attached Hermes worker brief and follow it exactly.",
        "--format",
        "json",
        "--agent",
        agent,
        "--dir",
        workdir,
    ]
    if model:
        cmd.extend(["--model", model])
    if reasoning_level:
        cmd.extend(["--variant", reasoning_level])
    cmd.extend(["--title", run_title])
    if cfg.get("dangerously_skip_permissions"):
        cmd.append("--dangerously-skip-permissions")
    if brief_path is not None:
        cmd.extend(["--file", str(brief_path)])

    process_env = _opencode_process_env(config_home, env=env)
    run_started_ms = int(time.time() * 1000)
    try:
        configured_startup_timeout = float(
            cfg.get("startup_timeout_seconds")
            if cfg.get("startup_timeout_seconds") is not None
            else _DEFAULT_STARTUP_TIMEOUT_SECONDS
        )
        startup_timeout = (
            0.0
            if configured_startup_timeout <= 0
            else min(max(10.0, configured_startup_timeout), timeout)
        )
        proc = _run_opencode_process(
            cmd,
            timeout=timeout,
            startup_timeout=startup_timeout,
            workdir=workdir,
            env=process_env,
            scope_session_key=scope_session_key,
            scope_purpose=f"OpenCode coding worker {agent} pass",
        )
    except Exception as exc:
        return OpenCodeRunResult(error=f"OpenCode {agent} run failed to start: {exc}")
    finally:
        if brief_path is not None:
            try:
                brief_path.unlink()
            except OSError:
                pass
        if config_home is not None:
            shutil.rmtree(config_home, ignore_errors=True)

    result = _parse_opencode_output(proc.stdout, proc.stderr, on_event=on_event)
    result.exit_code = proc.returncode
    result.stdout = proc.stdout or ""
    result.stderr = proc.stderr or ""
    if proc.timed_out:
        result.timed_out = True
        result.should_retire = True
        if result.error is None:
            if proc.startup_timed_out and not result.events:
                result.error = (
                    f"OpenCode {agent} produced no JSON events for "
                    f"{proc.duration_seconds:g}s during startup and was killed "
                    f"before the full {timeout:g}s turn timeout. This usually "
                    "means OpenCode is stuck bootstrapping the repository "
                    "(snapshot/file watcher setup) before reaching the model."
                )
            elif not result.events:
                detail = _shorten_opencode_error_details(
                    "\n".join(
                        part.strip()
                        for part in (proc.stderr, proc.stdout)
                        if part and part.strip()
                    ),
                    limit=1000,
                )
                suffix = f" Output: {detail}" if detail else ""
                result.error = (
                    f"OpenCode {agent} produced no JSON events and timed out "
                    f"after {timeout:g}s.{suffix}"
                )
            else:
                result.error = f"OpenCode {agent} run timed out after {timeout:g}s."
    if proc.returncode == 0 and result.error is None and not result.final_text.strip():
        session_id = result.thread_id or _last_session_id(result.events)
        export_status: dict[str, Any] = {
            "status": "not_attempted",
            "reason": "no_session_id",
        }
        if (
            not session_id
            and not result.events
            and not (proc.stdout or "").strip()
            and not (proc.stderr or "").strip()
        ):
            session_id = _discover_recent_opencode_session_id(
                env=process_env,
                title_nonce=run_nonce,
                workspace=workdir,
                agent=agent,
                model=model,
                started_ms=run_started_ms,
            )
        if session_id:
            result.thread_id = session_id
            result.final_text, export_status = _load_final_text_from_export_with_status(
                binary_or_error,
                session_id,
                env=process_env,
            )
        result.export_status = export_status
    if proc.returncode != 0 and result.error is None:
        result.error = _classify_opencode_error(
            result.stdout,
            result.stderr,
            f"OpenCode {agent} exited with code {proc.returncode}.",
        )
    if result.error is not None:
        result.error = _classify_opencode_error(result.error, result.stdout, result.stderr)
    result.thread_id = result.thread_id or _last_session_id(result.events)
    result.turn_id = result.thread_id
    result.tool_iterations = len(result.events)
    return result


def _run_opencode_process(
    cmd: list[str],
    *,
    workdir: str,
    timeout: float,
    startup_timeout: float,
    env: Optional[dict[str, str]] = None,
    scope_session_key: str = "",
    scope_purpose: str = "OpenCode coding worker",
) -> _OpenCodeProcessResult:
    """Run OpenCode while watching for no-output startup stalls.

    ``opencode run --format json`` emits JSONL on stdout only after the run
    reaches the session/model path. The process is intentionally launched with
    stdin closed so gateway/service workers cannot hang on an inherited
    interactive prompt before JSON output starts.
    """
    started = time.monotonic()
    popen_cmd = cmd
    child_scope = None
    try:
        from hermes_cli.gateway_child_isolation import build_gateway_child_scope_argv

        popen_cmd, child_scope = build_gateway_child_scope_argv(
            cmd,
            env=env or os.environ,
            cwd=workdir,
            kind="coding-worker",
            purpose=scope_purpose,
            command_label="opencode-run",
            session_key=scope_session_key or os.environ.get("HERMES_SESSION_KEY", ""),
            pipe_stdio=True,
        )
    except Exception:
        child_scope = None
    try:
        proc = subprocess.Popen(
            popen_cmd,
            cwd=None if child_scope is not None and child_scope.enabled else workdir,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        raise

    line_queue: queue.Queue[tuple[str, Optional[str]]] = queue.Queue()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _reader(name: str, stream: Any) -> None:
        try:
            if stream is not None:
                for line in iter(stream.readline, ""):
                    if line == "":
                        break
                    line_queue.put((name, line))
        finally:
            line_queue.put((name, None))

    threads = [
        threading.Thread(target=_reader, args=("stdout", proc.stdout), daemon=True),
        threading.Thread(target=_reader, args=("stderr", proc.stderr), daemon=True),
    ]
    for thread in threads:
        thread.start()

    closed_streams: set[str] = set()
    timed_out = False
    startup_timed_out = False
    terminal_auth_error_detected = False

    def _terminate() -> None:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=3)

    while True:
        now = time.monotonic()
        elapsed = now - started
        if proc.poll() is None:
            if elapsed >= timeout:
                timed_out = True
                _terminate()
            elif startup_timeout > 0 and not stdout_lines and elapsed >= startup_timeout:
                timed_out = True
                startup_timed_out = True
                _terminate()

        try:
            name, line = line_queue.get(timeout=0.1)
        except queue.Empty:
            if proc.poll() is not None and len(closed_streams) >= 2:
                break
            continue

        if line is None:
            closed_streams.add(name)
        elif name == "stdout":
            stdout_lines.append(line)
        elif name == "stderr":
            stderr_lines.append(line)

        if (
            line is not None
            and not terminal_auth_error_detected
            and proc.poll() is None
            and _opencode_line_is_terminal_auth_error(name, line)
        ):
            terminal_auth_error_detected = True
            _terminate()

        if proc.poll() is not None and len(closed_streams) >= 2:
            break

    for thread in threads:
        thread.join(timeout=1)

    return _OpenCodeProcessResult(
        returncode=proc.returncode,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
        timed_out=timed_out,
        startup_timed_out=startup_timed_out,
        duration_seconds=round(time.monotonic() - started, 2),
    )


def _opencode_line_is_terminal_auth_error(stream_name: str, line: str) -> bool:
    """Return whether a live OpenCode output line is a terminal auth failure.

    Stdout is JSONL, so only explicit error events are eligible. This avoids
    aborting when normal assistant content merely mentions an older auth error.
    Stderr is reserved for process errors and may contain a plain-text failure.
    """
    candidate = str(line or "").strip()
    if not candidate:
        return False
    if stream_name == "stdout":
        try:
            event = json.loads(candidate)
        except (TypeError, ValueError):
            return False
        if not isinstance(event, dict) or str(event.get("type") or "").lower() != "error":
            return False
        candidate = json.dumps(event, ensure_ascii=False)
    elif stream_name != "stderr":
        return False
    return _classify_opencode_error(candidate).startswith("OpenCode authentication failed.")


def _parse_opencode_output(
    stdout: str,
    stderr: str,
    *,
    on_event: Callable[[dict[str, Any]], None],
) -> OpenCodeRunResult:
    events: list[dict[str, Any]] = []
    texts: list[str] = []
    raw_text_lines: list[str] = []
    error: Optional[str] = None
    session_id: Optional[str] = None

    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            raw_text_lines.append(stripped)
            continue
        if not isinstance(event, dict):
            raw_text_lines.append(stripped)
            continue
        events.append(event)
        on_event(event)
        session_id = session_id or _event_session_id(event)
        if str(event.get("type") or "").lower() == "error":
            error = _event_error_text(event) or "OpenCode reported an error."
            continue
        text = _event_text(event)
        if text:
            texts.append(text)

    final_text = "\n".join(texts).strip()
    if not final_text and raw_text_lines:
        final_text = "\n".join(raw_text_lines).strip()
    if not final_text and stderr and error is None:
        final_text = stderr.strip()

    return OpenCodeRunResult(
        final_text=final_text,
        error=error,
        events=events,
        thread_id=session_id,
        turn_id=session_id,
    )


def _load_final_text_from_export(
    binary: str, session_id: Optional[str], *, env: Optional[dict[str, str]] = None
) -> str:
    text, _status = _load_final_text_from_export_with_status(binary, session_id, env=env)
    return text


def _load_final_text_from_export_with_status(
    binary: str, session_id: Optional[str], *, env: Optional[dict[str, str]] = None
) -> tuple[str, dict[str, Any]]:
    """Recover assistant text from ``opencode export`` when JSONL output is sparse."""
    if not session_id:
        return "", {"status": "not_attempted", "reason": "no_session_id"}
    try:
        proc = subprocess.run(
            [binary, "export", session_id],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except Exception as exc:
        return "", {
            "status": "failed",
            "reason": type(exc).__name__,
            "session_id": session_id,
        }
    if proc.returncode != 0:
        return "", {
            "status": "failed",
            "reason": f"exit_code_{proc.returncode}",
            "session_id": session_id,
            "stderr_snippet": _bounded_snippet(proc.stderr, limit=500),
        }
    text = _parse_opencode_export_text(proc.stdout)
    return text, {
        "status": "recovered" if text else "empty",
        "session_id": session_id,
    }


def _bounded_snippet(text: str, *, limit: int = 1000) -> str:
    snippet = (text or "").strip()
    if len(snippet) <= limit:
        return snippet
    return snippet[:limit].rstrip() + "... [truncated]"


def _git_artifact_snapshot(workspace: str) -> dict[str, Any]:
    cwd = str(Path(workspace).expanduser().resolve())
    base = {
        "available": False,
        "cwd": cwd,
        "branch": None,
        "commit": None,
        "dirty": False,
        "status_entries": 0,
        "error": None,
    }

    def run_git(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            encoding="utf-8",
            errors="replace",
        )

    try:
        root = run_git(["rev-parse", "--show-toplevel"])
        if root.returncode != 0:
            base["error"] = _bounded_snippet(root.stderr or root.stdout, limit=300) or "not_git_repository"
            return base
        branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"])
        commit = run_git(["rev-parse", "HEAD"])
        status = run_git(["status", "--porcelain"])
    except Exception as exc:
        base["error"] = type(exc).__name__
        return base

    status_text = status.stdout if status.returncode == 0 else ""
    entries = [line for line in status_text.splitlines() if line.strip()]
    base.update(
        {
            "available": True,
            "branch": (branch.stdout or "").strip() if branch.returncode == 0 else None,
            "commit": (commit.stdout or "").strip() if commit.returncode == 0 else None,
            "dirty": bool(entries),
            "status_entries": len(entries),
            "error": None if status.returncode == 0 else _bounded_snippet(status.stderr, limit=300),
        }
    )
    return base


def _attach_no_final_metadata(
    result: OpenCodeRunResult,
    *,
    workspace: str,
    git_before: dict[str, Any],
) -> None:
    git_after = _git_artifact_snapshot(workspace)
    commit_changed = bool(
        git_before.get("available")
        and git_after.get("available")
        and git_before.get("commit")
        and git_after.get("commit")
        and git_before.get("commit") != git_after.get("commit")
    )
    file_changes = bool(git_after.get("available") and git_after.get("dirty"))
    recoverable = bool(commit_changed and not git_after.get("dirty"))
    result.no_final_metadata = {
        "classification": "no_final_text",
        "evidence_status": "recoverable_degraded" if recoverable else "degraded",
        "failure_class": "no_final_text",
        "backend": result.backend or BACKEND_OPENCODE,
        "thread_id": result.thread_id,
        "turn_id": result.turn_id,
        "cwd": str(Path(workspace).expanduser().resolve()),
        "branch": git_after.get("branch"),
        "commit": git_after.get("commit"),
        "export_status": result.export_status or {
            "status": "not_attempted",
            "reason": "unavailable",
        },
        "stderr_snippet": _bounded_snippet(result.stderr, limit=1000),
        "error_snippet": _bounded_snippet(result.error or "", limit=1000),
        "local_file_changes": file_changes,
        "local_commit_detected": commit_changed,
        "clean_committed_branch": recoverable,
        "git_before": git_before,
        "git_after": git_after,
    }


def _discover_recent_opencode_session_id(
    *,
    env: Optional[dict[str, str]],
    title_nonce: str,
    workspace: str,
    agent: str,
    model: str,
    started_ms: int,
) -> Optional[str]:
    """Find a just-created OpenCode session using safe session metadata only."""
    if not title_nonce:
        return None
    db_path = _opencode_data_root(env) / "opencode" / "opencode.db"
    if not db_path.is_file():
        return None

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None

    try:
        rows = conn.execute(
            """
            SELECT id, title, directory, agent, model, time_created
            FROM session
            WHERE title LIKE ? AND time_created >= ?
            ORDER BY time_created DESC
            LIMIT 5
            """,
            (f"%{title_nonce}%", max(0, started_ms - 5000)),
        ).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()

    matches: list[tuple[str, int]] = []
    for session_id, title, directory, session_agent, session_model, created in rows:
        if title_nonce not in str(title or ""):
            continue
        if directory and str(Path(str(directory)).expanduser()) != workspace:
            continue
        if session_agent and str(session_agent) != agent:
            continue
        if session_model and not _opencode_session_model_matches(str(session_model), model):
            continue
        try:
            created_ms = int(created)
        except (TypeError, ValueError):
            created_ms = 0
        matches.append((str(session_id), created_ms))

    if not matches:
        return None
    matches.sort(key=lambda item: item[1], reverse=True)
    return matches[0][0]


def _opencode_data_root(env: Optional[dict[str, str]]) -> Path:
    source = env if env is not None else os.environ
    xdg_data = source.get("XDG_DATA_HOME")
    if xdg_data:
        return Path(xdg_data).expanduser()
    home = source.get("HOME") or str(Path.home())
    return Path(home).expanduser() / ".local" / "share"


def _opencode_session_model_matches(stored: str, requested: str) -> bool:
    requested = (requested or "").strip()
    if not requested:
        return True
    if stored == requested:
        return True
    try:
        parsed = json.loads(stored)
    except json.JSONDecodeError:
        return False
    if not isinstance(parsed, dict):
        return False
    provider = str(parsed.get("providerID") or parsed.get("provider") or "").strip()
    model_id = str(parsed.get("id") or parsed.get("model") or "").strip()
    return bool(provider and model_id and requested == f"{provider}/{model_id}")


def _parse_opencode_export_text(stdout: str) -> str:
    text = (stdout or "").strip()
    if not text:
        return ""
    json_start = text.find("{")
    if json_start > 0:
        text = text[json_start:]
    try:
        exported = json.loads(text)
    except json.JSONDecodeError:
        return ""
    messages = exported.get("messages") if isinstance(exported, dict) else None
    if not isinstance(messages, list):
        return ""

    assistant_texts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        info_raw = message.get("info")
        info = info_raw if isinstance(info_raw, dict) else {}
        if str(info.get("role") or message.get("role") or "").lower() != "assistant":
            continue
        part_texts: list[str] = []
        for part in message.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if str(part.get("type") or "").lower() != "text":
                continue
            if part.get("synthetic") is True:
                continue
            content = str(part.get("text") or "").strip()
            if content:
                part_texts.append(content)
        if part_texts:
            assistant_texts.append("\n".join(part_texts).strip())
    return "\n".join(filter(None, assistant_texts)).strip()


def _write_brief(prompt: str, *, workspace: Optional[Path] = None) -> Path:
    root = (
        workspace / ".hermes-opencode"
        if workspace is not None
        else Path(tempfile.gettempdir()) / "opencode"
    )
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        prefix="hermes-worker-",
        suffix=".md",
        dir=str(root),
        delete=False,
    ) as handle:
        handle.write(prompt)
        handle.write("\n")
        return Path(handle.name)


def _built_in_worker_opencode_models() -> set[str]:
    from hermes_cli.model_tiers import DEFAULT_MODEL_TIERS

    return {
        str(tier.get("opencode_model") or "").strip()
        for tier in DEFAULT_MODEL_TIERS.values()
        if str(tier.get("opencode_model") or "").strip()
    }


def _worker_provider_config(model: str, reasoning_level: str) -> tuple[str, dict[str, Any]]:
    provider_id = _opencode_provider_id(model)
    model_id = model.split("/", 1)[1] if provider_id else ""
    provider_cfg = copy.deepcopy(_opencode_provider_config_for_model(model))
    if not provider_id or not model_id:
        raise ValueError(f"OpenCode worker model must use provider/model form: {model!r}")
    if not provider_cfg:
        raise ValueError(
            f"OpenCode provider {provider_id!r} is not configured; cannot represent worker model {model!r}."
        )
    models = provider_cfg.setdefault("models", {})
    if not isinstance(models, dict):
        raise ValueError(f"OpenCode provider {provider_id!r} has an invalid models catalog.")
    for built_in_model in _built_in_worker_opencode_models():
        built_in_provider = _opencode_provider_id(built_in_model)
        if built_in_provider != provider_id:
            continue
        built_in_id = built_in_model.split("/", 1)[1]
        entry = models.setdefault(built_in_id, {"name": built_in_id, "reasoning": True})
        if isinstance(entry, dict):
            entry.setdefault("reasoning", True)
            variants = entry.setdefault("variants", {})
            if isinstance(variants, dict):
                for level in _VALID_REASONING_LEVELS:
                    variants.setdefault(level, {"reasoningEffort": level})
    entry = models.get(model_id)
    if not isinstance(entry, dict):
        raise ValueError(f"OpenCode provider {provider_id!r} cannot represent worker model {model!r}.")
    variants = entry.get("variants")
    if reasoning_level and (not isinstance(variants, dict) or reasoning_level not in variants):
        raise ValueError(
            f"OpenCode provider {provider_id!r} model {model_id!r} cannot represent variant {reasoning_level!r}."
        )
    return provider_id, provider_cfg


def _worker_provider_config_for_tier(
    model: str,
    reasoning_level: str,
    *,
    fast_mode: bool,
) -> tuple[str, dict[str, Any]]:
    provider_id, provider_cfg = _worker_provider_config(model, reasoning_level)
    if not reasoning_level:
        return provider_id, provider_cfg
    model_id = model.split("/", 1)[1]
    entry = provider_cfg["models"][model_id]
    variants = entry.get("variants")
    if not isinstance(variants, dict) or reasoning_level not in variants:
        raise ValueError(
            f"OpenCode provider {provider_id!r} model {model_id!r} cannot represent "
            f"variant {reasoning_level!r}."
        )
    variant = dict(variants[reasoning_level])
    for existing_key in ("service_tier", "serviceTier", "speed"):
        variant.pop(existing_key, None)
    if fast_mode:
        npm = str(provider_cfg.get("npm") or "").strip()
        if provider_id == "hermes-codex" and npm == "@ai-sdk/openai-compatible":
            variant["service_tier"] = "priority"
        elif provider_id == "openai" and npm == "@ai-sdk/openai":
            variant["serviceTier"] = "priority"
        elif provider_id == "anthropic" and npm == "@ai-sdk/anthropic":
            from hermes_cli.models import _is_anthropic_fast_model

            if not _is_anthropic_fast_model(model_id):
                raise ValueError(
                    f"OpenCode Anthropic model {model_id!r} does not support "
                    "fast mode; only the verified Opus 4.6 path accepts speed='fast'."
                )
            variant["speed"] = "fast"
        else:
            raise ValueError(
                f"OpenCode provider {provider_id!r} has no verified fast-mode "
                "variant encoding; configure a supported provider or disable fast_mode."
            )
    variants[reasoning_level] = variant
    return provider_id, provider_cfg


def _write_worker_config(
    model: str,
    reasoning_level: str = "",
    *,
    fast_mode: bool = False,
) -> Path:
    """Create an isolated OpenCode config with no remote MCP startup work."""
    provider_id, provider_cfg = _worker_provider_config_for_tier(
        model,
        reasoning_level,
        fast_mode=fast_mode,
    )
    root = Path(tempfile.mkdtemp(prefix="hermes-opencode-config-"))
    config_dir = root / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "plugin": [],
        "permission": "allow",
        "mcp": {},
        "model": model or _DEFAULT_OPENCODE_MODEL,
    }
    payload["provider"] = {provider_id: provider_cfg}
    (config_dir / "opencode.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


def _opencode_process_env(
    config_home: Optional[Path],
    *,
    env: Optional[dict[str, str]] = None,
) -> Optional[dict[str, str]]:
    if env is None and config_home is None:
        return None
    extra_env = env or {}
    try:
        from tools.environments.local import _sanitize_subprocess_env

        process_env = _sanitize_subprocess_env(dict(os.environ), extra_env)
    except Exception:
        process_env = os.environ.copy()
        for key in list(process_env):
            if key.startswith("_HERMES_FORCE_"):
                process_env.pop(key, None)
        process_env.update(extra_env)
    if config_home is not None:
        process_env["XDG_CONFIG_HOME"] = str(config_home)
    return process_env


def _plan_prompt(prompt: str) -> str:
    return (
        "Create a concise implementation plan for the attached Hermes worker "
        "brief. Do not edit repository files. Focus on the minimum safe changes, "
        "key files to inspect, and verification steps. Return plain text.\n\n"
        f"Worker brief:\n{prompt}"
    )


def _normalize_reasoning_level(value: Any) -> str:
    from hermes_constants import normalize_reasoning_effort

    raw = normalize_reasoning_effort(value)
    if raw == "ultra":
        raw = "max"
    return raw if raw in _VALID_REASONING_LEVELS else ""


def _contains_signal(text: str, signal: str) -> bool:
    if " " in signal or "-" in signal:
        return signal in text
    return bool(re.search(rf"\b{re.escape(signal)}\b", text))


def _event_session_id(event: dict[str, Any]) -> Optional[str]:
    for key in ("sessionID", "sessionId", "session_id"):
        value = event.get(key)
        if value:
            return str(value)
    return None


def _last_session_id(events: list[dict[str, Any]]) -> Optional[str]:
    session_id = None
    for event in events:
        session_id = _event_session_id(event) or session_id
    return session_id


def _event_error_text(event: dict[str, Any]) -> str:
    err = event.get("error")
    if isinstance(err, str):
        return err
    if isinstance(err, dict):
        parts = []
        for key in ("message", "code", "name"):
            value = err.get(key)
            if value:
                parts.append(str(value))
        data = err.get("data")
        if isinstance(data, dict):
            message = data.get("message")
            if message:
                parts.append(str(message))
        return ": ".join(parts)
    return ""


def _event_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return "\n".join(filter(None, (_event_text(item) for item in value))).strip()
    if not isinstance(value, dict):
        return ""

    if str(value.get("type") or "").lower() == "error":
        return ""
    for key in ("text", "message", "content", "output", "result", "final", "part"):
        if key in value:
            text = _event_text(value.get(key))
            if text:
                return text
    data = value.get("data")
    if isinstance(data, dict):
        text = _event_text(data)
        if text:
            return text
    return ""


def _classify_opencode_error(*parts: str) -> str:
    text = "\n".join(part for part in parts if part).strip()
    lower = text.lower()
    details = _shorten_opencode_error_details(text)
    if any(
        needle in lower
        for needle in (
            "contextoverflowerror",
            "context_length_exceeded",
            "input exceeds context window",
            "exceeds the context window",
            "context window of this model",
        )
    ):
        return (
            "OpenCode context window exceeded. Reduce the worker prompt or "
            f"retry with a larger-context model. Details: {details}"
        )
    if any(
        needle in lower
        for needle in (
            "token_invalidated",
            "authentication token has been invalidated",
            "authentication failed",
            "not authenticated",
            "unauthorized",
            "401",
            "signing in again",
            "please login",
            "please log in",
            "invalid api key",
            "invalid_api_key",
        )
    ):
        return (
            "OpenCode authentication failed. Run `opencode auth login` "
            f"or configure a valid OpenCode provider, then retry. Details: {details}"
        )
    return text or "OpenCode worker failed."


def _shorten_opencode_error_details(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    marker = "\n... [truncated]"
    if limit <= len(marker):
        return marker[:limit]
    return text[: limit - len(marker)] + marker
