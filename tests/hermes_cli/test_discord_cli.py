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


def test_trace_thread_url_fetches_thread_without_message_or_reactions(capsys):
    calls = []
    responses = {
        "discord_get_channel": {"id": "2", "type": "private_thread"},
        "discord_get_thread": {"thread": {"id": "2"}, "messages": []},
    }

    def dispatch(name, args):
        calls.append((name, args))
        return json.dumps(responses[name])

    code = run(
        SimpleNamespace(
            discord_action="trace",
            message_url_or_id="https://discord.com/channels/1/2",
            channel_id=None,
            limit=25,
            before="100",
            after=None,
            json=True,
        ),
        dispatch=dispatch,
    )

    assert code == 0
    assert calls == [
        ("discord_get_channel", {"channel_id": "2"}),
        (
            "discord_get_thread",
            {"thread_id": "2", "limit": 25, "before": "100"},
        ),
    ]
    payload = json.loads(capsys.readouterr().out)
    assert payload["thread"]["thread"]["id"] == "2"


def test_trace_public_thread_url_uses_generic_thread_type_detection(capsys):
    calls = []

    def dispatch(name, args):
        calls.append((name, args))
        if name == "discord_get_channel":
            return json.dumps({"channel": {"id": "2", "type": "public_thread"}})
        return json.dumps({"thread": {"id": "2"}, "messages": []})

    code = run(
        SimpleNamespace(
            discord_action="trace",
            message_url_or_id="https://canary.discordapp.com/channels/1/2",
            channel_id=None,
            limit=50,
            before=None,
            after=None,
            json=True,
        ),
        dispatch=dispatch,
    )

    assert code == 0
    assert [name for name, _args in calls] == [
        "discord_get_channel",
        "discord_get_thread",
    ]
    capsys.readouterr()


def test_trace_channel_url_fetches_recent_messages(capsys):
    calls = []

    def dispatch(name, args):
        calls.append((name, args))
        if name == "discord_get_channel":
            return json.dumps({"id": "1534600248553373887", "type": "text"})
        return json.dumps({"messages": [], "count": 0})

    code = run(
        SimpleNamespace(
            discord_action="trace",
            message_url_or_id=(
                "https://discord.com/channels/1502787243230756904/"
                "1534600248553373887"
            ),
            channel_id=None,
            limit=10,
            before=None,
            after="200",
            json=True,
        ),
        dispatch=dispatch,
    )

    assert code == 0
    assert calls == [
        ("discord_get_channel", {"channel_id": "1534600248553373887"}),
        (
            "discord_list_recent",
            {
                "channel_id": "1534600248553373887",
                "limit": 10,
                "after": "200",
            },
        ),
    ]
    assert json.loads(capsys.readouterr().out)["recent"]["count"] == 0


def test_trace_rejects_malformed_discord_channel_url_without_dispatch(capsys):
    calls = []
    code = run(
        SimpleNamespace(
            discord_action="trace",
            message_url_or_id="https://discord.com/channels/1/not-a-channel",
            channel_id=None,
            limit=50,
            before=None,
            after=None,
            json=True,
        ),
        dispatch=lambda name, args: calls.append((name, args)) or "{}",
    )

    assert code == 1
    assert calls == []
    assert "channel/thread URL" in json.loads(capsys.readouterr().out)["error"]


def test_trace_channel_metadata_error_is_propagated(capsys):
    code = run(
        SimpleNamespace(
            discord_action="trace",
            message_url_or_id="https://discord.com/channels/1/2",
            channel_id=None,
            limit=50,
            before=None,
            after=None,
            json=True,
        ),
        dispatch=lambda _name, _args: json.dumps({"error": "forbidden"}),
    )

    assert code == 1
    assert json.loads(capsys.readouterr().out) == {"error": "forbidden"}


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
