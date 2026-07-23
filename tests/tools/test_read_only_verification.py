import json
import subprocess
from pathlib import Path

import pytest

from tools.read_only_command_policy import read_only_terminal_check
from tools.read_only_verification_tool import (
    parse_read_only_verification_command,
    read_only_verify,
)


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q; touch escaped",
        "pytest -q | tee result.txt",
        "python -m pytest ../../outside",
        "env FLAG=1 pytest -q",
        "bash -c 'pytest -q'",
        "git status",
    ],
)
def test_read_only_verification_parser_rejects_shell_and_unbounded_commands(command):
    argv, error = parse_read_only_verification_command(command)
    assert argv is None
    assert error


def test_read_only_terminal_policy_is_shell_free_and_process_only():
    assert read_only_terminal_check({"command": "ps aux"}) is True
    assert read_only_terminal_check({"command": "pwd"}) is True
    assert read_only_terminal_check({"command": "git status"}) is not True
    assert read_only_terminal_check({"command": "date --set tomorrow"}) is not True
    assert read_only_terminal_check({"command": "ps aux | tee pids.txt"}) is not True
    assert read_only_terminal_check({"command": "touch source.txt"}) is not True


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def test_read_only_verify_uses_disposable_snapshot_and_cleans_artifacts(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Hermes Tests")
    (repo / "test_snapshot.py").write_text(
        "import os\n"
        "from pathlib import Path\n\n"
        "def test_snapshot_is_writable():\n"
        "    Path('artifact.txt').write_text('temporary', encoding='utf-8')\n"
        "    assert Path('artifact.txt').read_text(encoding='utf-8') == 'temporary'\n"
        "    assert os.getenv('AWS_ACCESS_KEY_ID') is None\n"
        "    assert os.getenv('FAL_KEY') is None\n"
        "    assert os.getenv('GOOGLE_APPLICATION_CREDENTIALS') is None\n"
        "    assert os.getenv('DATABASE_URL') is None\n"
        "    assert os.getenv('SAFE_BUILD_FLAG') == 'visible'\n",
        encoding="utf-8",
    )
    _git(repo, "add", "test_snapshot.py")
    _git(repo, "commit", "-m", "test fixture")
    before = _git(repo, "status", "--porcelain=v1").stdout
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "aws-secret")
    monkeypatch.setenv("FAL_KEY", "fal-secret")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/secret/credentials.json")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:password@example.invalid/db")
    monkeypatch.setenv("SAFE_BUILD_FLAG", "visible")

    payload = json.loads(
        read_only_verify(
            command="python -m pytest -q",
            workdir=str(repo),
            timeout=60,
            runtime_mode="read_only",
        )
    )

    assert payload["success"] is True, payload
    assert payload["artifacts_cleaned"] is True
    assert "1 passed" in payload["output"]
    assert not (repo / "artifact.txt").exists()
    assert _git(repo, "status", "--porcelain=v1").stdout == before
