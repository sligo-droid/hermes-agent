"""Source hygiene checks for shipped skills and always-routed docs."""

from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL_SUPPORT_DIRS = frozenset(("references", "templates", "assets", "scripts"))


def test_shipped_skill_roots_fit_context_budget():
    """Every shipped root SKILL.md must stay small enough to load directly."""
    offenders: list[str] = []
    for base in (REPO_ROOT / "skills", REPO_ROOT / "optional-skills"):
        for skill_md in sorted(base.rglob("SKILL.md")):
            # Match active-skill discovery: supporting material is explicit
            # progressive disclosure, not a root skill body loaded by default.
            if any(part in _SKILL_SUPPORT_DIRS for part in skill_md.relative_to(base).parts[:-1]):
                continue
            size = len(skill_md.read_text(encoding="utf-8"))
            if size > 20_000:
                offenders.append(f"{skill_md.relative_to(REPO_ROOT)}: {size:,} chars")

    assert offenders == []


def test_always_routed_docs_fit_context_budget():
    """Always-routed docs should stay compact; details belong in focused docs."""
    char_limits = {
        "AGENTS.md": 20_000,
        "docs/context.md": 10_000,
        "docs/sligo-command-center.md": 15_000,
    }
    byte_limits = {
        "docs/project-state.md": 3_000,
    }

    char_offenders = []
    for rel, limit in char_limits.items():
        size = len((REPO_ROOT / rel).read_text(encoding="utf-8"))
        if size > limit:
            char_offenders.append(f"{rel}: {size:,} chars > {limit:,}")

    byte_offenders = []
    for rel, limit in byte_limits.items():
        size = len((REPO_ROOT / rel).read_bytes())
        if size > limit:
            byte_offenders.append(f"{rel}: {size:,} bytes > {limit:,}")

    assert char_offenders == []
    assert byte_offenders == []
