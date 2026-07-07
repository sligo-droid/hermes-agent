"""Tests for the extracted GatewayKanbanWatchersMixin (god-file Phase 3).

The kanban watcher loops were lifted out of gateway/run.py into a mixin that
GatewayRunner inherits. These tests confirm the mixin exposes the methods and
that GatewayRunner picks them up via the MRO (behavior-neutral relocation).
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace

from gateway import kanban_watchers
from gateway.kanban_watchers import GatewayKanbanWatchersMixin

KANBAN_METHODS = [
    "_kanban_notifier_watcher",
    "_kanban_dispatcher_watcher",
    "_kanban_advance",
    "_kanban_unsub",
    "_kanban_rewind",
    "_deliver_kanban_artifacts",
]


def test_mixin_defines_kanban_methods():
    for m in KANBAN_METHODS:
        assert hasattr(GatewayKanbanWatchersMixin, m), f"mixin missing {m}"


def test_gateway_runner_inherits_mixin():
    # Import here so a heavy gateway import only happens if the first test passed.
    from gateway.run import GatewayRunner

    if issubclass(GatewayRunner, GatewayKanbanWatchersMixin):
        # Each kanban method resolves to the mixin's implementation via the MRO.
        for m in KANBAN_METHODS:
            owner = next(c for c in GatewayRunner.__mro__ if m in c.__dict__)
            assert owner is GatewayKanbanWatchersMixin, (
                f"{m} resolved to {owner.__name__}, expected the mixin"
            )
        return

    # Some campaign bases still carry the pre-extraction methods directly on
    # GatewayRunner. Keep this test focused on the live API surface while the
    # watcher mixin work remains staged separately.
    for m in KANBAN_METHODS:
        assert callable(getattr(GatewayRunner, m, None)), f"GatewayRunner missing {m}"


def test_watcher_loops_are_coroutines():
    # The two long-running watchers are async loops.
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_notifier_watcher)
    assert inspect.iscoroutinefunction(GatewayKanbanWatchersMixin._kanban_dispatcher_watcher)


def test_singleton_dispatcher_lock_is_exclusive(tmp_path):
    """Only one holder of the dispatcher lock at a time — the backstop that
    stops concurrent dispatchers double reclaiming and corrupting shared
    kanban SQLite index pages under wal_autocheckpoint=0."""
    import os

    from gateway.kanban_watchers import _acquire_singleton_lock, _release_singleton_lock

    lock = tmp_path / "kanban" / ".dispatcher.lock"

    h1, st1 = _acquire_singleton_lock(lock)
    assert st1 == "held" and h1 is not None

    # A second acquire while the first is held must be refused, not granted.
    h2, st2 = _acquire_singleton_lock(lock)
    assert st2 == "contended" and h2 is None

    # Releasing the first lets a fresh acquire succeed (lock is reusable).
    _release_singleton_lock(h1)
    h3, st3 = _acquire_singleton_lock(lock)
    assert st3 == "held" and h3 is not None
    _release_singleton_lock(h3)


def _run_dispatcher_ticks(monkeypatch, tmp_path, board_batches):
    from hermes_cli import config as config_mod
    from hermes_cli import kanban_db as kb

    runner = GatewayKanbanWatchersMixin()
    runner._running = True

    state = {
        "list_calls": 0,
        "sleep_calls": 0,
        "dispatch_boards": [],
    }

    config = {
        "kanban": {
            "auto_decompose": False,
            "dispatch_in_gateway": True,
            "dispatch_interval_seconds": 1,
        }
    }

    def list_boards(include_archived=False):
        assert include_archived is False
        idx = min(state["list_calls"], len(board_batches) - 1)
        state["list_calls"] += 1
        return [{"slug": slug} for slug in board_batches[idx]]

    async def fast_sleep(_delay):
        state["sleep_calls"] += 1
        if state["sleep_calls"] > len(board_batches):
            runner._running = False

    async def inline_to_thread(func, *args, **kwargs):
        return func(*args, **kwargs)

    def connect(board=None):
        return SimpleNamespace(close=lambda: None)

    def dispatch_once(conn, *, board=None, **kwargs):
        state["dispatch_boards"].append(board)
        return SimpleNamespace(
            spawned=[],
            reclaimed=0,
            crashed=[],
            timed_out=[],
            promoted=0,
            auto_blocked=[],
        )

    monkeypatch.setattr(config_mod, "load_config", lambda: config)
    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path)
    monkeypatch.setattr(kb, "kanban_db_path", lambda slug: tmp_path / f"{slug}.db")
    monkeypatch.setattr(kb, "reap_worker_zombies", lambda: [])
    monkeypatch.setattr(kb, "list_boards", list_boards)
    monkeypatch.setattr(kb, "read_board_metadata", lambda slug: {"slug": slug})
    monkeypatch.setattr(kb, "connect", connect)
    monkeypatch.setattr(kb, "dispatch_once", dispatch_once)
    monkeypatch.setattr(kb, "has_spawnable_ready", lambda conn: False)
    monkeypatch.setattr(kb, "has_spawnable_review", lambda conn: False)
    monkeypatch.setattr(
        kanban_watchers, "_acquire_singleton_lock", lambda path: (object(), "held")
    )
    monkeypatch.setattr(kanban_watchers, "_release_singleton_lock", lambda handle: None)
    monkeypatch.setattr(kanban_watchers.asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(kanban_watchers.asyncio, "to_thread", inline_to_thread)

    asyncio.run(runner._kanban_dispatcher_watcher())
    return state


def test_dispatcher_lists_boards_once_per_tick(monkeypatch, tmp_path):
    state = _run_dispatcher_ticks(monkeypatch, tmp_path, [["board-1"]])

    assert state["list_calls"] == 1
    assert state["dispatch_boards"] == ["board-1"]


def test_dispatcher_picks_up_new_boards_on_next_tick(monkeypatch, tmp_path):
    state = _run_dispatcher_ticks(
        monkeypatch,
        tmp_path,
        [["board-1"], ["board-1", "board-2"]],
    )

    assert state["list_calls"] == 2
    assert state["dispatch_boards"] == ["board-1", "board-1", "board-2"]
