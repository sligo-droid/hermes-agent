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
    monkeypatch.setenv("HERMES_DISCORD_WORKER_READ_ONLY", "1")
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


def test_read_only_worker_blocks_control_mutations(monkeypatch):
    from hermes_cli import discord_worker_read as reader

    monkeypatch.setenv("HERMES_DISCORD_WORKER_READ_ONLY", "1")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token")

    with pytest.raises(RuntimeError, match="read-only"):
        reader.discord_request("PATCH", "/channels/123/messages/456", body={"content": "no"})
    with pytest.raises(RuntimeError, match="read-only"):
        reader.update_board("board-a", goal_status="done")
    with pytest.raises(RuntimeError, match="read-only"):
        reader.update_task_status("board-a", "task-a", "done")
    with pytest.raises(RuntimeError, match="read-only"):
        reader.sync_summary_message("board-a")


def test_worker_broker_rejects_invalid_discord_endpoint(monkeypatch):
    from hermes_cli import discord_worker_read as reader

    monkeypatch.setattr(reader, "_discord_request", lambda *args, **kwargs: {"ok": True})
    base_url, bearer = reader.start_read_broker("bot-token")
    req = request.Request(
        f"{base_url}/http://example.com",
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


def test_discord_request_uses_broker_for_patch_without_bot_token(monkeypatch):
    from hermes_cli import discord_worker_read as reader

    calls = []

    def fake_request(method, path, token, params=None, body=None, timeout=15):
        calls.append((method, path, token, params, body, timeout))
        return {"id": "456", "edited": True}

    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setattr(reader, "_discord_request", fake_request)
    base_url, bearer = reader.start_read_broker("bot-token")
    monkeypatch.setenv("HERMES_DISCORD_WORKER_CONTROL_URL", base_url)
    monkeypatch.setenv("HERMES_DISCORD_WORKER_CONTROL_TOKEN", bearer)
    try:
        payload = reader.discord_request(
            "PATCH",
            "/channels/123/messages/456",
            body={"content": "updated"},
        )
    finally:
        reader._shutdown_read_broker_for_tests()

    assert payload == {"id": "456", "edited": True}
    assert calls == [
        (
            "PATCH",
            "/channels/123/messages/456",
            "bot-token",
            None,
            {"content": "updated"},
            15,
        )
    ]


def test_update_board_mutates_status_and_summary_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    from hermes_cli import discord_worker_boards as boards
    from hermes_cli import kanban_db
    from hermes_cli import discord_worker_read as reader

    board = boards.start_direct_goal(thread_id="9001", goal="Ship it")

    result = reader.update_board(
        board.slug,
        goal_status="done",
        phase="complete",
        clear_blocked_reason=True,
        concise_outcome="PR #136 is merged.",
        sync_summary=True,
        sync_reaction=True,
        persist_summary=True,
    )

    worker = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert result["success"] is True
    assert worker["goal_status"] == "done"
    assert worker["phase"] == "complete"
    assert worker["blocked_reason"] == ""
    assert worker["concise_outcome"] == "PR #136 is merged."
    assert worker["terminal_summary_sync_pending"] is True
    assert worker["terminal_reaction_sync_pending"] is True
    assert worker["terminal_completion_message_pending"] is True
    assert boards.read_board_run_summary(board.slug)["goal_status"] == "done"


def test_sync_summary_patches_discord_summary_message(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "kanban"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    from hermes_cli import discord_worker_boards as boards
    from hermes_cli import discord_worker_read as reader

    board = boards.start_direct_goal(thread_id="9001", goal="Ship it")
    boards.set_feature_summary_handle(board.slug, message_id="summary-1", source_message_id="source-1")
    reader.update_board(
        board.slug,
        goal_status="done",
        phase="complete",
        concise_outcome="PR #136 is merged.",
        pr_url="https://github.com/sligo-labs/PID/pull/136",
    )
    calls = []

    def fake_request(method, path, token, params=None, body=None, timeout=15):
        calls.append({"method": method, "path": path, "token": token, "body": body})
        return {"id": "summary-1"}

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "bot-token")
    monkeypatch.setattr(reader, "_discord_request", fake_request)

    code = reader.main(["sync-summary", "--board", board.slug])

    assert code == 0
    assert json.loads(capsys.readouterr().out)["success"] is True
    assert calls[0]["method"] == "PATCH"
    assert calls[0]["path"] == "/channels/9001/messages/summary-1"
    assert calls[0]["token"] == "bot-token"
    embed = calls[0]["body"]["embeds"][0]
    assert embed["fields"][0] == {"name": "Status", "value": "Complete", "inline": True}
    assert any(field["name"] == "GitHub PR" for field in embed["fields"])


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
