"""Codex-backed usage reporting for Hermes /usage.

This intentionally reads the Codex app-server account/rate-limit surface instead
of summarizing Hermes' current session. The useful signal for this command is
subscription model usage, especially the weekly window.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from agent.transports.codex_app_server import (
    CodexAppServerClient,
    CodexAppServerError,
    check_codex_binary,
)


def build_codex_usage_report(*, codex_bin: str = "codex", timeout: float = 20.0) -> str:
    """Return a human-readable Codex subscription/model-usage report."""
    ok, message = check_codex_binary(codex_bin)
    if not ok:
        return f"Codex Status\n\nUnable to read Codex status: {message}"

    client: CodexAppServerClient | None = None
    try:
        client_obj = CodexAppServerClient(codex_bin=codex_bin)
        client = client_obj
        client_obj.initialize(
            client_name="hermes-usage",
            client_title="Hermes /usage",
            client_version="0",
            timeout=timeout,
        )
        account_response = client_obj.request(
            "account/read", {"refreshToken": True}, timeout=timeout
        )
        limits_response = client_obj.request("account/rateLimits/read", timeout=timeout)
    except (CodexAppServerError, TimeoutError, OSError) as exc:
        return f"Codex Status\n\nUnable to read Codex status: {exc}"
    finally:
        if client is not None:
            client.close()

    return format_codex_status_report(account_response, limits_response)


def format_codex_status_report(
    account_response: dict[str, Any] | None,
    limits_response: dict[str, Any] | None,
) -> str:
    """Format Codex account/rate-limit payloads without exposing the email."""
    account = (account_response or {}).get("account") or {}
    limits_response = limits_response or {}
    default_snapshot = limits_response.get("rateLimits")
    by_limit_id = limits_response.get("rateLimitsByLimitId") or {}

    snapshots: list[dict[str, Any]] = []
    if isinstance(by_limit_id, dict) and by_limit_id:
        snapshots = [s for s in by_limit_id.values() if isinstance(s, dict)]
    elif isinstance(default_snapshot, dict):
        snapshots = [default_snapshot]

    plan = _clean_label(account.get("planType") or _first_plan(snapshots))
    account_type = _clean_label(account.get("type"))

    lines = ["Codex Status", ""]
    if account_type or plan:
        account_bits = []
        if account_type:
            account_bits.append(account_type)
        if plan:
            account_bits.append(f"plan {plan}")
        lines.append("Account: " + ", ".join(account_bits))

    if not snapshots:
        lines.append("No Codex rate-limit usage returned.")
        return "\n".join(lines)

    lines.append("Usage:")
    for snapshot in sorted(snapshots, key=_snapshot_sort_key):
        label = _snapshot_label(snapshot)
        lines.append(f"- {label}")
        primary = _format_window(snapshot.get("primary"), fallback_name="short window")
        secondary = _format_window(snapshot.get("secondary"), fallback_name="weekly")
        if primary:
            lines.append(f"  - {primary}")
        if secondary:
            lines.append(f"  - {secondary}")
        credits = _format_credits(snapshot.get("credits"))
        if credits:
            lines.append(f"  - {credits}")
        reached = snapshot.get("rateLimitReachedType")
        if reached:
            lines.append(f"  - Limit state: {_clean_label(reached)}")

    return "\n".join(lines)


def _first_plan(snapshots: list[dict[str, Any]]) -> str:
    for snapshot in snapshots:
        plan = snapshot.get("planType")
        if plan:
            return str(plan)
    return ""


def _snapshot_label(snapshot: dict[str, Any]) -> str:
    name = _clean_label(snapshot.get("limitName"))
    limit_id = _clean_label(snapshot.get("limitId"))
    if name and limit_id:
        return f"{name} ({limit_id})"
    if name:
        return name
    if limit_id:
        return f"Codex ({limit_id})"
    return "Codex"


def _snapshot_sort_key(snapshot: dict[str, Any]) -> tuple[int, str]:
    name = str(snapshot.get("limitName") or "")
    limit_id = str(snapshot.get("limitId") or "")
    # Named model buckets are the most useful; put the aggregate bucket after.
    unnamed = 1 if not name else 0
    return (unnamed, name.lower() or limit_id.lower())


def _format_window(window: Any, *, fallback_name: str) -> str:
    if not isinstance(window, dict):
        return ""
    used = window.get("usedPercent")
    if used is None:
        return ""
    duration = _window_name(window.get("windowDurationMins"), fallback_name=fallback_name)
    resets = _format_reset(window.get("resetsAt"))
    suffix = f", resets {resets}" if resets else ""
    return f"{duration}: {used}% used{suffix}"


def _window_name(minutes: Any, *, fallback_name: str) -> str:
    try:
        minutes_int = int(minutes)
    except (TypeError, ValueError):
        return fallback_name
    if minutes_int == 10080:
        return "weekly"
    if minutes_int % 1440 == 0:
        days = minutes_int // 1440
        return f"{days}d"
    if minutes_int % 60 == 0:
        hours = minutes_int // 60
        return f"{hours}h"
    return f"{minutes_int}m"


def _format_reset(value: Any) -> str:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return ""
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


def _format_credits(credits: Any) -> str:
    if not isinstance(credits, dict):
        return ""
    if credits.get("unlimited"):
        return "Credits: unlimited"
    balance = credits.get("balance")
    has_credits = credits.get("hasCredits")
    if balance is not None:
        return f"Credits: {balance}"
    if has_credits is not None:
        return f"Credits: {'available' if has_credits else 'none'}"
    return ""


def _clean_label(value: Any) -> str:
    text = str(value or "").strip()
    return text.replace("_", " ")
