"""Claude Opus 5 helpers for Hermes slash commands."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from utils import base_url_host_matches


OPUS_PROVIDER = "anthropic"
OPUS_MODEL = "claude-opus-5"
OPUS_ROUTE = "anthropic_oauth"
OPUS_TRANSPORT = "anthropic_oauth"
OPUS_PROXY_ROUTE = "anthropic_proxy"
OPUS_PROXY_TRANSPORT = "anthropic_proxy"
OPUS_REASONING = {"enabled": True, "effort": "medium"}
OPUS_DEFAULT_TOOLSETS = ["file", "terminal", "web", "browser", "discord"]
OPUS_PLAN_MODE = "plan"
OPUS_IMPLEMENTATION_MODE = "implementation"

# Bare Discord `/opus` normally starts an implementation turn.  Keep clear
# natural-language requests for a plan on the safer plan-only route instead.
_OPUS_NATURAL_PLAN_INTENT = re.compile(
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

OPUS_RUNTIME_NOTE = """This is `/opus`: use Claude Opus 5, planning only, inspect repo with read-only tools as needed, save a plan artifact if the plan skill requires it, and do not implement. Do not edit files, create branches, open pull requests, deploy, or claim that implementation, tests, commits, PRs, or deployment happened. Generate a concrete Markdown implementation plan only. Your final answer must contain the full plan markdown, plus the saved path if you wrote one; do not answer with only a brief path/status note. Hermes/gateway will handle Discord delivery, threading, and artifact indexing outside the Opus turn, so do not describe or perform those operational steps."""

OPUS_IMPLEMENTATION_RUNTIME_NOTE = """This is Discord `/opus` implementation mode: use Claude Opus 5 as the model for orchestration, judgment, and review while following Hermes' normal Discord action-request policy. Opus changes the orchestration model, provenance, and coding-worker backend only; it does not change mutation authority, visual QA, or trusted closeout behavior. General analysis may use `delegate_task(read_only=true)`; all coding and implementation workers must use the normal `delegate_coding_task` workflow, which this turn pins to the Codex backend. Do not use OpenCode coding workers. Preserve structured handoffs, safe background scheduling, scope reservations, isolated parallel worktrees, merge-back, and post-worker behavior. Choose `model_tier` deliberately from the canonical values `trivial`, `basic`, `intermediate`, or `advanced`, using `advanced` for the hardest work. Escalate `model_tier` on retry; set explicit `reasoning_effort` only for exceptional overrides because routine routing uses the effort bundled with the selected tier. The Workdir in this request is the pre-provisioned mutable workspace: pass it as `cwd` when delegating or omit `cwd` to inherit it, and do not create a second checkout. Codex coding workers edit and run focused verification locally. The gateway-owned trusted closeout state machine handles commit, push, PR, CI, merge, canonical sync, and durable completion exactly as it does for an ordinary Discord action request. Never claim lifecycle completion from a background dispatch handle or worker prose alone; rely on the gateway's final closeout state."""


@dataclass(frozen=True)
class OpusPlanRequest:
    prompt: str
    session_id: str = ""
    workdir: str = ""
    source_text: str = ""
    platform: str = ""
    transport: str = OPUS_TRANSPORT
    max_tokens: int = 12000
    timeout_seconds: int = 300


def opus_metadata(
    config: dict[str, Any] | None = None,
    *,
    mode: str = OPUS_PLAN_MODE,
) -> dict[str, Any]:
    normalized_mode = normalize_opus_mode(mode)
    route = _opus_route(_opus_config(config))
    metadata = {
        "command": "opus",
        "opus_mode": normalized_mode,
        "route": route,
        "transport": _opus_transport(route),
        "provider": OPUS_PROVIDER,
        "model": OPUS_MODEL,
        "reply_to_mode": "all",
        # A proxy uses its own API key on the Hermes-facing hop, but may
        # terminate through Claude Code OAuth upstream.  Keep credential
        # identity separate from the tool-name compatibility that upstream
        # OAuth requires (no single-underscore ``mcp_`` names on the wire).
        "anthropic_oauth_tool_name_compat": route == OPUS_PROXY_ROUTE,
    }
    if normalized_mode == OPUS_PLAN_MODE:
        metadata.update(
            {
                "plan_artifact_kind": "opus_plan",
                "response_kind": "opus_plan",
                "kind": "opus_plan",
            }
        )
    else:
        metadata.update(
            {
                "response_kind": "opus_implementation",
                "kind": "opus_implementation",
                "coding_worker_backend": "codex",
            }
        )
    return metadata


def opus_enabled_toolsets(config: dict[str, Any] | None = None) -> list[str]:
    cfg = _opus_config(config)
    configured = cfg.get("enabled_toolsets")
    if isinstance(configured, list):
        toolsets = [str(item).strip() for item in configured if str(item).strip()]
        if toolsets:
            return toolsets
    return list(OPUS_DEFAULT_TOOLSETS)


def opus_reasoning_config(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return Opus's fixed reasoning level.

    Opus is a dedicated premium route with a deliberately pinned reasoning
    level. It intentionally ignores ``discord.action_request_reasoning_effort``
    and ``discord.feature_request_reasoning_effort``: those knobs size ordinary
    Discord action requests, and letting them raise or lower this route made
    the effort drift with unrelated config (a configured ``xhigh`` silently
    promoted every opus turn). ``config`` is accepted for call-site
    compatibility and is not read.
    """
    return dict(OPUS_REASONING)


def build_opus_user_instruction(request: OpusPlanRequest) -> str:
    sections = [
        ("User Request", request.prompt.strip()),
        ("Opus Plan-Only Contract", OPUS_RUNTIME_NOTE),
        ("Session", request.session_id.strip()),
        ("Platform", request.platform.strip()),
        ("Workdir", request.workdir.strip()),
        ("Source Text", request.source_text.strip()),
    ]
    rendered = [f"## {title}\n{value.strip()}" for title, value in sections if value]
    return "\n\n".join(rendered).strip()


def normalize_opus_mode(value: Any) -> str:
    """Normalize an Opus execution mode, preserving plan-only as the safe default."""
    raw = str(value or "").strip().lower()
    return OPUS_IMPLEMENTATION_MODE if raw == OPUS_IMPLEMENTATION_MODE else OPUS_PLAN_MODE


def parse_opus_command_args(args: str) -> tuple[str, str]:
    """Return ``(mode, request)`` for a Discord ``/opus`` command payload.

    The explicit ``plan`` subcommand and clear leading natural-language plan
    requests are plan-only. A bare Discord ``/opus <request>`` otherwise
    uses the implementation route; non-Discord callers decide their own mode
    before using this parser. Natural-language requests retain their full
    wording so the plan skill receives the user's intent unchanged.
    """
    request = str(args or "").strip()
    if not request:
        return OPUS_IMPLEMENTATION_MODE, ""
    match = re.match(r"^plan(?:\s+(.*))?$", request, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return OPUS_PLAN_MODE, str(match.group(1) or "").strip()
    if _OPUS_NATURAL_PLAN_INTENT.match(request):
        return OPUS_PLAN_MODE, request
    return OPUS_IMPLEMENTATION_MODE, request


def build_opus_implementation_instruction(request: OpusPlanRequest) -> str:
    """Build the user-turn payload for Discord Opus implementation mode."""
    sections = [
        ("User Request", request.prompt.strip()),
        ("Opus Implementation Contract", OPUS_IMPLEMENTATION_RUNTIME_NOTE),
        ("Session", request.session_id.strip()),
        ("Platform", request.platform.strip()),
        ("Workdir", request.workdir.strip()),
        ("Source Text", request.source_text.strip()),
    ]
    rendered = [f"## {title}\n{value.strip()}" for title, value in sections if value]
    return "\n\n".join(rendered).strip()


def build_opus_plan_invocation(request: OpusPlanRequest, task_id: str | None = None) -> str | None:
    from agent.skill_commands import build_skill_invocation_message, resolve_skill_command_key

    cmd_key = resolve_skill_command_key("plan")
    if cmd_key is None:
        return None
    return build_skill_invocation_message(
        cmd_key,
        build_opus_user_instruction(request),
        task_id=task_id or request.session_id or None,
        runtime_note=OPUS_RUNTIME_NOTE,
    )


def opus_session_model_override(config: dict[str, Any] | None = None) -> tuple[dict[str, str] | None, str]:
    cfg = _opus_config(config)
    if cfg.get("enabled") is False:
        return None, "/opus is disabled in Hermes config."
    if str(cfg.get("provider") or OPUS_PROVIDER).strip().lower() != OPUS_PROVIDER:
        return None, "/opus is pinned to provider=anthropic; configured provider is unsupported."
    if str(cfg.get("model") or OPUS_MODEL).strip() != OPUS_MODEL:
        return None, "/opus is pinned to model=claude-opus-5; configured model is unsupported."
    route = _opus_route(cfg)
    if route not in {OPUS_ROUTE, OPUS_PROXY_ROUTE}:
        return None, "/opus supports only route=anthropic_oauth or route=anthropic_proxy."
    configured_api_mode = str(cfg.get("api_mode") or "").strip()
    if configured_api_mode and configured_api_mode != "anthropic_messages":
        return None, "/opus is pinned to api_mode=anthropic_messages; configured API mode is unsupported."

    if route == OPUS_PROXY_ROUTE:
        return _opus_proxy_session_model_override(cfg)

    preflight_error = _anthropic_budget_preflight_error()
    if preflight_error:
        return None, preflight_error

    try:
        from agent.anthropic_adapter import _is_oauth_token
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(
            requested=OPUS_PROVIDER,
            target_model=OPUS_MODEL,
            credential_preference="pool_first",
        )
    except Exception as exc:
        return None, f"Opus 5 is not configured through Hermes' Anthropic route: {exc}"

    api_key = str(runtime.get("api_key") or "")
    if not api_key:
        route_error = _anthropic_budget_preflight_error()
        if route_error:
            return None, route_error
        return None, (
            "Opus 5 is not configured through Hermes' Anthropic route. "
            "Run `hermes auth add anthropic` or configure the Anthropic credential used by Hermes."
        )
    if str(runtime.get("provider") or "") != OPUS_PROVIDER:
        return None, f"Opus route resolved unexpected provider {runtime.get('provider')!r}; refusing to fall back."

    base_url = str(runtime.get("base_url") or "https://api.anthropic.com")
    api_mode = str(runtime.get("api_mode") or "")
    if api_mode != "anthropic_messages":
        return None, f"Opus route resolved unsupported API mode {api_mode!r}; refusing to fall back."
    if not _is_oauth_token(api_key) and base_url_host_matches(base_url, "api.anthropic.com"):
        return None, (
            "/opus permits API-key credentials only through a configured Hermes Anthropic proxy; "
            "direct api.anthropic.com requests require OAuth credentials."
        )

    return {
        "model": OPUS_MODEL,
        "provider": OPUS_PROVIDER,
        "api_key": api_key,
        "base_url": base_url,
        "api_mode": api_mode,
        "disable_fallback": "true",
    }, ""


def _opus_proxy_session_model_override(cfg: dict[str, Any]) -> tuple[dict[str, str] | None, str]:
    """Resolve the explicit API-key proxy route without consulting OAuth/pools."""
    key_env = str(cfg.get("key_env") or "").strip()
    base_url = str(cfg.get("base_url") or "").strip().rstrip("/")
    if not key_env:
        return None, "/opus Anthropic proxy route requires opus.key_env naming a profile secret."
    if not _ENV_VAR_NAME_RE.fullmatch(key_env):
        return None, "/opus Anthropic proxy route has an invalid opus.key_env name."
    if not base_url:
        return None, "/opus Anthropic proxy route requires opus.base_url."
    if not _is_valid_opus_proxy_base_url(base_url):
        return None, "/opus Anthropic proxy route has an invalid opus.base_url; use an absolute HTTP(S) proxy endpoint."
    if base_url_host_matches(base_url, "api.anthropic.com"):
        return None, (
            "/opus Anthropic proxy route refuses api.anthropic.com API-key routing; "
            "configure a non-public Anthropic-compatible proxy endpoint."
        )

    try:
        from agent.secret_scope import get_secret

        api_key = str(get_secret(key_env, "") or "").strip()
    except Exception as exc:
        return None, f"/opus Anthropic proxy route could not read opus.key_env safely: {exc}"
    if not api_key:
        return None, f"/opus Anthropic proxy route requires the configured secret {key_env}."

    return {
        "model": OPUS_MODEL,
        "provider": OPUS_PROVIDER,
        "api_key": api_key,
        "base_url": base_url,
        "api_mode": "anthropic_messages",
        "disable_fallback": "true",
    }, ""


def _opus_budget_error_message(detail: str = "") -> str:
    base = (
        "Opus 5 budget/quota is expended on Hermes' configured Anthropic route; "
        "/opus is pinned to claude-opus-5 and will not fall back to another model or provider."
    )
    cleaned = " ".join(str(detail or "").split())[:500]
    if cleaned:
        return f"{base} Provider detail: {cleaned}"
    return base


def _anthropic_budget_preflight_error() -> str:
    """Surface already-known Anthropic pool exhaustion before trying fallbacks.

    The credential pool can know that every Anthropic OAuth credential is in an
    exhausted cooldown before the request starts. In that case `/opus` should
    return a terminal Opus-specific error, not a generic "route unavailable"
    that invites later code to try another model/provider.
    """
    try:
        from agent.credential_pool import STATUS_DEAD, STATUS_EXHAUSTED, load_pool

        pool = load_pool(OPUS_PROVIDER)
        if not pool.has_credentials() or pool.has_available():
            return ""
        entries = pool.entries()
    except Exception:
        return ""

    exhausted = [entry for entry in entries if getattr(entry, "last_status", None) == STATUS_EXHAUSTED]
    if exhausted:
        return _opus_budget_error_message(_summarize_pool_exhaustion(exhausted[0]))

    dead = [entry for entry in entries if getattr(entry, "last_status", None) == STATUS_DEAD]
    if dead and len(dead) == len(entries):
        return (
            "All configured Anthropic OAuth credentials are marked permanently unavailable; "
            "/opus is pinned to claude-opus-5 and will not fall back to another model or provider. "
            "Re-authenticate Anthropic before using `/opus` again."
        )

    return (
        "No selectable Anthropic OAuth credential is available for `/opus`; "
        "/opus is pinned to claude-opus-5 and will not fall back to another model or provider."
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


def _opus_config(config: dict[str, Any] | None) -> dict[str, Any]:
    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config()
        except Exception:
            config = {}
    section = config.get("opus") if isinstance(config, dict) else {}
    return section if isinstance(section, dict) else {}


_ENV_VAR_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _opus_route(cfg: dict[str, Any]) -> str:
    return str(cfg.get("route") or OPUS_ROUTE).strip().lower()


def _opus_transport(route: str) -> str:
    return OPUS_PROXY_TRANSPORT if route == OPUS_PROXY_ROUTE else OPUS_TRANSPORT


def _is_valid_opus_proxy_base_url(base_url: str) -> bool:
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
