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


def _patch_github_boundary(monkeypatch):
    monkeypatch.setattr(finalizer, "github_remote_preflight_error", lambda *_a, **_k: None)
    monkeypatch.setattr(finalizer, "github_origin_repo", lambda *_a, **_k: "acme/example")


def _pr_payload(
    worktree: Path,
    *,
    head: str | None = None,
    state: str = "OPEN",
    draft: bool = True,
    merge_commit: str = "",
) -> str:
    head_sha = head or _git(worktree, "rev-parse", "HEAD").stdout.strip()
    return json.dumps(
        {
            "state": state,
            "isDraft": draft,
            "mergeStateStatus": "CLEAN",
            "mergedAt": "2026-07-18T00:00:00Z" if state == "MERGED" else None,
            "mergeCommit": {"oid": merge_commit} if merge_commit else None,
            "url": "https://github.com/acme/example/pull/7",
            "headRefOid": head_sha,
            "headRefName": "fable/test",
            "headRepository": {"nameWithOwner": "acme/example"},
        }
    )


def test_prepare_rejects_dirty_owned_worktree(monkeypatch, tmp_path):
    _canonical, _remote, worktree = _linked_repo(tmp_path)
    (worktree / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    _patch_github_boundary(monkeypatch)

    result = finalizer.prepare_fable_git_lifecycle(str(worktree), "merge")

    assert result.success is False
    assert "requires a clean owned worktree" in result.error


def test_finalize_commits_pushes_draft_pr_and_returns_durable_closeout_without_watch(
    monkeypatch,
    tmp_path,
):
    canonical, _remote, worktree = _linked_repo(tmp_path)
    real_run = finalizer._run
    calls = []

    def fake_run(args, *, cwd, timeout=60, github=False):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(args, 0, stdout="authenticated\n", stderr="")
        if args[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout="\n", stderr="")
        if args[:3] == ["gh", "pr", "create"]:
            assert "--draft" in args
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="https://github.com/acme/example/pull/7\n",
                stderr="",
            )
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, stdout=_pr_payload(worktree), stderr="")
        if args[:3] == ["gh", "pr", "checks"] or args[:3] == ["gh", "pr", "merge"]:
            raise AssertionError(f"synchronous closeout command is forbidden: {args}")
        return real_run(args, cwd=Path(cwd), timeout=timeout, github=github)

    monkeypatch.setattr(finalizer, "_run", fake_run)
    _patch_github_boundary(monkeypatch)

    preparation = finalizer.prepare_fable_git_lifecycle(str(worktree), "merge")
    assert preparation.success is True
    (worktree / "tracked.txt").write_text("initial\nchanged\n", encoding="utf-8")

    result = finalizer.finalize_fable_git_lifecycle(
        preparation,
        mode="merge",
        task="update the lifecycle behavior",
        worker_summary="Changed tracked.txt and ran focused checks.",
        closeout_mode="enforce",
    )

    assert result.success is True
    assert result.status == "closeout_pending"
    assert result.checks_status == "pending"
    assert result.pr_url == "https://github.com/acme/example/pull/7"
    assert result.commit_performed is True
    assert result.push_performed is True
    assert result.pr_created is True
    assert result.merge_performed is False
    assert result.merge_observed is False
    assert result.changed_files == ["tracked.txt"]
    assert _git(worktree, "status", "--short").stdout == ""
    assert result.closeout_id.endswith(result.commit)
    state = result.closeout_state
    assert state["source"] == "fable"
    assert state["status"] == "pending"
    assert state["workspace"]["path"] == str(worktree)
    assert state["workspace"]["canonical_path"] == str(canonical)
    assert state["pr"]["head_sha"] == result.commit
    assert state["pr"]["is_draft"] is True
    assert state["local_verification"] == {"status": "pending"}
    spans = state["telemetry"]["phase_spans"]
    root_span = next(span for span in spans if span["name"] == "fable_finalization")
    assert root_span["attempt_id"].startswith("att_")
    assert "finalization-1" not in repr(root_span)
    command_spans = [span for span in spans if span.get("parent_id") == root_span["id"]]
    assert command_spans
    assert all(span.get("attempt_id") == root_span["attempt_id"] for span in command_spans)
    assert not any(args[:3] == ["gh", "pr", "checks"] for args in calls)
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


def test_trusted_local_verification_requires_exact_root_head_and_mutation_boundary(tmp_path):
    _canonical, _remote, worktree = _linked_repo(tmp_path)
    head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    evidence = {
        "mutation_generation": 2,
        "mutation_boundary": 5,
        "verification_evidence": [
            {
                "status": "success",
                "surface": "verification",
                "repository_root": str(worktree),
                "canonical_command": "scripts/run_tests.sh tests/focused",
                "scope": "tests",
                "verified_head_sha": head,
                "mutation_generation": 2,
                "mutation_boundary": 5,
            }
        ],
    }

    assert finalizer._trusted_local_verification_receipt(worktree, head, evidence) == {
        "status": "passed",
        "head_sha": head,
    }
    stale = {**evidence, "mutation_boundary": 6}
    assert finalizer._trusted_local_verification_receipt(worktree, head, stale) == {
        "status": "pending"
    }


def test_shadow_merge_preserves_legacy_finalizer_with_exact_head_guard(monkeypatch, tmp_path):
    _canonical, _remote, worktree = _linked_repo(tmp_path)
    real_run = finalizer._run
    calls: list[list[str]] = []
    merge_state = {"merged": False}

    def fake_run(args, *, cwd, timeout=60, github=False):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(args, 0, stdout="authenticated\n", stderr="")
        if args[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
        if args[:3] == ["gh", "pr", "list"]:
            return subprocess.CompletedProcess(args, 0, stdout="\n", stderr="")
        if args[:3] == ["gh", "pr", "create"]:
            assert "--draft" not in args
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="https://github.com/acme/example/pull/7\n",
                stderr="",
            )
        if args[:3] == ["gh", "pr", "view"] and "statusCheckRollup" in args:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps({"statusCheckRollup": [{"status": "COMPLETED"}]}),
                stderr="",
            )
        if args[:3] == ["gh", "pr", "view"]:
            state = "MERGED" if merge_state["merged"] else "OPEN"
            merge_commit = _git(worktree, "rev-parse", "HEAD").stdout.strip()
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=_pr_payload(
                    worktree,
                    state=state,
                    draft=False,
                    merge_commit=merge_commit if state == "MERGED" else "",
                ),
                stderr="",
            )
        if args[:3] == ["gh", "pr", "checks"]:
            return subprocess.CompletedProcess(args, 0, stdout="passed\n", stderr="")
        if args[:3] == ["gh", "pr", "merge"]:
            merge_state["merged"] = True
            return subprocess.CompletedProcess(args, 0, stdout="merged\n", stderr="")
        return real_run(args, cwd=Path(cwd), timeout=timeout, github=github)

    monkeypatch.setattr(finalizer, "_run", fake_run)
    monkeypatch.setattr(finalizer, "_sync_after_merge", lambda *_a, **_k: ("synced", ""))
    _patch_github_boundary(monkeypatch)

    preparation = finalizer.prepare_fable_git_lifecycle(str(worktree), "merge")
    (worktree / "tracked.txt").write_text("shadow merge\n", encoding="utf-8")
    result = finalizer.finalize_fable_git_lifecycle(
        preparation,
        mode="merge",
        task="merge through the authorized legacy finalizer",
        worker_summary="Changed tracked.txt and ran focused checks.",
        closeout_mode="shadow",
    )

    assert result.success is True
    assert result.status == "merged"
    assert result.merge_performed is True
    assert result.closeout_state["mode"] == "shadow"
    merge_call = next(args for args in calls if args[:3] == ["gh", "pr", "merge"])
    assert merge_call[merge_call.index("--match-head-commit") + 1] == result.commit


def test_existing_pr_stale_local_head_cannot_bind_closeout_evidence(monkeypatch, tmp_path):
    _canonical, _remote, worktree = _linked_repo(tmp_path)
    (worktree / "tracked.txt").write_text("local head\n", encoding="utf-8")
    _git(worktree, "add", "tracked.txt")
    _git(worktree, "commit", "-m", "local head")
    local_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    _git(worktree, "push", "-u", "origin", "fable/test")
    (worktree / "tracked.txt").write_text("remote head\n", encoding="utf-8")
    _git(worktree, "add", "tracked.txt")
    _git(worktree, "commit", "-m", "remote head")
    remote_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    _git(worktree, "push", "origin", "fable/test")
    _git(worktree, "reset", "--hard", local_head)

    real_run = finalizer._run
    calls: list[list[str]] = []

    def fake_run(args, *, cwd, timeout=60, github=False):
        calls.append(args)
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=_pr_payload(worktree, head=remote_head, draft=False),
                stderr="",
            )
        return real_run(args, cwd=Path(cwd), timeout=timeout, github=github)

    monkeypatch.setattr(finalizer, "_run", fake_run)
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

    result = finalizer.finalize_fable_git_lifecycle(
        preparation,
        mode="merge",
        task="amend the existing PR",
        worker_summary="No additional local changes were needed.",
        closeout_mode="enforce",
    )

    assert result.commit == local_head
    assert result.success is False
    assert result.status == "pr_head_verification_blocked"
    assert "does not match the intended verified commit" in result.error
    assert result.closeout_state == {}
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


def test_pr_only_lifecycle_returns_terminal_unmerged_closeout(monkeypatch, tmp_path):
    _canonical, _remote, worktree = _linked_repo(tmp_path)
    real_run = finalizer._run

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
                stdout="https://github.com/acme/example/pull/8\n",
                stderr="",
            )
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, stdout=_pr_payload(worktree), stderr="")
        return real_run(args, cwd=Path(cwd), timeout=timeout, github=github)

    monkeypatch.setattr(finalizer, "_run", fake_run)
    _patch_github_boundary(monkeypatch)
    preparation = finalizer.prepare_fable_git_lifecycle(str(worktree), "pr")
    (worktree / "tracked.txt").write_text("pr only\n", encoding="utf-8")

    result = finalizer.finalize_fable_git_lifecycle(
        preparation,
        mode="pr",
        task="open the requested PR",
        worker_summary="Changed the file.",
    )

    assert result.success is True
    assert result.status == "pr_opened"
    assert result.checks_status == "not_waited"
    assert result.closeout_state["status"] == "pr_open"
    assert result.closeout_state["policy"]["merge"] == "never"


def test_existing_pr_conflict_is_delegated_then_handed_to_closeout(monkeypatch, tmp_path):
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
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, stdout=_pr_payload(worktree), stderr="")
        if args[:3] in (["gh", "pr", "checks"], ["gh", "pr", "merge"]):
            raise AssertionError(args)
        return real_run(args, cwd=Path(cwd), timeout=timeout, github=github)

    monkeypatch.setattr(finalizer, "_run", fake_run)
    _patch_github_boundary(monkeypatch)

    preparation = finalizer.prepare_fable_git_lifecycle(str(worktree), "merge")
    assert preparation.success is True
    assert preparation.recovery_kind == "merge_conflict"
    assert preparation.conflict_files == ["tracked.txt"]
    (worktree / "tracked.txt").write_text("main\nfeature\n", encoding="utf-8")

    result = finalizer.finalize_fable_git_lifecycle(
        preparation,
        mode="merge",
        task="resolve the concurrent lifecycle edit",
        worker_summary="Resolved tracked.txt and ran focused checks.",
        closeout_mode="enforce",
    )

    assert result.success is True
    assert result.status == "closeout_pending"
    assert result.recovery_kind == "merge_conflict"
    assert result.commit_performed is True
    assert result.push_performed is True
    assert result.pr_created is False
    assert result.merge_performed is False
    assert len(_git(worktree, "show", "-s", "--format=%P", result.commit).stdout.split()) == 2
    assert result.closeout_state["pr"]["url"].endswith("/pull/7")


def test_existing_merged_pr_does_not_bind_stale_local_head_to_remote_pr(
    monkeypatch,
    tmp_path,
):
    canonical, _remote, worktree = _linked_repo(tmp_path)
    (worktree / "tracked.txt").write_text("landed\n", encoding="utf-8")
    _git(worktree, "add", "tracked.txt")
    _git(worktree, "commit", "-m", "landed elsewhere")
    _git(worktree, "push", "-u", "origin", "fable/test")
    remote_pr_head = _git(worktree, "rev-parse", "HEAD").stdout.strip()
    _git(worktree, "push", "origin", "fable/test:main")
    _git(canonical, "fetch", "origin", "main")
    _git(canonical, "reset", "--hard", "origin/main")
    (canonical / "post-merge.txt").write_text("base advanced\n", encoding="utf-8")
    _git(canonical, "add", "post-merge.txt")
    _git(canonical, "commit", "-m", "advance base after merge")
    _git(canonical, "push", "origin", "main")

    real_run = finalizer._run

    def fake_run(args, *, cwd, timeout=60, github=False):
        if args[:3] == ["gh", "auth", "status"]:
            return subprocess.CompletedProcess(args, 0, stdout="authenticated\n", stderr="")
        if args[:3] == ["gh", "repo", "view"]:
            return subprocess.CompletedProcess(args, 0, stdout="main\n", stderr="")
        if args[:3] == ["gh", "pr", "list"]:
            value = "https://github.com/acme/example/pull/7\n" if "merged" in args else "\n"
            return subprocess.CompletedProcess(args, 0, stdout=value, stderr="")
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=_pr_payload(
                    worktree,
                    head=remote_pr_head,
                    state="MERGED",
                    draft=False,
                    merge_commit=remote_pr_head,
                ),
                stderr="",
            )
        return real_run(args, cwd=Path(cwd), timeout=timeout, github=github)

    monkeypatch.setattr(finalizer, "_run", fake_run)
    _patch_github_boundary(monkeypatch)

    preparation = finalizer.prepare_fable_git_lifecycle(str(worktree), "merge")
    assert preparation.recovery_kind == "merged_pr_observation"
    assert _git(worktree, "rev-parse", "HEAD").stdout.strip() != remote_pr_head

    result = finalizer.finalize_fable_git_lifecycle(
        preparation,
        mode="merge",
        task="verify the existing PR",
        worker_summary="No file changes were needed.",
        closeout_mode="enforce",
    )

    assert result.success is False
    assert result.status == "pr_head_verification_blocked"
    assert "does not match the intended verified commit" in result.error
    assert result.closeout_state == {}
    assert result.merge_performed is False
