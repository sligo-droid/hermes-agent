"""Gateway-owned nonblocking trusted closeout watcher."""

from __future__ import annotations

import asyncio
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from gateway.work_ledger import GatewayWorkLedger
from hermes_cli.trusted_closeout import (
    closeout_terminal_eligible,
    reconcile_trusted_closeout,
)
from hermes_constants import get_hermes_home
from utils import atomic_json_write


_DIRTY_MARKER_MAX_IDS = 50


def _bounded_float(value: Any, *, default: float, minimum: float, maximum: float) -> float:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return max(minimum, min(maximum, number))


def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    if value is None or isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number) or not number.is_integer():
        return default
    return max(minimum, min(maximum, int(number)))


def closeout_dirty_marker_path() -> Path:
    return get_hermes_home() / "gateway" / "closeout-dirty.json"


def mark_closeout_dirty(work_item_id: str = "") -> None:
    """Best-effort cross-process wakeup containing identifiers only."""

    path = closeout_dirty_marker_path()
    identifiers: list[str] = []
    try:
        import json

        current = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(current, dict) and isinstance(current.get("work_item_ids"), list):
            identifiers = [str(item)[:240] for item in current["work_item_ids"] if str(item).strip()]
    except Exception:
        identifiers = []
    work_id = str(work_item_id or "").strip()[:240]
    if work_id and work_id not in identifiers:
        identifiers.append(work_id)
    atomic_json_write(
        path,
        {
            "version": 1,
            "dirty_at": time.time(),
            "work_item_ids": identifiers[-_DIRTY_MARKER_MAX_IDS:],
        },
        indent=2,
        sort_keys=True,
    )


class TrustedCloseoutWatcher:
    """Reconcile due ledger closeouts without replaying their model turns."""

    def __init__(
        self,
        ledger: GatewayWorkLedger,
        *,
        config: dict[str, Any] | None = None,
        reconcile: Callable[..., Any] = reconcile_trusted_closeout,
        owner: str = "",
        is_agent_active: Callable[[dict[str, Any]], bool] | None = None,
        on_terminal: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> None:
        raw = config if isinstance(config, dict) else {}
        self.ledger = ledger
        self.reconcile = reconcile
        self.post_merge_config = dict(raw)
        self.poll_seconds = _bounded_float(
            raw.get("poll_seconds"),
            default=30.0,
            minimum=1.0,
            maximum=3600.0,
        )
        self.lease_seconds = _bounded_float(
            raw.get("lease_seconds"),
            default=120.0,
            minimum=1.0,
            maximum=3600.0,
        )
        self.green_unmerged_overdue_seconds = _bounded_float(
            raw.get("green_unmerged_overdue_seconds"),
            default=0.0,
            minimum=0.0,
            maximum=30 * 24 * 3600.0,
        )
        self.max_concurrency = _bounded_int(
            raw.get("max_concurrency"),
            default=2,
            minimum=1,
            maximum=16,
        )
        self.owner = str(owner or f"gateway-closeout:{os.getpid()}:{uuid.uuid4().hex[:8]}")[:160]
        self.is_agent_active = is_agent_active or (lambda _item: False)
        self.on_terminal = on_terminal
        self.wakeup = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._marker_path = closeout_dirty_marker_path()

    def notify(self, work_item_id: str = "", *, cross_process: bool = False) -> None:
        if cross_process:
            mark_closeout_dirty(work_item_id)
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self.wakeup.set)
        else:
            self.wakeup.set()

    def _consume_marker(self) -> bool:
        try:
            self._marker_path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError:
            return self._marker_path.exists()

    async def _renew_closeout_lease(
        self,
        work_id: str,
        revision: int,
        stop: asyncio.Event,
        ownership_lost: threading.Event,
    ) -> None:
        """Heartbeat one logical reconciliation lease without advancing its CAS."""

        interval = max(0.1, min(10.0, self.lease_seconds / 3.0))
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            try:
                renewed = await asyncio.to_thread(
                    self.ledger.renew_closeout_lease,
                    work_id,
                    owner=self.owner,
                    lease_seconds=self.lease_seconds,
                    expected_revision=revision,
                )
            except Exception:
                ownership_lost.set()
                return
            if not renewed:
                ownership_lost.set()
                return

    async def _reconcile_item(self, item: dict[str, Any], semaphore: asyncio.Semaphore) -> bool:
        async with semaphore:
            work_id = str(item.get("id") or "")
            state = item.get("closeout") if isinstance(item.get("closeout"), dict) else {}
            revision = int(state.get("revision") or 0)
            leased = await asyncio.to_thread(
                self.ledger.lease_closeout,
                work_id,
                owner=self.owner,
                lease_seconds=self.lease_seconds,
                expected_revision=revision,
            )
            if leased is None:
                return False
            leased_state = leased.get("closeout") if isinstance(leased.get("closeout"), dict) else {}
            leased_revision = int(leased_state.get("revision") or 0)
            ownership_lost = threading.Event()
            heartbeat_stop = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._renew_closeout_lease(
                    work_id,
                    leased_revision,
                    heartbeat_stop,
                    ownership_lost,
                )
            )
            heartbeat_stopped = False

            async def stop_heartbeat() -> None:
                nonlocal heartbeat_stopped
                if heartbeat_stopped:
                    return
                heartbeat_stopped = True
                heartbeat_stop.set()
                await heartbeat

            wake_immediately = False
            try:
                try:
                    leased_enforced = str(leased_state.get("mode") or "").lower() == "enforce"
                    if leased_enforced and str(leased_state.get("status") or "") in {
                        "blocked",
                        "repair_required",
                    }:
                        next_state = dict(leased_state)
                    else:
                        transition = await asyncio.to_thread(
                            self.reconcile,
                            leased_state,
                            poll_seconds=self.poll_seconds,
                            post_merge_config=self.post_merge_config,
                            green_unmerged_overdue_seconds=self.green_unmerged_overdue_seconds,
                            mutation_allowed=lambda: not ownership_lost.is_set(),
                        )
                        next_state = transition.state
                        wake_immediately = bool(getattr(transition, "wake_immediately", False))
                except Exception:
                    # Release the lease without persisting exception text. The
                    # periodic fallback can retry a deterministic engine failure.
                    next_state = dict(leased_state)
                    next_state["next_due_at"] = time.time() + self.poll_seconds

                if ownership_lost.is_set():
                    return False
                next_enforced = str(next_state.get("mode") or "").lower() == "enforce"
                next_blocked = next_enforced and str(next_state.get("status") or "") in {
                    "blocked",
                    "repair_required",
                }
                if next_blocked:
                    stored = self.ledger.get(work_id) or leased
                    if (
                        stored.get("status") in {"claimed", "agent_running"}
                        and self.is_agent_active(stored)
                    ):
                        await stop_heartbeat()
                        if ownership_lost.is_set():
                            return False
                        released = await asyncio.to_thread(
                            self.ledger.release_closeout,
                            work_id,
                            owner=self.owner,
                            expected_revision=leased_revision,
                            closeout_state=next_state,
                        )
                        return released is not None
                    await stop_heartbeat()
                    if ownership_lost.is_set():
                        return False
                    blocked = await asyncio.to_thread(
                        self.ledger.finalize_blocked_closeout,
                        work_id,
                        owner=self.owner,
                        expected_revision=leased_revision,
                        closeout_state=next_state,
                        final_response=(
                            "Trusted closeout blocked: a deterministic lifecycle gate requires repair."
                        ),
                        reason="trusted_closeout_repair_required",
                    )
                    if blocked is None:
                        return False
                    callback = self.on_terminal
                    if callback is not None:
                        callback_result = callback(blocked)
                        if asyncio.iscoroutine(callback_result):
                            await callback_result
                    return True

                await stop_heartbeat()
                if ownership_lost.is_set():
                    return False
                released = await asyncio.to_thread(
                    self.ledger.release_closeout,
                    work_id,
                    owner=self.owner,
                    expected_revision=leased_revision,
                    closeout_state=next_state,
                )
                if released is None:
                    return False
                if wake_immediately:
                    self.wakeup.set()
                if closeout_terminal_eligible(released):
                    stored = self.ledger.get(work_id) or {}
                    if (
                        stored.get("status") in {"claimed", "agent_running"}
                        and self.is_agent_active(stored)
                    ):
                        # The model/worker still owns delivery. Persist terminal
                        # closeout, but never synthesize completion over a live turn.
                        return True
                    if stored.get("status") in {"accepted", "claimed", "agent_running"}:
                        status = str(released.get("status") or "completed")
                        if status == "pr_open":
                            summary = "Trusted closeout completed: the PR is open and intentionally unmerged under the configured policy."
                        else:
                            summary = "Trusted closeout completed: the PR merge and all configured closeout gates passed."
                        await asyncio.to_thread(
                            self.ledger.mark_agent_done,
                            work_id,
                            final_response=summary,
                            summary_status="Complete",
                        )
                        stored = self.ledger.get(work_id) or stored
                    if stored.get("status") == "summary_updated":
                        await asyncio.to_thread(self.ledger.mark_completed, work_id)
                        stored = self.ledger.get(work_id) or stored
                    callback = self.on_terminal
                    if callback is not None:
                        callback_result = callback(stored)
                        if asyncio.iscoroutine(callback_result):
                            await callback_result
                return True
            finally:
                if not heartbeat_stopped:
                    ownership_lost.set()
                await stop_heartbeat()

    async def run_once(self) -> int:
        """Reconcile one bounded due batch."""

        self._loop = asyncio.get_running_loop()
        items = await asyncio.to_thread(
            self.ledger.pending_closeouts,
            limit=self.max_concurrency,
        )
        if not items:
            return 0
        semaphore = asyncio.Semaphore(self.max_concurrency)
        results = await asyncio.gather(
            *(self._reconcile_item(item, semaphore) for item in items),
            return_exceptions=False,
        )
        return sum(result is True for result in results)

    async def _wait_for_wakeup(self) -> None:
        """Wait for an event while polling the cross-process marker cheaply."""

        deadline = time.monotonic() + self.poll_seconds
        while True:
            if self.wakeup.is_set() or self._consume_marker():
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            try:
                await asyncio.wait_for(
                    self.wakeup.wait(),
                    timeout=min(0.25, remaining),
                )
                return
            except TimeoutError:
                continue

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """Wait on same-process signals, dirty markers, and periodic fallback."""

        self._loop = asyncio.get_running_loop()
        while stop_event is None or not stop_event.is_set():
            # Clear before observing either wake source. A same-process notify
            # after this point remains set, while a cross-process marker written
            # during reconciliation is consumed by the post-pass check below.
            self.wakeup.clear()
            self._consume_marker()
            await self.run_once()
            if stop_event is not None and stop_event.is_set():
                break
            marker_arrived = self._consume_marker()
            if marker_arrived or self.wakeup.is_set():
                continue
            await self._wait_for_wakeup()


__all__ = [
    "TrustedCloseoutWatcher",
    "closeout_dirty_marker_path",
    "mark_closeout_dirty",
]
