"""Shared policy for records exposed through Hermes skill catalogs."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable


RETIRED_SKILL_MARKER = "obsidian"
_IDENTITY_FIELDS = (
    "name",
    "identifier",
    "repo",
    "path",
    "resolved_github_id",
    "source_url",
    "detail_url",
    "install_command",
    "integration",
)
_ADVERTISING_TERMS = ("skill", "integration", "vault", "note", "plugin")


def is_retired_skill_record(record: Any) -> bool:
    """Return whether a skill record advertises the retired integration."""
    if not isinstance(record, dict):
        return False

    def contains_marker(value: Any) -> bool:
        return RETIRED_SKILL_MARKER in str(value).lower()

    if any(contains_marker(record.get(field, "")) for field in _IDENTITY_FIELDS):
        return True
    if any(contains_marker(tag) for tag in record.get("tags", []) if isinstance(tag, str)):
        return True

    description = str(record.get("description", "")).lower()
    if RETIRED_SKILL_MARKER in description and any(
        term in description for term in _ADVERTISING_TERMS
    ):
        return True

    for extra in (record.get("extra"), record.get("metadata")):
        if isinstance(extra, dict) and any(
            contains_marker(extra.get(field, "")) for field in _IDENTITY_FIELDS
        ):
            return True
    return False


def filter_skill_records(records: Iterable[Any]) -> list[Any]:
    """Return only records allowed to appear in live skill discovery."""
    return [record for record in records if not is_retired_skill_record(record)]


def sanitize_skills_index(index: Any) -> dict[str, Any] | None:
    """Validate and sanitize a raw public skills index.

    Invalid indexes fail closed instead of being cached or exposed.
    """
    if not isinstance(index, dict) or not isinstance(index.get("skills"), list):
        return None
    sanitized = dict(index)
    sanitized["skills"] = filter_skill_records(index["skills"])
    sanitized["skill_count"] = len(sanitized["skills"])
    return sanitized


def atomic_write_skills_index(path: Path, index: dict[str, Any]) -> None:
    """Atomically write a sanitized index on the destination filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(index, handle, separators=(",", ":"), ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
