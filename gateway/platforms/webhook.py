"""Generic webhook platform adapter.

Runs an aiohttp HTTP server that receives webhook POSTs from external
services (GitHub, GitLab, JIRA, Stripe, etc.), validates HMAC signatures,
transforms payloads into agent prompts, and routes responses back to the
source or to another configured platform.

Configuration lives in config.yaml under platforms.webhook.extra.routes.
Each route defines:
  - events: which event types to accept (header-based filtering)
  - secret or secret_env: HMAC secret for signature validation (REQUIRED)
  - prompt: template string formatted with the webhook payload
  - skills: optional list of skills to load for the agent
  - deliver: where to send the response (telegram, discord, log, etc.)
  - deliver_extra: additional delivery config (chat_id, etc.)
  - deliver_only: if true, skip the agent — the rendered prompt IS the
    message that gets delivered.  Use for external push notifications
    (Supabase, monitoring alerts, inter-agent pings) where zero LLM cost
    and sub-second delivery matter more than agent reasoning.

Security:
  - HMAC secret is required per route (validated at startup)
  - Rate limiting per route (fixed-window, configurable)
  - Idempotency cache prevents duplicate agent runs on webhook retries
  - Body size limits checked before reading payload
  - Set secret to "INSECURE_NO_AUTH" to skip validation (testing only)
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

try:
    from aiohttp import web

    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    web = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)

logger = logging.getLogger(__name__)

_BUILTIN_DELIVER_PLATFORMS = {
    "telegram", "discord", "slack", "signal", "sms", "whatsapp",
    "matrix", "mattermost", "homeassistant", "email", "dingtalk",
    "feishu", "wecom", "wecom_callback", "weixin", "bluebubbles",
    "qqbot", "yuanbao",
}

_GITHUB_PR_AMEND_REST_STATUS_REACTIONS = {"eyes", "rocket", "-1", "+1"}
_GITHUB_PR_AMEND_GRAPHQL_STATUS_REACTIONS = {"EYES", "ROCKET", "THUMBS_DOWN", "THUMBS_UP"}

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8644
_INSECURE_NO_AUTH = "INSECURE_NO_AUTH"
_DYNAMIC_ROUTES_FILENAME = "webhook_subscriptions.json"

# Hostnames/IP literals that only serve connections originating on the same
# machine. Anything else is treated as a public bind for safety-rail purposes.
_LOOPBACK_HOSTS = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "ip6-localhost",
    "ip6-loopback",
})


def _is_loopback_host(host: str) -> bool:
    """True when `host` binds only to the local machine.

    Covers IPv4 loopback, the standard `localhost` alias, IPv6 loopback in
    both bracketed and bare form, and the common Debian-style aliases. Any
    falsy value (empty string, None) is conservatively treated as non-loopback
    because an unset host usually means the platform-default public bind.
    """
    if not host:
        return False
    return host.strip().lower() in _LOOPBACK_HOSTS


def check_webhook_requirements() -> bool:
    """Check if webhook adapter dependencies are available."""
    return AIOHTTP_AVAILABLE


class WebhookAdapter(BasePlatformAdapter):
    """Generic webhook receiver that triggers agent runs from HTTP POSTs."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEBHOOK)
        self._host: str = config.extra.get("host", DEFAULT_HOST)
        self._port: int = int(config.extra.get("port", DEFAULT_PORT))
        self._global_secret: str = config.extra.get("secret", "")
        self._global_secret_env: str = config.extra.get("secret_env", "")
        self._static_routes: Dict[str, dict] = config.extra.get("routes", {})
        self._dynamic_routes: Dict[str, dict] = {}
        self._dynamic_routes_mtime: float = 0.0
        self._routes: Dict[str, dict] = dict(self._static_routes)
        self._runner = None

        # Delivery info keyed by session chat_id.
        #
        # Read by every send() invocation for the chat_id (status messages
        # AND the final response).  Cleaned up via TTL on each POST so the
        # dict stays bounded — see _prune_delivery_info().  Do NOT pop on
        # send(), or interim status messages (e.g. fallback notifications,
        # context-pressure warnings) will consume the entry before the
        # final response arrives, causing the response to silently fall
        # back to the "log" deliver type.
        self._delivery_info: Dict[str, dict] = {}
        self._delivery_info_created: Dict[str, float] = {}

        # Reference to gateway runner for cross-platform delivery (set externally)
        self.gateway_runner = None

        # Idempotency: TTL cache of recently processed delivery IDs.
        # Prevents duplicate agent runs when webhook providers retry.
        self._seen_deliveries: Dict[str, float] = {}
        self._idempotency_ttl: int = 3600  # 1 hour

        # Rate limiting: per-route timestamps in a fixed window.
        self._rate_counts: Dict[str, List[float]] = {}
        self._rate_limit: int = int(config.extra.get("rate_limit", 30))  # per minute

        # GitHub PR amendment routes enqueue accepted requests onto Discord/Kanban
        # worker boards. Keep an in-process branch lock so duplicate/tagged
        # events cannot race against the same ref while this gateway instance
        # is alive.
        self._github_pr_amend_locks: set[str] = set()
        self._github_pr_amend_lock_boards: dict[str, str] = {}
        self._github_pr_amend_reaction_actor_login: str | None = None

        # Body size limit (auth-before-body pattern)
        self._max_body_bytes: int = int(
            config.extra.get("max_body_bytes", 1_048_576)
        )  # 1MB

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        # Load agent-created subscriptions before validating
        self._reload_dynamic_routes()

        # Validate routes at startup — secret is required per route
        for name, route in self._routes.items():
            secret = self._resolve_secret(route, name)
            if not secret:
                raise ValueError(
                    f"[webhook] Route '{name}' has no HMAC secret. "
                    f"Set 'secret' or 'secret_env' on the route or globally. "
                    f"For testing without auth, set secret to '{_INSECURE_NO_AUTH}'."
                )

            # Safety rail: refuse to start if INSECURE_NO_AUTH is combined with a
            # non-loopback bind. The escape hatch is for local testing only;
            # serving an unauthenticated route on a public interface is a
            # deployment-grade footgun we'd rather crash early than ship.
            if secret == _INSECURE_NO_AUTH and not _is_loopback_host(self._host):
                raise ValueError(
                    f"[webhook] Route '{name}' uses INSECURE_NO_AUTH secret "
                    f"but is bound to non-loopback host '{self._host}'. "
                    f"INSECURE_NO_AUTH is for local testing only. "
                    f"Refusing to start to prevent accidental exposure."
                )
            # deliver_only routes bypass the agent — the POST body becomes a
            # direct push notification via the configured delivery target.
            # Validate up-front so misconfiguration surfaces at startup rather
            # than on the first webhook POST.
            if route.get("deliver_only"):
                deliver = route.get("deliver", "log")
                if not deliver or deliver == "log":
                    raise ValueError(
                        f"[webhook] Route '{name}' has deliver_only=true but "
                        f"deliver is '{deliver}'. Direct delivery requires a "
                        f"real target (telegram, discord, slack, etc.)."
                    )

        app = web.Application()
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/webhooks/{route_name}", self._handle_webhook)

        # Port conflict detection — fail fast if port is already in use
        import socket as _socket
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as _s:
                _s.settimeout(1)
                _s.connect(('127.0.0.1', self._port))
            logger.error('[webhook] Port %d already in use. Set a different port in config.yaml: platforms.webhook.port', self._port)
            return False
        except (ConnectionRefusedError, OSError):
            pass  # port is free

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        self._mark_connected()

        route_names = ", ".join(self._routes.keys()) or "(none configured)"
        logger.info(
            "[webhook] Listening on %s:%d — routes: %s",
            self._host,
            self._port,
            route_names,
        )
        return True

    async def disconnect(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        self._mark_disconnected()
        logger.info("[webhook] Disconnected")

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Deliver the agent's response to the configured destination.

        chat_id is ``webhook:{route}:{delivery_id}``.  The delivery info
        stored during webhook receipt is read with ``.get()`` (not popped)
        so that interim status messages emitted before the final response
        — fallback-model notifications, context-pressure warnings, etc. —
        do not consume the entry and silently downgrade the final response
        to the ``log`` deliver type.  TTL cleanup happens on POST.
        """
        delivery = self._delivery_info.get(chat_id, {})
        deliver_type = delivery.get("deliver", "log")

        if deliver_type == "log":
            logger.info("[webhook] Response for %s: %s", chat_id, content[:200])
            return SendResult(success=True)

        if deliver_type == "github_comment":
            return await self._deliver_github_comment(content, delivery)

        # Cross-platform delivery — any platform with a gateway adapter.
        # Check both built-in names and plugin-registered platforms.
        _is_known_platform = deliver_type in _BUILTIN_DELIVER_PLATFORMS
        if not _is_known_platform:
            try:
                from gateway.platform_registry import platform_registry
                _is_known_platform = platform_registry.is_registered(deliver_type)
            except Exception:
                pass
        if self.gateway_runner and _is_known_platform:
            return await self._deliver_cross_platform(
                deliver_type, content, delivery
            )

        logger.warning("[webhook] Unknown deliver type: %s", deliver_type)
        return SendResult(
            success=False, error=f"Unknown deliver type: {deliver_type}"
        )

    def _prune_delivery_info(self, now: float) -> None:
        """Drop delivery_info entries older than the idempotency TTL.

        Mirrors the cleanup pattern used for ``_seen_deliveries``.  Called
        on each POST so the dict size is bounded by ``rate_limit * TTL``
        even if many webhooks fire and never receive a final response.
        """
        cutoff = now - self._idempotency_ttl
        stale = [
            k
            for k, t in self._delivery_info_created.items()
            if t < cutoff
        ]
        for k in stale:
            self._delivery_info.pop(k, None)
            self._delivery_info_created.pop(k, None)

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "webhook"}

    # ------------------------------------------------------------------
    # HTTP handlers
    # ------------------------------------------------------------------

    async def _handle_health(self, request: "web.Request") -> "web.Response":
        """GET /health — simple health check."""
        return web.json_response({"status": "ok", "platform": "webhook"})

    def _resolve_secret(self, route_config: dict, route_name: str = "") -> str:
        """Resolve route/global secret values without logging secret contents."""
        route_secret = route_config.get("secret") or ""
        if route_secret:
            return str(route_secret)

        route_secret_env = route_config.get("secret_env") or ""
        if route_secret_env:
            secret = os.environ.get(str(route_secret_env), "")
            if secret:
                return secret
            logger.error(
                "[webhook] Route %s secret_env '%s' is unset or empty",
                route_name or "(unknown)",
                route_secret_env,
            )
            return ""

        if self._global_secret:
            return str(self._global_secret)

        if self._global_secret_env:
            secret = os.environ.get(str(self._global_secret_env), "")
            if secret:
                return secret
            logger.error(
                "[webhook] Global secret_env '%s' is unset or empty for route %s",
                self._global_secret_env,
                route_name or "(unknown)",
            )
            return ""

        return ""

    def _reload_dynamic_routes(self) -> None:
        """Reload agent-created subscriptions from disk if the file changed."""
        from hermes_constants import get_hermes_home
        hermes_home = get_hermes_home()
        subs_path = hermes_home / _DYNAMIC_ROUTES_FILENAME
        if not subs_path.exists():
            if self._dynamic_routes:
                self._dynamic_routes = {}
                self._routes = dict(self._static_routes)
                logger.debug("[webhook] Dynamic subscriptions file removed, cleared dynamic routes")
            return
        try:
            mtime = subs_path.stat().st_mtime
            if mtime <= self._dynamic_routes_mtime:
                return  # No change
            data = json.loads(subs_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return
            # Merge: static routes take precedence over dynamic ones.
            # Reject any dynamic route whose effective secret is empty —
            # an empty secret would cause _handle_webhook to skip HMAC
            # validation entirely, letting unauthenticated callers in.
            new_dynamic: Dict[str, dict] = {}
            for k, v in data.items():
                if k in self._static_routes:
                    continue
                effective_secret = self._resolve_secret(v, k)
                if not effective_secret:
                    logger.warning(
                        "[webhook] Dynamic route '%s' skipped: no resolved HMAC secret. "
                        "Set 'secret' or 'secret_env', or use '%s' to explicitly "
                        "disable auth (testing only).",
                        k,
                        _INSECURE_NO_AUTH,
                    )
                    continue
                if (
                    effective_secret == _INSECURE_NO_AUTH
                    and not _is_loopback_host(self._host)
                ):
                    logger.warning(
                        "[webhook] Dynamic route '%s' skipped: INSECURE_NO_AUTH "
                        "is only allowed on loopback hosts. Current host: '%s'.",
                        k,
                        self._host,
                    )
                    continue
                new_dynamic[k] = v
            self._dynamic_routes = new_dynamic
            self._routes = {**self._dynamic_routes, **self._static_routes}
            self._dynamic_routes_mtime = mtime
            logger.info(
                "[webhook] Reloaded %d dynamic route(s): %s",
                len(self._dynamic_routes),
                ", ".join(self._dynamic_routes.keys()) or "(none)",
            )
        except Exception as e:
            logger.error("[webhook] Failed to reload dynamic routes: %s", e)

    async def _handle_webhook(self, request: "web.Request") -> "web.Response":
        """POST /webhooks/{route_name} — receive and process a webhook event."""
        # Hot-reload dynamic subscriptions on each request (mtime-gated, cheap)
        self._reload_dynamic_routes()

        route_name = request.match_info.get("route_name", "")
        route_config = self._routes.get(route_name)

        if not route_config:
            return web.json_response(
                {"error": f"Unknown route: {route_name}"}, status=404
            )

        # ── Auth-before-body ─────────────────────────────────────
        # Check Content-Length before reading the full payload.
        content_length = request.content_length or 0
        if content_length > self._max_body_bytes:
            return web.json_response(
                {"error": "Payload too large"}, status=413
            )

        # Read body (must be done before any validation)
        try:
            raw_body = await request.read()
        except Exception as e:
            logger.error("[webhook] Failed to read body: %s", e)
            return web.json_response({"error": "Bad request"}, status=400)

        # Validate HMAC signature FIRST (skip only for the explicit local-test
        # INSECURE_NO_AUTH mode). Missing/empty secrets must fail closed here,
        # not only during connect(), so direct handler reuse cannot turn a
        # network webhook route into an unauthenticated agent-dispatch surface.
        secret = self._resolve_secret(route_config, route_name)
        if not secret:
            logger.error(
                "[webhook] Route %s has no HMAC secret; refusing request",
                route_name,
            )
            return web.json_response(
                {"error": "Webhook route is missing an HMAC secret"},
                status=403,
            )
        if secret != _INSECURE_NO_AUTH:
            if not self._validate_signature(request, raw_body, secret):
                logger.warning(
                    "[webhook] Invalid signature for route %s", route_name
                )
                return web.json_response(
                    {"error": "Invalid signature"}, status=401
                )

        # ── Rate limiting (after auth) ───────────────────────────
        now = time.time()
        window = self._rate_counts.setdefault(route_name, [])
        window[:] = [t for t in window if now - t < 60]
        if len(window) >= self._rate_limit:
            return web.json_response(
                {"error": "Rate limit exceeded"}, status=429
            )
        window.append(now)

        # Parse payload
        try:
            payload = json.loads(raw_body)
        except json.JSONDecodeError:
            # Try form-encoded as fallback
            try:
                import urllib.parse

                payload = dict(
                    urllib.parse.parse_qsl(raw_body.decode("utf-8"))
                )
            except Exception:
                return web.json_response(
                    {"error": "Cannot parse body"}, status=400
                )

        # Check event type filter
        event_type = (
            request.headers.get("X-GitHub-Event", "")
            or request.headers.get("X-GitLab-Event", "")
            or payload.get("event_type", "")
            or payload.get("type", "")
            or "unknown"
        )
        allowed_events = route_config.get("events", [])
        if allowed_events and event_type not in allowed_events:
            logger.debug(
                "[webhook] Ignoring event %s for route %s (allowed: %s)",
                event_type,
                route_name,
                allowed_events,
            )
            return web.json_response(
                {"status": "ignored", "event": event_type}
            )

        # Build a unique delivery ID
        delivery_id = request.headers.get(
            "X-GitHub-Delivery",
            request.headers.get(
                "svix-id",
                request.headers.get("X-Request-ID", str(int(time.time() * 1000))),
            ),
        )

        # ── Idempotency ─────────────────────────────────────────
        # Skip duplicate deliveries (webhook retries).
        now = time.time()
        # Prune expired entries
        self._seen_deliveries = {
            k: v
            for k, v in self._seen_deliveries.items()
            if now - v < self._idempotency_ttl
        }
        if delivery_id in self._seen_deliveries:
            logger.info(
                "[webhook] Skipping duplicate delivery %s", delivery_id
            )
            return web.json_response(
                {"status": "duplicate", "delivery_id": delivery_id},
                status=200,
            )
        self._seen_deliveries[delivery_id] = now

        # ── Specialized GitHub PR amendment mode ─────────────────
        # This route is intentionally not a normal prompt-to-agent webhook. It
        # performs deterministic sender/repo/branch gates, then starts a
        # bounded coding worker that may amend and push the PR head branch.
        if self._is_github_pr_amend_route(route_config):
            return await self._handle_github_pr_amend(
                route_name=route_name,
                route_config=route_config,
                payload=payload,
                event_type=event_type,
                delivery_id=delivery_id,
            )

        # Format prompt from template for generic prompt/direct-delivery routes.
        # Specialized routes above intentionally skip this prompt path so
        # untrusted GitHub comment text cannot accidentally become the top-level
        # agent instruction before deterministic policy gates run.
        prompt_template = route_config.get("prompt", "")
        prompt = self._render_prompt(
            prompt_template, payload, event_type, route_name
        )

        # Inject skill content if configured.
        # We call build_skill_invocation_message() directly rather than
        # using /skill-name slash commands — the gateway's command parser
        # would intercept those and break the flow.
        skills = route_config.get("skills", [])
        if skills:
            try:
                from agent.skill_commands import (
                    build_skill_invocation_message,
                    get_skill_commands,
                )

                skill_cmds = get_skill_commands()
                for skill_name in skills:
                    cmd_key = f"/{skill_name}"
                    if cmd_key in skill_cmds:
                        skill_content = build_skill_invocation_message(
                            cmd_key, user_instruction=prompt
                        )
                        if skill_content:
                            prompt = skill_content
                            break  # Load the first matching skill
                    else:
                        logger.warning(
                            "[webhook] Skill '%s' not found", skill_name
                        )
            except Exception as e:
                logger.warning("[webhook] Skill loading failed: %s", e)

        # ── Direct delivery mode (deliver_only) ─────────────────
        # Skip the agent entirely — the rendered prompt IS the message we
        # deliver.  Use case: external services (Supabase, monitoring,
        # cron jobs, other agents) that need to push a plain notification
        # to a user's chat with zero LLM cost.  Reuses the same HMAC auth,
        # rate limiting, idempotency, and template rendering as agent mode.
        if route_config.get("deliver_only"):
            delivery = {
                "deliver": route_config.get("deliver", "log"),
                "deliver_extra": self._render_delivery_extra(
                    route_config.get("deliver_extra", {}), payload
                ),
                "payload": payload,
            }
            logger.info(
                "[webhook] direct-deliver event=%s route=%s target=%s msg_len=%d delivery=%s",
                event_type,
                route_name,
                delivery["deliver"],
                len(prompt),
                delivery_id,
            )
            try:
                result = await self._direct_deliver(prompt, delivery)
            except Exception:
                logger.exception(
                    "[webhook] direct-deliver failed route=%s delivery=%s",
                    route_name,
                    delivery_id,
                )
                return web.json_response(
                    {"status": "error", "error": "Delivery failed", "delivery_id": delivery_id},
                    status=502,
                )

            if result.success:
                return web.json_response(
                    {
                        "status": "delivered",
                        "route": route_name,
                        "target": delivery["deliver"],
                        "delivery_id": delivery_id,
                    },
                    status=200,
                )
            # Delivery attempted but target rejected it — surface as 502
            # with a generic error (don't leak adapter-level detail).
            logger.warning(
                "[webhook] direct-deliver target rejected route=%s target=%s error=%s",
                route_name,
                delivery["deliver"],
                result.error,
            )
            return web.json_response(
                {"status": "error", "error": "Delivery failed", "delivery_id": delivery_id},
                status=502,
            )

        # Use delivery_id in session key so concurrent webhooks on the
        # same route get independent agent runs (not queued/interrupted).
        session_chat_id = f"webhook:{route_name}:{delivery_id}"

        # Store delivery info for send().  Read by every send() invocation
        # for this chat_id (interim status messages and the final response),
        # so we do NOT pop on send.  TTL-based cleanup keeps the dict bounded.
        deliver_config = {
            "deliver": route_config.get("deliver", "log"),
            "deliver_extra": self._render_delivery_extra(
                route_config.get("deliver_extra", {}), payload
            ),
            "payload": payload,
        }
        self._delivery_info[session_chat_id] = deliver_config
        self._delivery_info_created[session_chat_id] = now
        self._prune_delivery_info(now)

        # Build source and event
        source = self.build_source(
            chat_id=session_chat_id,
            chat_name=f"webhook/{route_name}",
            chat_type="webhook",
            user_id=f"webhook:{route_name}",
            user_name=route_name,
        )
        event = MessageEvent(
            text=prompt,
            message_type=MessageType.TEXT,
            source=source,
            raw_message=payload,
            message_id=delivery_id,
        )

        logger.info(
            "[webhook] %s event=%s route=%s prompt_len=%d delivery=%s",
            request.method,
            event_type,
            route_name,
            len(prompt),
            delivery_id,
        )

        # Non-blocking — return 202 Accepted immediately
        task = asyncio.create_task(self.handle_message(event))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

        return web.json_response(
            {
                "status": "accepted",
                "route": route_name,
                "event": event_type,
                "delivery_id": delivery_id,
            },
            status=202,
        )

    # ------------------------------------------------------------------
    # Specialized GitHub PR amendment mode
    # ------------------------------------------------------------------

    def _is_github_pr_amend_route(self, route_config: dict) -> bool:
        """True when a route should use the PR-amend coding-worker path."""
        mode = str(route_config.get("mode") or route_config.get("type") or "").strip().lower()
        return mode == "github_pr_amend" or bool(route_config.get("github_pr_amend"))

    def _github_pr_amend_lock_is_active(self, lock_key: str) -> bool:
        board = self._github_pr_amend_lock_boards.get(lock_key, "")
        if not board:
            return True
        try:
            from hermes_cli import kanban_db
            from hermes_cli.discord_worker_roles import (
                DISCORD_WORKER_META_KEY,
                TERMINAL_GOAL_STATUSES,
            )

            metadata = kanban_db.read_board_metadata(board)
            worker = metadata.get(DISCORD_WORKER_META_KEY)
            worker = worker if isinstance(worker, dict) else {}
            goal_status = str(worker.get("goal_status") or "").strip().lower()
            phase = str(worker.get("phase") or "").strip().lower()
            terminal = bool(
                worker.get("cancelled")
                or goal_status in TERMINAL_GOAL_STATUSES
                or phase == "complete"
            )
        except Exception:
            logger.debug(
                "[github-pr-amend] treating unreadable lock board as active lock=%s board=%s",
                lock_key,
                board,
                exc_info=True,
            )
            return True
        if terminal:
            self._github_pr_amend_locks.discard(lock_key)
            self._github_pr_amend_lock_boards.pop(lock_key, None)
            return False
        return True

    async def _handle_github_pr_amend(
        self,
        *,
        route_name: str,
        route_config: dict,
        payload: dict,
        event_type: str,
        delivery_id: str,
    ) -> Any:
        """Gate a GitHub mention event and enqueue a Discord/Kanban worker board."""
        assert web is not None
        from gateway.github_pr_amend import (
            GitHubPrAmendError,
            build_pr_amend_discord_card,
            build_pr_amend_intake_artifact,
            evaluate_request,
            extract_request,
            fetch_pr_info,
            fetch_pr_related_context,
            github_pr_amend_reaction_targets,
            policy_from_route,
            preflight_request,
            publish_and_activate_pr_amend_intake,
            resolve_pr_amend_existing_discord_route,
            resolve_pr_amend_discord_channel,
            write_pr_amend_intake_artifact,
        )

        try:
            policy = policy_from_route(route_config)
            request = extract_request(
                event_type,
                payload,
                delivery_id=delivery_id,
            )
        except GitHubPrAmendError as exc:
            logger.info(
                "[github-pr-amend] ignored route=%s delivery=%s reason=%s",
                route_name,
                delivery_id,
                exc,
            )
            return web.json_response(
                {"status": "ignored", "route": route_name, "reason": str(exc)},
                status=200,
            )
        except Exception as exc:
            logger.exception(
                "[github-pr-amend] failed to normalize webhook route=%s delivery=%s",
                route_name,
                delivery_id,
            )
            return web.json_response(
                {"status": "error", "error": "Failed to normalize GitHub event"},
                status=400,
            )

        preflight_reason = preflight_request(request, policy)
        if preflight_reason:
            logger.info(
                "[github-pr-amend] ignored before PR lookup route=%s repo=%s pr=%s delivery=%s reason=%s",
                route_name,
                request.repo,
                request.pr_number,
                delivery_id,
                preflight_reason,
            )
            return web.json_response(
                {
                    "status": "ignored",
                    "route": route_name,
                    "reason": preflight_reason,
                    "delivery_id": delivery_id,
                },
                status=200,
            )

        try:
            pr_info = await asyncio.to_thread(fetch_pr_info, request.repo, request.pr_number)
        except Exception as exc:
            logger.warning(
                "[github-pr-amend] failed to fetch PR metadata route=%s repo=%s pr=%s delivery=%s: %s",
                route_name,
                request.repo,
                request.pr_number,
                delivery_id,
                exc,
            )
            self._seen_deliveries.pop(delivery_id, None)
            return web.json_response(
                {"status": "error", "error": "Failed to fetch PR metadata"},
                status=502,
            )

        decision = evaluate_request(request, pr_info, policy)
        if not decision.accepted:
            logger.info(
                "[github-pr-amend] ignored route=%s repo=%s pr=%s delivery=%s reason=%s",
                route_name,
                request.repo,
                request.pr_number,
                delivery_id,
                decision.reason,
            )
            return web.json_response(
                {
                    "status": "ignored",
                    "route": route_name,
                    "reason": decision.reason,
                    "delivery_id": delivery_id,
                },
                status=200,
            )

        if (
            decision.lock_key in self._github_pr_amend_locks
            and self._github_pr_amend_lock_is_active(decision.lock_key)
        ):
            logger.info(
                "[github-pr-amend] branch already locked route=%s lock=%s delivery=%s",
                route_name,
                decision.lock_key,
                delivery_id,
            )
            return web.json_response(
                {
                    "status": "locked",
                    "route": route_name,
                    "lock_key": decision.lock_key,
                    "delivery_id": delivery_id,
                },
                status=409,
            )

        self._github_pr_amend_locks.add(decision.lock_key)
        reaction_metadata: dict[str, Any] = {}
        try:
            fetched_context = await asyncio.to_thread(
                fetch_pr_related_context,
                request.repo,
                request.pr_number,
            )
            artifact = build_pr_amend_intake_artifact(
                request,
                decision,
                policy,
                pr_info,
                payload,
                fetched_context,
            )
            artifact_path = write_pr_amend_intake_artifact(artifact)
            reaction_metadata = {
                "repo": request.repo,
                "pr_number": str(request.pr_number),
                "source_kind": request.source_kind,
                "source_id": request.source_id,
                "source_node_id": request.source_node_id,
                "reaction_targets": github_pr_amend_reaction_targets(artifact),
            }
            channel_id = resolve_pr_amend_discord_channel(route_config, request)
            existing_route = resolve_pr_amend_existing_discord_route(artifact)
            if existing_route:
                channel_id = str(existing_route.get("discord_channel_id") or channel_id or "").strip()
            degraded_reason = self._github_pr_amend_degraded_reason(
                source_kind=request.source_kind,
                channel_id=channel_id,
                existing_route=existing_route,
            )
            if degraded_reason:
                self._github_pr_amend_locks.discard(decision.lock_key)
                self._github_pr_amend_lock_boards.pop(decision.lock_key, None)
                await self._safe_github_pr_amend_reactions(reaction_metadata, request, "-1")
                return web.json_response(
                    {
                        "status": "degraded",
                        "route": route_name,
                        "reason": degraded_reason,
                        "discord_dispatch": "skipped",
                        "delivery_id": delivery_id,
                        "lock_key": decision.lock_key,
                        "pr": request.pr_number,
                        "artifact_path": str(artifact_path),
                    },
                    status=202,
                )
            card = build_pr_amend_discord_card(artifact, artifact_path=artifact_path)
            if isinstance(card.get("github_pr_amend"), dict):
                reaction_metadata = card["github_pr_amend"]
            await self._safe_github_pr_amend_reactions(reaction_metadata, request, "eyes")
            await self._safe_github_pr_amend_reactions(reaction_metadata, request, "rocket")
            discord_metadata = await asyncio.to_thread(
                publish_and_activate_pr_amend_intake,
                card,
                channel_id=channel_id,
                existing=existing_route or None,
            )
            if not str(discord_metadata.get("discord_board") or "").strip():
                raise RuntimeError("Discord activation did not return a worker board")
        except Exception:
            self._github_pr_amend_locks.discard(decision.lock_key)
            self._github_pr_amend_lock_boards.pop(decision.lock_key, None)
            self._seen_deliveries.pop(delivery_id, None)
            logger.exception(
                "[github-pr-amend] failed to queue worker-board intake route=%s delivery=%s",
                route_name,
                delivery_id,
            )
            await self._safe_github_pr_amend_reactions(reaction_metadata, request, "-1")
            return web.json_response(
                {
                    "status": "error",
                    "error": "Failed to queue PR amendment worker board",
                    "delivery_id": delivery_id,
                    **({"artifact_path": str(locals().get("artifact_path"))} if locals().get("artifact_path") else {}),
                },
                status=500,
            )

        logger.info(
            "[github-pr-amend] queued route=%s repo=%s pr=%s lock=%s delivery=%s board=%s",
            route_name,
            request.repo,
            request.pr_number,
            decision.lock_key,
            delivery_id,
            discord_metadata.get("discord_board"),
        )
        self._github_pr_amend_lock_boards[decision.lock_key] = str(
            discord_metadata.get("discord_board") or ""
        )
        return web.json_response(
            {
                "status": "queued",
                "route": route_name,
                "event": event_type,
                "delivery_id": delivery_id,
                "lock_key": decision.lock_key,
                "pr": request.pr_number,
                "artifact_path": str(artifact_path),
                **discord_metadata,
            },
            status=202,
        )

    @staticmethod
    def _github_pr_amend_degraded_reason(
        *,
        source_kind: str,
        channel_id: str,
        existing_route: dict[str, Any],
    ) -> str:
        if not channel_id:
            return "missing_discord_route"
        return ""

    def _github_pr_amend_reaction_requests(
        self,
        metadata: dict[str, Any],
        fallback: Any | None = None,
    ) -> list[Any]:
        """Build reaction request objects from stored PR-amend metadata."""

        base = metadata if isinstance(metadata, dict) else {}
        raw_targets = base.get("reaction_targets")
        targets = raw_targets if isinstance(raw_targets, list) else []
        requests: list[Any] = []
        for target in targets:
            if not isinstance(target, dict):
                continue
            source_kind = str(target.get("source_kind") or "").strip()
            source_id = str(target.get("source_id") or "").strip()
            source_node_id = str(target.get("source_node_id") or "").strip()
            if not source_kind or not (source_id or source_node_id):
                continue
            requests.append(
                SimpleNamespace(
                    repo=str(
                        target.get("repo")
                        or base.get("repo")
                        or base.get("upstream_repo")
                        or getattr(fallback, "repo", "")
                        or ""
                    ),
                    pr_number=str(
                        target.get("pr_number")
                        or base.get("pr_number")
                        or base.get("upstream_pr_number")
                        or getattr(fallback, "pr_number", "")
                        or ""
                    ),
                    source_kind=source_kind,
                    source_id=source_id,
                    source_node_id=source_node_id,
                )
            )
        if requests:
            return requests
        if fallback is not None:
            return [fallback]
        return [
            SimpleNamespace(
                repo=str(base.get("repo") or base.get("upstream_repo") or ""),
                pr_number=str(base.get("pr_number") or base.get("upstream_pr_number") or ""),
                source_kind=str(base.get("source_kind") or ""),
                source_id=str(base.get("source_id") or ""),
                source_node_id=str(base.get("source_node_id") or ""),
            )
        ]

    def _github_pr_amend_reaction_endpoint(self, request: Any) -> str:
        """Return the GitHub reactions endpoint for a PR-amend trigger."""
        repo = getattr(request, "repo", "")
        source_kind = getattr(request, "source_kind", "")
        source_id = getattr(request, "source_id", "")
        if source_kind == "issue_comment" and source_id:
            return f"repos/{repo}/issues/comments/{source_id}/reactions"
        if source_kind == "review_comment" and source_id:
            return f"repos/{repo}/pulls/comments/{source_id}/reactions"
        return ""

    async def _safe_github_pr_amend_reactions(
        self,
        metadata: dict[str, Any],
        fallback: Any,
        content: str,
    ) -> None:
        request_metadata = metadata
        if content == "-1" and isinstance(metadata, dict):
            request_metadata = {key: value for key, value in metadata.items() if key != "reaction_targets"}
        for request in self._github_pr_amend_reaction_requests(request_metadata, fallback):
            await self._safe_github_pr_amend_reaction(request, content)

    async def _safe_github_pr_amend_reaction(self, request: Any, content: str) -> None:
        """Best-effort GitHub reaction; never block or fail the amend job."""
        try:
            await self._add_github_pr_amend_reaction(request, content)
        except Exception:
            logger.exception(
                "[github-pr-amend] reaction failed unexpectedly repo=%s pr=%s kind=%s id=%s content=%s",
                getattr(request, "repo", ""),
                getattr(request, "pr_number", ""),
                getattr(request, "source_kind", ""),
                getattr(request, "source_id", ""),
                content,
            )

    async def sync_github_pr_amend_terminal_reaction(self, metadata: dict[str, Any], state: str) -> bool:
        """Best-effort terminal reaction sync for a PR-amend trigger."""
        normalized_state = str(state or "").strip().lower()
        if normalized_state == "blocked":
            return True
        content = "+1" if normalized_state == "done" else "-1"
        request_metadata = metadata
        if content == "-1":
            # A worker-board blocked/error state is aggregate state for the trigger.
            # Do not stamp every fetched inline child comment as failed merely
            # because the parent review/discussion is non-green.
            request_metadata = {key: value for key, value in metadata.items() if key != "reaction_targets"}
        requests = self._github_pr_amend_reaction_requests(request_metadata)
        if not requests:
            return False
        results = [await self._add_github_pr_amend_reaction(request, content) for request in requests]
        return all(results)

    async def _add_github_pr_amend_reaction(self, request: Any, content: str) -> bool:
        """Add a GitHub reaction to the triggering object via ``gh api``."""
        if getattr(request, "source_kind", "") == "review":
            return await self._add_github_pr_amend_graphql_reaction(request, content)

        endpoint = self._github_pr_amend_reaction_endpoint(request)
        if not endpoint:
            logger.warning(
                "[github-pr-amend] no reaction endpoint repo=%s pr=%s kind=%s id=%s content=%s",
                getattr(request, "repo", ""),
                getattr(request, "pr_number", ""),
                getattr(request, "source_kind", ""),
                getattr(request, "source_id", ""),
                content,
            )
            return False

        if await self._delete_prior_github_pr_amend_reactions(request, endpoint, content):
            return True
        argv = [
            "gh",
            "api",
            "-X",
            "POST",
            endpoint,
            "-f",
            f"content={content}",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
        ]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                argv,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(
                "[github-pr-amend] reaction command failed repo=%s pr=%s kind=%s id=%s content=%s error=%s",
                getattr(request, "repo", ""),
                getattr(request, "pr_number", ""),
                getattr(request, "source_kind", ""),
                getattr(request, "source_id", ""),
                content,
                exc,
            )
            return False

        if result.returncode != 0:
            logger.warning(
                "[github-pr-amend] gh reaction failed repo=%s pr=%s kind=%s id=%s content=%s exit=%s stderr=%s",
                getattr(request, "repo", ""),
                getattr(request, "pr_number", ""),
                getattr(request, "source_kind", ""),
                getattr(request, "source_id", ""),
                content,
                result.returncode,
                (result.stderr or "").strip()[:500],
            )
            return False

        logger.info(
            "[github-pr-amend] added reaction repo=%s pr=%s kind=%s id=%s content=%s",
            getattr(request, "repo", ""),
            getattr(request, "pr_number", ""),
            getattr(request, "source_kind", ""),
            getattr(request, "source_id", ""),
            content,
        )
        return True

    def _github_pr_amend_graphql_reaction_content(self, content: str) -> str:
        return {
            "+1": "THUMBS_UP",
            "-1": "THUMBS_DOWN",
            "laugh": "LAUGH",
            "confused": "CONFUSED",
            "heart": "HEART",
            "hooray": "HOORAY",
            "rocket": "ROCKET",
            "eyes": "EYES",
        }.get(str(content or ""), "")

    async def _add_github_pr_amend_graphql_reaction(self, request: Any, content: str) -> bool:
        """Add a reaction to a PR review summary via GraphQL Reactable nodes."""

        node_id = str(getattr(request, "source_node_id", "") or "").strip()
        reaction_content = self._github_pr_amend_graphql_reaction_content(content)
        if not node_id or not reaction_content:
            logger.warning(
                "[github-pr-amend] no GraphQL reaction target repo=%s pr=%s kind=%s id=%s node=%s content=%s",
                getattr(request, "repo", ""),
                getattr(request, "pr_number", ""),
                getattr(request, "source_kind", ""),
                getattr(request, "source_id", ""),
                node_id,
                content,
            )
            return False

        existing = await self._github_pr_amend_graphql_viewer_reactions(node_id)
        status_reactions = _GITHUB_PR_AMEND_GRAPHQL_STATUS_REACTIONS
        if existing is not None:
            for prior in sorted(status_reactions - {reaction_content}):
                if prior in existing:
                    await self._remove_github_pr_amend_graphql_reaction(node_id, prior)
            if reaction_content in existing:
                return True

        query = """
mutation($subjectId: ID!, $content: ReactionContent!) {
  addReaction(input: {subjectId: $subjectId, content: $content}) {
    reaction { content }
    subject { id }
  }
}
""".strip()
        return await self._run_github_pr_amend_graphql(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"subjectId={node_id}",
                "-F",
                f"content={reaction_content}",
            ],
            action="add",
            node_id=node_id,
            content=reaction_content,
        )

    async def _github_pr_amend_graphql_viewer_reactions(self, node_id: str) -> set[str] | None:
        query = """
query($id: ID!) {
  node(id: $id) {
    ... on Reactable {
      reactionGroups { content viewerHasReacted }
    }
  }
}
""".strip()
        argv = ["gh", "api", "graphql", "-f", f"query={query}", "-F", f"id={node_id}"]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                argv,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("[github-pr-amend] GraphQL reaction query failed node=%s error=%s", node_id, exc)
            return None
        if result.returncode != 0:
            logger.warning(
                "[github-pr-amend] gh GraphQL reaction query failed node=%s exit=%s stderr=%s",
                node_id,
                result.returncode,
                (result.stderr or "").strip()[:500],
            )
            return None
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            logger.warning("[github-pr-amend] gh GraphQL reaction query returned invalid JSON node=%s", node_id)
            return None
        if data.get("errors"):
            logger.warning("[github-pr-amend] gh GraphQL reaction query returned errors node=%s errors=%s", node_id, data.get("errors"))
            return None
        node = data.get("data", {}).get("node") if isinstance(data.get("data"), dict) else None
        groups = node.get("reactionGroups") if isinstance(node, dict) else None
        if not isinstance(groups, list):
            return None
        return {
            str(group.get("content") or "")
            for group in groups
            if isinstance(group, dict) and group.get("viewerHasReacted")
        }

    async def _remove_github_pr_amend_graphql_reaction(self, node_id: str, reaction_content: str) -> bool:
        query = """
mutation($subjectId: ID!, $content: ReactionContent!) {
  removeReaction(input: {subjectId: $subjectId, content: $content}) {
    subject { id }
  }
}
""".strip()
        return await self._run_github_pr_amend_graphql(
            [
                "gh",
                "api",
                "graphql",
                "-f",
                f"query={query}",
                "-F",
                f"subjectId={node_id}",
                "-F",
                f"content={reaction_content}",
            ],
            action="remove",
            node_id=node_id,
            content=reaction_content,
        )

    async def _run_github_pr_amend_graphql(
        self,
        argv: list[str],
        *,
        action: str,
        node_id: str,
        content: str,
    ) -> bool:
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                argv,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning(
                "[github-pr-amend] GraphQL reaction %s failed node=%s content=%s error=%s",
                action,
                node_id,
                content,
                exc,
            )
            return False
        if result.returncode != 0:
            logger.warning(
                "[github-pr-amend] gh GraphQL reaction %s failed node=%s content=%s exit=%s stderr=%s",
                action,
                node_id,
                content,
                result.returncode,
                (result.stderr or "").strip()[:500],
            )
            return False
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            logger.warning(
                "[github-pr-amend] gh GraphQL reaction %s returned invalid JSON node=%s content=%s",
                action,
                node_id,
                content,
            )
            return False
        if data.get("errors"):
            logger.warning(
                "[github-pr-amend] gh GraphQL reaction %s returned errors node=%s content=%s errors=%s",
                action,
                node_id,
                content,
                data.get("errors"),
            )
            return False
        logger.info("[github-pr-amend] GraphQL reaction %s node=%s content=%s", action, node_id, content)
        return True

    async def _github_pr_amend_authenticated_login(self) -> str:
        """Return the login used by gh for PR-amend reactions, cached per adapter."""

        if self._github_pr_amend_reaction_actor_login is not None:
            return self._github_pr_amend_reaction_actor_login
        argv = ["gh", "api", "user"]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                argv,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("[github-pr-amend] reaction actor lookup failed: %s", exc)
            self._github_pr_amend_reaction_actor_login = ""
            return ""
        if result.returncode != 0:
            logger.warning(
                "[github-pr-amend] gh reaction actor lookup failed exit=%s stderr=%s",
                result.returncode,
                (result.stderr or "").strip()[:500],
            )
            self._github_pr_amend_reaction_actor_login = ""
            return ""
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            logger.warning("[github-pr-amend] gh reaction actor lookup returned invalid JSON")
            self._github_pr_amend_reaction_actor_login = ""
            return ""
        if not isinstance(data, dict):
            self._github_pr_amend_reaction_actor_login = ""
            return ""
        login = str(data.get("login") or "").strip()
        self._github_pr_amend_reaction_actor_login = login
        return login

    async def _github_pr_amend_reaction_owned_by_actor(self, reaction: dict[str, Any]) -> bool:
        raw_user = reaction.get("user")
        user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
        login = str(user.get("login") or "").strip()
        if login:
            actor_login = await self._github_pr_amend_authenticated_login()
            return bool(actor_login and login.lower() == actor_login.lower())
        # Older tests/fixtures and some bot-shaped API payloads omit login. Keep
        # the legacy behavior only when there is no login to compare, so we do
        # not delete human or third-party-bot reactions on real GitHub comments.
        return str(user.get("type") or "").lower() == "bot"

    async def _delete_prior_github_pr_amend_reactions(
        self,
        request: Any,
        endpoint: str,
        content: str,
    ) -> bool:
        status_reactions = _GITHUB_PR_AMEND_REST_STATUS_REACTIONS
        if content not in status_reactions:
            return False
        list_argv = [
            "gh",
            "api",
            endpoint,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
        ]
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                list_argv,
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("[github-pr-amend] reaction list failed endpoint=%s error=%s", endpoint, exc)
            return False
        if result.returncode != 0:
            logger.warning(
                "[github-pr-amend] gh reaction list failed endpoint=%s exit=%s stderr=%s",
                endpoint,
                result.returncode,
                (result.stderr or "").strip()[:500],
            )
            return False
        try:
            reactions = json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            logger.warning("[github-pr-amend] gh reaction list returned invalid JSON endpoint=%s", endpoint)
            return False
        if not isinstance(reactions, list):
            return False
        already_present = False
        for reaction in reactions:
            if not isinstance(reaction, dict):
                continue
            prior = str(reaction.get("content") or "")
            if prior not in status_reactions:
                continue
            if not await self._github_pr_amend_reaction_owned_by_actor(reaction):
                continue
            if prior == content:
                already_present = True
                continue
            reaction_id = str(reaction.get("id") or "").strip()
            if not reaction_id:
                continue
            delete_argv = [
                "gh",
                "api",
                "-X",
                "DELETE",
                f"{endpoint}/{reaction_id}",
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                "X-GitHub-Api-Version: 2022-11-28",
            ]
            try:
                deleted = await asyncio.to_thread(
                    subprocess.run,
                    delete_argv,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.warning(
                    "[github-pr-amend] reaction delete failed endpoint=%s reaction=%s error=%s",
                    endpoint,
                    reaction_id,
                    exc,
                )
                continue
            if deleted.returncode != 0:
                logger.warning(
                    "[github-pr-amend] gh reaction delete failed endpoint=%s reaction=%s exit=%s stderr=%s",
                    endpoint,
                    reaction_id,
                    deleted.returncode,
                    (deleted.stderr or "").strip()[:500],
                )
        return already_present

    # ------------------------------------------------------------------
    # Signature validation
    # ------------------------------------------------------------------

    def _validate_signature(
        self, request: "web.Request", body: bytes, secret: str
    ) -> bool:
        """Validate webhook signature (GitHub, GitLab, Svix, generic HMAC-SHA256)."""
        def _header(name: str) -> str:
            return (
                request.headers.get(name, "")
                or request.headers.get(name.lower(), "")
                or request.headers.get(name.upper(), "")
            )

        # Svix / AgentMail:
        #   svix-id: msg_...
        #   svix-timestamp: unix seconds
        #   svix-signature: v1,<base64-hmac> [v1,<base64-hmac> ...]
        # Signed content is: "{id}.{timestamp}.{raw_body}".  Svix secrets
        # usually start with "whsec_" and the remainder is base64-encoded.
        svix_id = _header("svix-id")
        svix_timestamp = _header("svix-timestamp")
        svix_signature = _header("svix-signature")
        if svix_id or svix_timestamp or svix_signature:
            return self._validate_svix_signature(
                body=body,
                secret=secret,
                msg_id=svix_id,
                timestamp=svix_timestamp,
                signature_header=svix_signature,
            )

        # GitHub: X-Hub-Signature-256 = sha256=<hex>
        gh_sig = request.headers.get("X-Hub-Signature-256", "")
        if gh_sig:
            expected = "sha256=" + hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(gh_sig, expected)

        # GitLab: X-Gitlab-Token = <plain secret>
        gl_token = request.headers.get("X-Gitlab-Token", "")
        if gl_token:
            return hmac.compare_digest(gl_token, secret)

        # Generic: X-Webhook-Signature = <hex HMAC-SHA256>
        generic_sig = request.headers.get("X-Webhook-Signature", "")
        if generic_sig:
            expected = hmac.new(
                secret.encode(), body, hashlib.sha256
            ).hexdigest()
            return hmac.compare_digest(generic_sig, expected)

        # No recognised signature header but secret is configured → reject
        logger.debug(
            "[webhook] Secret configured but no signature header found"
        )
        return False

    def _validate_svix_signature(
        self,
        body: bytes,
        secret: str,
        msg_id: str,
        timestamp: str,
        signature_header: str,
        tolerance_seconds: int = 300,
    ) -> bool:
        """Validate Svix-compatible signatures used by AgentMail webhooks."""
        if not (msg_id and timestamp and signature_header and secret):
            return False

        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False
        if abs(int(time.time()) - ts) > tolerance_seconds:
            logger.warning("[webhook] Svix signature timestamp outside replay window")
            return False

        if secret.startswith("whsec_"):
            encoded_secret = secret.removeprefix("whsec_")
            try:
                key = base64.b64decode(encoded_secret, validate=True)
            except (binascii.Error, ValueError):
                logger.debug("[webhook] Invalid whsec_ Svix signing secret")
                return False
        else:
            # Be permissive for providers that document Svix-style headers but
            # hand out raw shared secrets rather than whsec_ base64 secrets.
            logger.debug("[webhook] Validating Svix-style signature with raw secret")
            key = secret.encode()

        signed_content = msg_id.encode() + b"." + timestamp.encode() + b"." + body
        expected = base64.b64encode(
            hmac.new(key, signed_content, hashlib.sha256).digest()
        ).decode()

        # Svix can send multiple signatures separated by spaces during secret
        # rotation. Each entry is formatted as "vN,<base64>".
        for part in signature_header.split():
            try:
                version, signature = part.split(",", 1)
            except ValueError:
                continue
            if version == "v1" and hmac.compare_digest(signature, expected):
                return True
        return False

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    def _render_prompt(
        self,
        template: str,
        payload: dict,
        event_type: str,
        route_name: str,
    ) -> str:
        """Render a prompt template with the webhook payload.

        Supports dot-notation access into nested dicts:
        ``{pull_request.title}`` → ``payload["pull_request"]["title"]``

        Special token ``{__raw__}`` dumps the entire payload as indented
        JSON (truncated to 4000 chars).  Useful for monitoring alerts or
        any webhook where the agent needs to see the full payload.
        """
        if not template:
            truncated = json.dumps(payload, indent=2)[:4000]
            return (
                f"Webhook event '{event_type}' on route "
                f"'{route_name}':\n\n```json\n{truncated}\n```"
            )

        def _resolve(match: re.Match) -> str:
            key = match.group(1)
            # Special token: dump the entire payload as JSON
            if key == "__raw__":
                return json.dumps(payload, indent=2)[:4000]
            value: Any = payload
            for part in key.split("."):
                if isinstance(value, dict):
                    value = value.get(part, f"{{{key}}}")
                else:
                    return f"{{{key}}}"
            if isinstance(value, (dict, list)):
                return json.dumps(value, indent=2)[:2000]
            return str(value)

        return re.sub(r"\{([a-zA-Z0-9_.]+)\}", _resolve, template)

    def _render_delivery_extra(
        self, extra: dict, payload: dict
    ) -> dict:
        """Render delivery_extra template values with payload data."""
        rendered: Dict[str, Any] = {}
        for key, value in extra.items():
            if isinstance(value, str):
                rendered[key] = self._render_prompt(value, payload, "", "")
            else:
                rendered[key] = value
        return rendered

    # ------------------------------------------------------------------
    # Response delivery
    # ------------------------------------------------------------------

    async def _direct_deliver(
        self, content: str, delivery: dict
    ) -> SendResult:
        """Deliver *content* directly without invoking the agent.

        Used by ``deliver_only`` routes: the rendered template becomes the
        literal message body, and we dispatch to the same delivery helpers
        that the agent-mode ``send()`` flow uses.  All target types that
        work in agent mode work here — Telegram, Discord, Slack, etc.
        """
        deliver_type = delivery.get("deliver", "log")

        if deliver_type == "log":
            # Shouldn't reach here — startup validation rejects deliver_only
            # with deliver=log — but guard defensively.
            logger.info("[webhook] direct-deliver log-only: %s", content[:200])
            return SendResult(success=True)

        if deliver_type == "github_comment":
            return await self._deliver_github_comment(content, delivery)

        # Fall through to the cross-platform dispatcher, which validates the
        # target name and routes via the gateway runner.
        return await self._deliver_cross_platform(
            deliver_type, content, delivery
        )

    async def _deliver_github_comment(
        self, content: str, delivery: dict
    ) -> SendResult:
        """Fail closed: GitHub text-comment delivery is disabled."""
        error = (
            "github_comment delivery is disabled; configure another "
            "delivery target"
        )
        logger.error("[webhook] %s", error)
        return SendResult(success=False, error=error)

    async def _deliver_cross_platform(
        self, platform_name: str, content: str, delivery: dict
    ) -> SendResult:
        """Route response to another platform (telegram, discord, etc.)."""
        if not self.gateway_runner:
            return SendResult(
                success=False,
                error="No gateway runner for cross-platform delivery",
            )

        try:
            target_platform = Platform(platform_name)
        except ValueError:
            return SendResult(
                success=False, error=f"Unknown platform: {platform_name}"
            )

        adapter = self.gateway_runner.adapters.get(target_platform)
        if not adapter:
            return SendResult(
                success=False,
                error=f"Platform {platform_name} not connected",
            )

        # Use home channel if no specific chat_id in deliver_extra
        extra = delivery.get("deliver_extra", {})
        chat_id = extra.get("chat_id", "")
        if not chat_id:
            home = self.gateway_runner.config.get_home_channel(target_platform)
            if home:
                chat_id = home.chat_id
            else:
                return SendResult(
                    success=False,
                    error=f"No chat_id or home channel for {platform_name}",
                )

        # Pass thread_id from deliver_extra so Telegram forum topics work
        metadata = None
        thread_id = extra.get("message_thread_id") or extra.get("thread_id")
        if thread_id:
            metadata = {"thread_id": thread_id}

        return await adapter.send(chat_id, content, metadata=metadata)
