"""Codex/OpenCode worker state sidecars for Discord boards."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional

from hermes_cli import kanban_db
from utils import atomic_json_write

CODEX_STATE_MAX_EVENTS = 200
CODEX_STATE_MAX_TOOL_TRACE = 80
CODEX_STATE_MAX_TEXT_BYTES = 24_000
CODEX_STATE_MAX_TRACE_TEXT_BYTES = 2_000
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
    trace = current.get("tool_trace") if isinstance(current.get("tool_trace"), list) else []
    trace_item = _tool_trace_from_event(event, ts=_now())
    if trace_item:
        trace.append(trace_item)
        if len(trace) > CODEX_STATE_MAX_TOOL_TRACE:
            trace = trace[-CODEX_STATE_MAX_TOOL_TRACE:]
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
        update={"events": events, "truncated_events": truncated, "tool_trace": trace},
    )


def _short_text(value: Any) -> str:
    return str(cap_state_value(str(value or ""), max_text=CODEX_STATE_MAX_TRACE_TEXT_BYTES) or "")


def _tool_trace_from_event(event: dict[str, Any], *, ts: int) -> Optional[dict[str, Any]]:
    if not isinstance(event, dict):
        return None
    method = str(event.get("method") or "")
    params = event.get("params") if isinstance(event.get("params"), dict) else {}
    item = params.get("item") if isinstance(params.get("item"), dict) else {}
    if method.startswith("opencode/"):
        return _opencode_tool_trace(method, item, ts=ts)
    return _codex_tool_trace(method, item, params, ts=ts)


def _codex_tool_trace(method: str, item: dict[str, Any], params: dict[str, Any], *, ts: int) -> Optional[dict[str, Any]]:
    if method != "item/completed":
        return None
    item_type = str(item.get("type") or "")
    if item_type == "commandExecution":
        out = item.get("aggregatedOutput") or item.get("output") or item.get("stderr") or item.get("stdout")
        entry = {
            "ts": ts,
            "source": "codex",
            "tool": "commandExecution",
            "command": _short_text(item.get("command") or item.get("cmd")),
            "status": _short_text(item.get("status") or ("completed" if method.endswith("completed") else "started")),
        }
        if item.get("exitCode") is not None:
            entry["exit_code"] = item.get("exitCode")
        if item.get("durationMs") is not None:
            entry["duration_ms"] = item.get("durationMs")
        if out:
            entry["output"] = _short_text(out)
        return entry
    tool_name = item.get("tool") or item.get("name") or item.get("toolName")
    if item_type and ("tool" in item_type.lower() or tool_name):
        entry = {
            "ts": ts,
            "source": "codex",
            "tool": _short_text(tool_name or item_type),
            "status": _short_text(item.get("status") or ("completed" if method.endswith("completed") else "started")),
        }
        if item.get("command") or item.get("summary"):
            entry["summary"] = _short_text(item.get("command") or item.get("summary"))
        if item.get("output") or item.get("error"):
            entry["output"] = _short_text(item.get("output") or item.get("error"))
        return entry
    return None


def _opencode_tool_trace(method: str, item: dict[str, Any], *, ts: int) -> Optional[dict[str, Any]]:
    event_type = str(item.get("type") or method.removeprefix("opencode/"))
    lower_type = event_type.lower()
    name = item.get("tool") or item.get("name") or item.get("tool_name")
    if not name and ("tool_use" in lower_type or lower_type in {"bash", "tool"}):
        name = item.get("command") and "bash" or event_type
    if not name:
        return None
    entry = {
        "ts": ts,
        "source": "opencode",
        "tool": _short_text(name),
        "status": _short_text(item.get("status") or item.get("state") or ("completed" if "complete" in lower_type else "started")),
    }
    command = item.get("command") or item.get("cmd")
    if isinstance(item.get("input"), dict):
        command = command or item["input"].get("command") or item["input"].get("cmd")
    if command:
        entry["command"] = _short_text(command)
    output = item.get("output") or item.get("result") or item.get("error")
    if output:
        entry["output"] = _short_text(output)
    return entry


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
        "ui_work_route": getattr(result, "ui_work_route", None),
    }
    write_codex_worker_state(
        task_id,
        board=board,
        update={"result": cap_state_value(payload)},
    )
