from hermes_cli.discord_thread_context import expand_discord_thread_references


THREAD_ID = "1511795999700680744"
PARENT_ID = "1504252294495998043"
GUILD_ID = "1502787243230756904"
STARTER_ID = THREAD_ID


def _message(message_id, content, *, bot=True, username="Sligo Labs"):
    return {
        "id": str(message_id),
        "content": content,
        "author": {"id": "9" if bot else "8", "username": username, "bot": bot},
    }


def _request(messages):
    def fake(method, path, token, params=None, body=None, timeout=15):
        assert token == "token"
        if path == f"/channels/{THREAD_ID}":
            return {"id": THREAD_ID, "type": 11, "name": "planning-thread"}
        if path == f"/channels/{PARENT_ID}/messages/{STARTER_ID}":
            return {"id": STARTER_ID, "thread": {"id": THREAD_ID, "type": 11, "name": "planning-thread"}}
        if path == f"/channels/{THREAD_ID}/messages":
            return list(reversed(messages))
        raise AssertionError(f"unexpected Discord request: {method} {path}")

    return fake


def _request_single_message(messages, *, fail_surrounding=False):
    target_id = messages[len(messages) // 2]["id"]

    def fake(method, path, token, params=None, body=None, timeout=15):
        assert token == "token"
        if path == f"/channels/{target_id}":
            return {"id": target_id, "type": 0, "name": "not-a-thread"}
        if path == f"/channels/{PARENT_ID}/messages/{target_id}":
            return next(item for item in messages if item["id"] == target_id)
        if path == f"/channels/{PARENT_ID}/messages":
            assert params == {"limit": "9", "around": target_id}
            if fail_surrounding:
                raise RuntimeError("missing access")
            return list(reversed(messages))
        raise AssertionError(f"unexpected Discord request: {method} {path}")

    return fake, target_id


def test_url_to_thread_starter_expands_last_plan_like_bot_messages():
    messages = [
        _message("100", "OP: please discuss", bot=False),
        _message("101", "(1/2) Plan\nBuild the first implementation slice."),
        _message("102", "(2/2) Test plan\nRun the focused checks."),
    ]
    url = f"https://discord.com/channels/{GUILD_ID}/{PARENT_ID}/{STARTER_ID}"

    expansions = expand_discord_thread_references(url, token="token", request_func=_request(messages))

    assert len(expansions) == 1
    formatted = expansions[0].formatted()
    assert "planning-thread" in formatted
    assert "Selected plan messages: 101, 102" in formatted
    assert "Build the first implementation slice" in formatted
    assert "OP: please discuss" not in formatted


def test_bare_thread_id_expands_thread_plan():
    messages = [
        _message("201", "Chatter", bot=False),
        _message("202", "## Implementation phases\n1. Add expansion.\n2. Verify."),
    ]

    expansions = expand_discord_thread_references(THREAD_ID, token="token", request_func=_request(messages))

    assert len(expansions) == 1
    assert expansions[0].thread_id == THREAD_ID
    assert expansions[0].selected_message_ids == ("202",)
    assert "Add expansion" in expansions[0].content


def test_multiple_plan_groups_selects_last_group():
    messages = [
        _message("301", "Plan\nOld plan that should be ignored."),
        _message("302", "interruption", bot=False),
        _message("303", "## Final recommendation\nUse the newer plan."),
        _message("304", "Acceptance criteria\nThe newer plan passes tests."),
    ]

    expansion = expand_discord_thread_references(THREAD_ID, token="token", request_func=_request(messages))[0]

    assert expansion.selected_message_ids == ("303", "304")
    assert "Use the newer plan" in expansion.content
    assert "Old plan" not in expansion.content


def test_token_falls_back_to_discord_tool_config(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    from tools import discord_tool

    monkeypatch.setattr(discord_tool, "_get_bot_token", lambda: "token")
    messages = [_message("401", "## Plan\nUse configured token.")]

    expansion = expand_discord_thread_references(THREAD_ID, request_func=_request(messages))[0]

    assert expansion.selected_message_ids == ("401",)
    assert "Use configured token" in expansion.content


def test_single_message_url_formats_degraded_context_with_surrounding_messages():
    messages = [
        _message("1511799412559708501", "Earlier context", bot=False, username="Alice"),
        _message("1511799412559708502", "Please implement this small fix", bot=False, username="Bob"),
        _message("1511799412559708503", "Later acknowledgement", bot=False, username="Alice"),
    ]
    request, target_id = _request_single_message(messages)
    url = f"https://discord.com/channels/{GUILD_ID}/{PARENT_ID}/{target_id}"

    expansion = expand_discord_thread_references(url, token="token", request_func=request)[0]
    formatted = expansion.formatted()

    assert expansion.context_kind == "single_message"
    assert expansion.surrounding_context_fetched is True
    assert "[Expanded Discord single-message context]" in formatted
    assert "[Expanded Discord thread plan]" not in formatted
    assert "Degraded context: true" in formatted
    assert "do not assume missing thread history" in formatted
    assert "Earlier context" in formatted
    assert "Please implement this small fix" in formatted
    assert expansion.selected_message_ids == tuple(item["id"] for item in messages)


def test_single_message_url_keeps_target_when_surrounding_context_unavailable():
    messages = [_message("1511799412559708602", "Only this message is visible", bot=False, username="Bob")]
    request, target_id = _request_single_message(messages, fail_surrounding=True)
    url = f"https://discord.com/channels/{GUILD_ID}/{PARENT_ID}/{target_id}"

    expansion = expand_discord_thread_references(url, token="token", request_func=request)[0]

    assert expansion.context_kind == "single_message"
    assert expansion.surrounding_context_fetched is False
    assert "Only this message is visible" in expansion.content
    assert any("surrounding context was unavailable" in warning for warning in expansion.warnings)
