"""Shared parent-turn deadline helpers for synchronous nested workers."""

from __future__ import annotations

import math
import time
from typing import Any


def remaining_nested_worker_budget(parent_agent: Any, requested_seconds: float) -> float:
    """Cap a requested worker timeout to the parent turn's absolute deadline."""

    try:
        requested = float(requested_seconds)
    except (TypeError, ValueError, OverflowError):
        requested = 0.0
    if not math.isfinite(requested):
        requested = 0.0
    requested = max(0.0, requested)

    deadline = getattr(parent_agent, "_nested_worker_deadline_monotonic", None)
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return requested
    try:
        remaining = float(deadline) - time.monotonic()
    except (TypeError, ValueError, OverflowError):
        return requested
    if not math.isfinite(remaining):
        return requested
    return max(0.0, min(requested, remaining))
