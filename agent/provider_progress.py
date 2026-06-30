"""Gateway-scoped useful-progress signals for provider no-progress tracking."""

from __future__ import annotations

import threading
import time
from typing import Any


_LOCK = threading.Lock()
_SIGNALS: dict[str, dict[str, Any]] = {}
_MAX_SIGNALS = 2048


def record_provider_progress_signal(
    gateway_session_key: str | None,
    reason: str,
    *,
    phase: str = "gateway",
    source: str = "gateway",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Record useful progress made outside the agent's tool/content loop."""

    key = str(gateway_session_key or "").strip()
    if not key:
        return None
    signal = {
        "gateway_session_key": key,
        "reason": str(reason or phase or "external_progress"),
        "phase": str(phase or "gateway"),
        "source": str(source or "gateway"),
        "at": time.time(),
    }
    if metadata:
        signal["metadata"] = {
            str(k): v
            for k, v in metadata.items()
            if isinstance(k, str) and isinstance(v, (str, int, float, bool, type(None)))
        }
    with _LOCK:
        _SIGNALS[key] = signal
        if len(_SIGNALS) > _MAX_SIGNALS:
            oldest = sorted(_SIGNALS.items(), key=lambda item: float(item[1].get("at") or 0))
            for stale_key, _ in oldest[: len(_SIGNALS) - _MAX_SIGNALS]:
                _SIGNALS.pop(stale_key, None)
    return dict(signal)


def latest_provider_progress_signal(gateway_session_key: str | None) -> dict[str, Any] | None:
    key = str(gateway_session_key or "").strip()
    if not key:
        return None
    with _LOCK:
        signal = _SIGNALS.get(key)
        return dict(signal) if isinstance(signal, dict) else None


def clear_provider_progress_signal(gateway_session_key: str | None) -> None:
    key = str(gateway_session_key or "").strip()
    if not key:
        return
    with _LOCK:
        _SIGNALS.pop(key, None)
