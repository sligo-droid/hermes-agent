"""Discord thread reference expansion for goal/feature planning."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional


DISCORD_THREAD_TYPES = {10, 11, 12}
DISCORD_REF_URL_RE = re.compile(
    r"https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild>\d{16,24})/(?P<channel>\d{16,24})/(?P<target>\d{16,24})"
)
DISCORD_SNOWFLAKE_RE = re.compile(r"(?<!\d)(?P<id>\d{17,20})(?!\d)")
PLAN_PART_RE = re.compile(r"\(\s*\d+\s*/\s*\d+\s*\)")
PLAN_MARKER_RE = re.compile(
    r"(?im)(^|\n)\s*(?:#{1,4}\s*)?(?:"
    r"plan|implementation phases?|recommendation|acceptance criteria|"
    r"proposed architecture|deliverables|test plan|final recommendation|"
    r"success means|stop when|phase\s+\d+|\d+\.\s+phase"
    r")\b"
)


@dataclass(frozen=True)
class DiscordThreadPlanExpansion:
    source: str
    thread_id: str
    thread_name: str = ""
    selected_message_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    content: str = ""

    def formatted(self) -> str:
        lines = ["[Expanded Discord thread plan]"]
        thread_label = self.thread_id
        if self.thread_name:
            thread_label = f"{self.thread_name} ({self.thread_id})"
        lines.append(f"Thread: {thread_label}")
        lines.append(f"Source: {self.source}")
        if self.selected_message_ids:
            lines.append("Selected plan messages: " + ", ".join(self.selected_message_ids))
        if self.warnings:
            lines.append("Warnings: " + "; ".join(self.warnings))
        lines.append("")
        lines.append(self.content.strip())
        return "\n".join(lines).strip()


def find_discord_thread_references(text: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    covered: list[tuple[int, int]] = []
    seen: set[tuple[str, str]] = set()
    for match in DISCORD_REF_URL_RE.finditer(str(text or "")):
        covered.append(match.span())
        key = ("url", match.group("target"))
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            {
                "kind": "url",
                "source": match.group(0),
                "channel_id": match.group("channel"),
                "target_id": match.group("target"),
            }
        )
    for match in DISCORD_SNOWFLAKE_RE.finditer(str(text or "")):
        if any(start <= match.start() and match.end() <= end for start, end in covered):
            continue
        snowflake = match.group("id")
        key = ("id", snowflake)
        if key in seen:
            continue
        seen.add(key)
        refs.append({"kind": "id", "source": snowflake, "target_id": snowflake})
    return refs


def has_discord_thread_reference(text: str) -> bool:
    return bool(find_discord_thread_references(text))


def expand_discord_thread_references(
    text: str,
    *,
    token: Optional[str] = None,
    request_func: Optional[Callable[..., Any]] = None,
    max_messages: int = 200,
    max_raw_chars: int = 200_000,
    max_output_chars: int = 45_000,
) -> list[DiscordThreadPlanExpansion]:
    """Resolve Discord thread refs in text and extract the last plan-like segment."""
    refs = find_discord_thread_references(text)
    if not refs:
        return []
    token = _resolve_discord_token(token)
    if not token:
        return [
            DiscordThreadPlanExpansion(
                source=ref["source"],
                thread_id=ref.get("target_id", ""),
                warnings=("Discord thread expansion skipped: DISCORD_BOT_TOKEN is not set.",),
            )
            for ref in refs
        ]
    if request_func is None:
        from tools.discord_tool import _discord_request

        request_func = _discord_request

    expansions: list[DiscordThreadPlanExpansion] = []
    seen_threads: set[str] = set()
    for ref in refs:
        try:
            expansion = _expand_one_reference(
                ref,
                token=token,
                request_func=request_func,
                max_messages=max_messages,
                max_raw_chars=max_raw_chars,
                max_output_chars=max_output_chars,
            )
        except Exception as exc:
            expansion = DiscordThreadPlanExpansion(
                source=ref["source"],
                thread_id=ref.get("target_id", ""),
                warnings=(f"Discord thread expansion failed: {exc}",),
            )
        key = expansion.thread_id or ref["source"]
        if key in seen_threads:
            continue
        seen_threads.add(key)
        expansions.append(expansion)
    return expansions


def format_discord_thread_expansions(expansions: list[DiscordThreadPlanExpansion]) -> str:
    blocks = [expansion.formatted() for expansion in expansions if expansion.formatted()]
    return "\n\n".join(blocks).strip()


def _resolve_discord_token(token: Optional[str]) -> str:
    explicit = str(token or "").strip()
    if explicit:
        return explicit
    env_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        from tools.discord_tool import _get_bot_token

        return str(_get_bot_token() or "").strip()
    except Exception:
        return ""


def _expand_one_reference(
    ref: dict[str, str],
    *,
    token: str,
    request_func: Callable[..., Any],
    max_messages: int,
    max_raw_chars: int,
    max_output_chars: int,
) -> DiscordThreadPlanExpansion:
    target_id = ref.get("target_id", "")
    source = ref.get("source", target_id)
    channel = _get_channel(target_id, token, request_func)
    if _is_thread_channel(channel):
        return _expand_thread_channel(
            channel,
            source=source,
            token=token,
            request_func=request_func,
            max_messages=max_messages,
            max_raw_chars=max_raw_chars,
            max_output_chars=max_output_chars,
        )
    if ref.get("kind") == "url":
        message = _get_message(ref.get("channel_id", ""), target_id, token, request_func)
        thread = message.get("thread") if isinstance(message, dict) else None
        if _is_thread_channel(thread):
            return _expand_thread_channel(
                thread,
                source=source,
                token=token,
                request_func=request_func,
                max_messages=max_messages,
                max_raw_chars=max_raw_chars,
                max_output_chars=max_output_chars,
            )
        content = _format_messages([message]) if isinstance(message, dict) else ""
        return DiscordThreadPlanExpansion(
            source=source,
            thread_id=target_id,
            selected_message_ids=tuple([target_id] if content else []),
            warnings=("Discord link resolved to a single message, not a thread plan.",),
            content=content,
        )
    return DiscordThreadPlanExpansion(
        source=source,
        thread_id=target_id,
        warnings=("Discord snowflake did not resolve to a thread channel.",),
    )


def _expand_thread_channel(
    channel: dict[str, Any],
    *,
    source: str,
    token: str,
    request_func: Callable[..., Any],
    max_messages: int,
    max_raw_chars: int,
    max_output_chars: int,
) -> DiscordThreadPlanExpansion:
    thread_id = str(channel.get("id") or "")
    messages, fetch_truncated = _fetch_thread_messages(
        thread_id,
        token,
        request_func,
        max_messages=max_messages,
        max_raw_chars=max_raw_chars,
    )
    selected, fallback_warning = _select_last_plan_messages(messages)
    content = _format_messages(selected)
    warnings: list[str] = []
    if fetch_truncated:
        warnings.append("Thread history fetch was capped before all messages were read.")
    if fallback_warning:
        warnings.append(fallback_warning)
    output_truncated = False
    if len(content) > max_output_chars:
        content = content[:max_output_chars].rstrip() + "\n[Expansion truncated to output cap.]"
        output_truncated = True
        warnings.append("Expanded plan text was truncated to output cap.")
    return DiscordThreadPlanExpansion(
        source=source,
        thread_id=thread_id,
        thread_name=str(channel.get("name") or ""),
        selected_message_ids=tuple(str(item.get("id") or "") for item in selected if item.get("id")),
        warnings=tuple(warnings),
        truncated=fetch_truncated or output_truncated,
        content=content,
    )


def _get_channel(channel_id: str, token: str, request_func: Callable[..., Any]) -> dict[str, Any]:
    try:
        payload = request_func("GET", f"/channels/{channel_id}", token, timeout=10)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _get_message(channel_id: str, message_id: str, token: str, request_func: Callable[..., Any]) -> dict[str, Any]:
    try:
        payload = request_func("GET", f"/channels/{channel_id}/messages/{message_id}", token, timeout=10)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_thread_channel(payload: Any) -> bool:
    return isinstance(payload, dict) and int(payload.get("type") or -1) in DISCORD_THREAD_TYPES


def _fetch_thread_messages(
    thread_id: str,
    token: str,
    request_func: Callable[..., Any],
    *,
    max_messages: int,
    max_raw_chars: int,
) -> tuple[list[dict[str, Any]], bool]:
    newest_to_oldest: list[dict[str, Any]] = []
    before: Optional[str] = None
    raw_chars = 0
    truncated = False
    while len(newest_to_oldest) < max_messages and raw_chars < max_raw_chars:
        limit = min(100, max_messages - len(newest_to_oldest))
        params = {"limit": str(limit)}
        if before:
            params["before"] = before
        payload = request_func(
            "GET",
            f"/channels/{thread_id}/messages",
            token,
            params=params,
            timeout=15,
        )
        if not isinstance(payload, list) or not payload:
            break
        for item in payload:
            if isinstance(item, dict):
                newest_to_oldest.append(item)
                raw_chars += len(str(item.get("content") or ""))
        if len(payload) < limit:
            break
        before = str(payload[-1].get("id") or "")
        if not before:
            break
    if len(newest_to_oldest) >= max_messages or raw_chars >= max_raw_chars:
        truncated = True
    return list(reversed(newest_to_oldest)), truncated


def _select_last_plan_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for message in messages:
        if _is_plan_message(message):
            current.append(message)
        elif current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    if groups:
        return groups[-1], ""
    recent = messages[-20:]
    return recent, "No plan-like message group found; included a bounded recent thread summary."


def _is_plan_message(message: dict[str, Any]) -> bool:
    content = str(message.get("content") or "")
    if not content.strip():
        return False
    author = message.get("author") if isinstance(message.get("author"), dict) else {}
    is_bot = bool(author.get("bot"))
    if not is_bot:
        return False
    if PLAN_PART_RE.search(content):
        return True
    if PLAN_MARKER_RE.search(content):
        return True
    return len(content) >= 900 and re.search(r"(?im)^\s*(?:\d+\.|[-*])\s+", content) is not None


def _format_messages(messages: list[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for message in messages:
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        name = str(author.get("global_name") or author.get("username") or author.get("id") or "unknown")
        suffix = " [bot]" if author.get("bot") else ""
        message_id = str(message.get("id") or "")
        chunks.append(f"[{name}{suffix} msg:{message_id}]\n{content}".strip())
    return "\n\n".join(chunks).strip()
