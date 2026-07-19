"""Bounded request-text assembly with formatting-preserving deduplication."""

from __future__ import annotations

import re
from typing import Any


DEFAULT_MAX_REQUEST_CHARS = 16_000
_MAX_REQUEST_CHARS = 64_000


def flatten_request_for_matching(text: Any) -> str:
    """Return a whitespace-collapsed request form for regex/keyword matching."""

    return " ".join(str(text or "").split())


def _comparison_key(value: Any) -> str:
    return flatten_request_for_matching(value).casefold()


def merge_request_fragments(
    *parts: Any,
    max_chars: int = DEFAULT_MAX_REQUEST_CHARS,
) -> str:
    """Join distinct request fragments while preserving first-copy formatting.

    Empty fragments and duplicates are removed using a case-insensitive,
    whitespace-normalized comparison key. The first occurrence is kept with its
    internal newlines and sentence boundaries intact. Output is hard-bounded.
    """

    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_REQUEST_CHARS
    limit = max(0, min(limit, _MAX_REQUEST_CHARS))
    if limit == 0:
        return ""

    kept: list[str] = []
    seen: set[str] = set()
    used = 0
    for part in parts:
        raw = str(part or "").strip()
        key = _comparison_key(raw)
        if not key or key in seen:
            continue
        seen.add(key)
        separator = "\n\n" if kept else ""
        remaining = limit - used
        if remaining <= len(separator):
            break
        fragment = raw[: remaining - len(separator)].rstrip()
        if not fragment:
            break
        kept.append(fragment)
        used += len(separator) + len(fragment)
        if used >= limit:
            break
    return "\n\n".join(kept)


__all__ = [
    "DEFAULT_MAX_REQUEST_CHARS",
    "flatten_request_for_matching",
    "merge_request_fragments",
]
