"""Tests for external skill directories (skills.external_dirs config)."""

import json
import os
from unittest.mock import patch

import pytest


@pytest.fixture
def external_skills_dir(tmp_path):
    """Create a temp dir with a sample external skill."""
    ext_dir = tmp_path / "external-skills"
    skill_dir = ext_dir / "my-external-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-external-skill\ndescription: A skill from an external directory\n---\n\n# My External Skill\n\nDo external things.\n"
    )
    return ext_dir


@pytest.fixture
def hermes_home(tmp_path):
    """Create a minimal HERMES_HOME with config."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "skills").mkdir()
    return home


class TestGetExternalSkillsDirs:
    def test_empty_config(self, hermes_home):
        (hermes_home / "config.yaml").write_text("skills:\n  external_dirs: []\n")
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_utils import get_external_skills_dirs
            result = get_external_skills_dirs()
        assert result == []


    def test_valid_dir_returned(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_utils import get_external_skills_dirs
            result = get_external_skills_dirs()
        assert len(result) == 1
        assert result[0] == external_skills_dir.resolve()






class TestGetAllSkillsDirs:
    def test_local_always_first(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            from agent.skill_utils import get_all_skills_dirs
            result = get_all_skills_dirs()
        assert result[0] == hermes_home / "skills"
        assert result[1] == external_skills_dir.resolve()

    def test_inherited_dirs_follow_external_dirs(self, hermes_home, external_skills_dir, tmp_path):
        inherited = tmp_path / "root-skills"
        inherited.mkdir()
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        with patch.dict(
            os.environ,
            {
                "HERMES_HOME": str(hermes_home),
                "HERMES_INHERITED_SKILLS_DIRS": str(inherited),
            },
        ):
            from agent.skill_utils import get_all_skills_dirs
            result = get_all_skills_dirs()
        assert result == [
            hermes_home / "skills",
            external_skills_dir.resolve(),
            inherited.resolve(),
        ]


class TestExternalSkillsInFindAll:
    def test_external_skills_found(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        local_skills = hermes_home / "skills"
        with (
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import _find_all_skills
            skills = _find_all_skills()
        names = [s["name"] for s in skills]
        assert "my-external-skill" in names

    def test_local_external_collision_is_not_advertised(
        self, hermes_home, external_skills_dir
    ):
        """Primary-root duplicates are ambiguous and omitted from offers."""
        local_skills = hermes_home / "skills"
        local_skill = local_skills / "my-external-skill"
        local_skill.mkdir(parents=True)
        (local_skill / "SKILL.md").write_text(
            "---\nname: my-external-skill\ndescription: Local version\n---\n\nLocal.\n"
        )
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        with (
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import _find_all_skills
            skills = _find_all_skills()
        assert not [s for s in skills if s["name"] == "my-external-skill"]

    def test_inherited_skills_found_as_fallback(self, hermes_home, tmp_path):
        inherited = tmp_path / "root-skills"
        inherited_skill = inherited / "software-development" / "test-driven-development"
        inherited_skill.mkdir(parents=True)
        (inherited_skill / "SKILL.md").write_text(
            "---\n"
            "name: test-driven-development\n"
            "description: Root TDD workflow\n"
            "---\n\n"
            "Root skill.\n"
        )
        local_skills = hermes_home / "skills"
        with (
            patch.dict(
                os.environ,
                {
                    "HERMES_HOME": str(hermes_home),
                    "HERMES_INHERITED_SKILLS_DIRS": str(inherited),
                },
            ),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import _find_all_skills
            skills = _find_all_skills()
        matching = [s for s in skills if s["name"] == "test-driven-development"]
        assert len(matching) == 1
        assert matching[0]["description"] == "Root TDD workflow"

    def test_local_skills_override_inherited(self, hermes_home, tmp_path):
        inherited = tmp_path / "root-skills"
        inherited_skill = inherited / "general-coding"
        inherited_skill.mkdir(parents=True)
        (inherited_skill / "SKILL.md").write_text(
            "---\nname: general-coding\ndescription: Root version\n---\n\nRoot.\n"
        )
        local_skills = hermes_home / "skills"
        local_skill = local_skills / "general-coding"
        local_skill.mkdir(parents=True)
        (local_skill / "SKILL.md").write_text(
            "---\nname: general-coding\ndescription: Local version\n---\n\nLocal.\n"
        )
        with (
            patch.dict(
                os.environ,
                {
                    "HERMES_HOME": str(hermes_home),
                    "HERMES_INHERITED_SKILLS_DIRS": str(inherited),
                },
            ),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import _find_all_skills
            skills = _find_all_skills()
        matching = [s for s in skills if s["name"] == "general-coding"]
        assert len(matching) == 1
        assert matching[0]["description"] == "Local version"


class TestExternalSkillView:
    def test_skill_view_finds_external(self, hermes_home, external_skills_dir):
        (hermes_home / "config.yaml").write_text(
            f"skills:\n  external_dirs:\n    - {external_skills_dir}\n"
        )
        local_skills = hermes_home / "skills"
        with (
            patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import skill_view
            result = json.loads(skill_view("my-external-skill"))
        assert result["success"] is True
        assert "external things" in result["content"]

    def test_skill_view_finds_inherited_when_missing_locally(self, hermes_home, tmp_path):
        inherited = tmp_path / "root-skills"
        inherited_skill = inherited / "general-coding"
        inherited_skill.mkdir(parents=True)
        (inherited_skill / "SKILL.md").write_text(
            "---\nname: general-coding\ndescription: Root coding rules\n---\n\nRoot AGENTS content.\n"
        )
        local_skills = hermes_home / "skills"
        with (
            patch.dict(
                os.environ,
                {
                    "HERMES_HOME": str(hermes_home),
                    "HERMES_INHERITED_SKILLS_DIRS": str(inherited),
                },
            ),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import skill_view
            result = json.loads(skill_view("general-coding"))
        assert result["success"] is True
        assert "Root AGENTS content" in result["content"]

    def test_skill_view_prefers_local_over_inherited_duplicate(self, hermes_home, tmp_path):
        inherited = tmp_path / "root-skills"
        inherited_skill = inherited / "general-coding"
        inherited_skill.mkdir(parents=True)
        (inherited_skill / "SKILL.md").write_text(
            "---\nname: general-coding\ndescription: Root coding rules\n---\n\nRoot.\n"
        )
        local_skills = hermes_home / "skills"
        local_skill = local_skills / "general-coding"
        local_skill.mkdir(parents=True)
        (local_skill / "SKILL.md").write_text(
            "---\nname: general-coding\ndescription: Local coding rules\n---\n\nLocal.\n"
        )
        with (
            patch.dict(
                os.environ,
                {
                    "HERMES_HOME": str(hermes_home),
                    "HERMES_INHERITED_SKILLS_DIRS": str(inherited),
                },
            ),
            patch("tools.skills_tool.SKILLS_DIR", local_skills),
        ):
            from tools.skills_tool import skill_view
            result = json.loads(skill_view("general-coding"))
        assert result["success"] is True
        assert "Local." in result["content"]
        assert "Root." not in result["content"]
