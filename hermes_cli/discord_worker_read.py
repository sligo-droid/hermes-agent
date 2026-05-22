"""Read-only Discord message helper for Kanban coding workers."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Sequence

from tools.discord_tool import DiscordAPIError, _discord_request


def _token() -> str:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is required for Discord worker reads")
    return token


def fetch_message(channel_id: str, message_id: str) -> Any:
    return _discord_request(
        "GET",
        f"/channels/{channel_id}/messages/{message_id}",
        _token(),
    )


def fetch_messages(
    channel_id: str,
    limit: int,
    *,
    before: str | None = None,
    after: str | None = None,
) -> Any:
    params = {"limit": str(max(1, min(int(limit), 100)))}
    if before:
        params["before"] = before
    if after:
        params["after"] = after
    return _discord_request(
        "GET",
        f"/channels/{channel_id}/messages",
        _token(),
        params=params,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read Discord messages using the worker's bot token."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    single = subparsers.add_parser("fetch-message", help="Fetch one Discord message")
    single.add_argument("--channel-id", required=True)
    single.add_argument("--message-id", required=True)

    history = subparsers.add_parser("fetch-messages", help="Fetch Discord channel history")
    history.add_argument("--channel-id", required=True)
    history.add_argument("--limit", required=True, type=int)
    history.add_argument("--before")
    history.add_argument("--after")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "fetch-message":
            payload = fetch_message(args.channel_id, args.message_id)
        else:
            payload = fetch_messages(
                args.channel_id,
                args.limit,
                before=args.before,
                after=args.after,
            )
    except DiscordAPIError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
