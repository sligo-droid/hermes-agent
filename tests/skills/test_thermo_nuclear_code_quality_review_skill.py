from __future__ import annotations

import re
from pathlib import Path

import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "skills"
    / "software-development"
    / "thermo-nuclear-code-quality-review"
)


def _skill_text() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter() -> dict:
    match = re.search(r"^---\n(.*?)\n---", _skill_text(), re.DOTALL)
    assert match, "SKILL.md missing YAML frontmatter"
    return yaml.safe_load(match.group(1))


def test_skill_exists_with_hermes_metadata() -> None:
    frontmatter = _frontmatter()

    assert SKILL_DIR.is_dir()
    assert frontmatter["name"] == "thermo-nuclear-code-quality-review"
    assert len(frontmatter["description"]) <= 60
    assert frontmatter["description"].endswith(".")
    assert frontmatter["platforms"] == ["linux", "macos", "windows"]
    assert set(frontmatter["metadata"]["hermes"]["tags"]) >= {
        "code-review",
        "maintainability",
        "architecture",
    }
    assert "related_skills" in frontmatter["metadata"]["hermes"]


def test_skill_is_standalone_and_cursor_metadata_removed() -> None:
    text = _skill_text()
    frontmatter = _frontmatter()

    assert "cursor-team-kit/skills/thermo-nuclear-code-quality-review/SKILL.md" in text
    assert "disable-model-invocation" not in frontmatter
    assert "thermonuclear review" in text
    assert "deep code quality audit" in text
    assert "harsh maintainability review" in text


def test_generic_review_does_not_trigger_thermonuclear_mode() -> None:
    text = _skill_text()

    assert "Do not use this skill automatically" in text
    assert "generic \"review\"" in text
    assert "Generic code review stays in the" in text
    assert "normal review skills" in text
