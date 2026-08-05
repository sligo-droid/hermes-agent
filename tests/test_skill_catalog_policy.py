import json

from skill_catalog_policy import (
    atomic_write_skills_index,
    filter_skill_records,
    sanitize_skills_index,
)


def test_filter_targets_skill_records_not_unrelated_text():
    retired_name = "obsi" + "dian"
    records = [
        {"name": "notes", "description": "Plain Markdown"},
        {"name": "retired", "extra": {"integration": retired_name}},
    ]

    assert filter_skill_records(records) == [records[0]]
    assert filter_skill_records([{"author": "Obsidian Blackbird"}]) == [
        {"author": "Obsidian Blackbird"}
    ]
    assert filter_skill_records([{"description": "black obsidian geology"}]) == [
        {"description": "black obsidian geology"}
    ]


def test_sanitize_fails_closed_for_malformed_index():
    assert sanitize_skills_index(None) is None
    assert sanitize_skills_index({"skills": "not-a-list"}) is None


def test_atomic_write_replaces_existing_index(tmp_path):
    destination = tmp_path / "api" / "skills-index.json"
    destination.parent.mkdir()
    destination.write_text("old")

    atomic_write_skills_index(destination, {"skills": [], "skill_count": 0})

    assert json.loads(destination.read_text()) == {"skills": [], "skill_count": 0}
    assert list(destination.parent.glob(".*.tmp")) == []
