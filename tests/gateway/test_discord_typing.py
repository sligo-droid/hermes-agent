import asyncio
from types import SimpleNamespace

import pytest

from gateway.config import PlatformConfig
import plugins.platforms.discord.adapter as discord_adapter_module
from plugins.platforms.discord.adapter import DiscordAdapter


class _RecordingHttp:
    def __init__(self, fail_once: bool = False):
        self.channel_ids: list[str] = []
        self._fail_once = fail_once

    async def request(self, route):
        self.channel_ids.append(str(route.channel_id))
        if self._fail_once:
            self._fail_once = False
            raise RuntimeError("temporary typing failure")


@pytest.fixture
def adapter():
    config = PlatformConfig(enabled=True, token="***")
    a = DiscordAdapter(config)
    a._client = SimpleNamespace(http=_RecordingHttp())
    return a


@pytest.fixture(autouse=True)
def route_factory(monkeypatch):
    monkeypatch.setattr(
        discord_adapter_module.discord.http,
        "Route",
        lambda method, path, **kwargs: SimpleNamespace(
            method=method,
            path=path,
            channel_id=kwargs.get("channel_id"),
        ),
    )


@pytest.mark.asyncio
async def test_send_typing_keeps_thread_indicator_alive_until_stopped(adapter, monkeypatch):
    sleep_delays: list[float] = []
    loop = asyncio.get_running_loop()

    async def fake_sleep(delay):
        sleep_delays.append(delay)
        await loop.run_in_executor(None, lambda: None)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await adapter.send_typing("parent-channel", metadata={"thread_id": "thread-123"})
    await loop.run_in_executor(None, lambda: None)
    await loop.run_in_executor(None, lambda: None)

    await adapter.stop_typing("parent-channel")

    assert adapter._client.http.channel_ids[:2] == ["thread-123", "thread-123"]
    assert sleep_delays
    assert all(delay < 8 for delay in sleep_delays)
    assert "thread-123" not in adapter._typing_tasks


@pytest.mark.asyncio
async def test_send_typing_loop_survives_transient_heartbeat_error(monkeypatch):
    config = PlatformConfig(enabled=True, token="***")
    adapter = DiscordAdapter(config)
    adapter._client = SimpleNamespace(http=_RecordingHttp(fail_once=True))
    sleep_count = 0
    loop = asyncio.get_running_loop()

    async def fake_sleep(delay):
        nonlocal sleep_count
        sleep_count += 1
        await loop.run_in_executor(None, lambda: None)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    await adapter.send_typing("parent-channel", metadata={"thread_id": "thread-456"})
    await loop.run_in_executor(None, lambda: None)
    await loop.run_in_executor(None, lambda: None)

    await adapter.stop_typing("parent-channel")

    assert adapter._client.http.channel_ids[:2] == ["thread-456", "thread-456"]
    assert sleep_count > 0
