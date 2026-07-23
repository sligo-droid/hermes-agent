import os
import stat
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SKILL_PATH = REPO_ROOT / ".agents/skills/autoreview/SKILL.md"
HELPER_PATH = REPO_ROOT / ".agents/skills/autoreview/scripts/autoreview"


def test_repo_owns_autoreview_skill_and_executable_helper():
    assert SKILL_PATH.is_file()
    assert HELPER_PATH.is_file()
    assert stat.S_IMODE(HELPER_PATH.stat().st_mode) & stat.S_IXUSR
    assert os.access(HELPER_PATH, os.X_OK)


def test_repo_autoreview_files_match_packaged_fallbacks():
    from hermes_cli.worker_autoreview import _HELPER_TEXT, _SKILL_TEXT

    assert HELPER_PATH.read_text(encoding="utf-8") == _HELPER_TEXT
    assert SKILL_PATH.read_text(encoding="utf-8") == _SKILL_TEXT


def test_repo_autoreview_helper_runs_in_git_worktree(tmp_path):
    subprocess.run(
        ["git", "init"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    result = subprocess.run(
        [str(HELPER_PATH), "--mode", "local"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "status: advisory_not_model_review" in result.stdout
    assert "branch:" in result.stdout
    assert "closeout:" in result.stdout
