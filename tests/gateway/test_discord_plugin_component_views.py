from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from plugins.platforms.discord.adapter import (
    DiscordAdapter,
    PluginPersistentView,
    _remember_channel_is_forum,
    _standalone_send,
)
from gateway.config import PlatformConfig


def test_persistent_plugin_view_keeps_free_text_action_last():
    calls = []

    async def handler(_interaction, action):
        calls.append(action)

    view = PluginPersistentView(
        {
            "name": "client-knowledge-review",
            "handler": handler,
            "components": [
                {"action": "approve", "label": "Approve all", "style": "success"},
                {"action": "reject", "label": "Reject", "style": "danger"},
                {
                    "action": "instructions",
                    "label": "✍️ Other",
                    "style": "secondary",
                },
            ],
        }
    )
    assert view.timeout is None
    assert [button.label for button in view.children] == [
        "Approve all",
        "Reject",
        "✍️ Other",
    ]
    assert view.children[-1].custom_id == "client-knowledge-review:instructions"
    response = SimpleNamespace(defer=AsyncMock(), is_done=lambda: False)
    asyncio.run(view.children[-1].callback(SimpleNamespace(response=response)))
    response.defer.assert_awaited_once_with(ephemeral=True, thinking=False)
    assert calls == ["instructions"]


def test_discord_component_view_accessor_discovers_plugins_before_restart_registration(
    monkeypatch,
):
    from hermes_cli import plugins as plugin_module

    calls = []
    manager = SimpleNamespace(
        discover_and_load=lambda force=False: calls.append(force),
        get_discord_component_views=lambda: [{"name": "review"}],
    )
    monkeypatch.setattr(plugin_module, "get_plugin_manager", lambda: manager)

    assert plugin_module.get_discord_component_views() == [{"name": "review"}]
    assert calls == [False]


@pytest.mark.asyncio
async def test_discord_cold_connect_registers_discovered_persistent_view(monkeypatch):
    added_views = []

    class Bot:
        user = SimpleNamespace(id=999)
        guilds = []
        tree = MagicMock()
        tree.sync = AsyncMock(return_value=[])

        def __init__(self, **_kwargs):
            self._events = {}

        def add_view(self, view):
            added_views.append(view)

        def event(self, callback):
            self._events[callback.__name__] = callback
            return callback

        async def start(self, _token):
            await self._events["on_ready"]()

        async def close(self):
            return None

    definition = {
        "name": "client-knowledge-review",
        "handler": AsyncMock(),
        "components": [
            {"action": "approve", "label": "Approve all", "style": "success"},
            {"action": "reject", "label": "Reject", "style": "danger"},
            {
                "action": "instructions",
                "label": "✍️ Other",
                "style": "secondary",
            },
        ],
    }
    monkeypatch.setattr(
        "gateway.status.acquire_scoped_lock",
        lambda *_args, **_kwargs: (True, None),
    )
    monkeypatch.setattr("gateway.status.release_scoped_lock", lambda *_args: None)
    monkeypatch.setattr(
        "plugins.platforms.discord.adapter.commands.Bot", lambda **kwargs: Bot(**kwargs)
    )
    monkeypatch.setattr(
        "hermes_cli.plugins.get_discord_component_views", lambda: [definition]
    )
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="test-token"))
    monkeypatch.setattr(adapter, "_resolve_allowed_usernames", AsyncMock())
    monkeypatch.setattr(adapter, "_run_post_connect_initialization", AsyncMock())

    assert await adapter.connect() is True
    assert len(added_views) == 1
    assert [item.label for item in added_views[0].children] == [
        "Approve all", "Reject", "✍️ Other"
    ]
    await adapter.disconnect()


def _resp(status, json_data=None, text_data=None):
    response = AsyncMock()
    response.status = status
    body = (
        json.dumps(json_data or {}).encode()
        if json_data is not None
        else (text_data or "").encode()
    )
    response.content = MagicMock()
    response.content.read = AsyncMock(side_effect=[body, b"", b""])
    response.get_encoding = MagicMock(return_value="utf-8")
    response.json = AsyncMock(return_value=json_data or {})
    response.text = AsyncMock(return_value=text_data or "")
    return response


def _sessions(response_groups):
    calls = []
    sessions = []
    for responses in response_groups:
        index = [0]

        def post(url, _responses=responses, _index=index, **kwargs):
            calls.append((url, kwargs.get("json")))
            response = _responses[min(_index[0], len(_responses) - 1)]
            _index[0] += 1
            context = MagicMock()
            context.__aenter__ = AsyncMock(return_value=response)
            context.__aexit__ = AsyncMock(return_value=False)
            return context

        session = MagicMock()
        session.post = MagicMock(side_effect=post)
        context = MagicMock()
        context.__aenter__ = AsyncMock(return_value=session)
        context.__aexit__ = AsyncMock(return_value=False)
        sessions.append(context)
    return sessions, calls


@pytest.mark.asyncio
async def test_standalone_structured_review_sends_parent_then_detail_thread():
    chat_id = "200"
    _remember_channel_is_forum(chat_id, False)
    sessions, calls = _sessions(
        [
            [_resp(200, {"id": "400"})],
            [
                _resp(201, {"id": "401"}),
                _resp(200, {"id": "402"}),
                _resp(200, {"id": "403"}),
            ],
        ]
    )
    metadata = {
        "require_single_message": True,
        "allowed_role_mentions": ["300"],
        "strict_role_mentions": True,
        "_discord_embed": {
            "title": "PID knowledge review",
            "description": "3 proposed additions",
        },
        "_discord_components": [
            {
                "type": 1,
                "components": [
                    {
                        "type": 2,
                        "style": 3,
                        "label": "Approve all",
                        "custom_id": "client-knowledge-review:approve",
                    },
                    {
                        "type": 2,
                        "style": 4,
                        "label": "Reject",
                        "custom_id": "client-knowledge-review:reject",
                    },
                    {
                        "type": 2,
                        "style": 2,
                        "label": "✍️ Other",
                        "custom_id": "client-knowledge-review:instructions",
                    },
                ],
            }
        ],
        "_discord_thread": {
            "name": "PID knowledge review details",
            "messages": ["detail one", "detail two"],
        },
    }
    with patch("aiohttp.ClientSession", side_effect=sessions):
        result = await _standalone_send(
            SimpleNamespace(token="bot-token", extra={}),
            chat_id,
            "<@&300>",
            metadata=metadata,
        )

    assert result == {
        "success": True,
        "platform": "discord",
        "chat_id": "200",
        "message_id": "400",
        "side_effect_state": "confirmed",
        "detail_state": "confirmed",
        "thread_id": "401",
    }
    parent = calls[0][1]
    assert parent["embeds"][0]["title"] == "PID knowledge review"
    assert [item["label"] for item in parent["components"][0]["components"]] == [
        "Approve all", "Reject", "✍️ Other"
    ]
    assert calls[1][0].endswith("/messages/400/threads")
    assert calls[2][1]["content"] == "detail one"
    assert calls[3][1]["content"] == "detail two"


@pytest.mark.asyncio
async def test_standalone_structured_review_retries_rate_limited_detail_message(monkeypatch):
    chat_id = "210"
    _remember_channel_is_forum(chat_id, False)
    sessions, calls = _sessions(
        [
            [_resp(200, {"id": "410"})],
            [
                _resp(201, {"id": "411"}),
                _resp(429, {"retry_after": 0.01}),
                _resp(200, {"id": "412"}),
            ],
        ]
    )
    sleep = AsyncMock()
    monkeypatch.setattr("plugins.platforms.discord.adapter.asyncio.sleep", sleep)
    metadata = {
        "require_single_message": True,
        "allowed_role_mentions": ["300"],
        "strict_role_mentions": True,
        "_discord_embed": {"title": "PID review"},
        "_discord_components": [{"type": 1, "components": [{
            "type": 2, "style": 2, "label": "✍️ Other",
            "custom_id": "client-knowledge-review:instructions",
        }]}],
        "_discord_thread": {"name": "details", "messages": ["detail one"]},
    }
    with patch("aiohttp.ClientSession", side_effect=sessions):
        result = await _standalone_send(
            SimpleNamespace(token="bot-token", extra={}),
            chat_id,
            "<@&300> review",
            metadata=metadata,
        )
    assert result["detail_state"] == "confirmed"
    sleep.assert_awaited_once()
    assert [
        body["content"]
        for url, body in calls
        if "/channels/411/messages" in url and body
    ] == ["detail one", "detail one"]
