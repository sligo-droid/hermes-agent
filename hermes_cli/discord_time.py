"""Discord snowflake timestamp helpers."""

from __future__ import annotations

import time
from typing import Any


DISCORD_EPOCH_SECONDS = 1_420_070_400.0
DISCORD_MAX_RECOVERY_AGE_SECONDS = 7 * 24 * 60 * 60


def discord_snowflake_timestamp(value: Any) -> float | None:
    """Return a Discord snowflake timestamp in epoch seconds."""
    raw = str(value or "").strip()
    if not raw.isdigit() or not (16 <= len(raw) <= 24):
        return None
    try:
        snowflake = int(raw)
    except (TypeError, ValueError):
        return None
    return ((snowflake >> 22) / 1000.0) + DISCORD_EPOCH_SECONDS


def discord_message_exceeds_age_limit(
    value: Any,
    *,
    max_age_seconds: float = DISCORD_MAX_RECOVERY_AGE_SECONDS,
    now: float | None = None,
) -> bool:
    """Return True only when a Discord message id is known to be too old."""
    timestamp = discord_snowflake_timestamp(value)
    if timestamp is None:
        return False
    current = time.time() if now is None else float(now)
    return current - timestamp > float(max_age_seconds)
