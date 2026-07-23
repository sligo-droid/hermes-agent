import json
import os
import subprocess
from pathlib import Path

import pytest

import tools.read_only_verification_tool as verification_tool
from tools.read_only_command_policy import read_only_terminal_check
from tools.read_only_verification_tool import (
    _OUTPUT_LIMIT,
    _prepared_dependency_roots,
    _verification_environment,
    parse_read_only_verification_command,
    read_only_verify,
)
from tools.git_inspection_tool import git_inspect


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
    assert read_only_terminal_check({"command": "ps -eo pid,ppid,stat,etime,comm,args"}) is True
    assert read_only_terminal_check({"command": "pgrep -af python"}) is True
    assert read_only_terminal_check({"command": "df -h ."}) is True
    assert read_only_terminal_check({"command": "uname -a"}) is True
    assert read_only_terminal_check({"command": "pwd"}) is True
    assert read_only_terminal_check({"command": "git status"}) is not True
    assert read_only_terminal_check({"command": "date --set tomorrow"}) is not True
    assert read_only_terminal_check({"command": "ps aux | tee pids.txt"}) is not True
    assert read_only_terminal_check({"command": "touch source.txt"}) is not True


@pytest.mark.parametrize(
    "command",
    [
        "pgrep --signal KILL python",
        "pgrep --delimiter , python",
        "ps e",
        "ps auxe",
        "ps -eo pid,environ",
        "ps --format pid,env",
        "df --sync",
        "df --output=source,target",
        "uname --version",
        "ps $USER",
        "ps ${USER}",
        "./ps aux",
        "bin/df -h .",
        "kill 123",
    ],
)
def test_read_only_terminal_policy_rejects_mutation_environment_and_output_escape(command):
    assert read_only_terminal_check({"command": command}) is not True


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _init_repo(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Hermes Tests")


def test_read_only_verify_uses_disposable_snapshot_and_cleans_artifacts(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    _init_repo(repo)
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
        "    assert os.getenv('SAFE_BUILD_FLAG') is None\n"
        "    assert os.getenv('HERMES_TEST_WORKERS') == '1'\n",
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
    monkeypatch.setenv("HERMES_TEST_WORKERS", "1")

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
    assert payload["dependencies"]
    assert "1 passed" in payload["output"]
    assert not (repo / "artifact.txt").exists()
    assert _git(repo, "status", "--porcelain=v1").stdout == before


def test_verification_environment_is_allowlist_not_redaction(monkeypatch):
    monkeypatch.setenv("SAFE_BUILD_FLAG", "visible")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "secret")
    monkeypatch.setenv("HERMES_TEST_WORKERS", "2")

    env = _verification_environment()

    assert env["HERMES_TEST_WORKERS"] == "2"
    assert "SAFE_BUILD_FLAG" not in env
    assert "AWS_ACCESS_KEY_ID" not in env
    assert set(env) <= {
        "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM", "COLORTERM",
        "NO_COLOR", "FORCE_COLOR", "PYTHONHASHSEED", "HERMES_TEST_WORKERS",
        "PATH", "HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
        "PYTHONDONTWRITEBYTECODE", "CI", "GIT_CONFIG_NOSYSTEM",
        "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT",
    }


def test_read_only_verify_neutralizes_escaping_symlink_and_hides_host_paths(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    sentinel = tmp_path / "host-sentinel.txt"
    sentinel.write_text("host secret", encoding="utf-8")
    (repo / "escape").symlink_to(sentinel)
    (repo / "test_isolation.py").write_text(
        "from pathlib import Path\n\n"
        "def test_isolated():\n"
        "    assert 'neutralized a symlink' in Path('escape').read_text()\n"
        f"    assert not Path({str(sentinel)!r}).exists()\n"
        "    assert not Path('/run/hermes-gateway.sock').exists()\n",
        encoding="utf-8",
    )
    _git(repo, "add", "escape", "test_isolation.py")
    _git(repo, "commit", "-m", "isolation fixture")

    payload = json.loads(
        read_only_verify(
            command="python -m pytest -q",
            workdir=str(repo),
            timeout=60,
            runtime_mode="read_only",
        )
    )

    assert payload["success"] is True, payload
    assert payload["neutralized_symlinks"] == ["escape"]
    assert sentinel.read_text(encoding="utf-8") == "host secret"


def test_prepared_dependencies_include_python_and_node_for_linked_worktree():
    root = Path(__file__).resolve().parents[2]
    dependencies = _prepared_dependency_roots(root)

    assert (dependencies["venv"] / "bin" / "python").is_file()
    assert dependencies["node_modules"].is_dir()
    assert dependencies["ui_node_modules"].is_dir()
    assert (dependencies["pnpm_runtime"] / "bin" / "pnpm.cjs").is_file()


def test_read_only_verify_runs_repository_test_wrapper(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    scripts = repo / "scripts"
    scripts.mkdir()
    wrapper = scripts / "run_tests.sh"
    wrapper.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nexec .venv/bin/python -m pytest \"$@\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    (repo / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", "scripts/run_tests.sh", "test_ok.py")
    _git(repo, "commit", "-m", "wrapper fixture")

    payload = json.loads(
        read_only_verify(
            command="scripts/run_tests.sh -q test_ok.py",
            workdir=str(repo),
            timeout=60,
            runtime_mode="read_only",
        )
    )

    assert payload["success"] is True, payload
    assert "1 passed" in payload["output"]


def test_read_only_verify_runs_pnpm_test_with_read_only_node_modules(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "package.json").write_text(
        json.dumps(
            {
                "private": True,
                "scripts": {"test": "vitest run smoke.test.js"},
            }
        ),
        encoding="utf-8",
    )
    (repo / "smoke.test.js").write_text(
        "import { expect, test } from 'vitest';\n"
        "test('sandboxed pnpm', () => expect(2 + 2).toBe(4));\n",
        encoding="utf-8",
    )
    _git(repo, "add", "package.json", "smoke.test.js")
    _git(repo, "commit", "-m", "pnpm fixture")

    payload = json.loads(
        read_only_verify(
            command="pnpm test",
            workdir=str(repo),
            timeout=90,
            runtime_mode="read_only",
        )
    )

    assert payload["success"] is True, payload
    assert "node_modules" in payload["dependencies"]
    assert "pnpm_runtime" in payload["dependencies"]
    assert "1 passed" in payload["output"]


def test_read_only_verify_bounds_output(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "test_output.py").write_text(
        "def test_output():\n"
        "    print('x' * 250000)\n"
        "    assert True\n",
        encoding="utf-8",
    )
    _git(repo, "add", "test_output.py")
    _git(repo, "commit", "-m", "output fixture")

    payload = json.loads(
        read_only_verify(
            command="python -m pytest -q -s test_output.py",
            workdir=str(repo),
            timeout=60,
            runtime_mode="read_only",
        )
    )

    assert payload["success"] is True, payload
    assert payload["output_truncated"] is True
    assert len(payload["output"]) <= _OUTPUT_LIMIT


def test_read_only_verify_timeout_cleans_snapshot(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "test_slow.py").write_text(
        "import time\n\n"
        "def test_slow():\n"
        "    time.sleep(10)\n",
        encoding="utf-8",
    )
    _git(repo, "add", "test_slow.py")
    _git(repo, "commit", "-m", "timeout fixture")
    real_temporary_directory = verification_tool.tempfile.TemporaryDirectory
    created_snapshots = []

    def tracked_temporary_directory(*args, **kwargs):
        kwargs["dir"] = tmp_path
        directory = real_temporary_directory(*args, **kwargs)
        created_snapshots.append(Path(directory.name))
        return directory

    monkeypatch.setattr(
        verification_tool.tempfile,
        "TemporaryDirectory",
        tracked_temporary_directory,
    )

    payload = json.loads(
        read_only_verify(
            command="python -m pytest -q test_slow.py",
            workdir=str(repo),
            timeout=1,
            runtime_mode="read_only",
        )
    )

    assert "exceeded the 1s timeout" in payload["error"]
    assert created_snapshots
    assert all(not path.exists() for path in created_snapshots)


def test_git_inspect_exposes_bounded_status_and_rejects_path_escape(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    (repo / "tracked.txt").write_text("two\n", encoding="utf-8")

    payload = json.loads(
        git_inspect(
            operation="status",
            workdir=str(repo),
            runtime_mode="read_only",
        )
    )
    denied = json.loads(
        git_inspect(
            operation="diff",
            paths=["../outside"],
            workdir=str(repo),
            runtime_mode="read_only",
        )
    )

    assert payload["success"] is True
    assert "tracked.txt" in payload["output"]
    assert "inside the repository" in denied["error"]


def test_git_inspect_bounds_large_diff_output(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracked = repo / "tracked.txt"
    tracked.write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")
    tracked.write_text("x" * 250_000 + "\n", encoding="utf-8")

    payload = json.loads(
        git_inspect(
            operation="diff",
            workdir=str(repo),
            runtime_mode="read_only",
        )
    )

    assert payload["success"] is True
    assert payload["output_truncated"] is True
    assert len(payload["output"]) <= 100_000
