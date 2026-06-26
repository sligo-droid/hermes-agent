from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skills" / "creative" / "taste-skill" / "SKILL.md"


def _frontmatter_and_body() -> tuple[dict[str, str], str]:
    text = SKILL.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    _, frontmatter, body = text.split("---", 2)
    values = {}
    for raw_line in frontmatter.splitlines():
        if ":" not in raw_line:
            continue
        key, value = raw_line.split(":", 1)
        values[key.strip()] = value.strip()
    return values, body


def test_taste_skill_metadata_and_source_grounding():
    frontmatter, body = _frontmatter_and_body()

    assert frontmatter["name"] == "taste-skill"
    description = frontmatter["description"]
    assert description.endswith(".")
    assert len(description) <= 60
    assert "https://www.tasteskill.dev/" in body
    assert "does not claim any unavailable Taste Skill tooling" in body
    assert "ui_visual_specialist" in body


def test_taste_skill_uses_modern_body_order():
    _, body = _frontmatter_and_body()
    headings = [
        "## When to Use",
        "## Prerequisites",
        "## How to Run",
        "## Quick Reference",
        "## Procedure",
        "## Pitfalls",
        "## Verification",
    ]
    positions = [body.index(heading) for heading in headings]

    assert positions == sorted(positions)


def test_taste_skill_points_to_related_ui_skills():
    _, body = _frontmatter_and_body()

    assert "`claude-design`" in body
    assert "`popular-web-designs`" in body
    assert "anti-slop quality gate" in body
