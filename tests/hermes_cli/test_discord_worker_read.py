from __future__ import annotations

import json
from urllib import error, request

import pytest


def test_fetch_message_uses_get_with_bot_token(monkeypatch, capsys):
    from hermes_cli import discord_worker_read as reader

    calls = []

    def fake_request(method, path, token, params=None, body=None, timeout=15):
        calls.append(
            {
                "method": method,
                "path": path,
                "token": token,
                "params": params,
                "body": body,
                "timeout": timeout,
            }
        )
        return {"id": "456", "content": "bug report"}

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token")
    monkeypatch.setattr(reader, "_discord_request", fake_request)

    code = reader.main(
        [
            "fetch-message",
            "--channel-id",
            "123",
            "--message-id",
            "456",
        ]
    )

    assert code == 0
    assert calls == [
        {
            "method": "GET",
            "path": "/channels/123/messages/456",
            "token": "bot-token",
            "params": None,
            "body": None,
            "timeout": 15,
        }
    ]
    assert json.loads(capsys.readouterr().out) == {"id": "456", "content": "bug report"}


def test_fetch_message_uses_read_broker_without_bot_token(monkeypatch, capsys):
    from hermes_cli import discord_worker_read as reader

    calls = []

    def fake_request(method, path, token, params=None, body=None, timeout=15):
        calls.append((method, path, token, params, body, timeout))
        return {"id": "456", "content": "from broker"}

    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(reader, "_discord_request", fake_request)
    base_url, bearer = reader.start_read_broker("bot-token")
    monkeypatch.setenv("HERMES_DISCORD_WORKER_READ_URL", base_url)
    monkeypatch.setenv("HERMES_DISCORD_WORKER_READ_TOKEN", bearer)
    try:
        code = reader.main(
            [
                "fetch-message",
                "--channel-id",
                "123",
                "--message-id",
                "456",
            ]
        )
    finally:
        reader._shutdown_read_broker_for_tests()

    assert code == 0
    assert calls == [("GET", "/channels/123/messages/456", "bot-token", None, None, 15)]
    assert json.loads(capsys.readouterr().out) == {"id": "456", "content": "from broker"}


def test_read_broker_rejects_unsupported_discord_endpoint(monkeypatch):
    from hermes_cli import discord_worker_read as reader

    monkeypatch.setattr(reader, "_discord_request", lambda *args, **kwargs: {"ok": True})
    base_url, bearer = reader.start_read_broker("bot-token")
    req = request.Request(
        f"{base_url}/guilds/123",
        headers={"Authorization": f"Bearer {bearer}"},
        method="GET",
    )
    try:
        with pytest.raises(error.HTTPError) as exc_info:
            request.urlopen(req, timeout=15)
    finally:
        reader._shutdown_read_broker_for_tests()

    assert exc_info.value.code == 404


def test_fetch_messages_uses_get_history_params(monkeypatch, capsys):
    from hermes_cli import discord_worker_read as reader

    calls = []

    def fake_request(method, path, token, params=None, body=None, timeout=15):
        calls.append((method, path, token, params, body))
        return [{"id": "789"}]

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token")
    monkeypatch.setattr(reader, "_discord_request", fake_request)

    code = reader.main(
        [
            "fetch-messages",
            "--channel-id",
            "123",
            "--limit",
            "250",
            "--before",
            "999",
            "--after",
            "111",
        ]
    )

    assert code == 0
    assert calls == [
        (
            "GET",
            "/channels/123/messages",
            "bot-token",
            {"limit": "100", "before": "999", "after": "111"},
            None,
        )
    ]
    assert json.loads(capsys.readouterr().out) == [{"id": "789"}]


def test_missing_token_is_clear_and_nonzero(monkeypatch, capsys):
    from hermes_cli import discord_worker_read as reader

    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    code = reader.main(
        [
            "fetch-message",
            "--channel-id",
            "123",
            "--message-id",
            "456",
        ]
    )

    assert code == 1
    assert "HERMES_DISCORD_WORKER_READ_URL is required" in capsys.readouterr().err
