from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

from hermes_cli.discord_cli import run
from hermes_cli.subcommands.discord import build_discord_parser


def _parser():
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    handler = lambda args: None
    build_discord_parser(subparsers, cmd_discord=handler)
    return parser, handler


def test_parser_wires_discord_handler_and_permalink():
    parser, handler = _parser()
    args = parser.parse_args([
        "discord",
        "get-message",
        "https://discord.com/channels/1/2/3",
        "--json",
    ])

    assert args.func is handler
    assert args.discord_action == "get-message"
    assert args.message_url_or_id.endswith("/1/2/3")
    assert args.json is True


def test_get_message_dispatches_existing_tool(capsys):
    calls = []

    def dispatch(name, args):
        calls.append((name, args))
        return json.dumps({"message": {"id": "3"}, "channel_id": "2"})

    code = run(
        SimpleNamespace(
            discord_action="get-message",
            message_url_or_id="https://discord.com/channels/1/2/3",
            channel_id=None,
            json=True,
        ),
        dispatch=dispatch,
    )

    assert code == 0
    assert calls == [
        (
            "discord_get_message",
            {"message_url_or_id": "https://discord.com/channels/1/2/3"},
        )
    ]
    assert json.loads(capsys.readouterr().out) == {
        "message": {"id": "3"},
        "channel_id": "2",
    }


def test_tool_error_is_json_and_nonzero(capsys):
    code = run(
        SimpleNamespace(
            discord_action="get-message",
            message_url_or_id="123",
            channel_id=None,
            json=True,
        ),
        dispatch=lambda _name, _args: json.dumps({"error": "channel_id is required"}),
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out) == {"error": "channel_id is required"}


def test_trace_composes_only_registered_read_tools(capsys):
    calls = []

    responses = {
        "discord_get_message": {
            "channel_id": "2",
            "message": {
                "id": "3",
                "channel_id": "2",
                "thread": {"id": "4"},
            },
        },
        "discord_get_channel": {"channel": {"id": "2", "type": "text"}},
        "discord_get_reactions": {"message_id": "3", "reactions": []},
        "discord_get_thread": {"thread": {"id": "4"}, "messages": []},
    }

    def dispatch(name, args):
        calls.append((name, args))
        return json.dumps(responses[name])

    code = run(
        SimpleNamespace(
            discord_action="trace",
            message_url_or_id="https://discord.com/channels/1/2/3",
            channel_id=None,
            limit=25,
            before=None,
            after=None,
            json=True,
        ),
        dispatch=dispatch,
    )

    assert code == 0
    assert [name for name, _args in calls] == [
        "discord_get_message",
        "discord_get_channel",
        "discord_get_reactions",
        "discord_get_thread",
    ]
    assert calls[-1][1] == {"thread_id": "4", "limit": 25}
    payload = json.loads(capsys.readouterr().out)
    assert payload["message"]["message"]["id"] == "3"
    assert payload["thread"]["thread"]["id"] == "4"


def test_trace_returns_nonzero_for_nested_tool_error(capsys):
    responses = {
        "discord_get_message": {
            "channel_id": "2",
            "message": {"id": "3", "channel_id": "2"},
        },
        "discord_get_channel": {"id": "2", "type": "text"},
        "discord_get_reactions": {"error": "forbidden"},
    }

    code = run(
        SimpleNamespace(
            discord_action="trace",
            message_url_or_id="https://discord.com/channels/1/2/3",
            channel_id=None,
            limit=25,
            before=None,
            after=None,
            json=True,
        ),
        dispatch=lambda name, _args: json.dumps(responses[name]),
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out)["reactions"] == {"error": "forbidden"}


def test_bare_discord_prints_help(capsys):
    parser, _handler = _parser()
    args = parser.parse_args(["discord"])

    code = run(args, dispatch=lambda _name, _args: "{}")

    assert code == 0
    assert "hermes discord" in capsys.readouterr().out
