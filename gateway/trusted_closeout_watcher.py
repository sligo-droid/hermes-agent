"""Gateway-owned nonblocking trusted closeout watcher."""

from __future__ import annotations

import asyncio
import inspect
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from agent.execution_guard import RenewableExecutionGuard
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
        on_preview: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
        on_terminal: Callable[[dict[str, Any]], Awaitable[None] | None] | None = None,
    ) -> None:
        raw = config if isinstance(config, dict) else {}
        self.ledger = ledger
        self.reconcile = reconcile
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
        self.max_concurrency = _bounded_int(
            raw.get("max_concurrency"),
            default=2,
            minimum=1,
            maximum=16,
        )
        self.owner = str(owner or f"gateway-closeout:{os.getpid()}:{uuid.uuid4().hex[:8]}")[:160]
        self.is_agent_active = is_agent_active or (lambda _item: False)
        self.on_preview = on_preview
        self.on_terminal = on_terminal
        self.wakeup = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._marker_path = closeout_dirty_marker_path()

    async def _notify_preview_ready(self, work_id: str) -> None:
        callback = self.on_preview
        if callback is None:
            return
        item = self.ledger.get(work_id)
        delivery = item.get("preview_delivery") if isinstance(item, dict) else None
        if not isinstance(item, dict) or not isinstance(delivery, dict):
            return
        if str(delivery.get("status") or "") != "pending":
            return
        callback_result = callback(item)
        if asyncio.iscoroutine(callback_result):
            await callback_result

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
        generation: int,
        stop: asyncio.Event,
        guard: RenewableExecutionGuard,
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
                    expected_generation=generation,
                )
            except Exception:
                guard.cancel("closeout lease renewal failed")
                return
            if not renewed or not guard.renew(self.lease_seconds):
                guard.cancel("closeout lease ownership lost")
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
            lease_generation = int(leased_state.get("lease_generation") or 0)
            guard = RenewableExecutionGuard(self.lease_seconds)
            heartbeat_stop = asyncio.Event()
            heartbeat = asyncio.create_task(
                self._renew_closeout_lease(
                    work_id,
                    leased_revision,
                    lease_generation,
                    heartbeat_stop,
                    guard,
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

            async def persist_mutation_uncertainty(value: Any) -> None:
                state_value = getattr(value, "state", value)
                if not isinstance(state_value, dict):
                    return
                uncertainty = state_value.get("mutation_uncertainty")
                if not isinstance(uncertainty, dict) or uncertainty.get("status") != "uncertain":
                    return
                persistence = asyncio.create_task(
                    asyncio.to_thread(
                        self.ledger.record_closeout_mutation_uncertainty,
                        work_id,
                        owner=self.owner,
                        expected_revision=leased_revision,
                        expected_generation=lease_generation,
                        uncertainty=uncertainty,
                    )
                )
                while not persistence.done():
                    try:
                        await asyncio.shield(persistence)
                    except asyncio.CancelledError:
                        guard.cancel("closeout watcher cancelled")
                try:
                    persistence.result()
                except BaseException:
                    pass

            try:
                reconcile_parameters = inspect.signature(self.reconcile).parameters
            except Exception:
                reconcile_parameters = {}
            reconcile_accepts_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in reconcile_parameters.values()
            )

            def fence_remote_mutation(
                operation: str,
                context: dict[str, Any],
            ) -> bool:
                fenced = self.ledger.record_closeout_mutation_start(
                    work_id,
                    owner=self.owner,
                    expected_revision=leased_revision,
                    expected_generation=lease_generation,
                    operation=operation,
                    context=context,
                )
                if not fenced:
                    guard.cancel("closeout mutation fence rejected")
                return fenced

            async def reconcile_with_guard() -> Any:
                kwargs: dict[str, Any] = {
                    "poll_seconds": self.poll_seconds,
                    "mutation_allowed": guard.mutation_allowed,
                    "mutation_started": fence_remote_mutation,
                    "control": guard,
                }
                if not reconcile_accepts_kwargs:
                    kwargs = {
                        key: value
                        for key, value in kwargs.items()
                        if key in reconcile_parameters
                    }
                worker = asyncio.create_task(
                    asyncio.to_thread(self.reconcile, leased_state, **kwargs)
                )
                try:
                    return await asyncio.shield(worker)
                except asyncio.CancelledError:
                    guard.cancel("closeout watcher cancelled")
                    while not worker.done():
                        try:
                            await asyncio.shield(worker)
                        except asyncio.CancelledError:
                            guard.cancel("closeout watcher cancelled")
                    try:
                        cancelled_result = worker.result()
                    except BaseException:
                        cancelled_result = None
                    await persist_mutation_uncertainty(cancelled_result)
                    raise

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
                        transition = await reconcile_with_guard()
                        next_state = transition.state
                        wake_immediately = bool(getattr(transition, "wake_immediately", False))
                except Exception:
                    # Release the lease without persisting exception text. The
                    # periodic fallback can retry a deterministic engine failure.
                    next_state = dict(leased_state)
                    next_state["next_due_at"] = time.time() + self.poll_seconds

                if guard.cancelled():
                    await persist_mutation_uncertainty(next_state)
                    return False
                next_enforced = str(next_state.get("mode") or "").lower() == "enforce"
                next_blocked = next_enforced and str(next_state.get("status") or "") in {
                    "blocked",
                    "repair_required",
                }
                if next_blocked:
                    stored = self.ledger.get(work_id) or leased
                    expected_run_state = self.ledger.run_state_snapshot(stored)
                    if (
                        stored.get("status") in {"claimed", "agent_running"}
                        and self.is_agent_active(stored)
                    ):
                        await stop_heartbeat()
                        if guard.cancelled():
                            await persist_mutation_uncertainty(next_state)
                            return False
                        released = await asyncio.to_thread(
                            self.ledger.release_closeout,
                            work_id,
                            owner=self.owner,
                            expected_revision=leased_revision,
                            expected_generation=lease_generation,
                            closeout_state=next_state,
                        )
                        if released is not None:
                            await self._notify_preview_ready(work_id)
                        return released is not None
                    await stop_heartbeat()
                    if guard.cancelled():
                        await persist_mutation_uncertainty(next_state)
                        return False
                    blocked = await asyncio.to_thread(
                        self.ledger.finalize_blocked_closeout,
                        work_id,
                        owner=self.owner,
                        expected_revision=leased_revision,
                        expected_generation=lease_generation,
                        closeout_state=next_state,
                        final_response=(
                            "Trusted closeout blocked: a deterministic lifecycle gate requires repair."
                        ),
                        reason="trusted_closeout_repair_required",
                        expected_run_state=expected_run_state,
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
                if guard.cancelled():
                    await persist_mutation_uncertainty(next_state)
                    return False
                if closeout_terminal_eligible(next_state):
                    stored = self.ledger.get(work_id) or leased
                    expected_run_state = self.ledger.run_state_snapshot(stored)
                    if (
                        stored.get("status") in {"claimed", "agent_running"}
                        and self.is_agent_active(stored)
                    ):
                        # The live model turn still owns delivery. Persist only the
                        # terminal closeout while its exact run state remains active.
                        released = await asyncio.to_thread(
                            self.ledger.release_closeout,
                            work_id,
                            owner=self.owner,
                            expected_revision=leased_revision,
                            expected_generation=lease_generation,
                            closeout_state=next_state,
                            expected_run_state=expected_run_state,
                        )
                        if released is not None:
                            await self._notify_preview_ready(work_id)
                        return released is not None
                    preview = next_state.get("preview") if isinstance(next_state.get("preview"), dict) else {}
                    pr = next_state.get("pr") if isinstance(next_state.get("pr"), dict) else {}
                    policy = next_state.get("policy") if isinstance(next_state.get("policy"), dict) else {}
                    visual_text = "passed" if policy.get("require_visual_qa") is True else "not required"
                    summary = (
                        "PR preview QA completed.\n\n"
                        f"- Preview: {preview.get('url') or 'not required'}\n"
                        f"- Draft PR: {pr.get('url') or 'open'}\n"
                        "- Main: untouched and unmerged\n"
                        f"- Visual QA: {visual_text}"
                    )
                    finalized = await asyncio.to_thread(
                        self.ledger.finalize_successful_closeout,
                        work_id,
                        owner=self.owner,
                        expected_revision=leased_revision,
                        expected_generation=lease_generation,
                        closeout_state=next_state,
                        final_response=summary,
                        expected_run_state=expected_run_state,
                    )
                    if finalized is None:
                        return False
                    callback = self.on_terminal
                    if callback is not None:
                        callback_result = callback(finalized)
                        if asyncio.iscoroutine(callback_result):
                            await callback_result
                    return True
                released = await asyncio.to_thread(
                    self.ledger.release_closeout,
                    work_id,
                    owner=self.owner,
                    expected_revision=leased_revision,
                    expected_generation=lease_generation,
                    closeout_state=next_state,
                )
                if released is None:
                    return False
                await self._notify_preview_ready(work_id)
                if wake_immediately:
                    self.wakeup.set()
                return True
            finally:
                guard.cancel("closeout reconciliation finished")
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
