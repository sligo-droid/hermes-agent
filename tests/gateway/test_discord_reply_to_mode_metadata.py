from __future__ import annotations

from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


class _FakeReplyMessage:
    def __init__(self, message_id: int):
        self.id = message_id

    def to_reference(self, fail_if_not_exists: bool = False):  # noqa: ARG002
        return f"ref:{self.id}"


class _FakeChannel:
    id = 123
    guild = SimpleNamespace(id=456)
    parent = None
    parent_id = None

    def __init__(self):
        self.sent: list[dict] = []

    async def fetch_message(self, message_id: int):
        return _FakeReplyMessage(message_id)

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=9000 + len(self.sent))


class _FakeClient:
    def __init__(self, channel: _FakeChannel):
        self.channel = channel

    def get_channel(self, channel_id: int):  # noqa: ARG002
        return self.channel

    async def fetch_channel(self, channel_id: int):  # noqa: ARG002
        return self.channel


@pytest.mark.asyncio
async def test_discord_send_metadata_reply_to_mode_all_replies_every_chunk():
    channel = _FakeChannel()
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="token"))
    adapter._client = _FakeClient(channel)
    adapter._reply_to_mode = "first"
    adapter._last_self_message_id = {}
    adapter.format_message = lambda content: content
    adapter.truncate_message = lambda content, max_length=4096, len_fn=None: ["chunk one", "chunk two"]
    adapter._persist_plan_artifact_for_send = lambda **_kwargs: None

    result = await adapter.send(
        "123",
        "ignored",
        reply_to="777",
        metadata={"reply_to_mode": "all"},
    )

    assert result.success
    assert [sent["reference"] for sent in channel.sent] == ["ref:777", "ref:777"]


@pytest.mark.asyncio
async def test_discord_send_metadata_reply_to_mode_off_suppresses_replies():
    channel = _FakeChannel()
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="token"))
    adapter._client = _FakeClient(channel)
    adapter._reply_to_mode = "first"
    adapter._last_self_message_id = {}
    adapter.format_message = lambda content: content
    adapter.truncate_message = lambda content, max_length=4096, len_fn=None: ["chunk one", "chunk two"]
    adapter._persist_plan_artifact_for_send = lambda **_kwargs: None

    result = await adapter.send(
        "123",
        "ignored",
        reply_to="777",
        metadata={"reply_to_mode": "off"},
    )

    assert result.success
    assert [sent["reference"] for sent in channel.sent] == [None, None]
