"""Read-only runtime timing report helpers for Hermes logs."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from hermes_constants import get_hermes_home


_SESSION_TAG_RE = re.compile(r"\s(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+\[([^\]]+)\]")
_TURN_SUMMARY_RE = re.compile(r"turn_runtime_summary\s+(\{.*\})")
_RESPONSE_READY_RE = re.compile(
    r"response ready:\s+platform=(?P<platform>\S+)\s+chat=(?P<chat>\S+)\s+"
    r"time=(?P<seconds>[0-9.]+)s\s+api_calls=(?P<api_calls>\d+)\s+"
    r"response=(?P<response_chars>\d+)\s+chars"
)


@dataclass
class RuntimeReport:
    """Aggregated runtime timing for recent log lines."""

    gateway_flows: list[dict] = field(default_factory=list)
    turns: list[dict] = field(default_factory=list)
    top_tools: list[dict] = field(default_factory=list)


def _session_tag(line: str) -> str | None:
    match = _SESSION_TAG_RE.search(line)
    return match.group(1) if match else None


def parse_runtime_report_lines(lines: Iterable[str], *, limit: int = 20) -> RuntimeReport:
    """Parse Hermes log lines into recent gateway-flow and agent-turn timings.

    The parser is intentionally read-only and deterministic. It consumes the
    existing ``response ready`` gateway line plus ``turn_runtime_summary`` JSON
    lines, keeping their timings separate instead of pretending they measure the
    same boundary.
    """

    gateway_flows: list[dict] = []
    turns: list[dict] = []
    tools: dict[str, dict] = defaultdict(
        lambda: {
            "name": "",
            "count": 0,
            "duration_ms": 0,
            "errors": 0,
            "blocked": 0,
            "chars": 0,
        }
    )

    for line in lines:
        ready = _RESPONSE_READY_RE.search(line)
        if ready:
            session = _session_tag(line)
            gateway_flows.append(
                {
                    "session": session,
                    "platform": ready.group("platform"),
                    "chat": ready.group("chat"),
                    "gateway_wall_ms": int(round(float(ready.group("seconds")) * 1000)),
                    "response_ready_api_calls": int(ready.group("api_calls")),
                    "response_chars": int(ready.group("response_chars")),
                }
            )
            continue

        summary = _TURN_SUMMARY_RE.search(line)
        if not summary:
            continue
        try:
            payload = json.loads(summary.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        turn = {
            "session": payload.get("session") or _session_tag(line),
            "platform": payload.get("platform") or "",
            "provider": payload.get("provider") or "unknown",
            "model": payload.get("model") or "",
            "model_tier": payload.get("model_tier") or "",
            "model_tier_source": payload.get("model_tier_source") or "none",
            "runtime_route": payload.get("runtime_route") or "",
            "runtime_role": payload.get("runtime_role") or "",
            "runtime_pass": payload.get("runtime_pass") or "",
            "reasoning_effort": payload.get("reasoning_effort") or "default",
            "reasoning_mode": payload.get("reasoning_mode") or "default",
            "reasoning_enabled": payload.get("reasoning_enabled"),
            "reasoning_source": payload.get("reasoning_source") or "default",
            "service_tier": payload.get("service_tier") or "default",
            "service_tier_source": payload.get("service_tier_source") or "default",
            "api_mode": payload.get("api_mode") or "default",
            "exit_reason": payload.get("exit_reason") or "unknown",
            "turn_total_ms": int(payload.get("total_ms") or 0),
            "api_ms": int(payload.get("api_ms") or 0),
            "tool_ms": int(payload.get("tool_ms") or 0),
            "overhead_ms": int(payload.get("overhead_ms") or 0),
            "api_calls": int(payload.get("api_calls") or 0),
            "tool_calls": int(payload.get("tool_calls") or 0),
            "response_len": int(payload.get("response_len") or 0),
        }
        turns.append(turn)

        for item in payload.get("top_tools") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            row = tools[name]
            row["name"] = name
            row["count"] += int(item.get("count") or 0)
            row["duration_ms"] += int(item.get("duration_ms") or 0)
            row["errors"] += int(item.get("errors") or 0)
            row["blocked"] += int(item.get("blocked") or 0)
            row["chars"] += int(item.get("chars") or 0)

    top_tools = sorted(tools.values(), key=lambda row: row["duration_ms"], reverse=True)
    return RuntimeReport(
        gateway_flows=gateway_flows[-limit:],
        turns=turns[-limit:],
        top_tools=top_tools[:limit],
    )


def runtime_report_from_files(paths: Sequence[Path] | None = None, *, limit: int = 20) -> RuntimeReport:
    """Build a report from Hermes log files, newest matching rows last."""

    if paths is None:
        log_dir = get_hermes_home() / "logs"
        paths = (log_dir / "agent.log", log_dir / "gateway.log")
    lines: list[str] = []
    for path in paths:
        try:
            lines.extend(path.read_text(errors="replace").splitlines())
        except FileNotFoundError:
            continue
    return parse_runtime_report_lines(lines, limit=limit)
