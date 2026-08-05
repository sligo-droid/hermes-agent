from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _adapter(monkeypatch, tmp_path):
    from gateway.config import PlatformConfig
    from plugins.platforms.discord import adapter as discord_adapter

    monkeypatch.setattr(discord_adapter, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(discord_adapter, "discord", SimpleNamespace(DMChannel=type("DMChannel", (), {})))

    instance = discord_adapter.DiscordAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr(instance, "_project_summary_state_path", lambda: tmp_path / "project-summary.json")
    monkeypatch.setattr(instance, "_production_url_from_env", lambda: None)
    return instance


class FakeChannel:
    def __init__(self):
        self.id = "chan-1"
        self.name = "examine"
        self.topic = ""
        self.guild = SimpleNamespace(id="guild-1", name="Sligo Labs")
        self.edit = AsyncMock(side_effect=self._edit)

    async def _edit(self, *, topic, reason):
        self.topic = topic


@pytest.mark.asyncio
async def test_project_topic_with_pending_github_remains_retryable(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path)
    channel = FakeChannel()

    handle = await adapter.initialize_project_summary(
        channel,
        project_context={"project_mapping_resolved": False, "channel_name": "examine"},
    )

    state = adapter._read_project_summary_state()
    key = adapter._project_summary_state_key(channel)
    assert handle is not None
    assert state[key]["success"] is False
    assert state[key]["pending_github_url"] is True
    assert channel.edit.await_count == 1

    await adapter.initialize_project_summary(
        channel,
        project_context={
            "project_mapping_resolved": True,
            "project_name": "Examine",
            "project_path": "/does/not/exist",
            "project_github_url": "https://github.com/sligo-labs/examine",
        },
    )

    state = adapter._read_project_summary_state()
    assert state[key]["success"] is True
    assert state[key]["repo_url"] == "https://github.com/sligo-labs/examine"
    assert channel.edit.await_count == 2
    assert "https://github.com/sligo-labs/examine" in channel.topic


@pytest.mark.asyncio
async def test_successful_project_topic_refreshes_missing_legacy_repo_metadata(monkeypatch, tmp_path):
    adapter = _adapter(monkeypatch, tmp_path)
    channel = FakeChannel()
    key = adapter._project_summary_state_key(channel)
    adapter._write_project_summary_state(
        {
            key: {
                "channel_id": channel.id,
                "guild_id": "guild-1",
                "success": True,
            }
        }
    )
    channel.topic = "\u200b\n\npending\npending"

    await adapter.initialize_project_summary(
        channel,
        project_context={
            "project_mapping_resolved": True,
            "project_name": "Examine",
            "project_path": "/does/not/exist",
            "project_github_url": "https://github.com/sligo-labs/examine",
        },
    )

    state = adapter._read_project_summary_state()
    assert state[key]["success"] is True
    assert state[key]["repo_url"] == "https://github.com/sligo-labs/examine"
    assert channel.edit.await_count == 1
    assert channel.topic == "\u200b\n\npending\nhttps://github.com/sligo-labs/examine"
