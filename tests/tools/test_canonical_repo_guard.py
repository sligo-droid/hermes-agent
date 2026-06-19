from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from tools.canonical_repo_guard import (
    canonical_main_routing_hint,
    canonical_main_terminal_violation,
    canonical_main_worker_violation,
    canonical_main_write_violation,
)
from tools import coding_worker_tool as cwt
from tools.coding_worker_tool import delegate_coding_task
from tools.code_execution_tool import execute_code
from tools.file_tools import patch_tool, write_file_tool


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=str(cwd), check=True, capture_output=True, text=True)


def _init_repo(repo: Path, *, branch: str = "main") -> Path:
    repo.mkdir(parents=True)
    _run(["git", "init"], repo)
    _run(["git", "config", "user.email", "tests@example.invalid"], repo)
    _run(["git", "config", "user.name", "Hermes Tests"], repo)
    _run(["git", "checkout", "-b", branch], repo)
    tracked = repo / "tracked.txt"
    tracked.write_text("old\n", encoding="utf-8")
    _run(["git", "add", "tracked.txt"], repo)
    _run(["git", "commit", "-m", "initial"], repo)
    return tracked


def _protect(monkeypatch, root: Path) -> None:
    monkeypatch.setenv("HERMES_CANONICAL_REPO_ROOTS", str(root))
    monkeypatch.delenv("HERMES_DISABLE_CANONICAL_REPO_GUARD", raising=False)
    monkeypatch.delenv("HERMES_ALLOW_CANONICAL_MAIN_WRITES", raising=False)


def test_write_guard_blocks_tracked_file_on_protected_main(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    tracked = _init_repo(workspace / "PID")
    _protect(monkeypatch, workspace)

    msg = canonical_main_write_violation(tracked)

    assert msg is not None
    assert "BLOCKED" in msg
    assert str(tracked) in msg


def test_file_tools_refuse_to_dirty_protected_main_tracked_files(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    tracked = _init_repo(workspace / "PID")
    _protect(monkeypatch, workspace)

    write_result = json.loads(write_file_tool(str(tracked), "new\n", task_id="canonical-guard-test"))
    patch_result = json.loads(
        patch_tool(
            mode="replace",
            path=str(tracked),
            old_string="old",
            new_string="new",
            task_id="canonical-guard-test",
        )
    )

    assert "protected canonical checkout" in write_result["error"]
    assert "protected canonical checkout" in patch_result["error"]
    assert tracked.read_text(encoding="utf-8") == "old\n"


def test_write_guard_allows_feature_worktree_even_when_root_is_protected(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "PID"
    tracked = _init_repo(repo)
    _protect(monkeypatch, workspace)
    _run(["git", "checkout", "-b", "fix/safe-worktree"], repo)

    assert canonical_main_write_violation(tracked) is None


def test_terminal_guard_blocks_non_readonly_command_in_protected_main(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "PID"
    _init_repo(repo)
    _protect(monkeypatch, workspace)

    msg = canonical_main_terminal_violation(repo, "python -m insights.runners.editorial_insight")

    assert msg is not None
    assert "non-read-only terminal command" in msg


def test_routing_hint_describes_protected_main_worktree_route(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "PID"
    _init_repo(repo)
    _protect(monkeypatch, workspace)

    msg = canonical_main_routing_hint(repo, action="delegate_coding_task")

    assert msg is not None
    assert "BLOCKED" in msg
    assert "delegate_coding_task" in msg
    assert str(repo) in msg
    assert "main" in msg
    assert "/home/droid/workspaces/" in msg
    assert "intentional safety guard" in msg
    assert "absolute worktree path" in msg
    assert "inspection-only" in msg


def test_terminal_guard_allows_readonly_git_and_worktree_creation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "PID"
    _init_repo(repo)
    _protect(monkeypatch, workspace)

    assert canonical_main_terminal_violation(repo, "git status --short --branch") is None
    assert canonical_main_terminal_violation(repo, "git diff -- docs/project-state.md") is None
    assert (
        canonical_main_terminal_violation(
            repo,
            "git worktree add -b fix/example /tmp/PID-fix-example origin/main",
        )
        is None
    )


def test_execute_code_refuses_project_mode_inside_protected_main(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "PID"
    _init_repo(repo)
    _protect(monkeypatch, workspace)

    import tools.approval as approval
    import tools.code_execution_tool as code_execution_tool
    import tools.terminal_tool as terminal_tool

    monkeypatch.setattr(terminal_tool, "_get_env_config", lambda: {"env_type": "local"})
    monkeypatch.setattr(approval, "check_execute_code_guard", lambda code, env_type: {"approved": True})
    monkeypatch.setattr(code_execution_tool, "_get_execution_mode", lambda: "project")
    monkeypatch.setattr(code_execution_tool, "_resolve_child_cwd", lambda mode, staging_dir: str(repo))

    result = json.loads(execute_code("print('analysis only')", enabled_tools=[]))

    assert result["status"] == "error"
    assert "protected canonical checkout" in result["error"]


def test_coding_worker_refuses_protected_main_before_backend_launch(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "PID"
    _init_repo(repo)
    _protect(monkeypatch, workspace)

    def fail_materialize(_workdir):
        raise AssertionError("autoreview helper should not materialize")

    def fail_backend():
        raise AssertionError("backend should not be loaded")

    monkeypatch.setattr("hermes_cli.worker_autoreview.materialize_autoreview_helper", fail_materialize)
    monkeypatch.setattr(cwt, "_load_coding_worker_timeout", fail_backend)

    result = json.loads(
        delegate_coding_task(
            task="change tracked.txt",
            cwd=str(repo),
            parent_agent=SimpleNamespace(api_mode="chat", session_cwd=str(repo)),
        )
    )

    assert "delegate_coding_task was pointed at a protected canonical checkout" in result["error"]
    assert "/home/droid/workspaces/" in result["error"]
    assert "absolute worktree path" in result["error"]


def test_coding_worker_allows_nonprotected_worktree(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    repo = workspace / "PID"
    _init_repo(repo)
    worktrees_root = tmp_path / "worktrees"
    worktree = worktrees_root / "PID-feature"
    _run(["git", "worktree", "add", "-b", "fix/example", str(worktree)], repo)
    _protect(monkeypatch, workspace)

    assert canonical_main_worker_violation(worktree) is None
