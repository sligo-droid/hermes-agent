"""Claude Fable 5 helpers for Hermes slash commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from utils import base_url_host_matches


FABLE_PROVIDER = "anthropic"
FABLE_MODEL = "claude-fable-5"
FABLE_ROUTE = "anthropic_oauth"
FABLE_TRANSPORT = "anthropic_oauth"
FABLE_PROXY_ROUTE = "anthropic_proxy"
FABLE_PROXY_TRANSPORT = "anthropic_proxy"
FABLE_REASONING = {"enabled": True, "effort": "high"}
FABLE_DEFAULT_TOOLSETS = ["file", "terminal", "web", "browser", "discord"]
FABLE_PLAN_MODE = "plan"
FABLE_IMPLEMENTATION_MODE = "implementation"

# Bare Discord `/fable` normally starts an implementation turn.  Keep clear
# natural-language requests for a plan on the safer plan-only route instead.
_FABLE_NATURAL_PLAN_INTENT = re.compile(
    r"""
    ^(?:
        (?:please\s+)?(?:make|create|write|draft)\s+(?:me\s+)?(?:an?\s+)?plan\b
        | (?:please\s+)?(?:help|assist)(?:\s+me)?\s+
          (?:(?:make|create|write|draft)\s+(?:me\s+)?(?:an?\s+)?plan|plan)\b
        | (?:can|could|would)\s+you(?:\s+please)?\s+
          (?:(?:make|create|write|draft)\s+(?:me\s+)?(?:an?\s+)?plan
          |(?:help|assist)(?:\s+me)?\s+plan)\b
    )
    """,
    re.IGNORECASE | re.VERBOSE | re.DOTALL,
)

FABLE_RUNTIME_NOTE = """This is `/fable`: use Claude Fable 5, planning only, inspect repo with read-only tools as needed, save a plan artifact if the plan skill requires it, and do not implement. Do not edit files, create branches, open pull requests, deploy, or claim that implementation, tests, commits, PRs, or deployment happened. Generate a concrete Markdown implementation plan only. Your final answer must contain the full plan markdown, plus the saved path if you wrote one; do not answer with only a brief path/status note. Hermes/gateway will handle Discord delivery, threading, and artifact indexing outside the Fable turn, so do not describe or perform those operational steps."""

FABLE_IMPLEMENTATION_RUNTIME_NOTE = """This is Discord `/fable` implementation mode: use Claude Fable 5 as the model for orchestration, judgment, and review while following Hermes' normal Discord action-request policy. Fable changes model and provenance only; it does not change tool policy, coding-worker routing, mutation authority, visual QA, or trusted closeout behavior. General analysis may use `delegate_task(read_only=true)`; coding uses the normal `delegate_coding_task` worker tiers, structured handoffs, safe background scheduling, scope reservations, isolated parallel worktrees, merge-back, and post-worker behavior. The Workdir in this request is the pre-provisioned mutable workspace: pass it as `cwd` when delegating or omit `cwd` to inherit it, and do not create a second checkout. Coding workers edit and run focused verification locally. The gateway-owned trusted closeout state machine handles commit, push, PR, CI, merge, canonical sync, and durable completion exactly as it does for an ordinary Discord action request. Never claim lifecycle completion from a background dispatch handle or worker prose alone; rely on the gateway's final closeout state."""


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


def fable_metadata(
    config: dict[str, Any] | None = None,
    *,
    mode: str = FABLE_PLAN_MODE,
) -> dict[str, Any]:
    normalized_mode = normalize_fable_mode(mode)
    route = _fable_route(_fable_config(config))
    metadata = {
        "command": "fable",
        "fable_mode": normalized_mode,
        "route": route,
        "transport": _fable_transport(route),
        "provider": FABLE_PROVIDER,
        "model": FABLE_MODEL,
        "reply_to_mode": "all",
        # A proxy uses its own API key on the Hermes-facing hop, but may
        # terminate through Claude Code OAuth upstream.  Keep credential
        # identity separate from the tool-name compatibility that upstream
        # OAuth requires (no single-underscore ``mcp_`` names on the wire).
        "anthropic_oauth_tool_name_compat": route == FABLE_PROXY_ROUTE,
    }
    if normalized_mode == FABLE_PLAN_MODE:
        metadata.update(
            {
                "plan_artifact_kind": "fable_plan",
                "response_kind": "fable_plan",
                "kind": "fable_plan",
            }
        )
    else:
        metadata.update(
            {
                "response_kind": "fable_implementation",
                "kind": "fable_implementation",
            }
        )
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


def normalize_fable_mode(value: Any) -> str:
    """Normalize a Fable execution mode, preserving plan-only as the safe default."""
    raw = str(value or "").strip().lower()
    return FABLE_IMPLEMENTATION_MODE if raw == FABLE_IMPLEMENTATION_MODE else FABLE_PLAN_MODE


def parse_fable_command_args(args: str) -> tuple[str, str]:
    """Return ``(mode, request)`` for a Discord ``/fable`` command payload.

    The explicit ``plan`` subcommand and clear leading natural-language plan
    requests are plan-only. A bare Discord ``/fable <request>`` otherwise
    uses the implementation route; non-Discord callers decide their own mode
    before using this parser. Natural-language requests retain their full
    wording so the plan skill receives the user's intent unchanged.
    """
    request = str(args or "").strip()
    if not request:
        return FABLE_IMPLEMENTATION_MODE, ""
    match = re.match(r"^plan(?:\s+(.*))?$", request, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return FABLE_PLAN_MODE, str(match.group(1) or "").strip()
    if _FABLE_NATURAL_PLAN_INTENT.match(request):
        return FABLE_PLAN_MODE, request
    return FABLE_IMPLEMENTATION_MODE, request


def build_fable_implementation_instruction(request: FablePlanRequest) -> str:
    """Build the user-turn payload for Discord Fable implementation mode."""
    sections = [
        ("User Request", request.prompt.strip()),
        ("Fable Implementation Contract", FABLE_IMPLEMENTATION_RUNTIME_NOTE),
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
    route = _fable_route(cfg)
    if route not in {FABLE_ROUTE, FABLE_PROXY_ROUTE}:
        return None, "/fable supports only route=anthropic_oauth or route=anthropic_proxy."
    configured_api_mode = str(cfg.get("api_mode") or "").strip()
    if configured_api_mode and configured_api_mode != "anthropic_messages":
        return None, "/fable is pinned to api_mode=anthropic_messages; configured API mode is unsupported."

    if route == FABLE_PROXY_ROUTE:
        return _fable_proxy_session_model_override(cfg)

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


def _fable_proxy_session_model_override(cfg: dict[str, Any]) -> tuple[dict[str, str] | None, str]:
    """Resolve the explicit API-key proxy route without consulting OAuth/pools."""
    key_env = str(cfg.get("key_env") or "").strip()
    base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not key_env:
        return None, "/fable Anthropic proxy route requires fable.key_env naming a profile secret."
    if not _ENV_VAR_NAME_RE.fullmatch(key_env):
        return None, "/fable Anthropic proxy route has an invalid fable.key_env name."
    if not base_url:
        return None, "/fable Anthropic proxy route requires fable.base_url."
    if not _is_valid_fable_proxy_base_url(base_url):
        return None, "/fable Anthropic proxy route has an invalid fable.base_url; use an absolute HTTP(S) proxy endpoint."
    if base_url_host_matches(base_url, "api.anthropic.com"):
        return None, (
            "/fable Anthropic proxy route refuses api.anthropic.com API-key routing; "
            "configure a non-public Anthropic-compatible proxy endpoint."
        )

    try:
        from agent.secret_scope import get_secret

        api_key = str(get_secret(key_env, "") or "").strip()
    except Exception as exc:
        return None, f"/fable Anthropic proxy route could not read fable.key_env safely: {exc}"
    if not api_key:
        return None, f"/fable Anthropic proxy route requires the configured secret {key_env}."

    return {
        "model": FABLE_MODEL,
        "provider": FABLE_PROVIDER,
        "api_key": api_key,
        "base_url": base_url,
        "api_mode": "anthropic_messages",
        "disable_fallback": "true",
    }, ""


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


_ENV_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _fable_route(cfg: dict[str, Any]) -> str:
    return str(cfg.get("route") or FABLE_ROUTE).strip().lower()


def _fable_transport(route: str) -> str:
    return FABLE_PROXY_TRANSPORT if route == FABLE_PROXY_ROUTE else FABLE_TRANSPORT


def _is_valid_fable_proxy_base_url(base_url: str) -> bool:
    parsed = urlparse(base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return False
    try:
        return parsed.port is None or 0 < parsed.port <= 65535
    except ValueError:
        return False
