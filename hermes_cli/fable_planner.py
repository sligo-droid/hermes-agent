"""Plan-only Claude Fable 5 helpers for Hermes slash commands."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from utils import base_url_host_matches


FABLE_PROVIDER = "anthropic"
FABLE_MODEL = "claude-fable-5"
FABLE_ROUTE = "anthropic_oauth"
FABLE_TRANSPORT = "anthropic_oauth"
FABLE_REASONING = {"enabled": True, "effort": "high"}
FABLE_DEFAULT_TOOLSETS = ["file", "terminal", "web", "browser", "discord"]

FABLE_RUNTIME_NOTE = """This is `/fable`: use Claude Fable 5, planning only, inspect repo with read-only tools as needed, save a plan artifact if the plan skill requires it, and do not implement. Do not edit files, create branches, open pull requests, deploy, or claim that implementation, tests, commits, PRs, or deployment happened. Generate a concrete Markdown implementation plan only. Your final answer must contain the full plan markdown, plus the saved path if you wrote one; do not answer with only a brief path/status note. Hermes/gateway will handle Discord delivery, threading, and artifact indexing outside the Fable turn, so do not describe or perform those operational steps."""


def fable_claude_code_env(base_env: dict[str, str] | None = None) -> dict[str, str]:
    """Build a scrubbed Claude Code environment pinned to interactive OAuth."""
    from hermes_constants import _process_user_home
    from tools.environments.local import _sanitize_subprocess_env

    env = _sanitize_subprocess_env(os.environ if base_env is None else base_env)
    for key in tuple(env):
        if key.startswith("ANTHROPIC_") or key == "CLAUDE_CODE_OAUTH_TOKEN":
            env.pop(key, None)

    account_home = _process_user_home() or Path.home()
    claude_config_dir = account_home / ".claude"
    if not (claude_config_dir / ".credentials.json").is_file():
        raise RuntimeError(
            "Claude Code OAuth credentials are unavailable for the Fable route; "
            "run `claude login` for this OS account before using the UI specialist."
        )
    env["CLAUDE_CONFIG_DIR"] = str(claude_config_dir)
    return env


@dataclass(frozen=True)
class FablePlanRequest:
    prompt: str
    session_id: str = ""
    workdir: str = ""
    source_text: str = ""
    platform: str = ""
    transport: str = FABLE_TRANSPORT
    max_tokens: int = 12000
    timeout_seconds: int = 300


@dataclass(frozen=True)
class FablePlanResult:
    ok: bool
    content: str
    transport: str
    model: str
    error: str = ""
    refusal: bool = False


def fable_metadata(result: FablePlanResult | None = None) -> dict[str, Any]:
    metadata = {
        "command": "fable",
        "plan_artifact_kind": "fable_plan",
        "response_kind": "fable_plan",
        "kind": "fable_plan",
        "transport": FABLE_TRANSPORT,
        "provider": FABLE_PROVIDER,
        "model": FABLE_MODEL,
        "reply_to_mode": "all",
    }
    if result is not None:
        metadata["transport"] = result.transport
        metadata["model"] = result.model
        metadata["ok"] = result.ok
        metadata["refusal"] = result.refusal
    return metadata


def fable_enabled_toolsets(config: dict[str, Any] | None = None) -> list[str]:
    cfg = _fable_config(config)
    configured = cfg.get("enabled_toolsets")
    if isinstance(configured, list):
        toolsets = [str(item).strip() for item in configured if str(item).strip()]
        if toolsets:
            return toolsets
    return list(FABLE_DEFAULT_TOOLSETS)


def fable_reasoning_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Resolve Fable's reasoning level without inheriting the agent default.

    Fable is a dedicated planning route. Its reasoning level follows the
    Discord action/feature setting when one is configured, but an absent,
    empty, or invalid value must not turn into ``None`` and later inherit the
    global agent default (which may be ``medium``).
    """
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}

    discord_cfg = config.get("discord") if isinstance(config, dict) else {}
    if not isinstance(discord_cfg, dict):
        discord_cfg = {}

    raw_effort = discord_cfg.get("action_request_reasoning_effort")
    if raw_effort is None:
        raw_effort = discord_cfg.get("feature_request_reasoning_effort")

    if raw_effort is None or not str(raw_effort).strip():
        return dict(FABLE_REASONING)

    try:
        from hermes_constants import parse_reasoning_effort

        parsed = parse_reasoning_effort(str(raw_effort))
    except Exception:
        parsed = None
    return parsed or dict(FABLE_REASONING)


def build_fable_user_instruction(request: FablePlanRequest) -> str:
    sections = [
        ("User Request", request.prompt.strip()),
        ("Fable Plan-Only Contract", FABLE_RUNTIME_NOTE),
        ("Session", request.session_id.strip()),
        ("Platform", request.platform.strip()),
        ("Workdir", request.workdir.strip()),
        ("Source Text", request.source_text.strip()),
    ]
    rendered = [f"## {title}\n{value.strip()}" for title, value in sections if value]
    return "\n\n".join(rendered).strip()


def build_fable_plan_invocation(request: FablePlanRequest, task_id: str | None = None) -> str | None:
    from agent.skill_commands import build_skill_invocation_message, resolve_skill_command_key

    cmd_key = resolve_skill_command_key("plan")
    if cmd_key is None:
        return None
    return build_skill_invocation_message(
        cmd_key,
        build_fable_user_instruction(request),
        task_id=task_id or request.session_id or None,
        runtime_note=FABLE_RUNTIME_NOTE,
    )


def fable_session_model_override(config: dict[str, Any] | None = None) -> tuple[dict[str, str] | None, str]:
    cfg = _fable_config(config)
    if cfg.get("enabled") is False:
        return None, "/fable is disabled in Hermes config."
    if str(cfg.get("provider") or FABLE_PROVIDER).strip().lower() != FABLE_PROVIDER:
        return None, "/fable is pinned to provider=anthropic; configured provider is unsupported."
    if str(cfg.get("model") or FABLE_MODEL).strip() != FABLE_MODEL:
        return None, "/fable is pinned to model=claude-fable-5; configured model is unsupported."
    if str(cfg.get("route") or FABLE_ROUTE).strip().lower() != FABLE_ROUTE:
        return None, "/fable is pinned to route=anthropic_oauth; configured route is unsupported."

    preflight_error = _anthropic_budget_preflight_error()
    if preflight_error:
        return None, preflight_error

    try:
        from agent.anthropic_adapter import _is_oauth_token
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(
            requested=FABLE_PROVIDER,
            target_model=FABLE_MODEL,
            credential_preference="pool_first",
        )
    except Exception as exc:
        return None, f"Fable 5 is not configured through Hermes' Anthropic route: {exc}"

    api_key = str(runtime.get("api_key") or "")
    if not api_key:
        route_error = _anthropic_budget_preflight_error()
        if route_error:
            return None, route_error
        return None, (
            "Fable 5 is not configured through Hermes' Anthropic route. "
            "Run `hermes auth add anthropic` or configure the Anthropic credential used by Hermes."
        )
    if str(runtime.get("provider") or "") != FABLE_PROVIDER:
        return None, f"Fable route resolved unexpected provider {runtime.get('provider')!r}; refusing to fall back."

    base_url = str(runtime.get("base_url") or "https://api.anthropic.com")
    api_mode = str(runtime.get("api_mode") or "")
    if api_mode != "anthropic_messages":
        return None, f"Fable route resolved unsupported API mode {api_mode!r}; refusing to fall back."
    if not _is_oauth_token(api_key) and base_url_host_matches(base_url, "api.anthropic.com"):
        return None, (
            "/fable permits API-key credentials only through a configured Hermes Anthropic proxy; "
            "direct api.anthropic.com requests require OAuth credentials."
        )

    return {
        "model": FABLE_MODEL,
        "provider": FABLE_PROVIDER,
        "api_key": api_key,
        "base_url": base_url,
        "api_mode": api_mode,
        "disable_fallback": "true",
    }, ""


def _is_fable_budget_error(exc: Exception) -> bool:
    """Return True for Anthropic/Fable budget, quota, billing, or credit exhaustion.

    `/fable` is intentionally pinned to Claude Fable 5. If that route reports
    budget exhaustion, the command must fail closed instead of using the general
    auxiliary provider/model fallback machinery.
    """
    try:
        from agent.auxiliary_client import _is_payment_error

        if _is_payment_error(exc):
            return True
    except Exception:
        pass

    status = getattr(exc, "status_code", None)
    text = str(exc).lower()
    if status == 402:
        return True
    return any(
        marker in text
        for marker in (
            "budget expended",
            "budget is expended",
            "budget exhausted",
            "budget is exhausted",
            "quota exceeded",
            "quota_exceeded",
            "daily quota",
            "daily limit",
            "weekly usage limit",
            "weekly limit",
            "resource exhausted",
            "no usable credits",
            "insufficient funds",
            "payment required",
            "balance_depleted",
            "out of funds",
            "run out of funds",
            "not available on the free tier",
            "model_not_supported_on_free_tier",
        )
    )


def _fable_budget_error_message(detail: str = "") -> str:
    base = (
        "Fable 5 budget/quota is expended on Hermes' configured Anthropic route; "
        "/fable is pinned to claude-fable-5 and will not fall back to another model or provider."
    )
    cleaned = " ".join(str(detail or "").split())[:500]
    if cleaned:
        return f"{base} Provider detail: {cleaned}"
    return base


def _anthropic_budget_preflight_error() -> str:
    """Surface already-known Anthropic pool exhaustion before trying fallbacks.

    The credential pool can know that every Anthropic OAuth credential is in an
    exhausted cooldown before the request starts. In that case `/fable` should
    return a terminal Fable-specific error, not a generic "route unavailable"
    that invites later code to try another model/provider.
    """
    try:
        from agent.credential_pool import STATUS_DEAD, STATUS_EXHAUSTED, load_pool

        pool = load_pool(FABLE_PROVIDER)
        if not pool.has_credentials() or pool.has_available():
            return ""
        entries = pool.entries()
    except Exception:
        return ""

    exhausted = [entry for entry in entries if getattr(entry, "last_status", None) == STATUS_EXHAUSTED]
    if exhausted:
        return _fable_budget_error_message(_summarize_pool_exhaustion(exhausted[0]))

    dead = [entry for entry in entries if getattr(entry, "last_status", None) == STATUS_DEAD]
    if dead and len(dead) == len(entries):
        return (
            "All configured Anthropic OAuth credentials are marked permanently unavailable; "
            "/fable is pinned to claude-fable-5 and will not fall back to another model or provider. "
            "Re-authenticate Anthropic before using `/fable` again."
        )

    return (
        "No selectable Anthropic OAuth credential is available for `/fable`; "
        "/fable is pinned to claude-fable-5 and will not fall back to another model or provider."
    )


def _summarize_pool_exhaustion(entry: Any) -> str:
    parts: list[str] = []
    code = getattr(entry, "last_error_code", None)
    reason = getattr(entry, "last_error_reason", None)
    message = getattr(entry, "last_error_message", None)
    reset_at = getattr(entry, "last_error_reset_at", None)
    if code:
        parts.append(f"status={code}")
    if reason:
        parts.append(f"reason={reason}")
    if reset_at:
        parts.append(f"reset_at={reset_at}")
    if message:
        parts.append(str(message))
    return "; ".join(parts)


def _fable_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    section = config.get("fable") if isinstance(config, dict) else {}
    return section if isinstance(section, dict) else {}


def _error(message: str) -> FablePlanResult:
    return FablePlanResult(False, "", FABLE_TRANSPORT, FABLE_MODEL, message)
