"""Discord thread reference expansion for goal/feature planning."""

from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional


logger = logging.getLogger(__name__)
DISCORD_THREAD_TYPES = {10, 11, 12}
DISCORD_CONTEXT_KIND_THREAD_PLAN = "thread_plan"
DISCORD_CONTEXT_KIND_SINGLE_MESSAGE = "single_message"
SINGLE_MESSAGE_CONTEXT_WARNING = (
    "Single-message context only: do not assume missing thread history, acceptance criteria, "
    "review notes, or completion signals."
)
_SINGLE_MESSAGE_SURROUNDING_LIMIT = 9
_SINGLE_MESSAGE_RAW_CHAR_CAP = 12_000
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
    context_kind: str = DISCORD_CONTEXT_KIND_THREAD_PLAN
    thread_name: str = ""
    channel_id: str = ""
    selected_message_ids: tuple[str, ...] = ()
    artifact_path: str = ""
    content_sha256: str = ""
    warnings: tuple[str, ...] = ()
    truncated: bool = False
    surrounding_context_fetched: bool = False
    content: str = ""

    def formatted(self) -> str:
        if self.context_kind == DISCORD_CONTEXT_KIND_SINGLE_MESSAGE:
            lines = ["[Expanded Discord single-message context]"]
        else:
            lines = ["[Expanded Discord thread plan]"]
        thread_label = self.thread_id
        if self.thread_name:
            thread_label = f"{self.thread_name} ({self.thread_id})"
        label = "Message" if self.context_kind == DISCORD_CONTEXT_KIND_SINGLE_MESSAGE else "Thread"
        lines.append(f"{label}: {thread_label}")
        if self.channel_id:
            lines.append(f"Channel: {self.channel_id}")
        lines.append(f"Source: {self.source}")
        lines.append(f"Context kind: {self.context_kind}")
        if self.context_kind == DISCORD_CONTEXT_KIND_SINGLE_MESSAGE:
            lines.append(f"Degraded context: true")
            lines.append(SINGLE_MESSAGE_CONTEXT_WARNING)
            lines.append(f"Bounded surrounding context fetched: {str(self.surrounding_context_fetched).lower()}")
        if self.selected_message_ids:
            lines.append("Selected plan messages: " + ", ".join(self.selected_message_ids))
        if self.artifact_path:
            lines.append(f"Artifact: {self.artifact_path}")
        if self.content_sha256:
            lines.append(f"Content SHA256: {self.content_sha256}")
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
    expansions: list[DiscordThreadPlanExpansion] = []
    unresolved_refs: list[dict[str, str]] = []
    seen_threads: set[str] = set()
    for ref in refs:
        artifact_expansion = _expand_artifact_reference(ref, max_output_chars=max_output_chars)
        if artifact_expansion is None:
            unresolved_refs.append(ref)
            continue
        key = artifact_expansion.thread_id or ref["source"]
        if key in seen_threads:
            continue
        seen_threads.add(key)
        expansions.append(artifact_expansion)
    if not unresolved_refs:
        return expansions

    token = _resolve_discord_token(token)
    if not token:
        expansions.extend(
            DiscordThreadPlanExpansion(
                source=ref["source"],
                thread_id=ref.get("target_id", ""),
                warnings=("Discord thread expansion skipped: DISCORD_BOT_TOKEN is not set.",),
            )
            for ref in unresolved_refs
        )
        return expansions
    if request_func is None:
        from tools.discord_tool import _discord_request

        request_func = _discord_request

    for ref in unresolved_refs:
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


def discord_context_quality_from_text(text: str) -> dict[str, Any]:
    """Return machine-readable quality derived from formatted Discord context."""
    value = str(text or "")
    has_single = "[Expanded Discord single-message context]" in value or "Context kind: single_message" in value
    has_thread = "[Expanded Discord thread plan]" in value or "Context kind: thread_plan" in value
    if has_single and not has_thread:
        kind = DISCORD_CONTEXT_KIND_SINGLE_MESSAGE
    elif has_single and has_thread:
        kind = "mixed"
    elif has_thread:
        kind = DISCORD_CONTEXT_KIND_THREAD_PLAN
    else:
        kind = "none"
    return {
        "kind": kind,
        "degraded": has_single,
        "has_thread_plan": has_thread,
        "has_single_message_context": has_single,
    }


def _expand_artifact_reference(
    ref: dict[str, str],
    *,
    max_output_chars: int,
) -> Optional[DiscordThreadPlanExpansion]:
    target_id = ref.get("target_id", "")
    if not target_id:
        return None
    try:
        from hermes_cli.discord_plan_artifacts import lookup_discord_plan_artifact

        artifact = lookup_discord_plan_artifact(target_id)
    except Exception:
        return None
    if artifact is None:
        return None

    content = artifact.content.strip()
    warnings: list[str] = []
    truncated = False
    if len(content) > max_output_chars:
        content = content[:max_output_chars].rstrip() + "\n[Artifact content truncated to output cap.]"
        warnings.append("Artifact plan text was truncated to output cap.")
        truncated = True
    if not content:
        warnings.append("Artifact was indexed but the artifact file could not be read.")

    return DiscordThreadPlanExpansion(
        source=ref.get("source", target_id),
        thread_id=artifact.thread_id or target_id,
        selected_message_ids=artifact.bot_message_ids,
        artifact_path=artifact.artifact_path,
        content_sha256=artifact.content_sha256,
        warnings=tuple(warnings),
        truncated=truncated,
        content=content,
    )


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
        channel_id = str(ref.get("channel_id") or "")
        messages = [message] if isinstance(message, dict) else []
        surrounding_fetched = False
        surrounding_truncated = False
        warnings = [
            "Discord link resolved to a single message, not a thread plan.",
            SINGLE_MESSAGE_CONTEXT_WARNING,
        ]
        if channel_id and target_id:
            surrounding, surrounding_truncated = _fetch_single_message_surrounding_context(
                channel_id,
                target_id,
                token,
                request_func,
                max_raw_chars=min(max_raw_chars, _SINGLE_MESSAGE_RAW_CHAR_CAP),
            )
            if surrounding:
                messages = surrounding
                content = _format_messages(messages)
                surrounding_fetched = len(messages) > 1 or any(
                    str(item.get("id") or "") != target_id for item in messages
                )
            else:
                warnings.append("Bounded surrounding context was unavailable for this message.")
        if surrounding_truncated:
            warnings.append("Bounded surrounding context was capped before all nearby messages were included.")
        output_truncated = False
        if len(content) > max_output_chars:
            content = content[:max_output_chars].rstrip() + "\n[Single-message context truncated to output cap.]"
            warnings.append("Single-message context was truncated to output cap.")
            output_truncated = True
        logger.info(
            "discord_context_resolved kind=%s source=%s channel=%s message=%s surrounding_fetched=%s truncated=%s",
            DISCORD_CONTEXT_KIND_SINGLE_MESSAGE,
            source,
            channel_id,
            target_id,
            surrounding_fetched,
            surrounding_truncated or output_truncated,
        )
        return DiscordThreadPlanExpansion(
            source=source,
            thread_id=target_id,
            context_kind=DISCORD_CONTEXT_KIND_SINGLE_MESSAGE,
            channel_id=channel_id,
            selected_message_ids=tuple(str(item.get("id") or "") for item in messages if item.get("id")),
            warnings=tuple(warnings),
            truncated=surrounding_truncated or output_truncated,
            surrounding_context_fetched=surrounding_fetched,
            content=content,
        )
    return DiscordThreadPlanExpansion(
        source=source,
        thread_id=target_id,
        warnings=("Discord snowflake did not resolve to a thread channel.",),
    )


def _fetch_single_message_surrounding_context(
    channel_id: str,
    message_id: str,
    token: str,
    request_func: Callable[..., Any],
    *,
    max_raw_chars: int,
) -> tuple[list[dict[str, Any]], bool]:
    try:
        payload = request_func(
            "GET",
            f"/channels/{channel_id}/messages",
            token,
            params={"limit": str(_SINGLE_MESSAGE_SURROUNDING_LIMIT), "around": message_id},
            timeout=15,
        )
    except Exception as exc:
        logger.info(
            "discord_single_message_surrounding_fetch_failed channel=%s message=%s error=%s",
            channel_id,
            message_id,
            exc,
        )
        return [], False
    if not isinstance(payload, list):
        return [], False
    messages: list[dict[str, Any]] = []
    raw_chars = 0
    truncated = False
    for item in sorted(
        (item for item in payload if isinstance(item, dict)),
        key=lambda item: int(str(item.get("id") or "0") or 0),
    ):
        content_len = len(str(item.get("content") or ""))
        if messages and raw_chars + content_len > max_raw_chars:
            truncated = True
            break
        messages.append(item)
        raw_chars += content_len
    if len(payload) >= _SINGLE_MESSAGE_SURROUNDING_LIMIT:
        truncated = True
    return messages, truncated


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
