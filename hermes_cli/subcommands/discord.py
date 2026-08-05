"""``hermes discord`` read-only command parser."""

from __future__ import annotations

from typing import Callable


def _add_json_flag(parser) -> None:
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit compact JSON for scripts and agents (default: pretty JSON)",
    )


def _add_history_args(parser, *, default_limit: int = 50) -> None:
    parser.add_argument("--limit", type=int, default=default_limit)
    parser.add_argument("--before", help="Return messages before this snowflake")
    parser.add_argument("--after", help="Return messages after this snowflake")


def build_discord_parser(subparsers, *, cmd_discord: Callable) -> None:
    """Attach the read-only ``discord`` command group to ``subparsers``."""
    parser = subparsers.add_parser(
        "discord",
        help="Read Discord through Hermes' native tools",
        description=(
            "Invoke Hermes' existing read-only Discord tools without starting "
            "an agent or MCP server. Uses the active Hermes profile's bot credentials."
        ),
    )
    actions = parser.add_subparsers(dest="discord_action")

    guilds = actions.add_parser("guilds", help="List accessible Discord guilds")
    _add_json_flag(guilds)

    channels = actions.add_parser("channels", help="List channels in a guild")
    channels.add_argument("guild_id")
    _add_json_flag(channels)

    channel = actions.add_parser("channel", help="Get channel or thread metadata")
    channel.add_argument("channel_id")
    _add_json_flag(channel)

    message = actions.add_parser(
        "get-message",
        help="Get one message by permalink or message ID",
    )
    message.add_argument("message_url_or_id")
    message.add_argument(
        "--channel-id",
        help="Required when the positional value is only a message ID",
    )
    _add_json_flag(message)

    recent = actions.add_parser("recent", help="List recent channel messages")
    recent.add_argument("channel_id")
    _add_history_args(recent)
    _add_json_flag(recent)

    thread = actions.add_parser("get-thread", help="Get thread metadata and messages")
    thread.add_argument("thread_id")
    _add_history_args(thread)
    _add_json_flag(thread)

    search = actions.add_parser(
        "search", help="Search recent paginated channel messages"
    )
    search.add_argument("channel_id")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--max-pages", type=int, default=5)
    search.add_argument("--before", help="Begin scanning before this snowflake")
    _add_json_flag(search)

    reactions = actions.add_parser("reactions", help="Get reactions for a message")
    reactions.add_argument("channel_id")
    reactions.add_argument("message_id")
    _add_json_flag(reactions)

    trace = actions.add_parser(
        "trace",
        help="Inspect a Discord channel, thread, or message URL",
    )
    trace.add_argument(
        "message_url_or_id",
        metavar="URL_OR_MESSAGE_ID",
        help="Discord channel/thread URL, message URL, or message ID",
    )
    trace.add_argument(
        "--channel-id",
        help="Required when the positional value is only a message ID",
    )
    _add_history_args(trace, default_limit=50)
    _add_json_flag(trace)

    parser.set_defaults(func=cmd_discord, _discord_parser=parser)
