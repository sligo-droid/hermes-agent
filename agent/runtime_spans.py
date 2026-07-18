"""Trusted bounded runtime spans and interval-union aggregation."""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from dataclasses import dataclass
from typing import Any


_ALLOWED_METADATA = frozenset(
    {
        "model",
        "provider",
        "tool",
        "operation",
        "route",
        "check",
        "repository",
        "ref_kind",
        "collector",
        "worker_tier",
        "adapter",
        "outcome",
        "source",
        "mode",
        "surface",
    }
)
_ALLOWED_STATUSES = frozenset({"ok", "error", "blocked", "timeout", "cancelled", "uncertain"})
_ALLOWED_PHASES = frozenset(
    {
        "model",
        "tools",
        "browser",
        "vision",
        "coding_worker",
        "check",
        "git",
        "ci",
        "github",
        "review",
        "deployment",
        "production_qa",
        "closeout",
        "canonical_sync",
        "restart",
        "gateway_handoff",
        "overhead",
    }
)
_KNOWN_SPAN_NAMES = frozenset(
    {
        "model_attempt",
        "closeout_handoff",
        "fable_finalization",
        "fable_command",
        "trusted_closeout",
        "post_merge_canonical_sync",
        "post_merge_ci",
        "post_merge_deployment",
        "post_merge_production_qa",
        "post_merge_restart",
        "operation",
    }
)
_HASHED_METADATA = frozenset(
    {
        "model",
        "provider",
        "tool",
        "operation",
        "route",
        "check",
        "repository",
        "collector",
        "adapter",
        "source",
    }
)
_METADATA_ENUMS = {
    "ref_kind": frozenset({"branch", "commit", "pull_request", "revision", "session", "tag", "task", "worktree"}),
    "worker_tier": frozenset({"primary", "fallback", "auxiliary", "coding", "review", "ui", "orchestrator", "leaf"}),
    "outcome": frozenset({"ok", "error", "blocked", "timeout", "cancelled", "uncertain", "passed", "failed"}),
    "mode": frozenset({"off", "shadow", "enforce", "enforce_explicit", "auto", "manual", "direct", "fallback", "foreground", "background", "sync", "async"}),
    "surface": _ALLOWED_PHASES | frozenset({"cli", "gateway", "discord", "telegram", "slack", "tui", "web"}),
}
_SPAN_ID_RE = re.compile(r"^span-[0-9a-f]{8}-[0-9]{4}$")
_OPAQUE_RE = re.compile(r"^(?:op|wrk|att|con|ref|meta)_[0-9a-f]{16}$")


def _token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _opaque(value: Any, prefix: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if _OPAQUE_RE.fullmatch(text) and text.startswith(f"{prefix}_"):
        return text
    digest = hashlib.sha256(f"{prefix}\0{text}".encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{digest}"


def _safe_span_id(value: Any) -> str:
    text = str(value or "").strip()
    return text if _SPAN_ID_RE.fullmatch(text) else ""


def _safe_name(value: Any) -> str:
    token = _token(value)
    if token in _KNOWN_SPAN_NAMES:
        return token
    return _opaque(value, "op") or "operation"


def _safe_phase(value: Any) -> str:
    token = _token(value)
    return token if token in _ALLOWED_PHASES else "overhead"


def _safe_correlation(value: Any, prefix: str) -> str:
    text = str(value or "").strip()
    if prefix == "ref" and _SPAN_ID_RE.fullmatch(text):
        return text
    return _opaque(text, prefix)


def _safe_metadata(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    safe: dict[str, Any] = {}
    for key in _ALLOWED_METADATA:
        if key not in value:
            continue
        item = value[key]
        if isinstance(item, bool):
            safe[key] = item
        elif isinstance(item, int):
            safe[key] = max(-1_000_000, min(item, 1_000_000))
        elif key in _HASHED_METADATA:
            opaque = _opaque(item, "meta")
            if opaque:
                safe[key] = opaque
        else:
            token = _token(item)
            if token in _METADATA_ENUMS.get(key, frozenset()):
                safe[key] = token
    return safe


@dataclass(frozen=True)
class _SpanHandle:
    id: str
    started_at: float
    started_monotonic: float
    name: str
    phase: str
    parent_id: str
    work_id: str
    attempt_id: str
    concurrency_id: str
    metadata: dict[str, Any]


class RuntimeSpanRecorder:
    """Thread-safe recorder whose exported shape never contains monotonic clocks."""

    def __init__(self, *, work_id: str = "", max_spans: int = 200) -> None:
        self.work_id = _safe_correlation(work_id, "wrk")
        self.max_spans = max(1, min(int(max_spans), 500))
        identity = f"{self.work_id or 'local'}:{time.time_ns()}"
        self._id_prefix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]
        self._lock = threading.Lock()
        self._counter = 0
        self._spans: list[dict[str, Any]] = []

    def start(
        self,
        name: str,
        *,
        phase: str,
        parent_id: str = "",
        work_id: str = "",
        attempt_id: str = "",
        concurrency_id: str = "",
        metadata: Any = None,
    ) -> _SpanHandle:
        with self._lock:
            self._counter += 1
            span_id = f"span-{self._id_prefix}-{self._counter:04d}"
        return _SpanHandle(
            id=span_id,
            started_at=time.time(),
            started_monotonic=time.monotonic(),
            name=_safe_name(name),
            phase=_safe_phase(phase),
            parent_id=_safe_correlation(parent_id, "ref"),
            work_id=_safe_correlation(work_id, "wrk") or self.work_id,
            attempt_id=_safe_correlation(attempt_id, "att"),
            concurrency_id=_safe_correlation(concurrency_id, "con"),
            metadata=_safe_metadata(metadata),
        )

    def finish(
        self,
        handle: _SpanHandle,
        *,
        status: str = "ok",
        metadata: Any = None,
    ) -> dict[str, Any]:
        duration = max(0.0, time.monotonic() - handle.started_monotonic)
        ended_at = handle.started_at + duration
        normalized_status = str(status or "").strip().lower()
        if normalized_status not in _ALLOWED_STATUSES:
            normalized_status = "uncertain"
        combined_metadata = dict(handle.metadata)
        combined_metadata.update(_safe_metadata(metadata))
        span: dict[str, Any] = {
            "id": handle.id,
            "name": handle.name,
            "phase": handle.phase,
            "started_at": round(handle.started_at, 6),
            "ended_at": round(ended_at, 6),
            "duration_s": round(duration, 6),
            "status": normalized_status,
        }
        for key, value in (
            ("parent_id", handle.parent_id),
            ("work_id", handle.work_id),
            ("attempt_id", handle.attempt_id),
            ("concurrency_id", handle.concurrency_id),
        ):
            if value:
                span[key] = value
        if combined_metadata:
            span["metadata"] = combined_metadata
        with self._lock:
            if len(self._spans) < self.max_spans:
                self._spans.append(span)
        return span

    def export(self) -> list[dict[str, Any]]:
        with self._lock:
            return sanitize_runtime_spans(self._spans, max_spans=self.max_spans)


def sanitize_runtime_spans(value: Any, *, max_spans: int = 200) -> list[dict[str, Any]]:
    """Return a bounded, allowlisted copy of persisted span data."""

    bounded = max(1, min(int(max_spans), 500))
    safe_spans: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        span_id = _safe_span_id(raw.get("id"))
        if not span_id or span_id in seen_ids:
            continue
        try:
            started_at = float(raw.get("started_at"))
            ended_at = float(raw.get("ended_at"))
            duration_s = float(raw.get("duration_s"))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(item) for item in (started_at, ended_at, duration_s)):
            continue
        if started_at < 0 or ended_at < started_at or duration_s < 0:
            continue
        status = str(raw.get("status") or "uncertain").strip().lower()
        if status not in _ALLOWED_STATUSES:
            status = "uncertain"
        span: dict[str, Any] = {
            "id": span_id,
            "name": _safe_name(raw.get("name")),
            "phase": _safe_phase(raw.get("phase")),
            "started_at": round(started_at, 6),
            "ended_at": round(ended_at, 6),
            "duration_s": round(duration_s, 6),
            "status": status,
        }
        correlation_prefixes = {
            "parent_id": "ref",
            "work_id": "wrk",
            "attempt_id": "att",
            "concurrency_id": "con",
        }
        for key, prefix in correlation_prefixes.items():
            identifier = _safe_correlation(raw.get(key), prefix)
            if identifier:
                span[key] = identifier
        metadata = _safe_metadata(raw.get("metadata"))
        if metadata:
            span["metadata"] = metadata
        safe_spans.append(span)
        seen_ids.add(span_id)
        if len(safe_spans) >= bounded:
            break
    return safe_spans


def start_agent_runtime_span(
    agent: Any,
    name: str,
    *,
    phase: str,
    parent_id: str = "",
    work_id: str = "",
    attempt_id: str = "",
    concurrency_id: str = "",
    metadata: Any = None,
) -> _SpanHandle | None:
    """Best-effort span start for runtime boundaries that must never fail work."""

    recorder = getattr(agent, "_turn_runtime_span_recorder", None)
    if not isinstance(recorder, RuntimeSpanRecorder):
        return None
    try:
        return recorder.start(
            name,
            phase=phase,
            parent_id=parent_id,
            work_id=work_id,
            attempt_id=attempt_id,
            concurrency_id=concurrency_id,
            metadata=metadata,
        )
    except Exception:
        return None


def finish_agent_runtime_span(
    agent: Any,
    handle: _SpanHandle | None,
    *,
    status: str = "ok",
    metadata: Any = None,
) -> None:
    """Best-effort span finish paired with :func:`start_agent_runtime_span`."""

    if handle is None:
        return
    recorder = getattr(agent, "_turn_runtime_span_recorder", None)
    if not isinstance(recorder, RuntimeSpanRecorder):
        return
    try:
        recorder.finish(handle, status=status, metadata=metadata)
    except Exception:
        return


def _intervals(spans: list[dict[str, Any]]) -> list[tuple[float, float]]:
    intervals: list[tuple[float, float]] = []
    for span in spans:
        try:
            start = float(span.get("started_at"))
            end = float(span.get("ended_at"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(start) and math.isfinite(end) and end >= start:
            intervals.append((start, end))
    return intervals


def _union_duration(intervals: list[tuple[float, float]]) -> float:
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    total = 0.0
    current_start, current_end = ordered[0]
    for start, end in ordered[1:]:
        if start <= current_end:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


def _peak_concurrency(intervals: list[tuple[float, float]]) -> int:
    events: list[tuple[float, int]] = []
    for start, end in intervals:
        events.append((start, 1))
        events.append((end, -1))
    active = peak = 0
    for _timestamp, delta in sorted(events, key=lambda item: (item[0], item[1])):
        active += delta
        peak = max(peak, active)
    return peak


def summarize_span_intervals(value: Any) -> dict[str, Any]:
    """Deduplicate span IDs and compute union/summed/overlap phase metrics."""

    spans: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in value if isinstance(value, list) else []:
        if not isinstance(raw, dict):
            continue
        span_id = str(raw.get("id") or "")
        if span_id and span_id in seen_ids:
            continue
        if span_id:
            seen_ids.add(span_id)
        spans.append(raw)
    intervals = _intervals(spans)
    union = _union_duration(intervals)
    summed = sum(max(0.0, end - start) for start, end in intervals)
    phases: dict[str, dict[str, Any]] = {}
    for span in spans:
        phase = _safe_phase(span.get("phase"))
        slot = phases.setdefault(phase, {"spans": [], "count": 0})
        slot["spans"].append(span)
        slot["count"] += 1
    phase_summary: dict[str, dict[str, Any]] = {}
    for phase, slot in phases.items():
        phase_intervals = _intervals(slot["spans"])
        phase_union = _union_duration(phase_intervals)
        phase_sum = sum(max(0.0, end - start) for start, end in phase_intervals)
        phase_summary[phase] = {
            "union_s": round(phase_union, 6),
            "summed_s": round(phase_sum, 6),
            "overlap_s": round(max(0.0, phase_sum - phase_union), 6),
            "count": slot["count"],
        }
    return {
        "union_s": round(union, 6),
        "summed_s": round(summed, 6),
        "overlap_s": round(max(0.0, summed - union), 6),
        "peak_concurrency": _peak_concurrency(intervals),
        "count": len(intervals),
        "phases": phase_summary,
    }


__all__ = [
    "RuntimeSpanRecorder",
    "finish_agent_runtime_span",
    "sanitize_runtime_spans",
    "start_agent_runtime_span",
    "summarize_span_intervals",
]
