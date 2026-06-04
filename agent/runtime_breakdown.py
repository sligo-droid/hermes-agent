"""Defensive runtime-breakdown helpers for public summaries."""

from __future__ import annotations

import math
from typing import Any


def _seconds(value: Any) -> float:
    try:
        seconds = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(seconds) or seconds < 0:
        return 0.0
    return seconds


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _duration_text(seconds: Any) -> str:
    total = int(round(_seconds(seconds)))
    if total < 60:
        return f"{total}s"
    minutes, sec = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def _clean_name(value: Any, *, limit: int = 32) -> str:
    text = str(value or "").strip()
    if not text:
        return "unknown"
    safe = []
    for ch in text:
        if ch.isalnum() or ch in {"_", "-", " ", "."}:
            safe.append(ch)
    text = "".join(safe).strip(" .") or "unknown"
    return text[:limit]


def build_turn_runtime_breakdown(
    stats: dict[str, Any] | None,
    total_elapsed_s: Any = None,
    scope: str = "agent_turn",
) -> dict[str, Any]:
    """Return a compact public-safe runtime breakdown for one agent turn."""
    stats = stats if isinstance(stats, dict) else {}
    wall = _seconds(total_elapsed_s)
    if wall <= 0:
        started = _seconds(stats.get("started_at"))
        ended = _seconds(stats.get("ended_at"))
        if started and ended >= started:
            wall = ended - started
    model = _seconds(stats.get("api_duration_s"))
    tools = _seconds(stats.get("tool_duration_s"))
    active = model + tools
    overhead = max(0.0, wall - active) if wall else 0.0
    if wall and active > wall:
        overhead = 0.0

    tool_rows = []
    tools_data = stats.get("tools") if isinstance(stats.get("tools"), dict) else {}
    for name, item in tools_data.items():
        if not isinstance(item, dict):
            continue
        duration = _seconds(item.get("duration_s"))
        if duration <= 0 and not item.get("count"):
            continue
        tool_rows.append(
            {
                "name": _clean_name(name),
                "duration_s": duration,
                "count": _count(item.get("count")),
                "errors": _count(item.get("errors")),
                "blocked": _count(item.get("blocked")),
            }
        )
    tool_rows.sort(key=lambda item: item["duration_s"], reverse=True)

    return {
        "schema_version": 1,
        "scope": _clean_name(scope, limit=48),
        "wall_s": wall,
        "model_s": model,
        "tools_s": tools,
        "overhead_s": overhead,
        "active_s": active,
        "api_calls": _count(stats.get("api_calls")),
        "tool_calls": _count(stats.get("tool_calls")),
        "tool_errors": _count(stats.get("tool_errors")),
        "tool_blocked": _count(stats.get("tool_blocked")),
        "tokens": {
            "input": _count(stats.get("input_tokens")),
            "output": _count(stats.get("output_tokens")),
            "total": _count(stats.get("total_tokens")),
            "cache_read": _count(stats.get("cache_read_tokens")),
            "cache_write": _count(stats.get("cache_write_tokens")),
            "reasoning": _count(stats.get("reasoning_tokens")),
        },
        "top_tools": tool_rows[:5],
        "active_exceeds_wall": bool(wall and active > wall + 0.25),
    }


def merge_runtime_breakdowns(
    breakdowns: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    scope: str = "goal",
) -> dict[str, Any]:
    """Accumulate turn breakdowns without carrying sensitive raw details."""
    phases: dict[str, dict[str, Any]] = {}
    merged: dict[str, Any] = {
        "schema_version": 1,
        "scope": _clean_name(scope, limit=48),
        "wall_s": 0.0,
        "model_s": 0.0,
        "tools_s": 0.0,
        "overhead_s": 0.0,
        "active_s": 0.0,
        "api_calls": 0,
        "tool_calls": 0,
        "tool_errors": 0,
        "tool_blocked": 0,
        "top_tools": [],
        "phases": [],
        "active_exceeds_wall": False,
    }
    tools: dict[str, dict[str, Any]] = {}
    for item in breakdowns or []:
        if not isinstance(item, dict):
            continue
        for key in ("wall_s", "model_s", "tools_s", "overhead_s", "active_s"):
            merged[key] += _seconds(item.get(key))
        for key in ("api_calls", "tool_calls", "tool_errors", "tool_blocked"):
            merged[key] += _count(item.get(key))
        merged["active_exceeds_wall"] = bool(merged["active_exceeds_wall"] or item.get("active_exceeds_wall"))
        for phase in item.get("phases") or []:
            if not isinstance(phase, dict):
                continue
            name = _clean_name(phase.get("name"), limit=16)
            slot = phases.setdefault(name, {"name": name, "duration_s": 0.0, "count": 0})
            slot["duration_s"] += _seconds(phase.get("duration_s"))
            slot["count"] += _count(phase.get("count"))
        for tool in item.get("top_tools") or []:
            if not isinstance(tool, dict):
                continue
            name = _clean_name(tool.get("name"))
            slot = tools.setdefault(name, {"name": name, "duration_s": 0.0, "count": 0, "errors": 0, "blocked": 0})
            slot["duration_s"] += _seconds(tool.get("duration_s"))
            slot["count"] += _count(tool.get("count"))
            slot["errors"] += _count(tool.get("errors"))
            slot["blocked"] += _count(tool.get("blocked"))
    merged["top_tools"] = sorted(tools.values(), key=lambda item: item["duration_s"], reverse=True)[:5]
    merged["phases"] = sorted(phases.values(), key=lambda item: item["duration_s"], reverse=True)[:6]
    return merged


def render_runtime_breakdown_text(breakdown: dict[str, Any] | None, compact: bool = True) -> str:
    """Render a Discord embed field value, capped to Discord's 1024 chars."""
    if not isinstance(breakdown, dict):
        return ""
    wall = _seconds(breakdown.get("wall_s") or breakdown.get("duration_seconds"))
    model = _seconds(breakdown.get("model_s"))
    tools = _seconds(breakdown.get("tools_s"))
    overhead = _seconds(breakdown.get("overhead_s"))
    phases = [p for p in (breakdown.get("phases") or []) if isinstance(p, dict)]
    lines: list[str] = []
    if compact:
        first = []
        if wall:
            first.append(f"{_duration_text(wall)} wall")
        if model:
            first.append(f"model {_duration_text(model)}")
        if tools:
            first.append(f"tools {_duration_text(tools)}")
        if overhead:
            first.append(f"overhead {_duration_text(overhead)}")
        if first:
            lines.append(" · ".join(first))
        elif phases:
            lines.append(f"{_duration_text(sum(_seconds(p.get('duration_s')) for p in phases))} active time")

        top_tools = [t for t in (breakdown.get("top_tools") or []) if isinstance(t, dict) and _seconds(t.get("duration_s")) > 0]
        if top_tools:
            rendered = " · ".join(f"{_clean_name(t.get('name'))} {_duration_text(t.get('duration_s'))}" for t in top_tools[:3])
            if rendered:
                lines.append(f"Top: {rendered}")
        if phases:
            rendered_phases = " · ".join(
                f"{_clean_name(p.get('name'), limit=16)} {_duration_text(p.get('duration_s'))}"
                for p in phases[:4]
                if _seconds(p.get("duration_s")) > 0
            )
            if rendered_phases:
                lines.append(f"Phases: {rendered_phases}")
    else:
        total_for_pct = wall or max(model + tools + overhead, sum(_seconds(p.get("duration_s")) for p in phases), 1.0)
        if wall:
            lines.append(f"{_duration_text(wall)} wall")
        rows = phases or [
            {"name": "Model", "duration_s": model, "count": _count(breakdown.get("api_calls"))},
            {"name": "Tools", "duration_s": tools, "count": _count(breakdown.get("tool_calls"))},
            {"name": "Overhead", "duration_s": overhead, "count": 0},
        ]
        for row in rows[:6]:
            duration = _seconds(row.get("duration_s"))
            if duration <= 0:
                continue
            pct = min(100, int(round((duration / total_for_pct) * 100))) if total_for_pct else 0
            filled = min(8, max(0, int(round(pct / 12.5))))
            bar = "█" * filled + "░" * (8 - filled)
            count = _count(row.get("count"))
            suffix = f" {count} calls" if count else ""
            lines.append(f"{_clean_name(row.get('name'), limit=16)} {bar} {_duration_text(duration)} {pct}%{suffix}")
    if breakdown.get("active_exceeds_wall"):
        lines.append("Active phase time may overlap due to concurrency.")
    text = "\n".join(line for line in lines if line).strip()
    if len(text) > 1024:
        text = text[:1021].rstrip() + "..."
    return text
