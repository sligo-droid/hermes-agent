"""Defensive runtime-breakdown helpers for public summaries."""

from __future__ import annotations

import math
from typing import Any

from agent.runtime_spans import sanitize_runtime_spans, summarize_span_intervals
from agent.visual_qa import sanitize_visual_receipt


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


def _visual_qa_summary(stats: dict[str, Any], receipts: list[dict[str, Any]]) -> dict[str, Any]:
    """Build public-safe visual QA observability from accepted receipt state."""
    level = str(stats.get("visual_qa_level") or "none").strip().lower()
    if level not in {"none", "surface", "artifact"}:
        level = "none"
    latest = None
    for receipt in receipts:
        if latest is None or _count(receipt.get("order")) >= _count(latest.get("order")):
            latest = receipt
    if level == "none":
        status = "not_applicable"
    elif latest is None:
        status = "missing"
    else:
        status = str(latest.get("status") or "missing").lower()
        if status not in {"passed", "failed", "blocked", "uncertain"}:
            status = "missing"
    # The executor increments this only when a tagged receipt is accepted.
    duration = _seconds(stats.get("visual_qa_check_duration_s")) if latest is not None else 0.0
    return {
        "level": level,
        "receipt_status": status,
        "followup_count": min(1, _count(stats.get("visual_qa_followup_count"))),
        "check_duration_s": duration,
    }


def _trusted_span_breakdown(stats: dict[str, Any]) -> dict[str, Any]:
    spans = sanitize_runtime_spans(stats.get("phase_spans"), max_spans=200)
    summary = summarize_span_intervals(spans)
    phase_rows = [
        {
            "name": name,
            "duration_s": item["union_s"],
            "summed_s": item["summed_s"],
            "overlap_s": item["overlap_s"],
            "count": item["count"],
        }
        for name, item in summary["phases"].items()
    ]
    phase_rows.sort(key=lambda item: item["duration_s"], reverse=True)
    if spans:
        span_window = max(span["ended_at"] for span in spans) - min(
            span["started_at"] for span in spans
        )
    else:
        span_window = 0.0
    return {
        "phase_spans": spans,
        "active_s": summary["union_s"],
        "summed_active_s": summary["summed_s"],
        "overlap_s": summary["overlap_s"],
        "max_concurrency": summary["peak_concurrency"],
        "span_window_s": round(max(0.0, span_window), 6),
        "phases": phase_rows[:12],
    }


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
    trusted = _trusted_span_breakdown(stats)
    has_trusted_spans = bool(trusted["phase_spans"])
    active = trusted["active_s"] if has_trusted_spans else model + tools
    summed_active = trusted["summed_active_s"] if has_trusted_spans else model + tools
    overlap = trusted["overlap_s"] if has_trusted_spans else max(0.0, summed_active - active)
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

    visual_receipts: list[dict[str, Any]] = []
    raw_receipts = stats.get("visual_qa_receipts")
    if isinstance(raw_receipts, list):
        latest: dict[str, Any] | None = None
        for raw in raw_receipts:
            receipt = sanitize_visual_receipt(raw)
            if receipt is not None and (
                latest is None
                or _count(receipt.get("order")) >= _count(latest.get("order"))
            ):
                latest = receipt
        if latest is not None:
            visual_receipts.append(latest)
    visual_qa = _visual_qa_summary(stats, visual_receipts)

    return {
        "schema_version": 2,
        "scope": _clean_name(scope, limit=48),
        "wall_s": wall,
        "model_s": model,
        "tools_s": tools,
        "overhead_s": overhead,
        "active_s": active,
        "summed_active_s": summed_active,
        "overlap_s": overlap,
        "max_concurrency": trusted["max_concurrency"] if has_trusted_spans else 1 if active else 0,
        "span_window_s": trusted["span_window_s"],
        "phase_spans": trusted["phase_spans"],
        "phases": trusted["phases"],
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
        "verification_evidence": list(stats.get("verification_evidence") or [])[:20]
        if isinstance(stats.get("verification_evidence"), list)
        else [],
        "mutation_generation": _count(stats.get("mutation_generation")),
        "mutation_boundary": _count(stats.get("mutation_boundary")),
        "visual_qa_receipts": visual_receipts,
        "visual_qa": visual_qa,
        "active_exceeds_wall": bool(wall and summed_active > wall + 0.25),
    }


def merge_runtime_breakdowns(
    breakdowns: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    *,
    scope: str = "goal",
) -> dict[str, Any]:
    """Accumulate turn breakdowns without carrying sensitive raw details."""
    phases: dict[str, dict[str, Any]] = {}
    merged: dict[str, Any] = {
        "schema_version": 2,
        "scope": _clean_name(scope, limit=48),
        "wall_s": 0.0,
        "model_s": 0.0,
        "tools_s": 0.0,
        "overhead_s": 0.0,
        "active_s": 0.0,
        "summed_active_s": 0.0,
        "overlap_s": 0.0,
        "max_concurrency": 0,
        "span_window_s": 0.0,
        "phase_spans": [],
        "api_calls": 0,
        "tool_calls": 0,
        "tool_errors": 0,
        "tool_blocked": 0,
        "top_tools": [],
        "verification_evidence": [],
        "mutation_generation": 0,
        "mutation_boundary": 0,
        "visual_qa_receipts": [],
        "visual_qa": {
            "level": "none",
            "receipt_status": "not_applicable",
            "followup_count": 0,
            "check_duration_s": 0.0,
        },
        "phases": [],
        "active_exceeds_wall": False,
    }
    tools: dict[str, dict[str, Any]] = {}
    for item in breakdowns or []:
        if not isinstance(item, dict):
            continue
        for key in ("wall_s", "model_s", "tools_s", "overhead_s", "active_s"):
            merged[key] += _seconds(item.get(key))
        merged["summed_active_s"] += _seconds(
            item.get("summed_active_s", item.get("active_s"))
        )
        merged["overlap_s"] += _seconds(item.get("overlap_s"))
        merged["max_concurrency"] = max(
            _count(merged.get("max_concurrency")),
            _count(item.get("max_concurrency")),
        )
        merged["span_window_s"] += _seconds(item.get("span_window_s"))
        merged["phase_spans"].extend(
            sanitize_runtime_spans(item.get("phase_spans"), max_spans=200)
        )
        for key in ("api_calls", "tool_calls", "tool_errors", "tool_blocked"):
            merged[key] += _count(item.get(key))
        if isinstance(item.get("verification_evidence"), list):
            merged["verification_evidence"].extend(
                evidence
                for evidence in item["verification_evidence"]
                if isinstance(evidence, dict)
            )
        merged["mutation_generation"] = _count(item.get("mutation_generation"))
        merged["mutation_boundary"] = _count(item.get("mutation_boundary"))
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
        for raw in item.get("visual_qa_receipts") or []:
            receipt = sanitize_visual_receipt(raw)
            if receipt is not None:
                merged["visual_qa_receipts"].append(receipt)
        visual = item.get("visual_qa") if isinstance(item.get("visual_qa"), dict) else {}
        level = str(visual.get("level") or "none").lower()
        if level in {"surface", "artifact"}:
            merged["visual_qa"]["level"] = level
            merged["visual_qa"]["receipt_status"] = str(
                visual.get("receipt_status") or "missing"
            ).lower()
        merged["visual_qa"]["followup_count"] = min(
            1,
            _count(merged["visual_qa"].get("followup_count"))
            + _count(visual.get("followup_count")),
        )
        merged["visual_qa"]["check_duration_s"] += _seconds(visual.get("check_duration_s"))
    merged["top_tools"] = sorted(tools.values(), key=lambda item: item["duration_s"], reverse=True)[:5]
    merged["phases"] = sorted(phases.values(), key=lambda item: item["duration_s"], reverse=True)[:6]
    merged["verification_evidence"] = merged["verification_evidence"][-20:]
    merged["visual_qa_receipts"] = merged["visual_qa_receipts"][-20:]
    if merged["visual_qa_receipts"]:
        merged["visual_qa"]["receipt_status"] = str(
            merged["visual_qa_receipts"][-1].get("status") or "missing"
        ).lower()
    merged["phase_spans"] = sanitize_runtime_spans(
        merged["phase_spans"], max_spans=200
    )
    if merged["phase_spans"]:
        trusted = _trusted_span_breakdown({"phase_spans": merged["phase_spans"]})
        merged["active_s"] = trusted["active_s"]
        merged["summed_active_s"] = trusted["summed_active_s"]
        merged["overlap_s"] = trusted["overlap_s"]
        merged["max_concurrency"] = trusted["max_concurrency"]
        merged["span_window_s"] = trusted["span_window_s"]
        merged["phases"] = trusted["phases"]
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
        visual_receipts = [
            receipt
            for receipt in (breakdown.get("visual_qa_receipts") or [])
            if isinstance(receipt, dict)
        ]
        visual = breakdown.get("visual_qa") if isinstance(breakdown.get("visual_qa"), dict) else {}
        if visual_receipts:
            receipt = visual_receipts[-1]
            lines.append(
                f"Visual QA: {_clean_name(visual.get('level'), limit=16)} — "
                f"{_clean_name(receipt.get('status'), limit=16)}"
            )
        visual_level = str(visual.get("level") or "none").lower()
        visual_active = bool(
            visual_receipts
            or visual_level in {"surface", "artifact"}
            or _seconds(visual.get("check_duration_s"))
            or _count(visual.get("followup_count"))
        )
        if visual_active and visual and not visual_receipts:
            level = _clean_name(visual.get("level"), limit=16)
            status = _clean_name(visual.get("receipt_status"), limit=16)
            lines.append(f"Visual QA: {level} — {status}")
        if visual_active and visual:
            extras = []
            duration = _seconds(visual.get("check_duration_s"))
            if duration:
                extras.append(f"check {_duration_text(duration)}")
            followups = min(1, _count(visual.get("followup_count")))
            if followups:
                extras.append(f"{followups} follow-up")
            if extras:
                lines.append("Visual QA: " + " · ".join(extras))
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
