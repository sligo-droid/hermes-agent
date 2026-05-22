"""Read-only Discord helper for Kanban role workers."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any


def _request(path: str, *, params: dict[str, str] | None = None) -> Any:
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("DISCORD_BOT_TOKEN is not available in this worker environment")
    from tools.discord_tool import _discord_request

    return _discord_request("GET", path, token, params=params, timeout=15)


def _fetch_message(args: argparse.Namespace) -> Any:
    return _request(f"/channels/{args.channel_id}/messages/{args.message_id}")


def _fetch_messages(args: argparse.Namespace) -> Any:
    params = {"limit": str(max(1, min(int(args.limit), 100)))}
    if args.before:
        params["before"] = args.before
    if args.after:
        params["after"] = args.after
    return _request(f"/channels/{args.channel_id}/messages", params=params)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Discord message access for Hermes Kanban workers."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    one = sub.add_parser("fetch-message")
    one.add_argument("--channel-id", required=True)
    one.add_argument("--message-id", required=True)
    one.set_defaults(func=_fetch_message)

    many = sub.add_parser("fetch-messages")
    many.add_argument("--channel-id", required=True)
    many.add_argument("--limit", type=int, default=25)
    many.add_argument("--before", default="")
    many.add_argument("--after", default="")
    many.set_defaults(func=_fetch_messages)

    args = parser.parse_args(argv)
    try:
        print(json.dumps(args.func(args), ensure_ascii=False, indent=2))
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

