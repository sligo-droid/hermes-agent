import json
import os
import subprocess
from pathlib import Path

import pytest

import tools.read_only_verification_tool as verification_tool
from tools.read_only_command_policy import read_only_terminal_check
from tools.read_only_verification_tool import (
    _MEMORY_LIMIT_BYTES,
    _OUTPUT_LIMIT,
    _TASK_LIMIT,
    _pnpm_cache_roots,
    _pnpm_package_manager_pin,
    _prepared_pnpm_runtime,
    _with_verification_cgroup,
    _prepared_dependency_roots,
    _verification_environment,
    parse_read_only_verification_command,
    read_only_verify,
)
from tools.git_inspection_tool import git_inspect


@pytest.fixture(autouse=True)
def _observation_workspace_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "tools.observation_workspace._active_workspace_root",
        lambda _task_id="default": tmp_path.resolve(),
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


def test_read_only_verification_parser_allows_git_diff_check():
    argv, error = parse_read_only_verification_command("git diff --check")

    assert argv == ["git", "diff", "--check"]
    assert error is None


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


def test_verification_cgroup_wrapper_sets_aggregate_memory_and_task_limits(monkeypatch):
    monkeypatch.setattr(verification_tool.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        verification_tool,
        "_cgroup_v2_limiter_available",
        lambda probe=True: True,
    )

    argv = _with_verification_cgroup(
        ["/usr/bin/true"],
        command_env={"HOME": "/tmp/home", "CI": "1"},
    )

    assert argv[:5] == [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
    ]
    assert f"MemoryMax={_MEMORY_LIMIT_BYTES}" in argv
    assert f"TasksMax={_TASK_LIMIT}" in argv
    assert "/usr/bin/env" in argv
    assert "HOME=/tmp/home" in argv
    assert "CI=1" in argv
    assert argv[-1] == "/usr/bin/true"


def test_read_only_verify_fails_closed_without_cgroup_limiter(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    _git(repo, "add", "test_ok.py")
    _git(repo, "commit", "-m", "limiter fixture")
    monkeypatch.setattr(
        verification_tool,
        "_cgroup_v2_limiter_available",
        lambda probe=True: False,
    )

    payload = json.loads(
        read_only_verify(
            command="python -m pytest -q",
            workdir=str(repo),
            runtime_mode="read_only",
        )
    )

    assert "failed closed" in payload["error"]
    assert "cgroup v2" in payload["error"]


def test_read_only_verify_action_contract_reaches_observational_sandbox(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    monkeypatch.setattr(
        verification_tool,
        "_cgroup_v2_limiter_available",
        lambda probe=True: False,
    )

    payload = json.loads(
        verification_tool.registry.dispatch(
            "read_only_verify",
            {
                "command": "python -m pytest -q",
                "workdir": str(repo),
            },
            runtime_mode="action",
        )
    )

    assert "cgroup v2" in payload["error"]
    assert "available only" not in payload["error"]


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


def _available_cached_pnpm_version() -> str | None:
    for cache_root in _pnpm_cache_roots():
        pnpm_root = cache_root / "pnpm"
        if not pnpm_root.is_dir():
            continue
        for candidate in sorted(pnpm_root.iterdir()):
            if _prepared_pnpm_runtime(candidate.name) is not None:
                return candidate.name
    return None


def test_prepared_dependencies_include_python_and_exact_pnpm_runtime():
    root = Path(__file__).resolve().parents[2]
    version = _available_cached_pnpm_version()
    runtime = _prepared_pnpm_runtime(version) if version else None
    dependencies = _prepared_dependency_roots(
        root,
        source_cwd=root,
        pnpm_runtime=runtime,
    )

    assert (dependencies["venv"] / "bin" / "python").is_file()
    if runtime is not None:
        assert dependencies["pnpm_runtime"] == runtime


def test_pnpm_runtime_selection_uses_exact_repo_pin_not_newest_cache(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    package = repo / "packages" / "app"
    package.mkdir(parents=True)
    (repo / "package.json").write_text(
        json.dumps({"packageManager": "pnpm@8.15.7"}),
        encoding="utf-8",
    )
    corepack = tmp_path / "corepack"
    for version in ("8.15.7", "10.28.2"):
        runtime = corepack / "pnpm" / version
        (runtime / "bin").mkdir(parents=True)
        (runtime / "dist").mkdir()
        (runtime / ".corepack").write_text("{}", encoding="utf-8")
        (runtime / "bin" / "pnpm.cjs").write_text("", encoding="utf-8")
        (runtime / "dist" / "pnpm.cjs").write_text("", encoding="utf-8")
    monkeypatch.setenv("COREPACK_HOME", str(corepack))
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(verification_tool.shutil, "which", lambda _name: None)

    version, manifest, error = _pnpm_package_manager_pin(package, repo)

    assert error is None
    assert manifest == repo / "package.json"
    assert version == "8.15.7"
    assert _prepared_pnpm_runtime(version) == (corepack / "pnpm" / version).resolve()


def test_nested_package_without_dependencies_does_not_fall_back_to_hermes(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    dashboard = repo / "dashboard"
    dashboard.mkdir()
    (dashboard / "package.json").write_text("{}\n", encoding="utf-8")
    (dashboard / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\n",
        encoding="utf-8",
    )

    dependencies = _prepared_dependency_roots(
        repo,
        source_cwd=dashboard,
    )

    assert "node_modules" not in dependencies


def test_read_only_verify_fails_before_sandbox_when_exact_pnpm_pin_is_absent(
    tmp_path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "package.json").write_text(
        json.dumps({
            "packageManager": "pnpm@8.15.7",
            "scripts": {"test": "node -e \"process.exit(0)\""},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(verification_tool, "_prepared_pnpm_runtime", lambda _version: None)
    monkeypatch.setattr(
        verification_tool,
        "_cgroup_v2_limiter_available",
        lambda probe=True: (_ for _ in ()).throw(
            AssertionError("cgroup probe must not run before exact pnpm selection")
        ),
    )

    payload = json.loads(
        read_only_verify(
            command="pnpm test",
            workdir=str(repo),
            runtime_mode="read_only",
        )
    )

    assert "Exact Corepack pnpm runtime 8.15.7" in payload["error"]
    assert "refusing offline verification" in payload["error"]


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


def test_read_only_verify_mounts_nested_dependencies_and_writable_vite_caches(tmp_path):
    pnpm_version = _available_cached_pnpm_version()
    if pnpm_version is None:
        pytest.skip("no complete Corepack pnpm runtime is cached")
    repo = tmp_path / "repo"
    _init_repo(repo)
    dashboard = repo / "dashboard"
    dashboard.mkdir()
    (dashboard / "package.json").write_text(
        json.dumps(
            {
                "private": True,
                "packageManager": f"pnpm@{pnpm_version}",
                "type": "module",
                "scripts": {"test": "node verify-dependencies.mjs"},
            }
        ),
        encoding="utf-8",
    )
    (dashboard / "pnpm-lock.yaml").write_text(
        "lockfileVersion: '9.0'\n",
        encoding="utf-8",
    )
    (dashboard / "verify-dependencies.mjs").write_text(
        "import assert from 'node:assert/strict';\n"
        "import { mkdir, writeFile } from 'node:fs/promises';\n"
        "import marker from 'nested-fixture-dependency';\n"
        "assert.equal(marker, 'nested-module-resolved');\n"
        "assert.equal(process.argv[2], 'fixture-selector');\n"
        "for (const cache of ['.vite', '.vite-temp']) {\n"
        "  const directory = new URL(`./node_modules/${cache}/`, import.meta.url);\n"
        "  await mkdir(directory, { recursive: true });\n"
        "  await writeFile(new URL('sandbox-cache.txt', directory), cache);\n"
        "}\n"
        "let dependencyWriteBlocked = false;\n"
        "try {\n"
        "  const mutation = new URL(\n"
        "    './node_modules/nested-fixture-dependency/mutation.txt',\n"
        "    import.meta.url,\n"
        "  );\n"
        "  await writeFile(mutation, 'blocked');\n"
        "} catch (error) {\n"
        "  dependencyWriteBlocked = ['EACCES', 'EPERM', 'EROFS'].includes(error.code);\n"
        "}\n"
        "assert.equal(dependencyWriteBlocked, true);\n"
        "console.log('nested-dependency-and-vite-cache-ok');\n",
        encoding="utf-8",
    )
    modules = dashboard / "node_modules"
    package = (
        modules
        / ".pnpm"
        / "nested-fixture-dependency@1.0.0"
        / "node_modules"
        / "nested-fixture-dependency"
    )
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "nested-fixture-dependency",
                "version": "1.0.0",
                "type": "module",
                "exports": "./index.mjs",
            }
        ),
        encoding="utf-8",
    )
    (package / "index.mjs").write_text(
        "export default 'nested-module-resolved';\n",
        encoding="utf-8",
    )
    (modules / "nested-fixture-dependency").symlink_to(
        Path(".pnpm")
        / "nested-fixture-dependency@1.0.0"
        / "node_modules"
        / "nested-fixture-dependency",
        target_is_directory=True,
    )
    (modules / ".vite").mkdir()
    (modules / ".vite" / "host-sentinel.txt").write_text(
        "host dependency cache",
        encoding="utf-8",
    )
    _git(
        repo,
        "add",
        "dashboard/package.json",
        "dashboard/pnpm-lock.yaml",
        "dashboard/verify-dependencies.mjs",
    )
    _git(repo, "commit", "-m", "nested pnpm fixture")

    payload = json.loads(
        read_only_verify(
            command="pnpm test -- fixture-selector",
            workdir=str(dashboard),
            timeout=90,
            runtime_mode="read_only",
        )
    )

    assert payload["success"] is True, payload
    assert "node_modules:dashboard/node_modules" in payload["dependencies"]
    assert "pnpm_runtime" in payload["dependencies"]
    assert "nested-dependency-and-vite-cache-ok" in payload["output"]
    assert not (modules / ".vite-temp").exists()
    assert not (modules / ".vite" / "sandbox-cache.txt").exists()
    assert (modules / ".vite" / "host-sentinel.txt").read_text(encoding="utf-8") == (
        "host dependency cache"
    )
    assert not (package / "mutation.txt").exists()


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


def test_git_inspect_schema_and_action_runtime_support_explicit_bounded_workdir(tmp_path):
    from tools.git_inspection_tool import GIT_INSPECT_SCHEMA

    workspace = tmp_path / "workspace"
    repo = workspace / "project"
    workspace.mkdir()
    _init_repo(repo)
    (repo / "tracked.txt").write_text("one\n", encoding="utf-8")
    _git(repo, "add", "tracked.txt")
    _git(repo, "commit", "-m", "initial")

    payload = json.loads(
        verification_tool.registry.dispatch(
            "git_inspect",
            {
                "operation": "status",
                "workdir": "workspace/project",
            },
            runtime_mode="action",
        )
    )

    assert "workdir" in GIT_INSPECT_SCHEMA["parameters"]["properties"]
    assert payload["success"] is True


def test_git_inspect_rejects_workdir_outside_active_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    _init_repo(outside)
    monkeypatch.setattr(
        "tools.observation_workspace._active_workspace_root",
        lambda _task_id="default": workspace.resolve(),
    )

    payload = json.loads(
        git_inspect(
            operation="status",
            workdir=str(outside),
            runtime_mode="read_only",
        )
    )

    assert "active workspace boundary" in payload["error"]


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


@pytest.mark.parametrize("operation", ["diff", "show", "log"])
def test_git_inspect_never_invokes_repository_textconv(tmp_path, operation):
    repo = tmp_path / "repo"
    _init_repo(repo)
    marker = tmp_path / f"textconv-{operation}-invoked"
    converter = repo / "converter.sh"
    converter.write_text(
        "#!/bin/sh\n"
        f"touch {str(marker)!r}\n"
        "cat \"$1\"\n",
        encoding="utf-8",
    )
    converter.chmod(0o755)
    (repo / ".gitattributes").write_text("*.dat diff=unsafe\n", encoding="utf-8")
    (repo / "sample.dat").write_text("one\n", encoding="utf-8")
    _git(repo, "config", "diff.unsafe.textconv", str(converter))
    _git(repo, "add", ".gitattributes", "sample.dat")
    _git(repo, "commit", "-m", "textconv fixture")
    (repo / "sample.dat").write_text("two\n", encoding="utf-8")

    payload = json.loads(
        git_inspect(
            operation=operation,
            revision="HEAD" if operation in {"show", "log"} else "",
            paths=["sample.dat"],
            workdir=str(repo),
            runtime_mode="read_only",
        )
    )

    assert payload["success"] is True, payload
    assert not marker.exists()
