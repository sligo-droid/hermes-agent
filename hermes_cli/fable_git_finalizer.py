"""Trusted GitHub lifecycle finalization for Discord ``/fable`` work.

Codex coding workers are implementation inputs: they edit and verify files in
an owned mutable worktree, but they do not receive Git metadata or GitHub PR
authority.  This module runs in the parent Hermes process after the worker
returns and owns commit, push, PR, CI, merge, and canonical-checkout sync.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from hermes_cli.github_remote import (
    github_cli_env,
    github_origin_repo,
    github_remote_preflight_error,
)
from hermes_cli.pr_body_format import check_project_state_requirement


FABLE_GIT_LIFECYCLE_PR = "pr"
FABLE_GIT_LIFECYCLE_MERGE = "merge"
_FABLE_GIT_LIFECYCLE_MODES = {
    FABLE_GIT_LIFECYCLE_PR,
    FABLE_GIT_LIFECYCLE_MERGE,
}
_PROTECTED_BRANCHES = {"main", "master"}
_CHECK_TIMEOUT_SECONDS = 900


@dataclass(frozen=True)
class FableGitPreparation:
    success: bool
    worktree: str = ""
    branch: str = ""
    base_branch: str = ""
    repo: str = ""
    error: str = ""


@dataclass
class FableGitFinalization:
    success: bool
    status: str
    mode: str
    worktree: str = ""
    branch: str = ""
    base_branch: str = ""
    repo: str = ""
    commit: str = ""
    pr_url: str = ""
    merge_commit: str = ""
    changed_files: list[str] = field(default_factory=list)
    checks_status: str = "not_run"
    canonical_sync_state: str = "not_run"
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _run(
    args: list[str],
    *,
    cwd: Path,
    timeout: int | float = 60,
    github: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        env=github_cli_env() if github else None,
    )


def _detail(result: subprocess.CompletedProcess[str], fallback: str) -> str:
    return " ".join((result.stderr or result.stdout or fallback).split())[:1200]


def _git_root(workdir: str) -> tuple[Path | None, str]:
    path = Path(str(workdir or "")).expanduser().resolve(strict=False)
    if not path.is_dir():
        return None, f"Fable worktree does not exist: {path}"
    result = _run(["git", "rev-parse", "--show-toplevel"], cwd=path, timeout=10)
    if result.returncode != 0 or not (result.stdout or "").strip():
        return None, _detail(result, f"{path} is not a Git worktree")
    return Path(result.stdout.strip()).resolve(strict=False), ""


def _current_branch(root: Path) -> tuple[str, str]:
    result = _run(["git", "branch", "--show-current"], cwd=root, timeout=10)
    branch = (result.stdout or "").strip()
    if result.returncode != 0 or not branch:
        return "", _detail(result, "Fable worktree is detached or has no branch")
    return branch, ""


def _default_branch(root: Path, repo: str) -> str:
    viewed = _run(
        [
            "gh",
            "repo",
            "view",
            repo,
            "--json",
            "defaultBranchRef",
            "--jq",
            ".defaultBranchRef.name",
        ],
        cwd=root,
        timeout=30,
        github=True,
    )
    branch = (viewed.stdout or "").strip()
    if viewed.returncode == 0 and branch:
        return branch
    symbolic = _run(
        ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        cwd=root,
        timeout=10,
    )
    value = (symbolic.stdout or "").strip()
    if symbolic.returncode == 0 and value.startswith("origin/"):
        return value.split("/", 1)[1]
    return "main"


def _status_porcelain(root: Path) -> tuple[str, str]:
    result = _run(
        ["git", "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=root,
        timeout=20,
    )
    if result.returncode != 0:
        return "", _detail(result, "git status failed")
    return result.stdout or "", ""


def prepare_fable_git_lifecycle(workdir: str, mode: str) -> FableGitPreparation:
    """Validate and refresh an owned Fable worktree before Codex edits it."""
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in _FABLE_GIT_LIFECYCLE_MODES:
        return FableGitPreparation(success=True, worktree=str(workdir or ""))

    root, error = _git_root(workdir)
    if root is None:
        return FableGitPreparation(success=False, error=error)
    branch, error = _current_branch(root)
    if error:
        return FableGitPreparation(success=False, worktree=str(root), error=error)
    if branch in _PROTECTED_BRANCHES:
        return FableGitPreparation(
            success=False,
            worktree=str(root),
            branch=branch,
            error=f"Refusing Fable Git lifecycle on protected branch {branch}.",
        )

    status, error = _status_porcelain(root)
    if error:
        return FableGitPreparation(
            success=False,
            worktree=str(root),
            branch=branch,
            error=error,
        )
    if status.strip():
        return FableGitPreparation(
            success=False,
            worktree=str(root),
            branch=branch,
            error=(
                "Fable Git lifecycle requires a clean owned worktree before the "
                "Codex worker starts; preserve or reconcile the existing changes first."
            ),
        )

    remote_error = github_remote_preflight_error(root, operation="finalize Fable PR")
    if remote_error:
        return FableGitPreparation(
            success=False,
            worktree=str(root),
            branch=branch,
            error=remote_error,
        )
    repo = github_origin_repo(root)
    if not repo:
        return FableGitPreparation(
            success=False,
            worktree=str(root),
            branch=branch,
            error="Cannot finalize Fable PR: origin is not a GitHub repository.",
        )

    auth = _run(["gh", "auth", "status"], cwd=root, timeout=30, github=True)
    if auth.returncode != 0:
        return FableGitPreparation(
            success=False,
            worktree=str(root),
            branch=branch,
            repo=repo,
            error=_detail(auth, "GitHub CLI authentication is unavailable"),
        )

    base_branch = _default_branch(root, repo)
    if branch == base_branch:
        return FableGitPreparation(
            success=False,
            worktree=str(root),
            branch=branch,
            base_branch=base_branch,
            repo=repo,
            error=f"Refusing Fable Git lifecycle on default branch {base_branch}.",
        )

    fetched = _run(
        ["git", "fetch", "origin", base_branch],
        cwd=root,
        timeout=300,
        github=True,
    )
    if fetched.returncode != 0:
        return FableGitPreparation(
            success=False,
            worktree=str(root),
            branch=branch,
            base_branch=base_branch,
            repo=repo,
            error=_detail(fetched, f"git fetch origin {base_branch} failed"),
        )
    refreshed = _run(
        ["git", "merge", "--ff-only", f"origin/{base_branch}"],
        cwd=root,
        timeout=120,
    )
    if refreshed.returncode != 0:
        return FableGitPreparation(
            success=False,
            worktree=str(root),
            branch=branch,
            base_branch=base_branch,
            repo=repo,
            error=(
                "Fable worktree is not a clean fast-forward of the current remote "
                f"base {base_branch}: {_detail(refreshed, 'git merge --ff-only failed')}"
            ),
        )

    return FableGitPreparation(
        success=True,
        worktree=str(root),
        branch=branch,
        base_branch=base_branch,
        repo=repo,
    )


def _changed_files(root: Path) -> list[str]:
    changed: list[str] = []
    seen: set[str] = set()
    for args in (
        ["git", "diff", "--name-only", "-z"],
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
    ):
        result = _run(args, cwd=root, timeout=30)
        if result.returncode != 0:
            continue
        for raw in (result.stdout or "").split("\0"):
            path = raw.strip()
            if path and path not in seen:
                seen.add(path)
                changed.append(path)
    return changed


def _single_line(value: str, *, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip(" .,:;-") + "…"


def _commit_title(task: str) -> str:
    summary = _single_line(task, limit=58) or "complete delegated implementation"
    return _single_line(f"feat(fable): {summary}", limit=72)


def _pr_body(
    *,
    task: str,
    worker_summary: str,
    changed_files: list[str],
    branch: str,
) -> str:
    request = _single_line(task, limit=240) or "Complete the requested Fable implementation."
    worker_result = _single_line(worker_summary, limit=300) or "Codex worker completed the requested implementation."
    body = (
        "## Summary\n"
        f"- {request}\n"
        f"- Codex implemented the change in Hermes-owned branch `{branch}`; trusted Hermes code owns the PR lifecycle.\n\n"
        "## Verification\n"
        f"- Worker handoff: {worker_result}\n"
        "- Hermes finalizer ran `git diff --check` before committing.\n\n"
        "Project-state: not needed - this change does not alter the repository's operational routing cursor."
    )
    ok, _message = check_project_state_requirement(body, changed_files)
    if ok:
        return body
    return (
        f"{body}\n\n"
        "Project-state: not needed - the implementation changes runtime behavior but does not change active project routing or pickup facts."
    )


def _existing_pr(root: Path, *, repo: str, branch: str, base_branch: str) -> str:
    result = _run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            branch,
            "--base",
            base_branch,
            "--state",
            "open",
            "--json",
            "url",
            "--jq",
            ".[0].url",
        ],
        cwd=root,
        timeout=30,
        github=True,
    )
    if result.returncode != 0:
        return ""
    value = (result.stdout or "").strip()
    return "" if value == "null" else value


def _open_pr(
    root: Path,
    *,
    repo: str,
    branch: str,
    base_branch: str,
    title: str,
    body: str,
) -> tuple[str, str]:
    existing = _existing_pr(
        root,
        repo=repo,
        branch=branch,
        base_branch=base_branch,
    )
    if existing:
        return existing, ""
    with tempfile.TemporaryDirectory(prefix="hermes-fable-pr-") as temp_dir:
        body_path = Path(temp_dir) / "body.md"
        body_path.write_text(body, encoding="utf-8")
        created = _run(
            [
                "gh",
                "pr",
                "create",
                "--repo",
                repo,
                "--base",
                base_branch,
                "--head",
                branch,
                "--title",
                title,
                "--body-file",
                str(body_path),
            ],
            cwd=root,
            timeout=120,
            github=True,
        )
    if created.returncode != 0:
        return "", _detail(created, "gh pr create failed")
    url = next(
        (line.strip() for line in reversed((created.stdout or "").splitlines()) if line.strip()),
        "",
    )
    return url, "" if url else "gh pr create returned no PR URL"


def _wait_for_checks(root: Path, *, repo: str, pr_url: str) -> str:
    watched = _run(
        [
            "gh",
            "pr",
            "checks",
            pr_url,
            "--repo",
            repo,
            "--watch",
            "--interval",
            "10",
        ],
        cwd=root,
        timeout=_CHECK_TIMEOUT_SECONDS,
        github=True,
    )
    if watched.returncode == 0:
        return ""
    detail = _detail(watched, "gh pr checks failed")
    if "no checks reported" in detail.lower():
        return ""
    return detail


def _pr_state(root: Path, *, repo: str, pr_url: str) -> tuple[dict[str, Any] | None, str]:
    viewed = _run(
        [
            "gh",
            "pr",
            "view",
            pr_url,
            "--repo",
            repo,
            "--json",
            "state,isDraft,mergeStateStatus,mergedAt,mergeCommit,url",
        ],
        cwd=root,
        timeout=60,
        github=True,
    )
    if viewed.returncode != 0:
        return None, _detail(viewed, "gh pr view failed")
    try:
        payload = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return None, f"gh pr view returned invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "gh pr view returned non-object JSON"
    return payload, ""


def _canonical_checkout(root: Path) -> Path | None:
    result = _run(["git", "rev-parse", "--git-common-dir"], cwd=root, timeout=10)
    raw = (result.stdout or "").strip()
    if result.returncode != 0 or not raw:
        return None
    common = Path(raw).expanduser()
    if not common.is_absolute():
        common = root / common
    common = common.resolve(strict=False)
    if common.name != ".git":
        return None
    canonical = common.parent
    return None if canonical == root else canonical


def _sync_after_merge(
    root: Path,
    *,
    base_branch: str,
    merge_commit: str,
) -> tuple[str, str]:
    canonical = _canonical_checkout(root)
    if canonical is None:
        return "not_applicable", ""
    try:
        from hermes_cli.canonical_checkout_sync import sync_protected_canonical_checkout

        result = sync_protected_canonical_checkout(
            str(canonical),
            base_branch,
            merge_commit,
        )
    except Exception as exc:
        return "failed", str(exc)
    if result.state.startswith("synced"):
        return result.state, ""
    return result.state, result.error or "Canonical checkout sync failed"


def finalize_fable_git_lifecycle(
    preparation: FableGitPreparation,
    *,
    mode: str,
    task: str,
    worker_summary: str,
) -> FableGitFinalization:
    """Commit and finalize the PR after a Codex worker returns."""
    normalized_mode = str(mode or "").strip().lower()
    root = Path(preparation.worktree).resolve(strict=False)
    result = FableGitFinalization(
        success=False,
        status="blocked",
        mode=normalized_mode,
        worktree=str(root),
        branch=preparation.branch,
        base_branch=preparation.base_branch,
        repo=preparation.repo,
    )
    if not preparation.success:
        result.error = preparation.error
        return result
    if normalized_mode not in _FABLE_GIT_LIFECYCLE_MODES:
        result.success = True
        result.status = "not_requested"
        return result

    changed_files = _changed_files(root)
    result.changed_files = changed_files
    if not changed_files:
        result.success = True
        result.status = "no_changes"
        result.checks_status = "not_needed"
        return result

    checked = _run(["git", "diff", "--check"], cwd=root, timeout=60)
    if checked.returncode != 0:
        result.error = _detail(checked, "git diff --check failed")
        return result
    staged = _run(["git", "add", "-A"], cwd=root, timeout=60)
    if staged.returncode != 0:
        result.error = _detail(staged, "git add failed")
        return result
    title = _commit_title(task)
    committed = _run(["git", "commit", "-m", title], cwd=root, timeout=120)
    if committed.returncode != 0:
        result.error = _detail(committed, "git commit failed")
        return result
    commit = _run(["git", "rev-parse", "HEAD"], cwd=root, timeout=10)
    result.commit = (commit.stdout or "").strip()

    pushed = _run(
        ["git", "push", "-u", "origin", preparation.branch],
        cwd=root,
        timeout=300,
        github=True,
    )
    if pushed.returncode != 0:
        result.status = "committed"
        result.error = _detail(pushed, "git push failed")
        return result

    body = _pr_body(
        task=task,
        worker_summary=worker_summary,
        changed_files=changed_files,
        branch=preparation.branch,
    )
    pr_url, error = _open_pr(
        root,
        repo=preparation.repo,
        branch=preparation.branch,
        base_branch=preparation.base_branch,
        title=title,
        body=body,
    )
    result.pr_url = pr_url
    if error:
        result.status = "pushed"
        result.error = error
        return result
    if normalized_mode == FABLE_GIT_LIFECYCLE_PR:
        result.success = True
        result.status = "pr_opened"
        result.checks_status = "not_waited"
        return result

    checks_error = _wait_for_checks(root, repo=preparation.repo, pr_url=pr_url)
    if checks_error:
        result.status = "pr_opened"
        result.checks_status = "failed_or_timed_out"
        result.error = checks_error
        return result
    result.checks_status = "passed"

    before_merge, error = _pr_state(
        root,
        repo=preparation.repo,
        pr_url=pr_url,
    )
    if error or before_merge is None:
        result.status = "pr_opened"
        result.error = error
        return result
    if bool(before_merge.get("isDraft")):
        result.status = "pr_opened"
        result.error = "Fable finalizer refuses to merge a draft PR."
        return result
    if str(before_merge.get("state") or "").upper() == "MERGED":
        merge = before_merge.get("mergeCommit") or {}
        result.merge_commit = str(merge.get("oid") or "") if isinstance(merge, dict) else ""
    else:
        merged = _run(
            [
                "gh",
                "pr",
                "merge",
                pr_url,
                "--repo",
                preparation.repo,
                "--merge",
                "--delete-branch",
            ],
            cwd=root,
            timeout=300,
            github=True,
        )
        if merged.returncode != 0:
            result.status = "pr_opened"
            result.error = _detail(merged, "gh pr merge failed")
            return result
        after_merge, error = _pr_state(
            root,
            repo=preparation.repo,
            pr_url=pr_url,
        )
        if error or after_merge is None:
            result.status = "pr_opened"
            result.error = error
            return result
        if str(after_merge.get("state") or "").upper() != "MERGED":
            result.status = "pr_opened"
            result.error = "GitHub did not report the PR merged after gh pr merge."
            return result
        merge = after_merge.get("mergeCommit") or {}
        result.merge_commit = str(merge.get("oid") or "") if isinstance(merge, dict) else ""

    sync_state, sync_error = _sync_after_merge(
        root,
        base_branch=preparation.base_branch,
        merge_commit=result.merge_commit,
    )
    result.canonical_sync_state = sync_state
    if sync_error:
        result.status = "merged_sync_blocked"
        result.error = sync_error
        return result

    fetched = _run(
        ["git", "fetch", "origin", preparation.base_branch],
        cwd=root,
        timeout=300,
        github=True,
    )
    aligned = _run(
        ["git", "merge", "--ff-only", f"origin/{preparation.base_branch}"],
        cwd=root,
        timeout=120,
    ) if fetched.returncode == 0 else fetched
    if aligned.returncode != 0:
        result.status = "merged_sync_blocked"
        result.error = _detail(aligned, "could not realign the owned Fable worktree")
        return result
    _run(["git", "branch", "--unset-upstream"], cwd=root, timeout=20)

    result.success = True
    result.status = "merged"
    return result
