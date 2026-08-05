#!/usr/bin/env python3
"""Sanitize a raw Skills Hub index before it becomes a public/runtime cache."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from skill_catalog_policy import atomic_write_skills_index, sanitize_skills_index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    try:
        raw = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Invalid skills index: {exc}", file=sys.stderr)
        return 2
    sanitized = sanitize_skills_index(raw)
    if sanitized is None:
        print("Invalid skills index: expected an object with a skills array", file=sys.stderr)
        return 2
    atomic_write_skills_index(args.output, sanitized)
    removed = len(raw["skills"]) - len(sanitized["skills"])
    print(f"Sanitized skills index: removed {removed} retired entries")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
