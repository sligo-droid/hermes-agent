from __future__ import annotations

from pathlib import Path
import subprocess

from hermes_cli.canonical_checkout_sync import (
    sync_canonical_checkout,
    sync_protected_canonical_checkout,
)


def _ok(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
    if args == ["rev-parse", "HEAD"]:
        return subprocess.CompletedProcess(args, 0, stdout="canonical-head\n", stderr="")
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


def test_syncs_clean_checkout_and_returns_persistable_state(tmp_path: Path) -> None:
    calls: list[tuple[list[str], Path, int]] = []

    def run_git(args: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append((args, cwd, timeout))
        return _ok(args)

    result = sync_canonical_checkout(tmp_path, "main", "merge-sha", run_git=run_git)

    assert result.state == "synced"
    assert result.error == ""
    assert result.path == str(tmp_path)
    assert result.branch == "main"
    assert result.head == "canonical-head"
    assert result.merge_commit == "merge-sha"
    assert result.synced_at.endswith("Z")
    assert result.as_worker_metadata()["canonical_sync_state"] == "synced"
    assert [args for args, _, _ in calls] == [
        ["status", "--porcelain"],
        ["fetch", "origin", "--prune", "+refs/heads/main:refs/remotes/origin/main"],
        ["rev-parse", "--verify", "refs/remotes/origin/main"],
        ["cat-file", "-e", "merge-sha^{commit}"],
        ["merge-base", "--is-ancestor", "merge-sha", "refs/remotes/origin/main"],
        ["checkout", "main"],
        ["merge", "--ff-only", "refs/remotes/origin/main"],
        ["rev-parse", "HEAD"],
        ["merge-base", "--is-ancestor", "merge-sha", "HEAD"],
    ]
    assert all(args[0] != "pull" for args, _, _ in calls)
    assert sum(args[0] == "fetch" for args, _, _ in calls) == 1


def test_blocks_missing_or_dirty_checkout_before_fetch_or_pull(tmp_path: Path) -> None:
    missing = sync_canonical_checkout(tmp_path / "missing", "main", "merge-sha", run_git=_ok)
    assert missing.state == "blocked"
    assert "missing or invalid" in missing.error

    calls: list[list[str]] = []

    def dirty(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=" M edited.py\n", stderr="")

    result = sync_canonical_checkout(tmp_path, "main", "merge-sha", run_git=dirty)

    assert result.state == "blocked"
    assert result.error == "Canonical checkout is dirty"
    assert calls == [["status", "--porcelain"]]


def test_cancellation_is_checked_immediately_before_first_mutating_git_command(
    tmp_path: Path,
) -> None:
    calls: list[list[str]] = []

    class Cancelled:
        @staticmethod
        def mutation_allowed() -> bool:
            return False

    def run_git(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _ok(args)

    result = sync_canonical_checkout(
        tmp_path,
        "main",
        "merge-sha",
        run_git=run_git,
        control=Cancelled(),
    )

    assert result.state == "blocked"
    assert "cancelled before mutation" in result.error
    assert calls == [["status", "--porcelain"]]


def test_blocks_when_fast_forward_merge_fails(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run_git(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:1] == ["merge"]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="diverged")
        return _ok(args)

    result = sync_canonical_checkout(tmp_path, "main", "merge-sha", run_git=run_git)

    assert result.state == "blocked"
    assert result.error == "Canonical checkout fast-forward merge failed: diverged"
    assert calls == [
        ["status", "--porcelain"],
        ["fetch", "origin", "--prune", "+refs/heads/main:refs/remotes/origin/main"],
        ["rev-parse", "--verify", "refs/remotes/origin/main"],
        ["cat-file", "-e", "merge-sha^{commit}"],
        ["merge-base", "--is-ancestor", "merge-sha", "refs/remotes/origin/main"],
        ["checkout", "main"],
        ["merge", "--ff-only", "refs/remotes/origin/main"],
    ]


def test_blocks_when_expected_merge_commit_is_not_in_head(tmp_path: Path) -> None:
    def run_git(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["merge-base", "--is-ancestor"] and args[-1] == "HEAD":
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="not an ancestor")
        return _ok(args)

    result = sync_canonical_checkout(tmp_path, "main", "missing-sha", run_git=run_git)

    assert result.state == "blocked"
    assert result.head == ""
    assert result.error == "Canonical checkout does not contain PR merge commit: not an ancestor"


def test_blocks_when_merge_commit_is_missing_without_changing_checkout(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run_git(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _ok(args)

    result = sync_canonical_checkout(tmp_path, "main", "", run_git=run_git)

    assert result.state == "blocked"
    assert result.error == "Canonical checkout merge commit is missing"
    assert calls == []


def test_syncs_clean_existing_worktree_when_branch_is_checked_out_elsewhere(tmp_path: Path) -> None:
    existing = tmp_path / "existing-main"
    existing.mkdir()
    calls: list[tuple[list[str], Path]] = []

    def run_git(args: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
        calls.append((args, cwd))
        if args == ["checkout", "main"]:
            return subprocess.CompletedProcess(args, 128, stdout="", stderr="already checked out")
        if args == ["worktree", "list", "--porcelain"]:
            output = f"worktree {tmp_path}\nHEAD old\nbranch refs/heads/other\n\nworktree {existing}\nHEAD old\nbranch refs/heads/main\n"
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        return _ok(args)

    result = sync_canonical_checkout(tmp_path, "main", "merge-sha", run_git=run_git)

    assert result.state == "synced_existing_worktree"
    assert result.path == str(existing)
    assert result.head == "canonical-head"
    assert [args for args, _ in calls] == [
        ["status", "--porcelain"],
        ["fetch", "origin", "--prune", "+refs/heads/main:refs/remotes/origin/main"],
        ["rev-parse", "--verify", "refs/remotes/origin/main"],
        ["cat-file", "-e", "merge-sha^{commit}"],
        ["merge-base", "--is-ancestor", "merge-sha", "refs/remotes/origin/main"],
        ["checkout", "main"],
        ["worktree", "list", "--porcelain"],
        ["status", "--porcelain"],
        ["merge", "--ff-only", "refs/remotes/origin/main"],
        ["rev-parse", "HEAD"],
        ["merge-base", "--is-ancestor", "merge-sha", "HEAD"],
    ]
    assert [cwd for _, cwd in calls[7:]] == [existing] * 4
    assert sum(args[0] == "fetch" for args, _ in calls) == 1
    assert all(args[0] != "pull" for args, _ in calls)


def test_uses_one_bounded_exact_fetch_only_when_merge_commit_is_missing(tmp_path: Path) -> None:
    calls: list[list[str]] = []
    commit_checks = 0

    def run_git(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        nonlocal commit_checks
        calls.append(args)
        if args[:2] == ["cat-file", "-e"]:
            commit_checks += 1
            if commit_checks == 1:
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="missing")
        return _ok(args)

    result = sync_canonical_checkout(tmp_path, "main", "a" * 40, run_git=run_git)

    assert result.state == "synced"
    fetches = [args for args in calls if args[0] == "fetch"]
    assert fetches == [
        ["fetch", "origin", "--prune", "+refs/heads/main:refs/remotes/origin/main"],
        ["fetch", "origin", "a" * 40],
    ]


def test_protected_sync_accepts_a_non_main_repository_default_branch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_CANONICAL_REPO_ROOTS", str(tmp_path))
    calls: list[list[str]] = []

    def run_git(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{tmp_path}\n", stderr="")
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="develop\n", stderr="")
        if args == ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="origin/develop\n", stderr="")
        return _ok(args)

    result = sync_protected_canonical_checkout(tmp_path, "develop", "a" * 40, run_git=run_git)

    assert result.state == "synced"
    assert result.path == str(tmp_path)
    assert result.branch == "develop"
    assert result.merge_commit == "a" * 40
    assert calls == [
        ["rev-parse", "--show-toplevel"],
        ["rev-parse", "--abbrev-ref", "HEAD"],
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        ["status", "--porcelain"],
        ["fetch", "origin", "--prune", "+refs/heads/develop:refs/remotes/origin/develop"],
        ["rev-parse", "--verify", "refs/remotes/origin/develop"],
        ["cat-file", "-e", f"{'a' * 40}^{{commit}}"],
        ["merge-base", "--is-ancestor", "a" * 40, "refs/remotes/origin/develop"],
        ["checkout", "develop"],
        ["merge", "--ff-only", "refs/remotes/origin/develop"],
        ["rev-parse", "HEAD"],
        ["merge-base", "--is-ancestor", "a" * 40, "HEAD"],
    ]


def test_protected_sync_rejects_noncanonical_target_before_mutating(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_CANONICAL_REPO_ROOTS", str(tmp_path / "other"))
    calls: list[list[str]] = []

    def run_git(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{tmp_path}\n", stderr="")
        return _ok(args)

    result = sync_protected_canonical_checkout(tmp_path, "main", "a" * 40, run_git=run_git)

    assert result.state == "blocked"
    assert result.error == "project_path must be a protected canonical checkout"
    assert calls == [["rev-parse", "--show-toplevel"]]


def test_protected_sync_requires_an_exact_full_merge_sha_before_repository_access(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def run_git(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        return _ok(args)

    for invalid in ("a" * 7, "a" * 41, "a" * 63):
        result = sync_protected_canonical_checkout(tmp_path, "main", invalid, run_git=run_git)
        assert result.state == "blocked"
        assert result.error == "merge_commit must be an exact 40- or 64-character hexadecimal Git SHA"
    assert calls == []


def test_protected_sync_accepts_a_64_character_sha_boundary(tmp_path: Path) -> None:
    result = sync_protected_canonical_checkout(tmp_path / "missing", "main", "a" * 64)

    assert result.state == "blocked"
    assert result.error.startswith("Canonical checkout missing or invalid")


def test_protected_sync_requires_the_requested_origin_default_branch(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HERMES_CANONICAL_REPO_ROOTS", str(tmp_path))
    calls: list[list[str]] = []

    def run_git(args: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args == ["rev-parse", "--show-toplevel"]:
            return subprocess.CompletedProcess(args, 0, stdout=f"{tmp_path}\n", stderr="")
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="develop\n", stderr="")
        if args == ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"]:
            return subprocess.CompletedProcess(args, 0, stdout="origin/main\n", stderr="")
        return _ok(args)

    result = sync_protected_canonical_checkout(tmp_path, "develop", "a" * 40, run_git=run_git)

    assert result.state == "blocked"
    assert result.error == "branch must match origin default branch 'main'"
    assert calls == [
        ["rev-parse", "--show-toplevel"],
        ["rev-parse", "--abbrev-ref", "HEAD"],
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
    ]
