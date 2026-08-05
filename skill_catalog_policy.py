"""Shared policy for records exposed through Hermes skill catalogs."""

from __future__ import annotations

import json
import os
import re
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
_ATTRIBUTION_FIELDS = frozenset(
    {
        "author",
        "authors",
        "contributor",
        "contributors",
        "maintainer",
        "maintainers",
        "owner_name",
    }
)
_NATURAL_MEANING_TERMS = re.compile(
    r"\b(?:volcanic\s+glass|igneous|geolog(?:y|ic|ical)?|mineral(?:ogy)?|"
    r"gem(?:stone)?|rock|lava|mahogany\s+obsidian|snowflake\s+obsidian|"
    r"obsidian\s+(?:black|color|colour|palette|pigment|shade))\b",
    re.IGNORECASE,
)
_CAPABILITY_TERMS = re.compile(
    r"\b(?:agent|api|app|automation|backup|capture|cli|convert|export|file|"
    r"import|index|ingest|integration|knowledge|markdown|memory|note|plugin|"
    r"publish|query|read|save|search|skill|sync|task|template|tool|vault|wiki|"
    r"write|zettelkasten)\b",
    re.IGNORECASE,
)
_ATTRIBUTION_LINE = re.compile(
    r"^\s*(?:[-*]\s*)?(?:author|authors|contributor|contributors|maintainer|maintainers|"
    r"created\s+by|credit|credits)\s*[:\-]\s*[^\n]*obsidian[^\n]*$",
    re.IGNORECASE,
)


def _marker_occurrences(value: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str]]:
    """Return paths and scalar text containing the retired product marker."""
    found: list[tuple[tuple[str, ...], str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            key_text = str(key)
            key_path = (*path, key_text.lower())
            if RETIRED_SKILL_MARKER in key_text.lower():
                # Marker-bearing keys are executable metadata, never attribution.
                found.append(((*path, "<key>"), key_text))
            found.extend(_marker_occurrences(child, key_path))
    elif isinstance(value, (list, tuple, set)):
        for index, child in enumerate(value):
            found.extend(_marker_occurrences(child, (*path, str(index))))
    elif value is not None:
        text = str(value)
        if RETIRED_SKILL_MARKER in text.lower():
            found.append((path, text))
    return found


def _is_attribution_occurrence(path: tuple[str, ...], text: str) -> bool:
    if not path:
        return False
    for index, part in enumerate(path):
        if part not in _ATTRIBUTION_FIELDS:
            continue
        # Attribution may be a scalar or a list of scalar names. Nested source,
        # repository, path, or arbitrary metadata beneath an attribution object
        # is provenance and must remain blocked.
        return (
            all(suffix.isdigit() for suffix in path[index + 1 :])
            and not _CAPABILITY_TERMS.search(text)
        )
    return False


def _is_natural_meaning_record(record: dict[str, Any], occurrences: list[tuple[tuple[str, ...], str]]) -> bool:
    """Recognize only narrow geological/color uses, never digital capabilities."""
    if not occurrences:
        return False
    # URLs, repository/path identity, install commands, and integrations are
    # executable provenance. A marker there is never merely a color/mineral.
    identity_fields = set(_IDENTITY_FIELDS) | {"url", "repo_url", "github_url", "source"}
    if any(any(part in identity_fields for part in path) for path, _ in occurrences):
        return False
    prose = " ".join(
        str(record.get(field, "")) for field in ("name", "description", "summary", "category")
    )
    prose += " " + " ".join(text for _, text in occurrences)
    return bool(_NATURAL_MEANING_TERMS.search(prose)) and not bool(
        _CAPABILITY_TERMS.search(prose)
    )


def is_retired_skill_record(record: Any) -> bool:
    """Return whether a record is unsafe for live skill discovery.

    The catalog is untrusted capability metadata. Malformed records and every
    product occurrence are rejected unless all occurrences are narrowly proven
    to be contributor attribution or a non-digital geological/color meaning.
    This intentionally fails closed instead of maintaining a changing denylist
    of skill names.
    """
    if not isinstance(record, dict):
        return True
    if not any(
        isinstance(record.get(field), str) and record[field].strip()
        for field in ("name", "identifier")
    ):
        return True

    occurrences = _marker_occurrences(record)
    if not occurrences:
        return False
    if all(_is_attribution_occurrence(path, text) for path, text in occurrences):
        return False
    if _is_natural_meaning_record(record, occurrences):
        return False
    return True


def is_retired_skill_text(text: Any) -> bool:
    """Return whether bounded skill/support text advertises the retired capability."""
    if not isinstance(text, str):
        return True
    marker_lines = [line for line in text.splitlines() if RETIRED_SKILL_MARKER in line.lower()]
    if not marker_lines:
        return False
    for line in marker_lines:
        if _ATTRIBUTION_LINE.match(line) and not _CAPABILITY_TERMS.search(line):
            continue
        if _NATURAL_MEANING_TERMS.search(line) and not _CAPABILITY_TERMS.search(line):
            continue
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
