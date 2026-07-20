from __future__ import annotations

import asyncio
import gc

import pytest

from gateway.run import GatewayRunner


def _runner() -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._background_tasks = set()
    runner._owned_runner_tasks = {}
    return runner


@pytest.mark.asyncio
async def test_owned_runner_is_retained_until_explicit_shutdown():
    runner = _runner()
    started = asyncio.Event()
    release = asyncio.Event()

    async def watcher():
        started.set()
        await release.wait()

    task = runner._start_owned_runner("required-async-reconciler", watcher)
    await started.wait()
    gc.collect()

    assert task is runner._owned_runner_tasks["required-async-reconciler"]
    assert task in runner._background_tasks
    assert not task.done()

    runner._running = False
    release.set()
    await runner._cancel_owned_runners()


@pytest.mark.asyncio
async def test_critical_owned_runner_restarts_after_exception():
    runner = _runner()
    restarted = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def watcher():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("watcher crashed")
        restarted.set()
        await release.wait()

    runner._start_owned_runner(
        "required-async-reconciler",
        watcher,
        restart_on_exit=True,
    )
    await asyncio.wait_for(restarted.wait(), timeout=2)

    assert calls == 2
    runner._running = False
    release.set()
    await runner._cancel_owned_runners()


@pytest.mark.asyncio
@pytest.mark.parametrize("first_exit", ["return", "raise"])
async def test_trusted_closeout_watcher_restarts_after_internal_exit(first_exit):
    runner = _runner()
    restarted = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class Watcher:
        async def run_forever(self, _shutdown_event):
            nonlocal calls
            calls += 1
            if calls == 1:
                if first_exit == "raise":
                    raise RuntimeError("closeout watcher crashed")
                return
            restarted.set()
            await release.wait()

    runner.trusted_closeout_watcher = Watcher()
    runner._start_owned_runner(
        "trusted-closeout-watcher",
        runner._trusted_closeout_watcher,
        restart_on_exit=True,
    )
    await asyncio.wait_for(restarted.wait(), timeout=2)

    assert calls == 2
    runner._running = False
    release.set()
    await runner._cancel_owned_runners()


@pytest.mark.asyncio
async def test_owned_runner_shutdown_awaits_cancellation_without_respawn():
    runner = _runner()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls = 0

    async def watcher():
        nonlocal calls
        calls += 1
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    task = runner._start_owned_runner(
        "required-async-reconciler",
        watcher,
        restart_on_exit=True,
    )
    await started.wait()
    runner._running = False
    await runner._cancel_owned_runners()
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    await asyncio.sleep(0.2)

    assert task.done()
    assert calls == 1
    assert runner._owned_runner_tasks == {}
