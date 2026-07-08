"""Tests for flag-gated adaptive skill-index demotion."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.prompt_builder import (
    clear_skills_system_prompt_cache,
    build_skills_system_prompt,
    get_last_skills_index_report,
)
from agent.system_prompt import build_system_prompt_parts


NOTICE = (
    "Note: some categories above are listed name-only to save context; "
    "skills_list shows full descriptions and skill_view(name) loads any skill."
)


@pytest.fixture(autouse=True)
def _clear_skills_cache():
    clear_skills_system_prompt_cache(clear_snapshot=True)
    yield
    clear_skills_system_prompt_cache(clear_snapshot=True)


def _seed_skill(home, category, name, description):
    skill_dir = home / "skills"
    for part in category.split("/"):
        skill_dir /= part
    skill_dir /= name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n",
        encoding="utf-8",
    )
    return skill_dir


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names={"skills_list", "skill_view"},
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="cli",
        pass_session_id=False,
        session_id="",
        session_role="operator",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _stable_prompt(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
        patch("run_agent.get_toolset_for_tool", return_value="skills"),
    ):
        return build_system_prompt_parts(agent)["stable"]


def test_compact_category_renders_names_only_and_keeps_other_categories(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "skills" / "media").mkdir(parents=True)
    (tmp_path / "skills" / "media" / "DESCRIPTION.md").write_text(
        "---\ndescription: Media skills\n---\n",
        encoding="utf-8",
    )
    _seed_skill(tmp_path, "media", "podcast-edit", "Edit podcast audio")
    _seed_skill(tmp_path, "media/images", "thumbnail-maker", "Make thumbnails")
    _seed_skill(tmp_path, "software-development", "tdd", "Test-driven workflow")

    result = build_skills_system_prompt(compact_categories=frozenset({"media"}))

    assert "  media: Media skills" in result
    assert "    - names: podcast-edit" in result
    assert "  media/images:" in result
    assert "    - names: thumbnail-maker" in result
    assert "Edit podcast audio" not in result
    assert "Make thumbnails" not in result
    assert "tdd: Test-driven workflow" in result
    for skill_name in ("podcast-edit", "thumbnail-maker"):
        assert skill_name in result
    assert result.index("</available_skills>") < result.index(NOTICE)
    assert result.index(NOTICE) < result.index("Proceed without loading")


def test_notice_only_appears_when_category_was_demoted(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_skill(tmp_path, "software-development", "tdd", "Test-driven workflow")

    result = build_skills_system_prompt(compact_categories=frozenset({"media"}))

    assert NOTICE not in result
    assert "tdd: Test-driven workflow" in result


def test_cache_key_isolated_by_compact_category_set(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_skill(
        tmp_path,
        "media",
        "podcast-edit",
        "Edit podcast audio with a detailed workflow, tool checklist, "
        "publishing notes, cleanup rules, verification steps, export presets, "
        "handoff checks, transcript handling, metadata review, loudness targets, "
        "thumbnail coordination, release-channel routing, and rollback notes.",
    )

    full = build_skills_system_prompt(compact_categories=frozenset())
    compact = build_skills_system_prompt(compact_categories=frozenset({"media"}))
    full_again = build_skills_system_prompt(compact_categories=frozenset())

    assert "podcast-edit: Edit podcast audio" in full
    assert "podcast-edit: Edit podcast audio" in full_again
    assert "    - names: podcast-edit" in compact
    assert "podcast-edit: Edit podcast audio" not in compact


def test_last_index_report_reflects_rendered_tiers_and_bytes(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in ("podcast-edit", "clip-master", "audio-cleanup", "show-notes"):
        _seed_skill(
            tmp_path,
            "media",
            name,
            "Edit podcast audio with a detailed workflow, tool checklist, "
            "publishing notes, cleanup rules, verification steps, export presets, "
            "handoff checks, transcript handling, metadata review, loudness targets, "
            "thumbnail coordination, release-channel routing, and rollback notes.",
        )
    _seed_skill(tmp_path, "software-development", "tdd", "Test-driven workflow")

    compact = build_skills_system_prompt(compact_categories=frozenset({"media"}))
    report = get_last_skills_index_report()

    assert report["mode"] == "compact"
    assert report["compact_categories"] == ["media"]
    assert report["categories"]["media"] == {"skills": 4, "tier": "names-only"}
    assert report["categories"]["software-development"] == {"skills": 1, "tier": "full"}
    assert report["bytes_rendered"] == len(compact.encode("utf-8"))
    assert report["bytes_full_equivalent"] > report["bytes_rendered"]

    build_skills_system_prompt(compact_categories=frozenset())
    full_report = get_last_skills_index_report()
    assert full_report["mode"] == "full"
    assert full_report["categories"]["media"]["tier"] == "full"


def test_worker_demotes_by_default_and_workers_full_opts_out(
    monkeypatch,
    tmp_path,
):
    # DEFAULT_CONFIG ships skills.index.workers = "focus": a worker child
    # agent gets the names-only index with NO user config present, and
    # "workers: full" restores the operator index for workers.
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _seed_skill(tmp_path, "media", "podcast-edit", "Edit podcast audio")

    worker_stable = _stable_prompt(_make_agent(session_role="worker"))
    assert "    - names: podcast-edit" in worker_stable
    assert "podcast-edit: Edit podcast audio" not in worker_stable

    (tmp_path / "config.yaml").write_text(
        "skills:\n  index:\n    workers: full\n",
        encoding="utf-8",
    )
    clear_skills_system_prompt_cache(clear_snapshot=True)
    worker_full = _stable_prompt(_make_agent(session_role="worker"))
    assert "podcast-edit: Edit podcast audio" in worker_full
    assert "    - names: podcast-edit" not in worker_full


def test_system_prompt_worker_focus_demotes_default_operator_does_not(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "skills:\n  index:\n    workers: focus\n",
        encoding="utf-8",
    )
    _seed_skill(tmp_path, "media", "podcast-edit", "Edit podcast audio")

    operator = _make_agent(session_role="operator")
    worker = _make_agent(session_role="worker")

    operator_stable = _stable_prompt(operator)
    worker_stable = _stable_prompt(worker)

    assert "podcast-edit: Edit podcast audio" in operator_stable
    assert "    - names: podcast-edit" not in operator_stable
    assert "    - names: podcast-edit" in worker_stable
    assert "podcast-edit: Edit podcast audio" not in worker_stable


def test_skills_index_full_env_restores_full_for_operator_and_worker(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HERMES_SKILLS_INDEX", "full")
    (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (tmp_path / "config.yaml").write_text(
        "agent:\n  coding_context: focus\n"
        "skills:\n  index:\n    workers: focus\n",
        encoding="utf-8",
    )
    _seed_skill(tmp_path, "media", "podcast-edit", "Edit podcast audio")

    for role in ("operator", "worker"):
        stable = _stable_prompt(_make_agent(session_role=role))
        assert "podcast-edit: Edit podcast audio" in stable
        assert "    - names: podcast-edit" not in stable
