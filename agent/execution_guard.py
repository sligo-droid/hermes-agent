"""Cooperative deadline and cancellation checks for sync/async boundaries."""

from __future__ import annotations

import threading
import time


class ExecutionGuardExpired(TimeoutError):
    """Raised before work begins after its caller deadline or cancellation."""


class CooperativeExecutionGuard:
    """Thread-safe cancellation token coupled to one monotonic deadline."""

    def __init__(self, deadline: float) -> None:
        self.deadline = float(deadline)
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())

    def check(self) -> None:
        if self._cancelled.is_set() or self.remaining() <= 0:
            self._cancelled.set()
            raise ExecutionGuardExpired("execution deadline exhausted")

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set() or self.remaining() <= 0


__all__ = ["CooperativeExecutionGuard", "ExecutionGuardExpired"]
