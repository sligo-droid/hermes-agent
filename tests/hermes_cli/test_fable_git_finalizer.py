from __future__ import annotations

import json
import subprocess
from pathlib import Path

from hermes_cli import fable_git_finalizer as finalizer


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )


def _linked_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    canonical = tmp_path / "canonical"
    remote = tmp_path / "remote.git"
    worktree = tmp_path / "worktree"
    canonical.mkdir()
    _git(canonical, "init")
    _git(canonical, "config", "user.email", "tests@example.invalid")
    _git(canonical, "config", "user.name", "Hermes Tests")
    _git(canonical, "checkout", "-b", "main")
    (canonical / "tracked.txt").write_text("initial\n", encoding="utf-8")
    _git(canonical, "add", "tracked.txt")
    _git(canonical, "commit", "-m", "initial")
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    _git(canonical, "remote", "add", "origin", str(remote))
    _git(canonical, "push", "-u", "origin", "main")
    subprocess.run(
        ["git", "--git-dir", str(remote), "symbolic-ref", "HEAD", "refs/heads/main"],
        check=True,
    )
    _git(canonical, "worktree", "add", "-b", "fable/test", str(worktree), "HEAD")
    return canonical, remote, worktree


def test_prepare_rejects_dirty_owned_worktree(monkeypatch, tmp_path):
    _canonical, _remote, worktree = _linked_repo(tmp_path)
    (worktree / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    monkeypatch.setattr(finalizer, "github_remote_preflight_error", lambda *_a, **_k: None)
    monkeypatch.setattr(finalizer, "github_origin_repo", lambda *_a, **_k: "acme/example")

    result = finalizer.prepare_fable_git_lifecycle(str(worktree), "merge")

    assert result.success is False
    assert "requires a clean owned worktree" in result.error


def test_finalize_owns_commit_pr_checks_merge_and_worktree_alignment(monkeypatch, tmp_path):
    _canonical, _remote, worktree = _linked_repo(tmp_path)
    real_run = finalizer._run
    merged = {"value": False}

    def fake_run(args, *, cwd, timeout=60, github=False):
        if args[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(args, 0, stdout="authenticated\n", stderr="")
        if args[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout="\n", stderr="")
        if args[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="https://github.com/acme/example/pull/7\n",
                stderr="",
            )
        if args[:3] == ["gh", "pr", "checks"]:
            return subprocess.CompletedProcess(args, 0, stdout="checks passed\n", stderr="")
        if args[:3] == ["gh", "pr", "merge"]:
            pushed = real_run(
                ["git", "push", "origin", "fable/test:main"],
                cwd=Path(cwd),
                timeout=timeout,
                github=False,
            )
            merged["value"] = pushed.returncode == 0
            return subprocess.CompletedProcess(args, pushed.returncode, stdout="", stderr=pushed.stderr)
        if args[:3] == ["gh", "pr", "view"]:
            head = real_run(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(cwd),
                timeout=10,
                github=False,
            ).stdout.strip()
            payload = {
                "state": "MERGED" if merged["value"] else "OPEN",
                "isDraft": False,
                "mergeStateStatus": "CLEAN",
                "mergedAt": "2026-07-15T00:00:00Z" if merged["value"] else None,
                "mergeCommit": {"oid": head} if merged["value"] else None,
                "url": "https://github.com/acme/example/pull/7",
            }
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")
        return real_run(args, cwd=Path(cwd), timeout=timeout, github=github)

    monkeypatch.setattr(finalizer, "_run", fake_run)
    monkeypatch.setattr(finalizer, "github_remote_preflight_error", lambda *_a, **_k: None)
    monkeypatch.setattr(finalizer, "github_origin_repo", lambda *_a, **_k: "acme/example")
    monkeypatch.setattr(finalizer, "_sync_after_merge", lambda *_a, **_k: ("not_applicable", ""))

    preparation = finalizer.prepare_fable_git_lifecycle(str(worktree), "merge")
    assert preparation.success is True
    (worktree / "tracked.txt").write_text("initial\nchanged\n", encoding="utf-8")

    result = finalizer.finalize_fable_git_lifecycle(
        preparation,
        mode="merge",
        task="update the lifecycle behavior",
        worker_summary="Changed tracked.txt and ran focused checks.",
    )

    assert result.success is True
    assert result.status == "merged"
    assert result.pr_url == "https://github.com/acme/example/pull/7"
    assert result.checks_status == "passed"
    assert result.merge_commit == _git(worktree, "rev-parse", "HEAD").stdout.strip()
    assert result.changed_files == ["tracked.txt"]
    assert _git(worktree, "status", "--short").stdout == ""
    assert _git(worktree, "rev-list", "--count", "origin/main..HEAD").stdout.strip() == "0"
