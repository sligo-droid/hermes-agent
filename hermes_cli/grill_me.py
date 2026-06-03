"""Natural-language planning prompt for the Hermes "grill me" flow."""

from __future__ import annotations

import re


_GRILL_ME_RE = re.compile(r"\bgrill\s+me\b", re.IGNORECASE)
_NEGATED_GRILL_ME_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|no\s+need\s+to|not|never)\s+grill\s+me\b",
    re.IGNORECASE,
)
_META_GRILL_ME_RE = re.compile(
    r"\b(?:research|investigate|explain|define|describe)\b"
    r"[^\n.!?]{0,80}\bgrill\s+me\b"
    r"[^\n.!?]{0,40}\b(?:feature|command|skill|workflow|mode)\b"
    r"|\b(?:plan|design|build|implement)\b"
    r"[^\n.!?]{0,80}\b(?:feature|command|skill|workflow|mode)\b"
    r"[^\n.!?]{0,80}\b(?:called|named)?\s*grill\s+me\b"
    r"|\bgrill\s+me\b[^\n.!?]{0,80}\b(?:mean|means|meaning|called|named)\b",
    re.IGNORECASE,
)
_QUESTION_META_RE = re.compile(
    r"\b(?:what\s+does|what\s+is|what's|how\s+does)\b[^\n.!?]{0,80}\bgrill\s+me\b",
    re.IGNORECASE,
)


def detect_grill_me_trigger(text: str) -> bool:
    """Return True for clear imperative uses of the exact phrase "grill me"."""
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped or stripped.startswith("/"):
        return False
    if not _GRILL_ME_RE.search(stripped):
        return False
    if _NEGATED_GRILL_ME_RE.search(stripped):
        return False
    if _QUESTION_META_RE.search(stripped):
        return False
    if _META_GRILL_ME_RE.search(stripped):
        return False
    return True


def build_grill_me_prompt(user_text: str, runtime_note: str = "") -> str:
    """Build the planning-only prompt sent to the agent for grill-me turns."""
    note = runtime_note.strip()
    note_block = f"\n\nRuntime note:\n{note}" if note else ""
    return (
        "Hermes grill-me planning mode is active. Treat the user's message as a "
        "request to pressure-test and refine a plan before implementation.\n\n"
        "Do not implement code, spawn workers, create Kanban tasks, invoke /goal, "
        "or hand off work. This is a planning conversation only until the user "
        "explicitly confirms they want implementation or a /goal/Kanban handoff.\n\n"
        "Inspect the repository, code, docs, and available context for answers "
        "that are discoverable. Ask the user only for missing decisions, goals, "
        "constraints, or tradeoffs that cannot be inferred.\n\n"
        "Question cadence: ask orthogonal, independent questions together in a "
        "tight round. Ask dependent questions serially when one answer changes "
        "or determines later questions. Do not batch dependent questions.\n\n"
        "End this turn with either a polished implementation/goal plan if the "
        "request is sufficiently specified, or one tight question round if it is "
        "under-specified. Make the plan concrete enough to hand to /goal only "
        "after explicit user confirmation.\n\n"
        f"Original user request:\n{user_text.strip()}"
        f"{note_block}"
    )
