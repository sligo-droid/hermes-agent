from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from hermes_cli import worktree_runtime as wr
from hermes_cli.config import DEFAULT_CONFIG


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Hermes Tests")
    _git(repo, "checkout", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return repo, remote


def _add_worktree(repo: Path, path: Path, branch: str = "feature") -> Path:
    _git(repo, "worktree", "add", "-b", branch, str(path), "main")
    return path


def _age_worktree(path: Path, *, days: int = 30) -> None:
    timestamp = time.time() - days * 86400
    git_dir = Path(_git(path, "rev-parse", "--git-dir"))
    if not git_dir.is_absolute():
        git_dir = path / git_dir
    for candidate in (path, path / ".git", git_dir / "index", git_dir / "HEAD", git_dir / "gitdir"):
        if candidate.exists() or candidate.is_symlink():
            os.utime(candidate, (timestamp, timestamp), follow_symlinks=False)


def test_prepare_reuses_primary_python_and_pnpm_dependencies(tmp_path, monkeypatch):
    repo, _remote = _repo_with_remote(tmp_path)
    worktree = _add_worktree(repo, tmp_path / "worktree")
    for root in (repo, worktree):
        (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
        (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        dashboard = root / "dashboard"
        dashboard.mkdir()
        (dashboard / "package.json").write_text('{"name":"dashboard"}', encoding="utf-8")
        (dashboard / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("python", encoding="utf-8")
    (repo / "dashboard" / "node_modules").mkdir()
    monkeypatch.delenv("HERMES_CODING_WORKER_PNPM_LINKS", raising=False)
    monkeypatch.delenv("HERMES_WORKTREE_PYTHON_VENV_LINKS", raising=False)

    notes = wr.prepare_worktree_dependency_links(worktree, config={})

    assert len(notes) == 2
    assert (worktree / ".venv").is_symlink()
    assert (worktree / ".venv").resolve() == (repo / ".venv").resolve()
    assert (worktree / "dashboard" / "node_modules").is_symlink()
    assert (worktree / "dashboard" / "node_modules").resolve() == (
        repo / "dashboard" / "node_modules"
    ).resolve()


def test_prepare_requires_exact_lock_match(tmp_path):
    repo, _remote = _repo_with_remote(tmp_path)
    worktree = _add_worktree(repo, tmp_path / "worktree")
    for root, version in ((repo, "1"), (worktree, "2")):
        (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
        (root / "uv.lock").write_text(f"version = {version}\n", encoding="utf-8")
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("python", encoding="utf-8")

    assert wr.prepare_worktree_dependency_links(worktree, config={}) == []
    assert not (worktree / ".venv").exists()


def test_dependency_reuse_respects_worker_mutation_scopes(tmp_path):
    repo, _remote = _repo_with_remote(tmp_path)
    worktree = _add_worktree(repo, tmp_path / "worktree")
    (worktree / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    (worktree / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    package = worktree / "ui"
    package.mkdir()
    (package / "package.json").write_text('{"name":"ui"}', encoding="utf-8")
    (package / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

    assert wr.dependency_reuse_for_scopes(worktree, None) == (False, False)
    assert wr.dependency_reuse_for_scopes(worktree, []) == (True, True)
    assert wr.dependency_reuse_for_scopes(worktree, ["src"]) == (True, True)
    assert wr.dependency_reuse_for_scopes(worktree, ["ui/src"]) == (True, True)
    assert wr.dependency_reuse_for_scopes(worktree, ["ui"]) == (False, True)
    assert wr.dependency_reuse_for_scopes(worktree, ["uv.lock"]) == (True, False)


def test_replace_existing_python_venv_uses_primary_exact_lock(tmp_path):
    repo, _remote = _repo_with_remote(tmp_path)
    worktree = _add_worktree(repo, tmp_path / "worktree")
    for root in (repo, worktree):
        (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").write_text("python", encoding="utf-8")
    (worktree / ".venv").mkdir()
    (worktree / ".venv" / "local-only").write_text("remove", encoding="utf-8")

    notes = wr.prepare_worktree_dependency_links(
        worktree,
        config={},
        replace_python_venv=True,
    )

    assert notes == [
        f"linked {worktree / '.venv'} -> {repo / '.venv'} "
        "(exact lock; unlink before syncing or changing Python dependencies)"
    ]
    assert (worktree / ".venv").is_symlink()
    assert not (worktree / ".venv" / "local-only").exists()


def test_cleanup_requires_old_clean_inactive_and_fully_pushed(tmp_path):
    repo, _remote = _repo_with_remote(tmp_path)
    worktree = _add_worktree(repo, tmp_path / "worktree")
    _age_worktree(worktree)

    eligible = wr.inspect_cleanup_candidate(
        worktree,
        older_than_days=7,
        active_cwds=(repo,),
    )
    assert eligible.eligible is True

    (worktree / "dirty.txt").write_text("dirty", encoding="utf-8")
    dirty = wr.inspect_cleanup_candidate(
        worktree,
        older_than_days=7,
        active_cwds=(repo,),
    )
    assert dirty.eligible is False
    assert "dirty" in dirty.reasons


def test_cleanup_removes_worktree_but_preserves_branch(tmp_path):
    repo, _remote = _repo_with_remote(tmp_path)
    worktree = _add_worktree(repo, tmp_path / "worktree")
    _age_worktree(worktree)

    result = wr.cleanup_worktrees(
        [worktree],
        older_than_days=7,
        apply=True,
        excludes=(repo,),
    )

    assert result["removed"] == [str(worktree)]
    assert not worktree.exists()
    assert _git(repo, "branch", "--list", "feature") == "feature"


def test_terminal_action_cleanup_is_interval_gated(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    ledger = SimpleNamespace(terminal_action_worktree_paths=lambda **_kwargs: [])

    first = wr.maybe_cleanup_terminal_action_worktrees(
        ledger,
        {"worktrees": {"cleanup": {"min_interval_hours": 24}}},
        tmp_path / "workspaces",
    )
    second = wr.maybe_cleanup_terminal_action_worktrees(
        ledger,
        {"worktrees": {"cleanup": {"min_interval_hours": 24}}},
        tmp_path / "workspaces",
    )

    assert first["scanned"] == 0
    assert second == {"skipped": "interval"}


def test_terminal_action_cleanup_removes_only_eligible_ledger_path(tmp_path, monkeypatch):
    hermes_home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    repo, _remote = _repo_with_remote(tmp_path)
    action_root = tmp_path / "workspaces"
    action_root.mkdir()
    worktree = _add_worktree(
        repo,
        action_root / "demo-discord-action-abc",
    )
    _age_worktree(worktree)
    ledger = SimpleNamespace(
        terminal_action_worktree_paths=lambda **_kwargs: [str(worktree)]
    )

    result = wr.maybe_cleanup_terminal_action_worktrees(
        ledger,
        {
            "worktrees": {
                "cleanup": {
                    "retention_days": 7,
                    "min_interval_hours": 24,
                    "max_per_run": 25,
                }
            }
        },
        action_root,
    )

    assert result["removed"] == [str(worktree)]
    assert not worktree.exists()


def test_worktree_runtime_defaults_enable_safe_reuse_and_bounded_cleanup():
    worktrees = DEFAULT_CONFIG["worktrees"]

    assert worktrees["dependency_reuse"] == {
        "pnpm": True,
        "python_venv": True,
    }
    assert worktrees["cleanup"] == {
        "enabled": True,
        "retention_days": 7,
        "min_interval_hours": 24,
        "max_per_run": 25,
    }


def test_dedupe_skips_locked_worktree(tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    (worktree / ".venv").mkdir(parents=True)
    monkeypatch.setattr(wr, "_active_process_cwds", lambda: ())
    monkeypatch.setattr(wr, "repo_root_for_path", lambda _path: worktree)
    monkeypatch.setattr(
        wr,
        "git_worktree_records",
        lambda _root: [wr.WorktreeRecord(str(worktree), locked=True)],
    )

    result = wr.dedupe_python_venvs([worktree], apply=False)

    assert result["eligible"] == 0
    assert result["reasons"] == {"locked": 1}
