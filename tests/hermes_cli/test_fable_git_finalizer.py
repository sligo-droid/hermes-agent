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
            if "statusCheckRollup" in args:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps(
                        {
                            "statusCheckRollup": [
                                {
                                    "__typename": "CheckRun",
                                    "status": "COMPLETED",
                                    "conclusion": "SUCCESS",
                                    "name": "test",
                                }
                            ]
                        }
                    ),
                    stderr="",
                )
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
    assert result.commit_performed is True
    assert result.push_performed is True
    assert result.pr_created is True
    assert result.merge_performed is True
    assert result.merge_observed is False
    assert result.merge_commit == _git(worktree, "rev-parse", "HEAD").stdout.strip()
    assert result.changed_files == ["tracked.txt"]
    assert _git(worktree, "status", "--short").stdout == ""
    assert _git(worktree, "rev-list", "--count", "origin/main..HEAD").stdout.strip() == "0"


def test_existing_pr_conflict_is_delegated_then_finalized_by_trusted_hermes(
    monkeypatch,
    tmp_path,
):
    canonical, _remote, worktree = _linked_repo(tmp_path)
    (worktree / "tracked.txt").write_text("feature\n", encoding="utf-8")
    _git(worktree, "add", "tracked.txt")
    _git(worktree, "commit", "-m", "feature change")
    _git(worktree, "push", "-u", "origin", "fable/test")

    (canonical / "tracked.txt").write_text("main\n", encoding="utf-8")
    _git(canonical, "add", "tracked.txt")
    _git(canonical, "commit", "-m", "concurrent main change")
    _git(canonical, "push", "origin", "main")

    real_run = finalizer._run
    merged = {"value": False}

    def fake_run(args, *, cwd, timeout=60, github=False):
        if args[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(args, 0, stdout="authenticated\n", stderr="")
        if args[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
        if args[:3] == ["gh", "pr", "list"]:
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
            if "statusCheckRollup" in args:
                return subprocess.CompletedProcess(
                    args,
                    0,
                    stdout=json.dumps(
                        {
                            "statusCheckRollup": [
                                {
                                    "__typename": "CheckRun",
                                    "status": "COMPLETED",
                                    "conclusion": "SUCCESS",
                                    "name": "test",
                                }
                            ]
                        }
                    ),
                    stderr="",
                )
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
    assert preparation.resume_existing_pr is True
    assert preparation.recovery_kind == "merge_conflict"
    assert preparation.conflict_files == ["tracked.txt"]
    assert "<<<<<<<" in (worktree / "tracked.txt").read_text(encoding="utf-8")

    # A timed-out recovery worker may leave this trusted merge state in place.
    # The next delegation must resume it rather than rejecting the worktree as dirty.
    preparation = finalizer.prepare_fable_git_lifecycle(str(worktree), "merge")
    assert preparation.success is True
    assert preparation.recovery_kind == "merge_conflict"
    assert preparation.conflict_files == ["tracked.txt"]

    # Codex owns only the file-content resolution. It intentionally does not
    # stage, commit, push, or touch the PR.
    (worktree / "tracked.txt").write_text("main\nfeature\n", encoding="utf-8")

    result = finalizer.finalize_fable_git_lifecycle(
        preparation,
        mode="merge",
        task="resolve the concurrent lifecycle edit",
        worker_summary="Resolved tracked.txt and ran focused checks.",
    )

    assert result.success is True
    assert result.status == "merged"
    assert result.recovery_kind == "merge_conflict"
    assert result.conflict_files == ["tracked.txt"]
    assert result.commit_performed is True
    assert result.push_performed is True
    assert result.pr_created is False
    assert result.merge_performed is True
    assert result.merge_observed is False
    assert len(_git(worktree, "show", "-s", "--format=%P", result.commit).stdout.split()) == 2
    assert _git(worktree, "status", "--short").stdout == ""
    assert _git(worktree, "rev-list", "--count", "origin/main..HEAD").stdout.strip() == "0"


def test_existing_pr_merge_observation_is_not_attributed_to_hermes(monkeypatch, tmp_path):
    _canonical, _remote, worktree = _linked_repo(tmp_path)
    (worktree / "tracked.txt").write_text("landed\n", encoding="utf-8")
    _git(worktree, "add", "tracked.txt")
    _git(worktree, "commit", "-m", "landed elsewhere")
    _git(worktree, "push", "-u", "origin", "fable/test")
    _git(worktree, "push", "origin", "fable/test:main")
    head = _git(worktree, "rev-parse", "HEAD").stdout.strip()

    real_run = finalizer._run

    def fake_run(args, *, cwd, timeout=60, github=False):
        if args[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(args, 0, stdout="authenticated\n", stderr="")
        if args[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
        if args[:3] == ["gh", "pr", "list"]:
            value = "https://github.com/acme/example/pull/7\n" if "merged" in args else "\n"
            return subprocess.CompletedProcess(args, 0, stdout=value, stderr="")
        return real_run(args, cwd=Path(cwd), timeout=timeout, github=github)

    monkeypatch.setattr(finalizer, "_run", fake_run)
    monkeypatch.setattr(finalizer, "github_remote_preflight_error", lambda *_a, **_k: None)
    monkeypatch.setattr(finalizer, "github_origin_repo", lambda *_a, **_k: "acme/example")

    preparation = finalizer.prepare_fable_git_lifecycle(str(worktree), "merge")

    assert preparation.success is True
    assert preparation.resume_existing_pr is True
    assert preparation.recovery_kind == "merged_pr_observation"
    monkeypatch.setattr(finalizer, "_wait_for_checks", lambda *_a, **_k: "")
    monkeypatch.setattr(
        finalizer,
        "_pr_state",
        lambda *_a, **_k: (
            {
                "state": "MERGED",
                "isDraft": False,
                "mergeCommit": {"oid": head},
            },
            "",
        ),
    )
    monkeypatch.setattr(finalizer, "_sync_after_merge", lambda *_a, **_k: ("not_applicable", ""))

    result = finalizer.finalize_fable_git_lifecycle(
        preparation,
        mode="merge",
        task="verify the existing PR",
        worker_summary="No file changes were needed.",
    )

    assert result.success is True
    assert result.status == "merged"
    assert result.commit_performed is False
    assert result.push_performed is False
    assert result.pr_created is False
    assert result.merge_performed is False
    assert result.merge_observed is True


def test_merge_conflict_failure_returns_deterministic_recovery_action(monkeypatch, tmp_path):
    _canonical, _remote, worktree = _linked_repo(tmp_path)
    preparation = finalizer.FableGitPreparation(
        success=True,
        worktree=str(worktree),
        branch="fable/test",
        base_branch="main",
        repo="acme/example",
        pr_url="https://github.com/acme/example/pull/7",
        resume_existing_pr=True,
        recovery_kind="existing_pr_resume",
    )
    real_run = finalizer._run

    def fake_run(args, *, cwd, timeout=60, github=False):
        if args[:3] == ["gh", "pr", "merge"]:
            return subprocess.CompletedProcess(
                args,
                1,
                stdout="",
                stderr="GraphQL: Pull Request has merge conflicts (mergePullRequest)",
            )
        return real_run(args, cwd=Path(cwd), timeout=timeout, github=github)

    monkeypatch.setattr(finalizer, "_run", fake_run)
    monkeypatch.setattr(finalizer, "_wait_for_checks", lambda *_a, **_k: "")
    monkeypatch.setattr(
        finalizer,
        "_pr_state",
        lambda *_a, **_k: (
            {
                "state": "OPEN",
                "isDraft": False,
                "mergeCommit": None,
            },
            "",
        ),
    )

    result = finalizer.finalize_fable_git_lifecycle(
        preparation,
        mode="merge",
        task="finish the PR",
        worker_summary="No additional file edits were needed.",
    )

    assert result.success is False
    assert result.status == "pr_opened"
    assert result.recovery_kind == "merge_conflict_retry"
    assert result.recovery_required is True
    assert "same cwd" in result.next_action
    assert result.merge_performed is False
    assert result.merge_observed is False


def test_wait_for_checks_waits_for_github_to_materialize_rollup(monkeypatch, tmp_path):
    calls = {"view": 0, "watch": 0, "sleep": 0}

    def fake_run(args, *, cwd, timeout=60, github=False):
        if args[:3] == ["gh", "pr", "view"]:
            calls["view"] += 1
            rollup = [] if calls["view"] == 1 else [{"status": "IN_PROGRESS", "name": "ci"}]
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({"statusCheckRollup": rollup}),
                stderr="",
            )
        if args[:3] == ["gh", "pr", "checks"]:
            calls["watch"] += 1
            return subprocess.CompletedProcess(args, 0, stdout="ci pass\n", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(finalizer, "_run", fake_run)
    monkeypatch.setattr(
        finalizer.time,
        "sleep",
        lambda _seconds: calls.__setitem__("sleep", calls["sleep"] + 1),
    )

    error = finalizer._wait_for_checks(
        tmp_path,
        repo="acme/example",
        pr_url="https://github.com/acme/example/pull/7",
    )

    assert error == ""
    assert calls == {"view": 2, "watch": 1, "sleep": 1}


def test_wait_for_checks_allows_persistently_empty_rollup_after_grace(monkeypatch, tmp_path):
    calls = {"view": 0}

    def fake_run(args, *, cwd, timeout=60, github=False):
        calls["view"] += 1
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=json.dumps({"statusCheckRollup": []}),
            stderr="",
        )

    monkeypatch.setattr(finalizer, "_run", fake_run)
    monkeypatch.setattr(finalizer, "_CHECK_MATERIALIZATION_TIMEOUT_SECONDS", 0)

    error = finalizer._wait_for_checks(
        tmp_path,
        repo="acme/no-checks",
        pr_url="https://github.com/acme/no-checks/pull/1",
    )

    assert error == ""
    assert calls["view"] == 1
