"""Codex/OpenCode worker state sidecars for Discord boards."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db
from utils import atomic_json_write

CODEX_STATE_MAX_EVENTS = 200
CODEX_STATE_MAX_TEXT_BYTES = 24_000
CODEX_STATE_LOG_TAIL_BYTES = 64_000


def _now() -> int:
    return int(time.time())


def codex_worker_state_path(task_id: str, *, board: Optional[str] = None) -> Path:
    """Return the per-ticket Codex app-server state sidecar path."""
    log_path = kanban_db.worker_log_path(str(task_id or ""), board=board)
    return log_path.with_name(f"{log_path.stem}.codex-state.json")


def read_codex_worker_state(task_id: str, *, board: Optional[str] = None) -> dict[str, Any]:
    path = codex_worker_state_path(task_id, board=board)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"error": "codex state sidecar could not be read"}
    return data if isinstance(data, dict) else {}


def cap_state_value(value: Any, *, max_text: int = CODEX_STATE_MAX_TEXT_BYTES) -> Any:
    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="replace")
        if len(encoded) <= max_text:
            return value
        clipped = encoded[:max_text].decode("utf-8", errors="replace")
        return f"{clipped}\n...[truncated {len(encoded) - max_text} bytes]"
    if isinstance(value, list):
        return [cap_state_value(item, max_text=max_text) for item in value[:80]]
    if isinstance(value, dict):
        return {
            str(key): cap_state_value(item, max_text=max_text)
            for key, item in list(value.items())[:80]
        }
    return value


def write_codex_worker_state(
    task_id: str,
    *,
    board: Optional[str],
    update: dict[str, Any],
) -> dict[str, Any]:
    path = codex_worker_state_path(task_id, board=board)
    path.parent.mkdir(parents=True, exist_ok=True)
    current = read_codex_worker_state(task_id, board=board)
    current.update(update)
    current["task_id"] = str(task_id)
    current["board"] = str(board or "")
    current["updated_at"] = _now()
    atomic_json_write(path, current, indent=2)
    return current


def record_codex_worker_event(
    task_id: str,
    *,
    board: Optional[str],
    event: dict[str, Any],
) -> None:
    """Append one raw Codex app-server notification to a bounded sidecar."""
    current = read_codex_worker_state(task_id, board=board)
    events = current.get("events") if isinstance(current.get("events"), list) else []
    item = ((event.get("params") or {}).get("item") or {}) if isinstance(event, dict) else {}
    events.append(
        {
            "ts": _now(),
            "method": str(event.get("method") or "") if isinstance(event, dict) else "",
            "item_type": str(item.get("type") or "") if isinstance(item, dict) else "",
            "payload": cap_state_value(event),
        }
    )
    truncated = int(current.get("truncated_events") or 0)
    if len(events) > CODEX_STATE_MAX_EVENTS:
        truncated += len(events) - CODEX_STATE_MAX_EVENTS
        events = events[-CODEX_STATE_MAX_EVENTS:]
    write_codex_worker_state(
        task_id,
        board=board,
        update={"events": events, "truncated_events": truncated},
    )


def record_codex_worker_result(
    task_id: str,
    *,
    board: Optional[str],
    result: Any,
) -> None:
    payload = {
        "backend": getattr(result, "backend", "codex"),
        "final_text": getattr(result, "final_text", ""),
        "error": getattr(result, "error", None),
        "interrupted": bool(getattr(result, "interrupted", False)),
        "timed_out": bool(getattr(result, "timed_out", False)),
        "should_retire": bool(getattr(result, "should_retire", False)),
        "tool_iterations": int(getattr(result, "tool_iterations", 0) or 0),
        "turn_id": getattr(result, "turn_id", None),
        "thread_id": getattr(result, "thread_id", None),
        "agents": getattr(result, "agents", []),
        "plan_text": getattr(result, "plan_text", ""),
        "exit_code": getattr(result, "exit_code", None),
        "duration_seconds": getattr(result, "duration_seconds", None),
        "run_profile": getattr(result, "run_profile", {}),
        "service_tier": getattr(result, "service_tier", None),
        "fast_mode": getattr(result, "fast_mode", None),
    }
    write_codex_worker_state(
        task_id,
        board=board,
        update={"result": cap_state_value(payload)},
    )
