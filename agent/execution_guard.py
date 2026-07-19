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


class RenewableExecutionGuard:
    """Thread-safe cancellation token backed by a renewable lease deadline."""

    def __init__(self, lease_seconds: float) -> None:
        self._lock = threading.Lock()
        self._cancelled = threading.Event()
        self._reason = ""
        self._lease_deadline = time.monotonic() + max(0.001, float(lease_seconds))

    def renew(self, lease_seconds: float) -> bool:
        """Advance the lease deadline unless ownership was already lost."""

        with self._lock:
            if self._cancelled.is_set() or time.monotonic() >= self._lease_deadline:
                self._cancelled.set()
                if not self._reason:
                    self._reason = "execution lease expired"
                return False
            self._lease_deadline = time.monotonic() + max(
                0.001,
                float(lease_seconds),
            )
            return True

    def cancel(self, reason: str = "execution cancelled") -> None:
        with self._lock:
            if not self._reason:
                self._reason = str(reason or "execution cancelled")[:160]
            self._cancelled.set()

    def remaining(self) -> float:
        with self._lock:
            remaining = self._lease_deadline - time.monotonic()
            expired = remaining <= 0
            if expired and not self._reason:
                self._reason = "execution lease expired"
        if expired:
            self._cancelled.set()
        return max(0.0, remaining)

    def mutation_allowed(self) -> bool:
        return not self._cancelled.is_set() and self.remaining() > 0

    def cancelled(self) -> bool:
        return not self.mutation_allowed()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason or "execution lease expired"

    def check(self) -> None:
        if not self.mutation_allowed():
            raise ExecutionGuardExpired(self.reason)


__all__ = [
    "CooperativeExecutionGuard",
    "ExecutionGuardExpired",
    "RenewableExecutionGuard",
]
