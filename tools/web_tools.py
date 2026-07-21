#!/usr/bin/env python3
"""
Standalone Web Tools Module

This module provides generic web tools that work with multiple backend providers.
Backend is selected during ``hermes tools`` setup (web.backend in config.yaml).
When available, Hermes can route Firecrawl calls through a Nous-hosted tool-gateway
for Nous Subscribers only.

Available tools:
- web_search_tool: Search the web for information
- web_extract_tool: Extract content from specific web pages

Backend compatibility:
- Exa: https://exa.ai (search, extract)
- Firecrawl: https://docs.firecrawl.dev/introduction (search, extract; direct or derived firecrawl-gateway.<domain> for Nous Subscribers)
- Parallel: https://docs.parallel.ai (search, extract)
- Tavily: https://tavily.com (search, extract)

Extraction limits:
- Returns clean provider text without auxiliary LLM calls
- Uses deterministic bounded head/tail truncation for long pages

Debug Mode:
- Set WEB_TOOLS_DEBUG=true to enable detailed logging
- Creates web_tools_debug_UUID.json in ./logs directory
- Captures all tool calls, results, and truncation metrics

Usage:
    from web_tools import web_search_tool, web_extract_tool
    
    # Search the web
    results = web_search_tool("Python machine learning libraries", limit=3)
    
    # Extract content from URLs  
    content = web_extract_tool(["https://example.com"], format="markdown")
"""

import asyncio
import json
import logging
import math
import os
import re
import sys
from typing import List, Dict, Any, Optional, TYPE_CHECKING
import httpx  # noqa: F401 — kept at module top so tests can patch tools.web_tools.httpx
# After the web-provider plugin migration (PR #25182), the Firecrawl SDK
# proxy, client construction, and response-shape normalizers all live in
# plugins.web.firecrawl.provider. We re-export the names that external
# code, integration tests, and unit-test patches reach for so the public
# surface stays stable.
if TYPE_CHECKING:
    from firecrawl import Firecrawl  # noqa: F401 — type hints only
from plugins.web.firecrawl.provider import (
    Firecrawl,  # noqa: F401  # re-exported for tests that mock.patch("tools.web_tools.Firecrawl")
    _firecrawl_backend_help_suffix,
    _get_firecrawl_client,  # noqa: F401  # re-exported for tests that `from tools.web_tools import _get_firecrawl_client`
    _get_firecrawl_gateway_url,
    _is_tool_gateway_ready,
    check_firecrawl_api_key,
)
# Tavily helpers re-exported for backward-compat with existing unit tests
# (tests/tools/test_web_tools_tavily.py imports these names directly).
from plugins.web.tavily.provider import (  # noqa: F401 — backward-compat names
    _normalize_tavily_documents,
    _normalize_tavily_search_results,
    _tavily_request,
)
# Parallel + Exa clients re-exported for backward-compat with existing
# unit tests (tests/tools/test_web_tools_config.py imports _get_parallel_client
# / _get_async_parallel_client / _get_exa_client directly).
from plugins.web.parallel.provider import (  # noqa: F401 — backward-compat names
    _get_async_parallel_client,
    _get_parallel_client,
)
from plugins.web.exa.provider import _get_exa_client  # noqa: F401

# Module-level cache slots for the per-vendor clients. The plugins read/write
# these via tools.web_tools so unit tests that reset
# ``tools.web_tools._<vendor>_client = None`` between cases keep working.
_firecrawl_client: Optional[Any] = None
_firecrawl_client_config: Optional[Any] = None
_parallel_client: Optional[Any] = None
_async_parallel_client: Optional[Any] = None
_exa_client: Optional[Any] = None

from tools.debug_helpers import DebugSession
# Imported solely so unit tests can monkeypatch these names on
# tools.web_tools (the firecrawl plugin reads them via its own import chain).
from tools.managed_tool_gateway import (  # noqa: F401 — backward-compat names for tests
    build_vendor_gateway_url,
    peek_nous_access_token as _peek_nous_access_token,
    read_nous_access_token as _read_nous_access_token,
    resolve_managed_tool_gateway,
)
from tools.tool_backend_helpers import (  # noqa: F401
    managed_nous_tools_enabled,
    nous_tool_gateway_unavailable_message,
    prefers_gateway,
)
from tools.url_safety import (
    async_is_safe_url,
    is_safe_url,
    normalize_url_for_request,
    sensitive_query_param_name,
)
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)
_debug = DebugSession("web_tools", env_var="WEB_TOOLS_DEBUG")


def _web_extract_url(value: Any) -> Optional[str]:
    """Return a usable URL from a model-supplied extract item.

    Models sometimes forward a complete web-search result instead of its URL.
    Accept the two common URL keys, but reject missing/non-string values rather
    than stringifying arbitrary objects into misleading fetch targets.
    """
    if isinstance(value, dict):
        value = value.get("url") or value.get("href")
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


# ─── Backend Selection ────────────────────────────────────────────────────────

def _env_value(name: str) -> str:
    """Resolve ``name`` via Hermes config-aware env, falling back to process env.

    Mirrors the SearXNG provider's ``_searxng_url()`` so that values set
    through Hermes' config/.env layer (``hermes config set``, ``hermes tools``)
    are honored here too — not just raw process-env exports. Without this,
    a config-only ``SEARXNG_URL`` (or any provider key) leaves the backend
    auto-detect cascade and ``check_web_api_key()`` blind to it. See #34290.
    """
    try:
        from hermes_cli.config import get_env_value

        val = get_env_value(name)
    except Exception:
        val = None
    if val is None:
        val = os.getenv(name, "")
    return (val or "").strip()


def _has_env(name: str) -> bool:
    return bool(_env_value(name))

def _load_web_config() -> dict:
    """Load the ``web:`` section from ~/.hermes/config.yaml."""
    try:
        from hermes_cli.config import load_config
        # ``or {}``: a present-but-null ``web:`` section (YAML ``web:`` with no
        # body) makes ``.get("web", {})`` return None, which would break every
        # caller that does ``_load_web_config().get(...)``. Honor the ``-> dict``
        # contract so callers never see None.
        return load_config().get("web") or {}
    except (ImportError, Exception):
        return {}


# The built-in web backends whose availability is driven by hardcoded
# env-var / package / OAuth probes below. Any name NOT in this set is a
# candidate plugin-registered provider and must be resolved through the
# web_search_registry (``is_available()``) instead. Kept as a single named
# constant so the whitelist early-returns and the availability chokepoint
# stay in sync.
#
# xAI is intentionally explicit-only: its credentials may also be used for
# inference, TTS, or image generation, so merely having them must not route
# general web traffic through Grok. Once configured, availability is still a
# cheap env/auth-store probe via ``has_xai_credentials()`` below.
_LEGACY_WEB_BACKENDS = frozenset(
    {"parallel", "firecrawl", "tavily", "exa", "searxng", "brave-free", "ddgs", "xai"}
)


def _registered_web_provider(backend: str):
    """Return a plugin-registered web provider by name, or ``None``.

    Consults ``agent.web_search_registry`` so backends contributed by the
    plugin system (which are absent from :data:`_LEGACY_WEB_BACKENDS`) are
    discoverable during availability/selection resolution. Returns ``None``
    on any lookup failure so callers can fall through to legacy checks.
    """
    if not backend:
        return None
    try:
        from agent.web_search_registry import get_provider

        return get_provider(backend)
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry lookup failed for %r: %s", backend, exc)
        return None


def _registered_web_provider_available(backend: str):
    """Availability of a registered provider, or ``None`` if unregistered."""
    provider = _registered_web_provider(backend)
    if provider is None:
        return None
    try:
        return bool(provider.is_available())
    except Exception as exc:  # noqa: BLE001 — a broken provider is unavailable
        logger.debug("web provider %r.is_available() raised: %s", backend, exc)
        return False


def _list_registered_web_providers():
    """Return all plugin-registered web providers (empty list on failure)."""
    try:
        from agent.web_search_registry import list_providers

        return list_providers()
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry list failed: %s", exc)
        return []


def _get_backend() -> str:
    """Determine which web backend to use (shared fallback).

    Reads ``web.backend`` from config.yaml (set by ``hermes tools``).
    Falls back to whichever API key is present for users who configured
    keys manually without running setup.
    """
    configured = (_load_web_config().get("backend") or "").lower().strip()
    if configured in _LEGACY_WEB_BACKENDS or _registered_web_provider(configured) is not None:
        return configured

    # Fallback for manual / legacy config — pick the highest-priority
    # available backend. Explicit user credentials (TAVILY_API_KEY etc.)
    # beat the managed-tool-gateway probe so a deliberate setup is not
    # pre-empted by a Nous OAuth token whose subscription tier may not
    # actually grant web-search access (the gateway then fails at runtime
    # with "no subscription" and the tool returns an error to the agent
    # without falling back). Free-tier backends trail the paid ones.
    backend_candidates = (
        ("tavily", _has_env("TAVILY_API_KEY")),
        ("exa", _has_env("EXA_API_KEY")),
        ("parallel", _has_env("PARALLEL_API_KEY")),
        ("firecrawl", _has_env("FIRECRAWL_API_KEY") or _has_env("FIRECRAWL_API_URL")),
        ("firecrawl", _is_tool_gateway_ready()),
        ("searxng", _has_env("SEARXNG_URL")),
        ("brave-free", _has_env("BRAVE_SEARCH_API_KEY")),
        ("ddgs", _ddgs_package_importable()),
    )
    for backend, available in backend_candidates:
        if available:
            return backend

    # Final fallback: walk plugin-registered providers so a custom backend
    # with no built-in credentials still resolves through its own availability
    # contract. Built-in names (including explicit-only xAI) are handled above.
    for provider in _list_registered_web_providers():
        if provider.name in _LEGACY_WEB_BACKENDS:
            continue
        try:
            if provider.is_available():
                return provider.name
        except Exception as exc:  # noqa: BLE001 — a broken provider is skipped
            logger.debug("web provider %r.is_available() raised: %s", provider.name, exc)

    return "firecrawl"  # default (backward compat)


def _get_search_backend() -> str:
    """Determine which backend to use for web_search specifically.

    Selection priority:
    1. ``web.search_backend`` (per-capability override)
    2. ``web.backend`` (shared fallback — existing behavior)
    3. Auto-detect from env vars

    This enables using different providers for search vs extract
    (e.g. SearXNG for search + Firecrawl for extract).
    """
    return _get_capability_backend("search")


def _get_extract_backend() -> str:
    """Determine which backend to use for web_extract specifically.

    Selection priority:
    1. ``web.extract_backend`` (per-capability override)
    2. ``web.backend`` (shared fallback — existing behavior)
    3. Auto-detect from env vars
    """
    return _get_capability_backend("extract")


def _get_capability_backend(capability: str) -> str:
    """Shared helper for per-capability backend selection.

    Reads ``web.{capability}_backend`` from config; if set and available,
    uses it. Otherwise falls through to the shared ``_get_backend()``.
    """
    cfg = _load_web_config()
    specific = (cfg.get(f"{capability}_backend") or "").lower().strip()
    if specific and _is_backend_available(specific):
        return specific
    return _get_backend()


def _is_backend_available(backend: str) -> bool:
    """Return True when the selected backend is currently usable.

    Non-built-in providers delegate availability to their registered
    ``WebSearchProvider``. Built-ins retain their cheap local probes so this
    function remains safe on tool discovery and CLI repaint paths.
    """
    backend = (backend or "").lower().strip()
    if backend not in _LEGACY_WEB_BACKENDS:
        registered = _registered_web_provider_available(backend)
        if registered is not None:
            return registered
    if backend == "exa":
        return _has_env("EXA_API_KEY")
    if backend == "parallel":
        return _has_env("PARALLEL_API_KEY")
    if backend == "firecrawl":
        return check_firecrawl_api_key()
    if backend == "tavily":
        return _has_env("TAVILY_API_KEY")
    if backend == "searxng":
        return _has_env("SEARXNG_URL")
    if backend == "brave-free":
        return _has_env("BRAVE_SEARCH_API_KEY")
    if backend == "ddgs":
        return _ddgs_package_importable()
    if backend == "xai":
        # Cheap probe — env var OR auth.json has OAuth tokens. Do not call the
        # full credential resolver here because it may refresh over the network.
        try:
            from tools.xai_http import has_xai_credentials

            return has_xai_credentials()
        except Exception:
            return False
    return False


def _ddgs_package_importable() -> bool:
    """Return True when the ``ddgs`` Python package can be imported.

    ddgs is the only backend whose availability is driven by a package
    presence rather than an env var / config entry.  Wrapped in a helper
    so auto-detect and ``_is_backend_available`` share the same check
    (and tests can monkeypatch a single symbol).
    """
    try:
        import ddgs  # noqa: F401
        return True
    except ImportError:
        return False

# ─── Firecrawl Client ────────────────────────────────────────────────────────

# ─── Firecrawl Client ────────────────────────────────────────────────────────
# After PR #25182, the firecrawl client, lazy SDK proxy, dual-auth config
# resolution, response normalizers, and check_firecrawl_api_key() all live
# in plugins.web.firecrawl.provider and are re-exported at the top of this
# module so external callers (integration tests, tool-registry gating) and
# unit tests that patch tools.web_tools.<name> continue to work.


def _web_requires_env() -> list[str]:
    """Return tool metadata env vars for the currently enabled web backends.

    The gateway env vars are always reported — they're metadata strings
    used by the tool registry to light up the tool when the variable is
    set.  Gating them on ``managed_nous_tools_enabled()`` only saved
    string noise in the metadata list, but cost a synchronous HTTP
    refresh against the Nous portal on every CLI startup (invoked at
    tool-registration time).  The behavioral contract is: if the env var
    is set, the tool sees it; if not, it doesn't.  Not-logged-in users
    simply don't have the vars set, so the extra entries are harmless.
    """
    return [
        "EXA_API_KEY",
        "PARALLEL_API_KEY",
        "TAVILY_API_KEY",
        "FIRECRAWL_API_KEY",
        "FIRECRAWL_API_URL",
        "FIRECRAWL_GATEWAY_URL",
        "TOOL_GATEWAY_DOMAIN",
        "TOOL_GATEWAY_SCHEME",
        "TOOL_GATEWAY_USER_TOKEN",
    ]


# ─── Parallel / Tavily / Firecrawl helpers — moved into plugins ──────────────
# After PR #25182, the per-vendor client construction, request helpers, and
# response normalizers all live in plugins.web.<vendor>.provider:
#   - parallel: plugins/web/parallel/provider.py
#   - tavily:   plugins/web/tavily/provider.py
#   - firecrawl: plugins/web/firecrawl/provider.py
# The names from the firecrawl plugin (Firecrawl proxy, _get_firecrawl_client,
# _to_plain_object, _normalize_result_list, _extract_web_search_results,
# _extract_scrape_payload, _is_tool_gateway_ready, etc.) are re-exported at
# the top of this module for backward-compat with integration tests and
# unit-test patches.


DEFAULT_EXTRACT_CHAR_LIMIT = 15_000
MIN_EXTRACT_CHAR_LIMIT = 2_000
MAX_EXTRACT_CHAR_LIMIT = 90_000
MAX_EXTRACT_RESULT_SIZE_CHARS = 100_000
# Reserve roughly 10K below the registry's 100K result ceiling for JSON,
# URLs, titles, errors, and truncation footers.
AGGREGATE_EXTRACT_CONTENT_BUDGET = 90_000
MAX_EXTRACT_URL_CHARS = 4_096
MAX_EXTRACT_TITLE_CHARS = 1_024
MAX_EXTRACT_ERROR_CHARS = 2_048
MAX_EXTRACT_POLICY_VALUE_CHARS = 512
METADATA_TRUNCATION_MARKER = "...[TRUNCATED]"
TRUNCATION_FOOTER_RESERVE = 512
LINE_SNAP_WINDOW = 256


def _clamp_extract_char_limit(value: Any) -> int:
    """Return a finite public web_extract limit, falling back safely."""
    if isinstance(value, bool):
        return DEFAULT_EXTRACT_CHAR_LIMIT
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_EXTRACT_CHAR_LIMIT
    if not math.isfinite(numeric):
        return DEFAULT_EXTRACT_CHAR_LIMIT
    return min(
        max(int(numeric), MIN_EXTRACT_CHAR_LIMIT),
        MAX_EXTRACT_CHAR_LIMIT,
    )


def _get_extract_char_limit(char_limit: Any = None) -> int:
    """Resolve per-call override, then ``web.extract_char_limit``, then default."""
    if char_limit is not None:
        return _clamp_extract_char_limit(char_limit)
    return _clamp_extract_char_limit(
        _load_web_config().get("extract_char_limit", DEFAULT_EXTRACT_CHAR_LIMIT)
    )


def _image_placeholder(alt: str = "") -> str:
    alt = " ".join(str(alt or "").split())
    return f"[IMAGE: {alt}]" if alt else "[IMAGE]"


def convert_base64_images_to_links(text: str) -> str:
    """Compact inline base64 images while preserving ordinary image URLs."""
    if not isinstance(text, str) or "data:image/" not in text.lower():
        return text

    def _html_image(match: re.Match[str]) -> str:
        tag = match.group(0)
        alt_match = re.search(
            r"\balt\s*=\s*(['\"])(.*?)\1", tag, flags=re.IGNORECASE | re.DOTALL
        )
        return _image_placeholder(alt_match.group(2) if alt_match else "")

    cleaned = re.sub(
        r"<img\b(?=[^>]*\bsrc\s*=\s*(['\"])data:image/[^>]*\1)[^>]*>",
        _html_image,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(
        r"!\[([^\]]*)\]\(\s*data:image/[^;\s)]+;base64,[A-Za-z0-9+/=]+\s*\)",
        lambda match: _image_placeholder(match.group(1)),
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\(\s*data:image/[^;\s)]+;base64,[A-Za-z0-9+/=]+\s*\)",
        "[IMAGE]",
        cleaned,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"data:image/[^;\s)]+;base64,[A-Za-z0-9+/=]+",
        "[IMAGE]",
        cleaned,
        flags=re.IGNORECASE,
    )


def clean_base64_images(text: str) -> str:
    """Backward-compatible alias for inline base64 image compaction."""
    return convert_base64_images_to_links(text)


def _snap_head_end(content: str, target: int) -> int:
    if target <= 0:
        return 0
    lower = max(0, target - LINE_SNAP_WINDOW)
    newline = content.rfind("\n", lower, target + 1)
    return newline + 1 if newline >= lower else target


def _snap_tail_start(content: str, target: int) -> int:
    if target >= len(content):
        return len(content)
    upper = min(len(content), target + LINE_SNAP_WINDOW)
    newline = content.find("\n", target, upper)
    return newline + 1 if newline != -1 else target


def _truncate_with_footer(
    content: str,
    url: str,
    char_limit: int,
) -> tuple[str, bool]:
    """Return deterministic ~75% head/~25% tail text within ``char_limit``."""
    del url  # Kept in the helper signature for stable callers and diagnostics.
    if len(content) <= char_limit:
        return content, False

    body_budget = max(1, char_limit - TRUNCATION_FOOTER_RESERVE)
    head_target = max(1, int(body_budget * 0.75))
    tail_target = max(0, body_budget - head_target)
    head_end = _snap_head_end(content, head_target)
    tail_start = _snap_tail_start(content, len(content) - tail_target)
    if tail_start < head_end:
        head_end = head_target
        tail_start = max(head_end, len(content) - tail_target)

    omitted = max(0, tail_start - head_end)
    if char_limit < MAX_EXTRACT_CHAR_LIMIT:
        next_step = (
            f"Try a higher char_limit (up to {MAX_EXTRACT_CHAR_LIMIT:,}), "
            "a more specific URL, or browser_navigate."
        )
    else:
        next_step = (
            "This is the maximum char_limit; try a more specific URL or "
            "browser_navigate."
        )
    footer = (
        "\n\n[TRUNCATED: deterministic head/tail view; "
        f"{omitted:,} middle characters omitted and not stored. {next_step}]\n\n"
    )
    output = content[:head_end] + footer + content[tail_start:]
    if len(output) > char_limit:
        overflow = len(output) - char_limit
        head_end = max(1, head_end - overflow)
        omitted = max(0, tail_start - head_end)
        footer = (
            "\n\n[TRUNCATED: deterministic head/tail view; "
            f"{omitted:,} middle characters omitted and not stored. {next_step}]\n\n"
        )
        output = content[:head_end] + footer + content[tail_start:]
    return output[:char_limit], True


def _json_content_cost(content: str) -> int:
    """Return the exact characters added when replacing JSON ``""``."""
    return len(json.dumps(content, ensure_ascii=False)) - 2


def _json_safe_text(value: Any) -> str:
    """Convert provider-controlled metadata to deterministic JSON-safe text."""
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if value is None:
        return ""
    if isinstance(value, (bool, int, float)):
        return str(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
            default=lambda item: f"<{type(item).__name__}>",
        )
    except (TypeError, ValueError, OverflowError):
        return f"<{type(value).__name__}>"


def _bounded_metadata_text(value: Any, limit: int) -> str:
    text = _json_safe_text(value)
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(METADATA_TRUNCATION_MARKER))
    return text[:keep] + METADATA_TRUNCATION_MARKER[:limit]


def _sanitize_blocked_by_policy(
    value: Any,
    value_limit: int = MAX_EXTRACT_POLICY_VALUE_CHARS,
) -> Dict[str, str]:
    if not isinstance(value, dict):
        return {"source": _bounded_metadata_text(value, value_limit)}
    sanitized = {
        key: _bounded_metadata_text(value.get(key), value_limit)
        for key in ("host", "rule", "source")
        if key in value
    }
    return sanitized or {"source": _bounded_metadata_text(value, value_limit)}


def _sanitize_extract_metadata(
    result: Dict[str, Any],
    *,
    url_limit: int = MAX_EXTRACT_URL_CHARS,
    title_limit: int = MAX_EXTRACT_TITLE_CHARS,
    error_limit: int = MAX_EXTRACT_ERROR_CHARS,
    policy_limit: int = MAX_EXTRACT_POLICY_VALUE_CHARS,
) -> Dict[str, Any]:
    sanitized: Dict[str, Any] = {
        "url": _bounded_metadata_text(result.get("url"), url_limit),
        "title": _bounded_metadata_text(result.get("title"), title_limit),
        "content": "",
        "error": (
            None
            if result.get("error") is None
            else _bounded_metadata_text(result.get("error"), error_limit)
        ),
    }
    if "blocked_by_policy" in result:
        sanitized["blocked_by_policy"] = _sanitize_blocked_by_policy(
            result.get("blocked_by_policy"), policy_limit
        )
    return sanitized


def _serialize_extract_results(results: List[Dict[str, Any]]) -> str:
    """Return valid JSON below the registry cap without dropping/reordering rows."""
    def _dump() -> str:
        return json.dumps({"results": results}, indent=2, ensure_ascii=False)

    serialized = _dump()
    if len(serialized) < MAX_EXTRACT_RESULT_SIZE_CHARS:
        return serialized

    # Content is always the first sacrifice. Work from the end so earlier
    # provider-ordered results retain their allocated evidence when possible.
    for result in reversed(results):
        if result.get("content"):
            result["content"] = ""
            serialized = _dump()
            if len(serialized) < MAX_EXTRACT_RESULT_SIZE_CHARS:
                return serialized

    # Initial metadata ceilings should normally make this unreachable. Keep a
    # deterministic emergency ladder for hostile/novel metadata shapes.
    for limits in (
        (1_024, 256, 512, 128),
        (256, 128, 256, 64),
    ):
        for index, result in enumerate(results):
            results[index] = _sanitize_extract_metadata(
                result,
                url_limit=limits[0],
                title_limit=limits[1],
                error_limit=limits[2],
                policy_limit=limits[3],
            )
        serialized = _dump()
        if len(serialized) < MAX_EXTRACT_RESULT_SIZE_CHARS:
            return serialized

    # Five minimal ordered envelopes are far below the cap; retain explicit
    # truncation evidence for any metadata that still had text.
    for index, result in enumerate(results):
        results[index] = _sanitize_extract_metadata(
            result,
            url_limit=96,
            title_limit=64,
            error_limit=96,
            policy_limit=32,
        )
    return _dump()


def _truncate_for_encoded_budget(
    content: str,
    url: str,
    char_limit: int,
    encoded_budget: int,
) -> tuple[str, bool]:
    """Honor both the public raw limit and the aggregate serialized budget."""
    if not content or encoded_budget <= 0:
        return "", bool(content)

    upper = min(char_limit, MAX_EXTRACT_CHAR_LIMIT)
    candidate, truncated = _truncate_with_footer(content, url, upper)
    if _json_content_cost(candidate) <= encoded_budget:
        return candidate, truncated

    lower = MIN_EXTRACT_CHAR_LIMIT
    best = ""
    while lower <= upper:
        midpoint = (lower + upper) // 2
        candidate, _ = _truncate_with_footer(content, url, midpoint)
        if _json_content_cost(candidate) <= encoded_budget:
            best = candidate
            lower = midpoint + 1
        else:
            upper = midpoint - 1
    return best, True


_RETURNED_TARGET_FIELDS = (
    "url",
    "actual_url",
    "actualURL",
    "actualUrl",
    "final_url",
    "finalURL",
    "finalUrl",
    "redirect_url",
    "redirectURL",
    "redirectUrl",
    "redirected_url",
    "redirectedURL",
    "redirectedUrl",
)
_RETURNED_METADATA_TARGET_FIELDS = _RETURNED_TARGET_FIELDS + (
    "sourceURL",
    "sourceUrl",
    "source_url",
)


def _returned_target_urls(result: Dict[str, Any]) -> List[str]:
    """Return distinct actual/final/redirect targets, excluding canonical hints."""
    values = [result.get(field) for field in _RETURNED_TARGET_FIELDS]
    metadata = result.get("metadata")
    if isinstance(metadata, dict):
        values.extend(metadata.get(field) for field in _RETURNED_METADATA_TARGET_FIELDS)

    urls: List[str] = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        normalized = normalize_url_for_request(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        urls.append(normalized)
    return urls


async def _post_fetch_block_result(
    result: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Block content if any returned actual/final/redirect target is unsafe."""
    for target_url in _returned_target_urls(result):
        if not await async_is_safe_url(target_url):
            return {
                "url": target_url,
                "title": result.get("title", ""),
                "content": "",
                "error": (
                    "Blocked: Extract provider returned a private or internal "
                    "network address"
                ),
            }
        blocked = check_website_access(target_url)
        if blocked:
            return {
                "url": target_url,
                "title": result.get("title", ""),
                "content": "",
                "error": blocked["message"],
                "blocked_by_policy": {
                    "host": blocked["host"],
                    "rule": blocked["rule"],
                    "source": blocked["source"],
                },
            }
    return None


# ─── Exa / Parallel inline helpers — moved into plugins ──────────────────────
# After PR #25182, the exa client + search/extract and parallel client +
# search/extract helpers all live in their respective plugins:
#   - plugins/web/exa/provider.py
#   - plugins/web/parallel/provider.py
# Both plugins register through agent.web_search_registry and the
# dispatchers in this file resolve them via get_active_*_provider().


def _ensure_web_plugins_loaded() -> None:
    """Idempotently trigger plugin discovery so the web registry is populated.

    Every bundled web provider (brave-free, ddgs, searxng, exa, parallel,
    tavily, firecrawl) registers itself via ``plugins/web/<vendor>/__init__.py``
    during plugin discovery. Tool dispatch can be reached from contexts that
    haven't already triggered discovery — subprocess agent runs, delegate
    children, standalone scripts, certain test paths — and without it the
    registry is empty and ``get_provider('firecrawl')`` returns ``None`` even
    when the user has ``web.extract_backend: firecrawl`` configured and
    ``FIRECRAWL_API_KEY`` set. The symptom is a misleading "No web extract
    provider configured" error (issue #27580).

    Mirrors :func:`tools.browser_tool._ensure_browser_plugins_loaded` exactly:
    the underlying discovery call is idempotent and cheap on subsequent
    invocations.
    """
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered

        _ensure_plugins_discovered()
    except Exception as exc:  # noqa: BLE001
        # Warning, not debug: if a plugin import is genuinely broken the
        # user otherwise hits the misleading "No web extract provider
        # configured" error this helper is meant to eliminate, with no
        # clue in normal logs about the real cause.
        logger.warning("Web plugin discovery failed (non-fatal): %s", exc)


def web_search_tool(query: str, limit: int = 5) -> str:
    """
    Search the web for information using available search API backend.

    This function provides a generic interface for web search that can work
    with multiple backends (Parallel or Firecrawl).

    Note: This function returns search result metadata only (URLs, titles, descriptions).
    Use web_extract_tool to get full content from specific URLs.
    
    Args:
        query (str): The search query to look up
        limit (int): Maximum number of results to return (default: 5)
    
    Returns:
        str: JSON string containing search results with the following structure:
             {
                 "success": bool,
                 "data": {
                     "web": [
                         {
                             "title": str,
                             "url": str,
                             "description": str,
                             "position": int
                         },
                         ...
                     ]
                 }
             }
    
    Raises:
        Exception: If search fails or API key is not set
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 5
    limit = min(max(limit, 1), 100)

    debug_call_data = {
        "parameters": {
            "query": query,
            "limit": limit
        },
        "error": None,
        "results_count": 0,
        "original_response_size": 0,
        "final_response_size": 0
    }
    
    try:
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return tool_error("Interrupted", success=False)

        # Dispatch through the web search registry. All 7 providers
        # (brave-free, ddgs, searxng, exa, parallel, tavily, firecrawl)
        # now live as plugins; the dispatcher is just a registry lookup +
        # delegation. Sync only — every provider's search() is sync.
        _ensure_web_plugins_loaded()
        from agent.web_search_registry import (
            get_active_search_provider,
            get_provider as _wsp_get_provider,
            _disabled_web_plugin_for,
        )

        backend = _get_search_backend()
        provider = _wsp_get_provider(backend) if backend else None
        if provider is None or not provider.supports_search():
            # Fall back to availability-walked active provider when the
            # configured backend isn't a registered search provider (typo,
            # uninstalled plugin, or capability mismatch).
            provider = get_active_search_provider()

        if provider is None:
            # A bundled web plugin the user explicitly disabled looks
            # identical to "no provider" here — point at the real cause
            # (re-enable the plugin) rather than a generic setup hint.
            disabled_key = _disabled_web_plugin_for(capability="search")
            if disabled_key:
                _vendor = disabled_key.split("/", 1)[-1]
                response_data = {
                    "success": False,
                    "error": (
                        f"web.search_backend is set to '{_vendor}', but its "
                        f"plugin ('{disabled_key}') is disabled in config. "
                        f"Re-enable it with `hermes plugins enable {disabled_key}` "
                        "(or remove it from plugins.disabled)."
                    ),
                }
            else:
                response_data = {
                    "success": False,
                    "error": (
                        "No web search provider configured. "
                        "Run `hermes tools` to set one up."
                    ),
                }
        else:
            logger.info(
                "Web search via %s: '%s' (limit: %d)",
                provider.name, query, limit,
            )
            response_data = provider.search(query, limit)

        debug_call_data["results_count"] = len(response_data.get("data", {}).get("web", []))
        result_json = json.dumps(response_data, indent=2, ensure_ascii=False)
        debug_call_data["final_response_size"] = len(result_json)
        _debug.log_call("web_search_tool", debug_call_data)
        _debug.save()
        return result_json

    except Exception as e:
        error_msg = f"Error searching web: {str(e)}"
        logger.debug("%s", error_msg)

        debug_call_data["error"] = error_msg
        _debug.log_call("web_search_tool", debug_call_data)
        _debug.save()

        return tool_error(error_msg)


async def web_extract_tool(
    urls: List[Any],
    format: str = None,
    char_limit: Any = None,
) -> str:
    """Extract deterministic bounded content with no auxiliary LLM calls.

    Args:
        urls (List[Any]): URL strings or search-result objects containing a
            string ``url`` or ``href`` field
        format (str): Desired output format ("markdown" or "html", optional)
        char_limit: Per-page inline character limit. Defaults to
            ``web.extract_char_limit`` (15,000) and is clamped to 2,000–90,000.

    Security: URLs are checked for embedded secrets before fetching.

    Returns:
        JSON string with the provider's ordered results and bounded content.
    """
    resolved_char_limit = _get_extract_char_limit(char_limit)
    # Block URLs containing embedded secrets (exfiltration prevention).
    # URL-decode first so percent-encoded secrets (%73k- = sk-) are caught.
    from agent.redact import _PREFIX_RE
    from urllib.parse import unquote
    normalized_urls: List[str] = []
    normalized_indices: List[int] = []
    invalid_urls: Dict[int, Dict[str, Any]] = {}
    for index, item in enumerate(urls):
        _url = _web_extract_url(item)
        if _url is None:
            invalid_urls[index] = {
                "url": "",
                "title": "",
                "content": "",
                "error": (
                    f"Invalid URL item at index {index}: expected a URL string "
                    "or an object with a string 'url' or 'href' field"
                ),
            }
            continue
        normalized_url = normalize_url_for_request(_url)
        if (
            _PREFIX_RE.search(_url)
            or _PREFIX_RE.search(unquote(_url))
            or _PREFIX_RE.search(normalized_url)
            or _PREFIX_RE.search(unquote(normalized_url))
        ):
            return json.dumps({
                "success": False,
                "error": "Blocked: URL contains what appears to be an API key or token. "
                         "Secrets must not be sent in URLs.",
            })
        sensitive_query_key = sensitive_query_param_name(normalized_url)
        if sensitive_query_key:
            return json.dumps({
                "success": False,
                "error": (
                    "Blocked: URL contains a credential-like query parameter "
                    f"({sensitive_query_key}). Web extract backends are third-party "
                    "readers; remove the sensitive query parameter or use a local "
                    "browser session when this access is explicitly required."
                ),
            })
        normalized_urls.append(normalized_url)
        normalized_indices.append(index)

    debug_call_data = {
        "parameters": {
            "urls": normalized_urls,
            "format": format,
            "char_limit": resolved_char_limit,
        },
        "error": None,
        "pages_extracted": 0,
        "original_response_size": 0,
        "final_response_size": 0,
        "processing_applied": []
    }
    
    try:
        logger.info("Extracting content from %d URL(s)", len(normalized_urls))

        # ── SSRF protection — filter out private/internal URLs before any backend ──
        safe_urls = []
        safe_indices = []
        ssrf_blocked: Dict[int, Dict[str, Any]] = {}
        for index, url in zip(normalized_indices, normalized_urls):
            if not await async_is_safe_url(url):
                ssrf_blocked[index] = {
                    "url": url, "title": "", "content": "",
                    "error": "Blocked: URL targets a private or internal network address",
                }
            else:
                safe_urls.append(url)
                safe_indices.append(index)

        # Dispatch only safe URLs to the configured backend
        if not safe_urls:
            results = []
        else:
            backend = _get_extract_backend()

            # All seven providers (brave-free, ddgs, searxng, exa, parallel,
            # tavily, firecrawl) now live as plugins. The dispatcher is a
            # registry lookup + delegation. Some providers' extract() is
            # async (parallel, firecrawl), others sync (exa, tavily) — we
            # detect coroutine functions and await; sync functions run
            # inline (the policy gate, SSRF re-check, etc. live inside the
            # provider itself for the firecrawl per-URL loop).
            _ensure_web_plugins_loaded()
            from agent.web_search_registry import (
                get_active_extract_provider,
                get_provider as _wsp_get_provider,
                _disabled_web_plugin_for,
            )

            provider = _wsp_get_provider(backend) if backend else None
            if provider is None or not provider.supports_extract():
                # When the configured name IS registered but doesn't support
                # extract (search-only providers like brave-free / ddgs /
                # searxng), surface that as a typed "search-only" error
                # rather than silently switching backends. When the name
                # isn't registered at all (typo / uninstalled plugin), fall
                # through to the active-provider walk.
                if provider is not None and not provider.supports_extract():
                    return json.dumps(
                        {
                            "success": False,
                            "error": (
                                f"{provider.display_name} is a search-only "
                                "backend and cannot extract URL content. "
                                "Set web.extract_backend to firecrawl, "
                                "tavily, exa, or parallel."
                            ),
                        },
                        ensure_ascii=False,
                    )
                provider = get_active_extract_provider()
                if provider is None:
                    # If the configured backend is a bundled web plugin the
                    # user explicitly disabled, the backend is set correctly
                    # and the real fix is to re-enable the plugin — say so
                    # instead of telling them to set web.extract_backend
                    # (which they already did). #40190 follow-up.
                    disabled_key = _disabled_web_plugin_for(capability="extract")
                    if disabled_key:
                        _vendor = disabled_key.split("/", 1)[-1]
                        return json.dumps(
                            {
                                "success": False,
                                "error": (
                                    f"web.extract_backend is set to '{_vendor}', "
                                    f"but its plugin ('{disabled_key}') is disabled "
                                    "in config. Re-enable it with "
                                    f"`hermes plugins enable {disabled_key}` "
                                    "(or remove it from plugins.disabled)."
                                ),
                            },
                            ensure_ascii=False,
                        )
                    return json.dumps(
                        {
                            "success": False,
                            "error": (
                                "No web extract provider configured. "
                                "Set web.extract_backend to firecrawl, "
                                "tavily, exa, or parallel."
                            ),
                        },
                        ensure_ascii=False,
                    )

            logger.info(
                "Web extract via %s: %d URL(s)", provider.name, len(safe_urls)
            )

            # Async-or-sync dispatch: parallel + firecrawl have async
            # extract(); exa + tavily are sync.
            import inspect
            if inspect.iscoroutinefunction(provider.extract):
                results = await provider.extract(safe_urls, format=format)
            else:
                # Run sync extract() in a thread so we don't block the
                # event loop on network I/O.
                results = await asyncio.to_thread(
                    provider.extract, safe_urls, format=format
                )


            # Third-party extractors may follow redirects or return an actual
            # destination alongside the requested URL. Re-check every returned
            # actual/final/redirect target before exposing page content. Canonical
            # hints are intentionally not treated as fetched destinations.
            validated_results = []
            for provider_result in results:
                if not isinstance(provider_result, dict):
                    validated_results.append(provider_result)
                    continue
                if provider_result.get("error") and not provider_result.get("content"):
                    # The provider already withheld content; preserve its
                    # established per-URL error/policy semantics unchanged.
                    validated_results.append(provider_result)
                    continue
                blocked_result = await _post_fetch_block_result(provider_result)
                validated_results.append(blocked_result or provider_result)
            results = validated_results

        # Merge any SSRF-blocked results back in
        if ssrf_blocked:
            results = ssrf_blocked + results
        # Reconstruct the original input order across invalid, blocked, and
        # provider-processed entries. Providers are expected to preserve the
        # order of the safe URL list they receive.
        if invalid_urls or ssrf_blocked:
            safe_results = {
                index: (
                    results[position]
                    if position < len(results)
                    else {
                        "url": safe_urls[position],
                        "title": "",
                        "content": "",
                        "error": "Extract backend returned no result for this URL",
                    }
                )
                for position, index in enumerate(safe_indices)
            }
            by_index = {**safe_results, **ssrf_blocked, **invalid_urls}
            results = [by_index[index] for index in range(len(urls))]

        response = {"results": results}
        
        pages_extracted = len(response.get('results', []))
        logger.info("Extracted content from %d pages", pages_extracted)
        
        debug_call_data["pages_extracted"] = pages_extracted
        try:
            debug_call_data["original_response_size"] = len(
                json.dumps(
                    response,
                    ensure_ascii=False,
                    default=lambda item: f"<{type(item).__name__}>",
                )
            )
        except (TypeError, ValueError, OverflowError):
            debug_call_data["original_response_size"] = None

        # Allocate the aggregate inline-content budget deterministically in
        # provider order. Short/error results return unused budget to later
        # entries; five long pages receive roughly equal shares.
        prepared_results = []
        source_results = response.get("results", [])
        for r in source_results:
            result = _sanitize_extract_metadata(r)
            raw_content = r.get("content", "")
            cleaned_content = convert_base64_images_to_links(
                _json_safe_text(raw_content)
            )
            prepared_results.append((result, cleaned_content))

        empty_response_size = len(
            json.dumps(
                {"results": [result for result, _content in prepared_results]},
                indent=2,
                ensure_ascii=False,
            )
        )
        remaining_budget = min(
            AGGREGATE_EXTRACT_CONTENT_BUDGET,
            max(0, MAX_EXTRACT_RESULT_SIZE_CHARS - empty_response_size - 1),
        )
        trimmed_results = []
        for position, (result, cleaned_content) in enumerate(prepared_results):
            remaining_items = len(source_results) - position
            fair_share = remaining_budget // max(1, remaining_items)
            bounded_content, truncated = _truncate_for_encoded_budget(
                cleaned_content,
                result["url"],
                resolved_char_limit,
                fair_share,
            )
            result["content"] = bounded_content
            remaining_budget = max(
                0, remaining_budget - _json_content_cost(bounded_content)
            )
            trimmed_results.append(result)
            if truncated:
                debug_call_data["processing_applied"].append("deterministic_truncation")

        debug_call_data["processing_applied"].append("base64_image_compaction")
        trimmed_response = {"results": trimmed_results}

        if trimmed_response.get("results") == []:
            cleaned_result = tool_error("Content was inaccessible or not found")
        else:
            cleaned_result = _serialize_extract_results(trimmed_results)
        
        debug_call_data["final_response_size"] = len(cleaned_result)
        
        # Log debug information
        _debug.log_call("web_extract_tool", debug_call_data)
        _debug.save()
        
        return cleaned_result
            
    except Exception as e:
        error_msg = f"Error extracting content: {str(e)}"
        logger.debug("%s", error_msg)
        
        debug_call_data["error"] = error_msg
        _debug.log_call("web_extract_tool", debug_call_data)
        _debug.save()
        
        return tool_error(error_msg)


async def web_crawl_tool(
    url: str,
    instructions: Optional[str] = None,
    use_llm_processing: bool = True,
) -> str:
    """Crawl a website using a crawl-capable web backend."""
    try:
        from tools.interrupt import is_interrupted

        if is_interrupted():
            return tool_error("Interrupted", success=False)
        if not is_safe_url(url):
            return json.dumps(
                {
                    "results": [
                        {
                            "url": url,
                            "title": "",
                            "content": "",
                            "error": "Blocked: URL targets a private or internal network address",
                        }
                    ]
                },
                ensure_ascii=False,
            )
        blocked = check_website_access(url)
        if blocked:
            return json.dumps(
                {
                    "results": [
                        {
                            "url": url,
                            "title": "",
                            "content": "",
                            "error": blocked["message"],
                            "blocked_by_policy": {
                                "host": blocked["host"],
                                "rule": blocked["rule"],
                                "source": blocked["source"],
                            },
                        }
                    ]
                },
                ensure_ascii=False,
            )

        from agent.web_search_registry import get_provider as _wsp_get_provider

        backend = _get_backend()
        provider = _wsp_get_provider(backend) if backend else None
        if provider is None:
            return json.dumps(
                {
                    "success": False,
                    "error": "No web crawl provider configured. Run `hermes tools` to set one up.",
                },
                ensure_ascii=False,
            )
        crawl = getattr(provider, "crawl", None)
        if crawl is None:
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        f"{provider.display_name} is a search-only backend and cannot crawl websites. "
                        "Set web.backend to firecrawl or tavily."
                    ),
                },
                ensure_ascii=False,
            )

        kwargs: Dict[str, Any] = {}
        if instructions:
            kwargs["instructions"] = instructions
        import inspect

        if inspect.iscoroutinefunction(crawl):
            response = await crawl(url, **kwargs)
        else:
            response = await asyncio.to_thread(crawl, url, **kwargs)
        return json.dumps(response, indent=2, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return tool_error(f"Error crawling website: {exc}")


# Convenience function to check Firecrawl credentials
def check_web_api_key() -> bool:

    """Check whether the configured web backend is available.

    Used as the ``check_fn`` gate for the ``web_search`` and ``web_extract``
    tool registry entries — so a plugin-registered provider that reports
    ``is_available()`` must light the tools up even when no built-in backend
    has credentials (issues #28651, #31873). Resolution funnels through
    :func:`_is_backend_available`, which delegates non-legacy names to the
    registry.
    """
    # ``or ""``: a null ``web.backend`` value yields None from ``.get``, and
    # ``None.lower()`` would raise. Mirrors ``_get_backend``.
    configured = (_load_web_config().get("backend") or "").lower().strip()
    if configured and _is_backend_available(configured):
        return True
    # Any built-in backend with credentials present. This is a boolean OR, so
    # unlike _get_backend() the probe order is irrelevant.
    if any(_is_backend_available(backend) for backend in _LEGACY_WEB_BACKENDS):
        return True
    # Any plugin-registered provider the registry considers active for either
    # capability. Delegating to the registry's own availability-filtered
    # resolvers keeps a single authority for "is a custom provider usable"
    # rather than re-implementing the walk here.
    try:
        from agent.web_search_registry import (
            get_active_search_provider,
            get_active_extract_provider,
        )

        return (
            get_active_search_provider() is not None
            or get_active_extract_provider() is not None
        )
    except Exception as exc:  # noqa: BLE001 — registry optional; never fatal
        logger.debug("web provider registry availability check failed: %s", exc)
        return False


if __name__ == "__main__":
    """
    Simple test/demo when run directly
    """
    print("🌐 Standalone Web Tools Module")
    print("=" * 40)
    
    # Check if API keys are available
    web_available = check_web_api_key()
    tool_gateway_available = _is_tool_gateway_ready()
    from hermes_cli.config import get_env_value as _gev
    firecrawl_key_available = bool((_gev("FIRECRAWL_API_KEY") or "").strip())
    firecrawl_url_available = bool((_gev("FIRECRAWL_API_URL") or "").strip())

    if web_available:
        backend = _get_backend()
        print(f"✅ Web backend: {backend}")
        if backend == "exa":
            print("   Using Exa API (https://exa.ai)")
        elif backend == "parallel":
            print("   Using Parallel API (https://parallel.ai)")
        elif backend == "tavily":
            print("   Using Tavily API (https://tavily.com)")
        elif backend == "searxng":
            print(f"   Using SearXNG (search only): {_env_value('SEARXNG_URL')}")
        elif backend == "brave-free":
            print("   Using Brave Search free tier (search only)")
        elif backend == "ddgs":
            print("   Using DuckDuckGo via ddgs package (search only)")
        elif firecrawl_url_available:
            print(f"   Using self-hosted Firecrawl: {(_gev('FIRECRAWL_API_URL') or '').strip().rstrip('/')}")
        elif firecrawl_key_available:
            print("   Using direct Firecrawl cloud API")
        elif tool_gateway_available:
            print(f"   Using Firecrawl tool-gateway: {_get_firecrawl_gateway_url()}")
        else:
            print("   Firecrawl backend selected but not configured")
    else:
        print("❌ No web search backend configured")
        print(
            "Set EXA_API_KEY, PARALLEL_API_KEY, TAVILY_API_KEY, FIRECRAWL_API_KEY, FIRECRAWL_API_URL"
            f"{_firecrawl_backend_help_suffix()}"
        )

    if not web_available:
        sys.exit(1)

    print("🛠️  Web tools ready for use!")
    
    # Show debug mode status
    if _debug.active:
        print(f"🐛 Debug mode ENABLED - Session ID: {_debug.session_id}")
        print(f"   Debug logs will be saved to: {_debug.log_dir}/web_tools_debug_{_debug.session_id}.json")
    else:
        print("🐛 Debug mode disabled (set WEB_TOOLS_DEBUG=true to enable)")
    
    print("\nBasic usage:")
    print("  from web_tools import web_search_tool, web_extract_tool")
    print("  import asyncio")
    print("")
    print("  # Search (synchronous)")
    print("  results = web_search_tool('Python tutorials')")
    print("")
    print("  # Extract (asynchronous)")
    print("  async def main():")
    print("      content = await web_extract_tool(['https://example.com'])")
    print("  asyncio.run(main())")
    
    print("\nDebug mode:")
    print("  # Enable debug logging")
    print("  export WEB_TOOLS_DEBUG=true")
    print("  # Debug logs capture:")
    print("  # - All tool calls with parameters")
    print("  # - Original API responses")
    print("  # - Deterministic truncation metrics")
    print("  # - Final processed results")
    print("  # Logs saved to: ./logs/web_tools_debug_UUID.json")
    
    print("\n📝 web_extract returns bounded deterministic head/tail content without LLM calls")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
from tools.registry import registry, tool_error

WEB_SEARCH_SCHEMA = {
    "name": "web_search",
    "description": "Search the web for information. Returns up to 5 results by default with titles, URLs, and descriptions. The query is passed through to the configured backend, so operators such as site:domain, filetype:pdf, intitle:word, -term, and \"exact phrase\" may work when the backend supports them.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The search query to look up on the web. You may include backend-supported operators such as site:example.com, filetype:pdf, intitle:word, -term, or \"exact phrase\"."
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return. Defaults to 5.",
                "minimum": 1,
                "maximum": 100,
                "default": 5
            }
        },
        "required": ["query"]
    }
}

WEB_EXTRACT_SCHEMA = {
    "name": "web_extract",
    "description": "Extract clean content from up to 5 web page or PDF URLs without LLM summarization. Long pages return a deterministic head/tail view. Inline content is capped by a 90,000-character aggregate budget per call; omitted middle text is not stored. Provider metadata is JSON-sanitized and bounded per result: URL 4,096, title 1,024, error 2,048, and policy values 512 characters. Use a more specific URL or browser_navigate when more context is needed.",
    "parameters": {
        "type": "object",
        "properties": {
            "urls": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of URLs to extract content from (max 5 URLs per call)",
                "maxItems": 5
            },
            "char_limit": {
                "type": "integer",
                "description": "Maximum inline characters requested per page before aggregate allocation. Defaults to web.extract_char_limit (15,000); allowed range 2,000–90,000.",
                "minimum": MIN_EXTRACT_CHAR_LIMIT,
                "maximum": MAX_EXTRACT_CHAR_LIMIT
            }
        },
        "required": ["urls"]
    }
}

registry.register(
    name="web_search",
    toolset="web",
    schema=WEB_SEARCH_SCHEMA,
    handler=lambda args, **kw: web_search_tool(args.get("query", ""), limit=args.get("limit", 5)),
    check_fn=check_web_api_key,
    requires_env=_web_requires_env(),
    emoji="🔍",
    max_result_size_chars=100_000,
)
registry.register(
    name="web_extract",
    toolset="web",
    schema=WEB_EXTRACT_SCHEMA,
    handler=lambda args, **kw: web_extract_tool(
        args.get("urls", [])[:5] if isinstance(args.get("urls"), list) else [],
        format="markdown",
        char_limit=args.get("char_limit"),
    ),
    check_fn=check_web_api_key,
    requires_env=_web_requires_env(),
    is_async=True,
    emoji="📄",
    max_result_size_chars=MAX_EXTRACT_RESULT_SIZE_CHARS,
)
