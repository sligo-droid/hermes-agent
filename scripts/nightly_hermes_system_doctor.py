#!/usr/bin/env python3
"""Maintained source helpers for the nightly Hermes system doctor.

The live cron script may emit provider warnings before its structured route
smoke payload. Keep the route parser here so future installs can reuse the
tested behavior instead of parsing whole stdout as JSON.
"""
from __future__ import annotations

import json
import re
from typing import Any


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def clean(text: str, limit: int = 4000) -> str:
    """Return sanitized diagnostic text safe for persisted reports."""
    text = ANSI_RE.sub("", text or "")
    text = re.sub(
        r"(?i)(api[_-]?key|token|secret|password)\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    if len(text) > limit:
        return text[:limit] + "...<truncated>"
    return text


def add_issue(issues: list[dict[str, str]], severity: str, name: str, detail: str) -> None:
    issues.append({"severity": severity, "name": name, "detail": clean(detail, 1800)})


def extract_compression_routes(stdout: str) -> list[dict[str, Any]]:
    """Extract the final JSON object with a top-level ``routes`` list.

    Compression route smoke can print provider warnings before the structured
    payload. Scan candidate JSON objects in stdout and accept only objects whose
    top-level ``routes`` value is a list, avoiding arbitrary JSON snippets.
    """
    decoder = json.JSONDecoder()
    route_results: list[dict[str, Any]] = []
    text = stdout or ""

    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        routes = payload.get("routes")
        if isinstance(routes, list):
            route_results = [item for item in routes if isinstance(item, dict)]

    return route_results


def record_compression_route_results(
    routes: dict[str, Any],
    issues: list[dict[str, str]],
    facts: dict[str, Any],
) -> list[dict[str, Any]]:
    """Record and report compression route smoke results from a run() result."""
    output = str(routes.get("output") or "")
    facts["compression_routes_exit"] = routes.get("exit")
    facts["compression_routes_output"] = output

    route_results = extract_compression_routes(output)
    facts["compression_route_results"] = route_results

    if not route_results:
        add_issue(
            issues,
            "critical",
            "compression configured provider routes missing",
            output or "No auxiliary.compression primary or fallback routes were discovered.",
        )
        return route_results

    for item in route_results:
        if item.get("exit") == 0:
            continue
        label = item.get("label") or "unknown"
        provider = item.get("provider") or "unknown-provider"
        model = item.get("model") or "unknown-model"
        add_issue(
            issues,
            "critical",
            f"compression configured provider route failed: {label} {provider}/{model}",
            json.dumps(item, sort_keys=True),
        )

    return route_results
