#!/usr/bin/env python3
"""Dry-run-first archive helper for the retired host vault."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def _tree_manifest(root: Path) -> list[dict[str, object]]:
    manifest: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Refusing symlink in vault: {path}")
        if not path.is_file():
            continue
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest.append({
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
            "sha256": digest.hexdigest(),
        })
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--archive-root", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    archive_root = args.archive_root.expanduser().resolve()
    if not source.is_dir() or source == Path(source.anchor):
        print(f"Refusing invalid source directory: {source}", file=sys.stderr)
        return 2
    if archive_root == source or source in archive_root.parents:
        print("Archive root must not be the source or below it", file=sys.stderr)
        return 2

    before = _tree_manifest(source)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = archive_root / f"{source.name}-{timestamp}"
    plan = {
        "mode": "execute" if args.execute else "dry-run",
        "source": str(source),
        "destination": str(destination),
        "file_count": len(before),
        "total_bytes": sum(int(item["size"]) for item in before),
    }
    print(json.dumps(plan, indent=2, sort_keys=True))
    if not args.execute:
        return 0

    archive_root.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        print(f"Refusing existing destination: {destination}", file=sys.stderr)
        return 2
    retired_source = source.with_name(f".{source.name}.retired-{timestamp}")
    if retired_source.exists():
        print(f"Refusing existing staging path: {retired_source}", file=sys.stderr)
        return 2
    os.rename(source, retired_source)
    try:
        frozen = _tree_manifest(retired_source)
        if before != frozen:
            raise RuntimeError("source changed before it could be frozen")
        shutil.copytree(retired_source, destination)
        after = _tree_manifest(destination)
        if frozen != after:
            raise RuntimeError("archive checksum verification failed")
    except Exception as exc:
        shutil.rmtree(destination, ignore_errors=True)
        if not source.exists() and retired_source.exists():
            os.rename(retired_source, source)
        print(f"Archive failed; source restored: {exc}", file=sys.stderr)
        return 3
    manifest_path = destination / ".hermes-archive-manifest.json"
    manifest_path.write_text(
        json.dumps({**plan, "mode": "executed", "files": after}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    shutil.rmtree(retired_source)
    print(f"Archived and removed source after checksum verification: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
