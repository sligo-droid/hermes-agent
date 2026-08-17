from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from hermes_cli.pr_workflow_preflight import (
    collect_pr_workflow_preflight,
    find_worktree_for_branch,
    render_pr_workflow_preflight,
)


def _result(stdout: str = "", *, returncode: int = 0, stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _runner_for(repo: Path, worktrees: str, *, dirty: bool = False):
    def run(args, **kwargs):
        assert Path(kwargs["cwd"]) == repo
        key = tuple(args)
        if key == ("rev-parse", "--show-toplevel"):
            return _result(str(repo) + "\n")
        if key == ("branch", "--show-current"):
            return _result("feature/head\n")
        if key == ("status", "--porcelain", "--untracked-files=normal"):
            return _result(" M file.py\n" if dirty else "")
        if key == ("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"):
            return _result("origin/feature/head\n")
        if key == ("rev-list", "--left-right", "--count", "@{u}...HEAD"):
            return _result("2 3\n")
        if key == ("rev-parse", "--verify", "refs/remotes/origin/main^{commit}"):
            return _result("abc123\n")
        if key == ("rev-list", "--left-right", "--count", "origin/main...HEAD"):
            return _result("1 4\n")
        if key == ("log", "-3", "--format=%h%x09%s"):
            return _result("abc123\tLatest change\ndef456\tEarlier change\n")
        if key == ("remote", "-v"):
            return _result(
                "origin https://user:secret@example.test/repo.git (fetch)\n"
                "origin https://user:secret@example.test/repo.git (push)\n"
            )
        if key == ("symbolic-ref", "refs/remotes/origin/HEAD"):
            return _result("refs/remotes/origin/main\n")
        if key == ("worktree", "list", "--porcelain"):
            return _result(worktrees)
        raise AssertionError(f"unexpected git call: {args}")

    return run


def test_collects_relevant_worktrees_and_redacts_remote_credentials(tmp_path):
    repo = tmp_path / "canonical checkout"
    base = tmp_path / "base worktree"
    head = tmp_path / "head worktree"
    repo.mkdir()
    base.mkdir()
    head.mkdir()
    worktrees = (
        f"worktree {repo}\nHEAD 1111111\nbranch refs/heads/feature/head\n\n"
        f"worktree {base}\nHEAD 2222222\nbranch refs/heads/main\n\n"
        f"worktree {head}\nHEAD 3333333\nbranch refs/heads/feature/head\n"
    )

    summary = collect_pr_workflow_preflight(
        repo,
        base_branch="main",
        head_branch="feature/head",
        run_git=_runner_for(repo, worktrees, dirty=True),
    )

    assert summary["success"] is True
    assert summary["current"] == {
        "branch": "feature/head",
        "clean": False,
        "changed_count": 1,
        "upstream": "origin/feature/head",
        "ahead": 3,
        "behind": 2,
        "base_ref": "origin/main",
        "base_ahead": 4,
        "base_behind": 1,
    }
    assert summary["recent_commits"][0]["subject"] == "Latest change"
    assert summary["default_branch"] == "main"
    assert summary["remotes"][0]["fetch"] == "https://<redacted>@example.test/repo.git"
    assert "secret" not in summary["output"]
    assert "base worktree" in summary["output"]
    assert "head worktree" in summary["output"]


def test_optional_pr_snapshot_groups_merge_gates_with_git_state(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    worktrees = f"worktree {repo}\nHEAD abc\nbranch refs/heads/feature/head\n"

    def run_gh(args, **kwargs):
        assert args[:3] == ["pr", "view", "42"]
        return _result(
            '{"number":42,"url":"https://github.com/acme/repo/pull/42",'
            '"state":"OPEN","isDraft":false,"headRefOid":"' + "a" * 40 + '",'
            '"headRefName":"feature/head","baseRefName":"main",'
            '"mergeStateStatus":"CLEAN","mergeable":"MERGEABLE",'
            '"reviewDecision":"APPROVED","statusCheckRollup":['
            '{"name":"tests","conclusion":"SUCCESS"}]}'
        )

    summary = collect_pr_workflow_preflight(
        repo,
        base_branch="main",
        head_branch="feature/head",
        pr_ref="42",
        run_git=_runner_for(repo, worktrees),
        run_gh=run_gh,
    )

    assert summary["success"] is True
    assert summary["pr"]["head_sha"] == "a" * 40
    assert summary["pr"]["checks"] == [{"name": "tests", "status": "SUCCESS"}]
    assert "mergeable=MERGEABLE" in summary["output"]
    assert "checks: tests=SUCCESS" in summary["output"]
    assert "clean" in summary["output"]


def test_find_worktree_for_branch_handles_spaces_and_reuses_runner(tmp_path):
    repo = tmp_path / "repo"
    target = tmp_path / "worktree with spaces"
    repo.mkdir()
    target.mkdir()
    output = (
        f"worktree {repo}\nHEAD abc\nbranch refs/heads/main\n\n"
        f"worktree {target}\nHEAD def\nbranch refs/heads/develop\n"
    )

    def run(args, **kwargs):
        assert args == ["worktree", "list", "--porcelain"]
        return _result(output)

    assert find_worktree_for_branch("develop", cwd=repo, run_git=run) == target


def test_large_inventory_is_summarized_without_unrelated_paths(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    records = [f"worktree {repo}\nHEAD root\nbranch refs/heads/main\n"]
    unrelated = []
    for index in range(1_184):
        path = tmp_path / f"unrelated-sentinel-{index:04d}"
        path.mkdir()
        unrelated.append(path)
        records.append(f"worktree {path}\nHEAD {index:04d}\nbranch refs/heads/branch-{index}\n")
    worktrees = "\n".join(records)

    summary = collect_pr_workflow_preflight(
        repo,
        run_git=_runner_for(repo, worktrees),
    )

    assert summary["worktrees"]["counts"]["total"] == 1_185
    assert summary["worktrees"]["counts"]["healthy_unrelated_omitted"] == 1_184
    assert len(summary["output"]) <= 4_096
    assert "unrelated-sentinel" not in summary["output"]


def test_render_caps_malformed_or_verbose_summary():
    rendered = render_pr_workflow_preflight(
        {
            "success": False,
            "canonical_path": "/repo",
            "current": {"branch": "main", "clean": True, "upstream": None},
            "worktrees": {"counts": {"total": 1}, "shown": []},
            "errors": ["worktree inventory: command failed"],
        },
        max_chars=256,
    )
    assert len(rendered) <= 256
    assert "NEEDS ATTENTION" in rendered
