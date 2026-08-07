"""Apply a hard size budget to quiescent gateway work-ledger history."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping


DEFAULT_LEDGER_TARGET_BYTES = 8 * 1024 * 1024
DEFAULT_LEDGER_HARD_BYTES = 12 * 1024 * 1024
DEFAULT_EMERGENCY_RECORD_SECONDS = 2 * 60 * 60


@dataclass(frozen=True)
class LedgerBudgetResult:
    before_bytes: int
    after_bytes: int
    tombstoned: int
    over_hard_budget: bool


def compact_json_size(data: Mapping[str, Any]) -> int:
    """Return the exact UTF-8 size of the ledger's compact JSON form."""

    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return len(payload.encode("utf-8"))


def _compact_value_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def enforce_ledger_budget(
    data: dict[str, Any],
    *,
    now: float,
    is_quiescent: Callable[[Mapping[str, Any]], bool],
    make_tombstone: Callable[[Mapping[str, Any]], dict[str, Any]],
    target_bytes: int = DEFAULT_LEDGER_TARGET_BYTES,
    hard_bytes: int = DEFAULT_LEDGER_HARD_BYTES,
    emergency_record_seconds: float = DEFAULT_EMERGENCY_RECORD_SECONDS,
) -> LedgerBudgetResult:
    """Tombstone oldest safe rows until compact JSON reaches its target.

    The owning ledger defines quiescence and tombstone compatibility.  This
    module owns only the byte-budget policy, so lifecycle safety has one source
    of truth.
    """

    items = data.get("items")
    if not isinstance(items, dict):
        data["items"] = {}
        items = data["items"]

    before_bytes = compact_json_size(data)
    current_bytes = before_bytes
    if current_bytes <= max(1, hard_bytes):
        return LedgerBudgetResult(
            before_bytes=before_bytes,
            after_bytes=before_bytes,
            tombstoned=0,
            over_hard_budget=False,
        )

    candidates = sorted(
        (
            (float(item.get("updated_at") or item.get("created_at") or 0), work_id, item)
            for work_id, item in items.items()
            if isinstance(item, dict)
            and item.get("tombstone") is not True
            and is_quiescent(item)
        ),
        key=lambda entry: (entry[0], entry[1]),
    )

    tombstoned = 0
    for terminal_at, work_id, item in candidates:
        if now - terminal_at < max(1.0, emergency_record_seconds):
            continue
        tombstone = make_tombstone(item)
        items[work_id] = tombstone
        tombstoned += 1
        current_bytes -= max(0, _compact_value_size(item) - _compact_value_size(tombstone))
        if current_bytes <= max(1, target_bytes):
            break

    after_bytes = compact_json_size(data)
    return LedgerBudgetResult(
        before_bytes=before_bytes,
        after_bytes=after_bytes,
        tombstoned=tombstoned,
        over_hard_budget=after_bytes > max(1, hard_bytes),
    )
