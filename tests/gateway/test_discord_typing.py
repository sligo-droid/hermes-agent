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


class _BlockingHttp(_RecordingHttp):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled = False
        self.completed = 0

    async def request(self, route):
        self.channel_ids.append(str(route.channel_id))
        if len(self.channel_ids) == 1:
            self.started.set()
            try:
                await self.release.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        self.completed += 1


class _DisconnectClient:
    def __init__(self, block_close: bool = False):
        self.http = _RecordingHttp()
        self.closed = False
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        if not block_close:
            self.release_close.set()

    async def close(self):
        self.close_started.set()
        await self.release_close.wait()
        self.closed = True


async def _wait_for_event(event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), timeout=1)


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


@pytest.mark.asyncio
async def test_concurrent_send_typing_calls_register_one_loop_before_initial_heartbeat():
    config = PlatformConfig(enabled=True, token="***")
    adapter = DiscordAdapter(config)
    http = _BlockingHttp()
    adapter._client = SimpleNamespace(http=http)

    first = asyncio.create_task(
        adapter.send_typing("parent-channel", metadata={"thread_id": "thread-race"})
    )
    second = None
    try:
        await _wait_for_event(http.started)
        typing_task = adapter._typing_tasks["thread-race"]

        second = asyncio.create_task(
            adapter.send_typing("parent-channel", metadata={"thread_id": "thread-race"})
        )
        await asyncio.sleep(0)

        assert second.done() is False
        assert http.channel_ids == ["thread-race"]
        assert adapter._typing_tasks == {"thread-race": typing_task}

        http.release.set()
        await asyncio.gather(first, second)
        assert http.completed == 1
        assert adapter._typing_tasks == {"thread-race": typing_task}
    finally:
        http.release.set()
        callers = [first]
        if second:
            callers.append(second)
        await asyncio.gather(*callers, return_exceptions=True)
        await adapter.stop_typing("parent-channel")

    assert typing_task.done()
    assert adapter._typing_tasks == {}
    assert adapter._typing_aliases == {}


@pytest.mark.asyncio
async def test_cancelled_send_typing_caller_allows_immediate_retry():
    config = PlatformConfig(enabled=True, token="***")
    adapter = DiscordAdapter(config)
    http = _BlockingHttp()
    adapter._client = SimpleNamespace(http=http)

    caller = asyncio.create_task(
        adapter.send_typing("parent-channel", metadata={"thread_id": "thread-cancel"})
    )
    try:
        await _wait_for_event(http.started)
        cancelled_task = adapter._typing_tasks["thread-cancel"]
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

        assert cancelled_task.done()
        assert http.cancelled is True
        assert adapter._typing_tasks == {}

        await adapter.send_typing(
            "parent-channel", metadata={"thread_id": "thread-cancel"}
        )
        replacement = adapter._typing_tasks["thread-cancel"]

        assert http.channel_ids == ["thread-cancel", "thread-cancel"]
        assert replacement is not cancelled_task
    finally:
        await adapter.stop_typing("parent-channel")

    assert replacement.done()
    assert adapter._typing_tasks == {}
    assert adapter._typing_aliases == {}


@pytest.mark.asyncio
async def test_stop_typing_cancels_in_flight_initial_heartbeat():
    config = PlatformConfig(enabled=True, token="***")
    adapter = DiscordAdapter(config)
    http = _BlockingHttp()
    adapter._client = SimpleNamespace(http=http)

    caller = asyncio.create_task(
        adapter.send_typing("parent-channel", metadata={"thread_id": "thread-stop"})
    )
    try:
        await _wait_for_event(http.started)
        await adapter.stop_typing("parent-channel")
        await caller

        assert http.cancelled is True
        assert http.completed == 0
        assert adapter._typing_tasks == {}
        assert adapter._typing_aliases == {}
    finally:
        http.release.set()
        await asyncio.gather(caller, return_exceptions=True)
        await adapter.stop_typing("parent-channel")


@pytest.mark.asyncio
async def test_cancelled_old_typing_loop_does_not_remove_replacement(adapter):
    await adapter.send_typing(
        "parent-channel", metadata={"thread_id": "thread-replacement"}
    )
    await asyncio.sleep(0)
    original = adapter._typing_tasks["thread-replacement"]
    replacement_ready = asyncio.get_running_loop().create_future()
    replacement_ready.set_result(None)
    replacement = asyncio.create_task(asyncio.Event().wait())
    adapter._typing_tasks["thread-replacement"] = replacement
    adapter._typing_ready["thread-replacement"] = replacement_ready

    try:
        original.cancel()
        await original

        assert adapter._typing_tasks["thread-replacement"] is replacement
        assert adapter._typing_ready["thread-replacement"] is replacement_ready
        assert adapter._typing_aliases == {
            "parent-channel": {"thread-replacement"}
        }
    finally:
        await adapter.stop_typing("parent-channel")

    assert replacement.done()
    assert adapter._typing_tasks == {}
    assert adapter._typing_aliases == {}


@pytest.mark.asyncio
async def test_disconnect_cancels_and_clears_typing_tasks():
    config = PlatformConfig(enabled=True, token="***")
    adapter = DiscordAdapter(config)
    client = _DisconnectClient()
    adapter._client = client

    await adapter.send_typing(
        "parent-channel", metadata={"thread_id": "thread-disconnect"}
    )
    await adapter.send_typing("direct-channel")
    typing_tasks = set(adapter._typing_tasks.values())

    await adapter.disconnect()

    assert all(task.done() for task in typing_tasks)
    assert adapter._typing_tasks == {}
    assert adapter._typing_ready == {}
    assert adapter._typing_aliases == {}
    assert client.closed is True
    assert adapter._client is None


@pytest.mark.asyncio
async def test_disconnect_rejects_new_typing_tasks_during_teardown():
    config = PlatformConfig(enabled=True, token="***")
    adapter = DiscordAdapter(config)
    client = _DisconnectClient(block_close=True)
    adapter._client = client

    disconnect = asyncio.create_task(adapter.disconnect())
    try:
        await _wait_for_event(client.close_started)
        await adapter.send_typing("late-channel")

        assert adapter._client is None
        assert adapter._typing_tasks == {}
        assert adapter._typing_ready == {}
        assert adapter._typing_aliases == {}
    finally:
        client.release_close.set()
        await disconnect

    assert client.closed is True
