"""Shared per-turn worker attribution helpers."""

from __future__ import annotations

import threading
from typing import Any


TURN_WORKER_RUNS_LOCK = threading.Lock()


def append_turn_worker_run(agent: Any, record: dict[str, Any]) -> dict[str, Any] | None:
    """Append one footer-safe worker record under the shared turn lock."""

    if agent is None or not isinstance(record, dict):
        return None
    with TURN_WORKER_RUNS_LOCK:
        runs = getattr(agent, "turn_worker_runs", None)
        if not isinstance(runs, list):
            runs = []
            agent.turn_worker_runs = runs
        runs.append(record)
    return record
