import json
from pathlib import Path

from skill_catalog_policy import (
    atomic_write_skills_index,
    filter_skill_records,
    is_retired_skill_record,
    is_retired_skill_text,
    sanitize_skills_index,
)


def test_filter_targets_skill_records_not_unrelated_text():
    retired_name = "obsi" + "dian"
    records = [
        {"name": "notes", "description": "Plain Markdown"},
        {"name": "retired", "extra": {"integration": retired_name}},
    ]

    assert filter_skill_records(records) == [records[0]]
    assert filter_skill_records([{"name": "markdown", "author": "Obsidian Blackbird"}]) == [
        {"name": "markdown", "author": "Obsidian Blackbird"}
    ]
    assert filter_skill_records([{"name": "geology", "description": "black obsidian geology"}]) == [
        {"name": "geology", "description": "black obsidian geology"}
    ]


def test_description_only_capabilities_are_rejected_without_name_denylist():
    records = [
        {"name": "daily-recap", "description": "Save the daily recap to Obsidian."},
        {"name": "auto-research", "description": "Research sources and publish briefings in Obsidian."},
        {"name": "cross-platform-memory-hub", "description": "Unified Obsidian-based memory for coding agents."},
        {"name": "knowledge-importer", "description": "Convert documents and save them to an Obsidian knowledge base."},
        {"name": "vault-sync-engine", "description": "Synchronize an Obsidian vault between devices."},
        {"name": "smart-auto-note", "description": "Automatically classify and write Obsidian notes."},
    ]

    assert filter_skill_records(records) == []


def test_attribution_and_geological_controls_remain_allowed():
    contributor = {
        "name": "plain-markdown",
        "description": "Write portable Markdown.",
        "author": "Obsidian Blackbird",
    }
    geology = {
        "name": "volcanic-rock-field-guide",
        "description": "Identify obsidian volcanic glass and other igneous rocks.",
    }

    assert not is_retired_skill_record(contributor)
    assert not is_retired_skill_record(geology)
    assert is_retired_skill_record({
        "name": "plain-markdown",
        "author": "Obsidian Blackbird; use Obsidian vault sync",
    })


def test_ambiguous_or_malformed_records_fail_closed():
    assert is_retired_skill_record("not-a-record")
    assert is_retired_skill_record({})
    assert is_retired_skill_record({"description": "Obsidian compatible"})
    assert is_retired_skill_record({"author": "Obsidian Blackbird", "repo": "org/obsidian-tools"})
    assert is_retired_skill_record({"name": "clean", "metadata": {"obsidian": False}})
    assert is_retired_skill_record({
        "name": "clean",
        "contributors": [{"name": "Obsidian Blackbird", "repo": "org/obsidian-tools"}],
    })


def test_bounded_text_policy_allows_only_attribution_or_natural_meaning():
    assert not is_retired_skill_text("Author: Obsidian Blackbird")
    assert not is_retired_skill_text("A guide to obsidian volcanic glass and igneous rock.")
    assert is_retired_skill_text("Save notes to Obsidian.")
    assert is_retired_skill_text("Author: Obsidian Blackbird\nRepository: org/obsidian-tools")
    assert is_retired_skill_text("Author: Obsidian Blackbird; use Obsidian vault sync")


def test_sanitize_fails_closed_for_malformed_index():
    assert sanitize_skills_index(None) is None
    assert sanitize_skills_index({"skills": "not-a-list"}) is None


def test_committed_catalog_fixture_filters_every_installable_capability_record():
    snapshot = json.loads(
        (Path(__file__).resolve().parent / "fixtures/skills-index-policy.json").read_text()
    )
    sanitized = sanitize_skills_index(snapshot)
    assert sanitized is not None
    assert [record["name"] for record in sanitized["skills"]] == [
        "plain-markdown",
        "volcanic-rock-field-guide",
    ]
    assert all(not is_retired_skill_record(record) for record in sanitized["skills"])
    assert all(
        "obsidian" not in json.dumps(record, ensure_ascii=False).lower()
        or record.get("author") == "Obsidian Blackbird"
        or any(
            term in str(record.get("description", "")).lower()
            for term in ("geolog", "igneous", "volcanic glass")
        )
        for record in sanitized["skills"]
    )


def test_atomic_write_replaces_existing_index(tmp_path):
    destination = tmp_path / "api" / "skills-index.json"
    destination.parent.mkdir()
    destination.write_text("old")

    atomic_write_skills_index(destination, {"skills": [], "skill_count": 0})

    assert json.loads(destination.read_text()) == {"skills": [], "skill_count": 0}
    assert list(destination.parent.glob(".*.tmp")) == []
