"""Bound the durable work-ledger payload without weakening live recovery.

The work ledger keeps rich records while work may still need recovery or
closeout.  Once a terminal record is fully quiescent, only a small durable
tombstone is needed to suppress duplicate Discord delivery.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


DEFAULT_LEDGER_TARGET_BYTES = 8 * 1024 * 1024
DEFAULT_LEDGER_HARD_BYTES = 12 * 1024 * 1024
DEFAULT_FULL_RECORD_SECONDS = 24 * 60 * 60
DEFAULT_EMERGENCY_RECORD_SECONDS = 2 * 60 * 60
DEFAULT_TOMBSTONE_SECONDS = 30 * 24 * 60 * 60

_COMPACTABLE_STATUSES = frozenset({"completed", "failed", "cancelled", "expired"})
_TERMINAL_CLOSEOUT_STATUSES = frozenset({"completed", "post_merge_complete"})
_TERMINAL_DELIVERY_STATUSES = frozenset({"completed", "uncertain", "unreachable"})
_PENDING_FLAGS = (
    "discord_thread_reaction_sync_pending",
    "terminal_reaction_sync_pending",
    "terminal_summary_sync_pending",
)


@dataclass(frozen=True)
class LedgerBudgetResult:
    before_bytes: int
    after_bytes: int
    tombstoned: int
    expired_tombstones: int
    over_hard_budget: bool


def compact_json_size(data: dict[str, Any]) -> int:
    """Return the exact UTF-8 size of the ledger's compact JSON form."""

    payload = json.dumps(
        data,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return len(payload.encode("utf-8"))


def _compact_value_size(value: Any) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def is_quiescent_terminal_item(item: Any, *, now: float) -> bool:
    """Return whether a rich terminal row can safely become a tombstone."""

    if not isinstance(item, dict) or item.get("tombstone") is True:
        return False
    if str(item.get("status") or "") not in _COMPACTABLE_STATUSES:
        return False
    if isinstance(item.get("active_run"), dict):
        return False
    lease_until = float(item.get("lease_until") or 0)
    if lease_until > now:
        return False
    if any(item.get(flag) is True for flag in _PENDING_FLAGS):
        return False

    closeout = item.get("closeout")
    if isinstance(closeout, dict):
        closeout_status = str(closeout.get("status") or "")
        if closeout_status not in _TERMINAL_CLOSEOUT_STATUSES:
            return False

    delivery = item.get("terminal_delivery")
    if isinstance(delivery, dict):
        if str(delivery.get("status") or "") not in _TERMINAL_DELIVERY_STATUSES:
            return False
        if delivery.get("summary_updated_at") is None:
            return False
    return True


def terminal_tombstone(item: dict[str, Any], *, now: float) -> dict[str, Any]:
    """Reduce a quiescent terminal row to duplicate-suppression metadata."""

    terminal_at = float(
        item.get("completed_at")
        or item.get("updated_at")
        or item.get("created_at")
        or now
    )
    return {
        "id": str(item.get("id") or ""),
        "status": str(item.get("status") or "completed"),
        "platform": str(item.get("platform") or "discord"),
        "message_id": item.get("message_id"),
        "created_at": item.get("created_at"),
        "updated_at": terminal_at,
        "terminal_at": terminal_at,
        "tombstone": True,
    }


def enforce_ledger_budget(
    data: dict[str, Any],
    *,
    now: float,
    target_bytes: int = DEFAULT_LEDGER_TARGET_BYTES,
    hard_bytes: int = DEFAULT_LEDGER_HARD_BYTES,
    full_record_seconds: float = DEFAULT_FULL_RECORD_SECONDS,
    emergency_record_seconds: float = DEFAULT_EMERGENCY_RECORD_SECONDS,
    tombstone_seconds: float = DEFAULT_TOMBSTONE_SECONDS,
) -> LedgerBudgetResult:
    """Mutate ``data`` to keep rich history bounded while retaining dedupe.

    Normal compaction targets old quiescent records.  If the ledger crosses
    the hard budget, quiescent records older than the recovery freshness
    window are also eligible.  Live or pending records always win over the
    byte budget.
    """

    items = data.get("items")
    if not isinstance(items, dict):
        data["items"] = {}
        items = data["items"]

    before_bytes = compact_json_size(data)
    expired_tombstones = 0
    for work_id, item in list(items.items()):
        if not isinstance(item, dict) or item.get("tombstone") is not True:
            continue
        terminal_at = float(item.get("terminal_at") or item.get("updated_at") or 0)
        if terminal_at and now - terminal_at > max(1.0, tombstone_seconds):
            del items[work_id]
            expired_tombstones += 1

    candidates = sorted(
        (
            (float(item.get("updated_at") or item.get("created_at") or 0), work_id, item)
            for work_id, item in items.items()
            if is_quiescent_terminal_item(item, now=now)
        ),
        key=lambda entry: (entry[0], entry[1]),
    )

    tombstoned = 0
    current_bytes = compact_json_size(data)
    for terminal_at, work_id, item in candidates:
        age = now - terminal_at
        normal_eligible = (
            current_bytes > max(1, target_bytes)
            and age >= max(1.0, full_record_seconds)
        )
        emergency_eligible = (
            current_bytes > max(target_bytes, hard_bytes)
            and age >= max(1.0, emergency_record_seconds)
        )
        if not normal_eligible and not emergency_eligible:
            continue
        tombstone = terminal_tombstone(item, now=now)
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
        expired_tombstones=expired_tombstones,
        over_hard_budget=after_bytes > max(1, hard_bytes),
    )
