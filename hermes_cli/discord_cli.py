"""Read-only CLI adapter for Hermes' native Discord tools.

This module intentionally contains no Discord REST implementation.  It imports
``tools.discord_tool`` for registration, dispatches the existing first-class
read tools through the central registry, and prints their JSON results.  That
gives external agents a lazy, process-per-use bridge without an always-on MCP
server or a second source of truth for Discord behavior.
"""

from __future__ import annotations

import json
import re
import sys
from argparse import Namespace
from collections.abc import Callable
from typing import Any


Dispatch = Callable[[str, dict[str, Any]], str]

_DISCORD_TRACE_URL_RE = re.compile(
    r"^https?://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild_id>[^/]+)/(?P<channel_id>\d+)"
    r"(?:/(?P<message_id>\d+))?/?(?:[?#].*)?$"
)
_DISCORD_CHANNEL_URL_PREFIX_RE = re.compile(
    r"^https?://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/channels/"
)


def _registry_dispatch(name: str, args: dict[str, Any]) -> str:
    # Import for registration side effects without discovering every Hermes
    # tool or starting plugin/MCP infrastructure.
    import tools.discord_tool  # noqa: F401
    from tools.registry import registry

    return registry.dispatch(name, args)


def _decode_result(raw: str) -> Any:
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {
            "error": "Discord tool returned invalid JSON.",
            "raw_result": str(raw),
        }


def _call(dispatch: Dispatch, tool_name: str, args: dict[str, Any]) -> Any:
    return _decode_result(dispatch(tool_name, args))


def _contains_error(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("error"):
            return True
        return any(_contains_error(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_error(item) for item in value)
    return False


def _clean_optional(**values: Any) -> dict[str, Any]:
    return {
        key: value for key, value in values.items() if value is not None and value != ""
    }


def _trace_channel(
    args: Namespace,
    dispatch: Dispatch,
    channel_id: str,
) -> dict[str, Any]:
    """Inspect a channel URL, selecting thread or channel message retrieval."""
    channel_result = _call(
        dispatch,
        "discord_get_channel",
        {"channel_id": channel_id},
    )
    if not isinstance(channel_result, dict) or channel_result.get("error"):
        return channel_result

    result: dict[str, Any] = {"channel": channel_result}
    channel = channel_result.get("channel") or channel_result
    channel_type = str(channel.get("type") or "") if isinstance(channel, dict) else ""
    history_args = _clean_optional(
        limit=args.limit,
        before=getattr(args, "before", None),
        after=getattr(args, "after", None),
    )

    if channel_type.endswith("_thread"):
        result["thread"] = _call(
            dispatch,
            "discord_get_thread",
            {"thread_id": channel_id, **history_args},
        )
    else:
        result["recent"] = _call(
            dispatch,
            "discord_list_recent",
            {"channel_id": channel_id, **history_args},
        )
    return result


def _trace(args: Namespace, dispatch: Dispatch) -> dict[str, Any]:
    """Inspect a Discord channel/thread URL or message reference."""
    trace_target = str(args.message_url_or_id or "").strip()
    url_match = _DISCORD_TRACE_URL_RE.match(trace_target)
    if url_match and not url_match.group("message_id"):
        return _trace_channel(args, dispatch, url_match.group("channel_id"))
    if _DISCORD_CHANNEL_URL_PREFIX_RE.match(trace_target) and not url_match:
        return {
            "error": (
                "trace target must be a Discord channel/thread URL "
                "(/channels/<guild>/<channel>) or message URL "
                "(/channels/<guild>/<channel>/<message>)."
            )
        }

    message_result = _call(
        dispatch,
        "discord_get_message",
        _clean_optional(
            message_url_or_id=args.message_url_or_id,
            channel_id=getattr(args, "channel_id", None),
        ),
    )
    if not isinstance(message_result, dict) or message_result.get("error"):
        return message_result

    message = message_result.get("message") or {}
    channel_id = str(
        message_result.get("channel_id") or message.get("channel_id") or ""
    )
    message_id = str(message.get("id") or "")

    result: dict[str, Any] = {"message": message_result}

    if channel_id:
        result["channel"] = _call(
            dispatch,
            "discord_get_channel",
            {"channel_id": channel_id},
        )

    if channel_id and message_id:
        result["reactions"] = _call(
            dispatch,
            "discord_get_reactions",
            {"channel_id": channel_id, "message_id": message_id},
        )

    embedded_thread = message.get("thread") if isinstance(message, dict) else None
    thread_id = ""
    if isinstance(embedded_thread, dict):
        thread_id = str(embedded_thread.get("id") or "")

    channel_result = result.get("channel")
    if not thread_id and isinstance(channel_result, dict):
        channel = channel_result.get("channel") or channel_result
        if isinstance(channel, dict) and str(channel.get("type") or "").endswith(
            "_thread"
        ):
            thread_id = channel_id

    if thread_id:
        result["thread"] = _call(
            dispatch,
            "discord_get_thread",
            _clean_optional(
                thread_id=thread_id,
                limit=args.limit,
                before=getattr(args, "before", None),
                after=getattr(args, "after", None),
            ),
        )
    else:
        result["thread"] = None

    return result


def _payload_for_args(args: Namespace, dispatch: Dispatch) -> Any:
    action = args.discord_action

    if action == "guilds":
        return _call(dispatch, "discord_list_guilds", {})
    if action == "channels":
        return _call(
            dispatch,
            "discord_list_channels",
            {"guild_id": args.guild_id},
        )
    if action == "channel":
        return _call(
            dispatch,
            "discord_get_channel",
            {"channel_id": args.channel_id},
        )
    if action == "get-message":
        return _call(
            dispatch,
            "discord_get_message",
            _clean_optional(
                message_url_or_id=args.message_url_or_id,
                channel_id=getattr(args, "channel_id", None),
            ),
        )
    if action == "recent":
        return _call(
            dispatch,
            "discord_list_recent",
            _clean_optional(
                channel_id=args.channel_id,
                limit=args.limit,
                before=getattr(args, "before", None),
                after=getattr(args, "after", None),
            ),
        )
    if action == "get-thread":
        return _call(
            dispatch,
            "discord_get_thread",
            _clean_optional(
                thread_id=args.thread_id,
                limit=args.limit,
                before=getattr(args, "before", None),
                after=getattr(args, "after", None),
            ),
        )
    if action == "search":
        return _call(
            dispatch,
            "discord_search_messages",
            _clean_optional(
                channel_id=args.channel_id,
                query=args.query,
                limit=args.limit,
                max_pages=args.max_pages,
                before=getattr(args, "before", None),
            ),
        )
    if action == "reactions":
        return _call(
            dispatch,
            "discord_get_reactions",
            {"channel_id": args.channel_id, "message_id": args.message_id},
        )
    if action == "trace":
        return _trace(args, dispatch)

    return {"error": f"Unknown Discord action: {action}"}


def run(args: Namespace, *, dispatch: Dispatch = _registry_dispatch) -> int:
    if not getattr(args, "discord_action", None):
        parser = getattr(args, "_discord_parser", None)
        if parser is not None:
            parser.print_help()
            return 0
        print("Run 'hermes discord --help' for available commands.", file=sys.stderr)
        return 2

    payload = _payload_for_args(args, dispatch)
    compact = bool(getattr(args, "json", False))
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=None if compact else 2,
            separators=(",", ":") if compact else None,
        )
    )
    return 1 if _contains_error(payload) else 0
