"""Read-only Command Center snapshot verification helpers."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen


COMPLETED_STATUSES = {"shipped", "done"}


def item_is_completed(item: dict[str, Any]) -> bool:
    return str(item.get("status") or "").lower() in COMPLETED_STATUSES


def item_has_revert_action(item: dict[str, Any]) -> bool:
    decision = item.get("decision") if isinstance(item.get("decision"), dict) else {}
    execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
    return bool(decision.get("undo_followup_action") or execution.get("undo_followup_action"))


def item_has_archive_action(item: dict[str, Any]) -> bool:
    execution = item.get("execution") if isinstance(item.get("execution"), dict) else {}
    decision = item.get("decision") if isinstance(item.get("decision"), dict) else {}
    return bool(execution.get("archiveable") or execution.get("archive_action") or decision.get("archive_action"))


def summarize_completed_actions(snapshot: dict[str, Any], *, project: str | None = None) -> dict[str, Any]:
    items = snapshot.get("work_items") if isinstance(snapshot.get("work_items"), list) else []
    if project:
        normalized = project.strip().lower()
        items = [item for item in items if str(item.get("project") or "").strip().lower() == normalized]
    completed = [item for item in items if isinstance(item, dict) and item_is_completed(item)]
    revertable = [item for item in completed if item_has_revert_action(item)]
    archiveable = [item for item in completed if item_has_archive_action(item)]
    missing_revert = [item for item in completed if not item_has_revert_action(item)]
    return {
        "project": project,
        "completed_count": len(completed),
        "revertable_completed_count": len(revertable),
        "archiveable_completed_count": len(archiveable),
        "missing_revert_count": len(missing_revert),
        "missing_revert_ids": [str(item.get("id") or "") for item in missing_revert],
        "revertable_completed_ids": [str(item.get("id") or "") for item in revertable],
    }


def _load_snapshot(source: str) -> dict[str, Any]:
    if source.startswith("http://") or source.startswith("https://"):
        request = Request(source, headers={"Accept": "application/json"})
        with urlopen(request, timeout=30) as response:  # noqa: S310 - operator-supplied read-only URL
            return json.loads(response.read().decode("utf-8"))
    return json.loads(Path(source).read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check completed Command Center rows for Revert/Archive action payloads.")
    parser.add_argument("snapshot", help="Snapshot JSON path or read-only snapshot URL")
    parser.add_argument("--project", help="Optional project key to filter")
    parser.add_argument("--require-revertable", action="store_true", help="Exit nonzero when completed rows exist but none expose Revert")
    args = parser.parse_args(argv)

    summary = summarize_completed_actions(_load_snapshot(args.snapshot), project=args.project)
    print(json.dumps(summary, indent=2, sort_keys=True))
    if args.require_revertable and summary["completed_count"] and not summary["revertable_completed_count"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
