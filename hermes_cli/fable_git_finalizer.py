"""Trusted GitHub lifecycle finalization for Discord ``/fable`` work.

Codex coding workers are implementation inputs: they edit and verify files in
an owned mutable worktree, but they do not receive Git metadata or GitHub PR
authority. This module runs in the parent Hermes process after the worker
returns. Enforce mode hands exact-head state to trusted closeout; shadow/off
mode preserves the authorized legacy CI, merge, and canonical-sync finalizer.
"""

from __future__ import annotations

import contextvars
import json
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent.runtime_phase_classification import classify_runtime_phase
from agent.runtime_spans import RuntimeSpanRecorder, sanitize_runtime_spans
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
_CHECK_MATERIALIZATION_TIMEOUT_SECONDS = 60
_CHECK_MATERIALIZATION_POLL_SECONDS = 2
_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_FABLE_SPAN_RECORDER: contextvars.ContextVar[RuntimeSpanRecorder | None] = (
    contextvars.ContextVar("fable_span_recorder", default=None)
)
_FABLE_SPAN_PARENT: contextvars.ContextVar[str] = contextvars.ContextVar(
    "fable_span_parent",
    default="",
)


@dataclass(frozen=True)
class FableGitPreparation:
    success: bool
    worktree: str = ""
    branch: str = ""
    base_branch: str = ""
    repo: str = ""
    pr_url: str = ""
    resume_existing_pr: bool = False
    recovery_kind: str = ""
    conflict_files: list[str] = field(default_factory=list)
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
    conflict_files: list[str] = field(default_factory=list)
    recovery_kind: str = ""
    recovery_required: bool = False
    next_action: str = ""
    checks_status: str = "not_run"
    canonical_sync_state: str = "not_run"
    commit_performed: bool = False
    push_performed: bool = False
    pr_created: bool = False
    merge_performed: bool = False
    merge_observed: bool = False
    closeout_id: str = ""
    closeout_state: dict[str, Any] = field(default_factory=dict)
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
    recorder = _FABLE_SPAN_RECORDER.get()
    handle = None
    if recorder is not None:
        first = str(args[0] if args else "").strip().lower()
        second = re.sub(
            r"[^a-z0-9_-]",
            "",
            str(args[1] if len(args) > 1 else "command").strip().lower(),
        )
        third = re.sub(
            r"[^a-z0-9_-]",
            "",
            str(args[2] if len(args) > 2 else "command").strip().lower(),
        )
        if first == "git":
            operation = f"git_{second or 'command'}"
        elif first == "gh" and second == "pr":
            operation = f"github_pr_{third or 'command'}"
        elif first == "gh":
            operation = f"github_{second or 'command'}"
        else:
            operation = "fable_command"
        handle = recorder.start(
            operation,
            phase=classify_runtime_phase(operation),
            parent_id=_FABLE_SPAN_PARENT.get(),
            attempt_id="finalization-1",
            concurrency_id="fable-finalization",
            metadata={"operation": operation},
        )
    try:
        result = subprocess.run(
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
    except subprocess.TimeoutExpired:
        if recorder is not None and handle is not None:
            recorder.finish(handle, status="timeout")
        raise
    except Exception:
        if recorder is not None and handle is not None:
            recorder.finish(handle, status="error")
        raise
    if recorder is not None and handle is not None:
        recorder.finish(handle, status="ok" if result.returncode == 0 else "error")
    return result


def _detail(result: subprocess.CompletedProcess[str], fallback: str) -> str:
    return " ".join((result.stderr or result.stdout or fallback).split())[:1200]


def _is_merge_conflict_error(value: str) -> bool:
    normalized = str(value or "").strip().lower()
    return any(
        marker in normalized
        for marker in (
            "has merge conflicts",
            "merge conflict",
            "merge conflicts",
            "not mergeable",
            "conflicting",
        )
    )


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


def _unmerged_files(root: Path) -> list[str]:
    result = _run(
        ["git", "diff", "--name-only", "--diff-filter=U", "-z"],
        cwd=root,
        timeout=30,
    )
    if result.returncode != 0:
        return []
    return [path for path in (result.stdout or "").split("\0") if path]


def _merge_in_progress(root: Path) -> bool:
    result = _run(
        ["git", "rev-parse", "--verify", "-q", "MERGE_HEAD"],
        cwd=root,
        timeout=10,
    )
    return result.returncode == 0 and bool((result.stdout or "").strip())


def prepare_fable_git_lifecycle(workdir: str, mode: str) -> FableGitPreparation:
    """Validate, refresh, or resume an owned Fable worktree before Codex edits it."""
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

    merge_in_progress = _merge_in_progress(root)
    status, error = _status_porcelain(root)
    if error:
        return FableGitPreparation(
            success=False,
            worktree=str(root),
            branch=branch,
            error=error,
        )
    if status.strip() and not merge_in_progress:
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

    pr_url = _existing_pr(
        root,
        repo=repo,
        branch=branch,
        base_branch=base_branch,
    )
    if pr_url:
        if merge_in_progress:
            conflict_files = _unmerged_files(root)
            return FableGitPreparation(
                success=True,
                worktree=str(root),
                branch=branch,
                base_branch=base_branch,
                repo=repo,
                pr_url=pr_url,
                resume_existing_pr=True,
                recovery_kind="merge_conflict" if conflict_files else "base_refresh",
                conflict_files=conflict_files,
            )
        contains_base = _run(
            ["git", "merge-base", "--is-ancestor", f"origin/{base_branch}", "HEAD"],
            cwd=root,
            timeout=30,
        )
        if contains_base.returncode == 0:
            return FableGitPreparation(
                success=True,
                worktree=str(root),
                branch=branch,
                base_branch=base_branch,
                repo=repo,
                pr_url=pr_url,
                resume_existing_pr=True,
                recovery_kind="existing_pr_resume",
            )
        if contains_base.returncode != 1:
            return FableGitPreparation(
                success=False,
                worktree=str(root),
                branch=branch,
                base_branch=base_branch,
                repo=repo,
                pr_url=pr_url,
                resume_existing_pr=True,
                error=_detail(contains_base, "could not compare the Fable PR branch with its base"),
            )

        refreshed = _run(
            ["git", "merge", "--no-commit", "--no-ff", f"origin/{base_branch}"],
            cwd=root,
            timeout=120,
        )
        conflict_files = _unmerged_files(root)
        if refreshed.returncode == 0:
            return FableGitPreparation(
                success=True,
                worktree=str(root),
                branch=branch,
                base_branch=base_branch,
                repo=repo,
                pr_url=pr_url,
                resume_existing_pr=True,
                recovery_kind="base_refresh",
            )
        if conflict_files:
            return FableGitPreparation(
                success=True,
                worktree=str(root),
                branch=branch,
                base_branch=base_branch,
                repo=repo,
                pr_url=pr_url,
                resume_existing_pr=True,
                recovery_kind="merge_conflict",
                conflict_files=conflict_files,
            )
        if _merge_in_progress(root):
            _run(["git", "merge", "--abort"], cwd=root, timeout=60)
        return FableGitPreparation(
            success=False,
            worktree=str(root),
            branch=branch,
            base_branch=base_branch,
            repo=repo,
            pr_url=pr_url,
            resume_existing_pr=True,
            error=_detail(refreshed, f"could not refresh the existing Fable PR from {base_branch}"),
        )

    merged_pr_url = _merged_pr(
        root,
        repo=repo,
        branch=branch,
        base_branch=base_branch,
    )
    if merged_pr_url:
        if merge_in_progress:
            aborted = _run(["git", "merge", "--abort"], cwd=root, timeout=60)
            if aborted.returncode != 0:
                return FableGitPreparation(
                    success=False,
                    worktree=str(root),
                    branch=branch,
                    base_branch=base_branch,
                    repo=repo,
                    pr_url=merged_pr_url,
                    resume_existing_pr=True,
                    recovery_kind="merged_pr_observation",
                    error=_detail(aborted, "could not clear obsolete local merge recovery state"),
                )
        aligned = _run(
            ["git", "merge", "--ff-only", f"origin/{base_branch}"],
            cwd=root,
            timeout=120,
        )
        if aligned.returncode != 0:
            return FableGitPreparation(
                success=False,
                worktree=str(root),
                branch=branch,
                base_branch=base_branch,
                repo=repo,
                pr_url=merged_pr_url,
                resume_existing_pr=True,
                recovery_kind="merged_pr_observation",
                error=_detail(aligned, "could not align the owned branch with its merged PR"),
            )
        return FableGitPreparation(
            success=True,
            worktree=str(root),
            branch=branch,
            base_branch=base_branch,
            repo=repo,
            pr_url=merged_pr_url,
            resume_existing_pr=True,
            recovery_kind="merged_pr_observation",
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
        ["git", "diff", "--cached", "--name-only", "-z"],
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


def _merged_pr(root: Path, *, repo: str, branch: str, base_branch: str) -> str:
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
            "merged",
            "--limit",
            "1",
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
    draft: bool,
) -> tuple[str, bool, str]:
    existing = _existing_pr(
        root,
        repo=repo,
        branch=branch,
        base_branch=base_branch,
    )
    if existing:
        return existing, False, ""
    with tempfile.TemporaryDirectory(prefix="hermes-fable-pr-") as temp_dir:
        body_path = Path(temp_dir) / "body.md"
        body_path.write_text(body, encoding="utf-8")
        create_args = [
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
        ]
        if draft:
            create_args.append("--draft")
        created = _run(
            create_args,
            cwd=root,
            timeout=120,
            github=True,
        )
    if created.returncode != 0:
        return "", False, _detail(created, "gh pr create failed")
    url = next(
        (line.strip() for line in reversed((created.stdout or "").splitlines()) if line.strip()),
        "",
    )
    return url, bool(url), "" if url else "gh pr create returned no PR URL"


def _reported_checks(
    root: Path,
    *,
    repo: str,
    pr_url: str,
) -> tuple[list[dict[str, Any]] | None, str]:
    viewed = _run(
        ["gh", "pr", "view", pr_url, "--repo", repo, "--json", "statusCheckRollup"],
        cwd=root,
        timeout=60,
        github=True,
    )
    if viewed.returncode != 0:
        return None, _detail(viewed, "gh pr view checks failed")
    try:
        payload = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return None, f"gh pr view checks returned invalid JSON: {exc}"
    raw = payload.get("statusCheckRollup") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return [], ""
    return [item for item in raw if isinstance(item, dict)], ""


def _wait_for_checks(root: Path, *, repo: str, pr_url: str) -> str:
    deadline = time.monotonic() + _CHECK_MATERIALIZATION_TIMEOUT_SECONDS
    while True:
        reported, error = _reported_checks(root, repo=repo, pr_url=pr_url)
        if error:
            return error
        if reported:
            break
        if time.monotonic() >= deadline:
            return ""
        time.sleep(_CHECK_MATERIALIZATION_POLL_SECONDS)

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
    return "" if watched.returncode == 0 else _detail(watched, "gh pr checks failed")


def _verified_remote_pr_head(
    root: Path,
    *,
    repo: str,
    pr_url: str,
    intended_commit: str,
) -> tuple[dict[str, Any] | None, str]:
    """Fetch and prove the authoritative remote PR head equals the intended commit."""

    intended = str(intended_commit or "").strip().lower()
    if not _SHA_RE.fullmatch(intended):
        return None, "Fable finalizer could not resolve an exact intended commit SHA."
    viewed = _run(
        [
            "gh",
            "pr",
            "view",
            pr_url,
            "--repo",
            repo,
            "--json",
            "state,isDraft,mergeStateStatus,mergedAt,mergeCommit,url,headRefOid,headRefName,headRepository",
        ],
        cwd=root,
        timeout=60,
        github=True,
    )
    if viewed.returncode != 0:
        return None, _detail(viewed, "gh pr view head verification failed")
    try:
        payload = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError as exc:
        return None, f"gh pr view head verification returned invalid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "gh pr view head verification returned non-object JSON"

    remote_head = str(payload.get("headRefOid") or "").strip().lower()
    head_ref = str(payload.get("headRefName") or "").strip()
    head_repository = payload.get("headRepository")
    head_repo = (
        str(head_repository.get("nameWithOwner") or "").strip()
        if isinstance(head_repository, dict)
        else ""
    )
    if not _SHA_RE.fullmatch(remote_head):
        return None, "GitHub returned an invalid Fable PR head SHA."
    if not head_ref:
        return None, "GitHub did not report the Fable PR head branch."
    if head_repo and head_repo.lower() != repo.lower():
        return None, (
            f"Fable PR head repository {head_repo} does not match trusted checkout origin {repo}."
        )

    checked_ref = _run(
        ["git", "check-ref-format", f"refs/heads/{head_ref}"],
        cwd=root,
        timeout=10,
    )
    if checked_ref.returncode != 0:
        return None, "GitHub returned an invalid Fable PR head branch."
    fetched = _run(
        ["git", "fetch", "origin", f"refs/heads/{head_ref}"],
        cwd=root,
        timeout=300,
        github=True,
    )
    if fetched.returncode != 0:
        return None, _detail(fetched, "could not fetch the authoritative Fable PR head")
    fetched_head = _run(["git", "rev-parse", "FETCH_HEAD"], cwd=root, timeout=10)
    fetched_sha = (fetched_head.stdout or "").strip().lower()
    if fetched_head.returncode != 0 or not _SHA_RE.fullmatch(fetched_sha):
        return None, _detail(fetched_head, "could not resolve the fetched Fable PR head")
    if fetched_sha != remote_head:
        return None, "Fetched Fable PR head does not match GitHub's authoritative head SHA."
    if remote_head != intended:
        return None, (
            "Fable PR head does not match the intended verified commit; "
            "refusing to bind local verification or closeout evidence."
        )
    return payload, ""


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
            "state,isDraft,mergeStateStatus,mergedAt,mergeCommit,url,headRefOid",
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

        sync_result = sync_protected_canonical_checkout(
            str(canonical),
            base_branch,
            merge_commit,
        )
    except Exception as exc:
        return "failed", str(exc)
    if sync_result.state.startswith("synced"):
        return sync_result.state, ""
    return sync_result.state, sync_result.error or "Canonical checkout sync failed"


def _finalize_fable_git_lifecycle_impl(
    preparation: FableGitPreparation,
    *,
    mode: str,
    task: str,
    worker_summary: str,
    closeout_mode: str,
) -> FableGitFinalization:
    """Finalize through legacy authority or enforced structured closeout."""
    normalized_mode = str(mode or "").strip().lower()
    normalized_closeout_mode = str(closeout_mode or "shadow").strip().lower()
    if normalized_closeout_mode not in {"off", "shadow", "enforce"}:
        normalized_closeout_mode = "shadow"
    root = Path(preparation.worktree).resolve(strict=False)
    result = FableGitFinalization(
        success=False,
        status="blocked",
        mode=normalized_mode,
        worktree=str(root),
        branch=preparation.branch,
        base_branch=preparation.base_branch,
        repo=preparation.repo,
        pr_url=preparation.pr_url,
        conflict_files=list(preparation.conflict_files),
        recovery_kind=preparation.recovery_kind,
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
    merge_in_progress = _merge_in_progress(root)
    if not changed_files and not merge_in_progress and not preparation.resume_existing_pr:
        result.success = True
        result.status = "no_changes"
        result.checks_status = "not_needed"
        return result

    title = _commit_title(task)
    if changed_files or merge_in_progress:
        checked = _run(["git", "diff", "--check"], cwd=root, timeout=60)
        if checked.returncode != 0:
            result.error = _detail(checked, "git diff --check failed")
            return result
        staged = _run(["git", "add", "-A"], cwd=root, timeout=60)
        if staged.returncode != 0:
            result.error = _detail(staged, "git add failed")
            return result
        unresolved = _unmerged_files(root)
        if unresolved:
            result.conflict_files = unresolved
            result.error = "Codex returned with unresolved merge conflicts: " + ", ".join(unresolved)
            return result
        cached_checked = _run(["git", "diff", "--cached", "--check"], cwd=root, timeout=60)
        if cached_checked.returncode != 0:
            result.error = _detail(cached_checked, "git diff --cached --check failed")
            return result
        committed = _run(["git", "commit", "-m", title], cwd=root, timeout=120)
        if committed.returncode != 0:
            result.error = _detail(committed, "git commit failed")
            return result
        result.commit_performed = True
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
        result.push_performed = True
    else:
        commit = _run(["git", "rev-parse", "HEAD"], cwd=root, timeout=10)
        result.commit = (commit.stdout or "").strip()

    body = _pr_body(
        task=task,
        worker_summary=worker_summary,
        changed_files=changed_files,
        branch=preparation.branch,
    )
    pr_url = preparation.pr_url
    error = ""
    if not pr_url:
        pr_url, result.pr_created, error = _open_pr(
            root,
            repo=preparation.repo,
            branch=preparation.branch,
            base_branch=preparation.base_branch,
            title=title,
            body=body,
            draft=(
                normalized_mode == FABLE_GIT_LIFECYCLE_PR
                or normalized_closeout_mode == "enforce"
            ),
        )
    result.pr_url = pr_url
    if error:
        result.status = "pushed"
        result.error = error
        return result
    pr_payload, error = _verified_remote_pr_head(
        root,
        repo=preparation.repo,
        pr_url=pr_url,
        intended_commit=result.commit,
    )
    if error or pr_payload is None:
        result.status = "pr_head_verification_blocked"
        result.error = error
        return result
    remote_head = str(pr_payload.get("headRefOid") or "").strip().lower()

    if normalized_closeout_mode != "off":
        from hermes_cli.trusted_closeout import normalize_closeout_state

        canonical = _canonical_checkout(root)
        closeout_id = f"fable:{preparation.repo}:{preparation.branch}:{remote_head}"
        closeout_status = "pr_open" if normalized_mode == FABLE_GIT_LIFECYCLE_PR else "pending"
        result.closeout_id = closeout_id
        result.closeout_state = normalize_closeout_state(
            {
                "id": closeout_id,
                "source": "fable",
                "mode": normalized_closeout_mode,
                "status": closeout_status,
                "workspace": {
                    "path": str(root),
                    "canonical_path": str(canonical or ""),
                    "repository": preparation.repo,
                    "branch": preparation.branch,
                    "base_branch": preparation.base_branch,
                },
                "policy": {
                    "merge": "never" if normalized_mode == FABLE_GIT_LIFECYCLE_PR else "auto",
                    "pr_open": "after_review_approval",
                    "early_draft_pr": True,
                    "require_local_verification": True,
                    "require_review": False,
                    "require_visual_qa": False,
                    "post_merge_requirements": {},
                },
                "local_verification": {"status": "passed", "head_sha": remote_head},
                "review": {"status": "not_required"},
                "visual_qa": {"status": "not_required"},
                "pr": {
                    "url": pr_url,
                    "title": title,
                    "state": str(pr_payload.get("state") or "OPEN"),
                    "is_draft": pr_payload.get("isDraft") is True,
                    "head_sha": remote_head,
                },
                "ci": {
                    "head_sha": remote_head,
                    "status": "not_checked",
                    "wait_state": "queued",
                },
                "next_due_at": 0,
            }
        )

    if normalized_mode == FABLE_GIT_LIFECYCLE_PR:
        result.success = True
        result.status = "pr_opened"
        result.checks_status = "not_waited"
        result.next_action = "PR lifecycle complete; the draft PR remains intentionally unmerged."
        return result

    if normalized_closeout_mode == "enforce":
        result.success = True
        result.status = "closeout_pending"
        result.checks_status = "pending"
        result.next_action = (
            "Persist the closeout state and let the trusted closeout watcher reconcile current-head CI and merge."
        )
        return result

    # Shadow is observational only. The previously-authorized legacy finalizer
    # remains responsible for CI, merge, and canonical sync so an explicit merge
    # request cannot become a permanent read-only handoff.
    checks_error = _wait_for_checks(root, repo=preparation.repo, pr_url=pr_url)
    if checks_error:
        result.status = "pr_opened"
        result.checks_status = "failed_or_timed_out"
        result.error = checks_error
        return result
    result.checks_status = "passed"

    before_merge, error = _verified_remote_pr_head(
        root,
        repo=preparation.repo,
        pr_url=pr_url,
        intended_commit=result.commit,
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
        result.merge_observed = True
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
                "--match-head-commit",
                remote_head,
            ],
            cwd=root,
            timeout=300,
            github=True,
        )
        if merged.returncode != 0:
            result.status = "pr_opened"
            result.error = _detail(merged, "gh pr merge failed")
            if _is_merge_conflict_error(result.error):
                result.recovery_kind = "merge_conflict_retry"
                result.recovery_required = True
                result.next_action = (
                    "Call delegate_coding_task again with the same cwd so trusted Hermes "
                    "can prepare the base merge and Codex can resolve the conflict files."
                )
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
        result.merge_performed = True
        merge = after_merge.get("mergeCommit") or {}
        result.merge_commit = str(merge.get("oid") or "") if isinstance(merge, dict) else ""

    if result.closeout_state:
        result.closeout_state["pr"]["state"] = "MERGED"
        result.closeout_state["pr"]["merge_sha"] = result.merge_commit

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
    aligned = (
        _run(
            ["git", "merge", "--ff-only", f"origin/{preparation.base_branch}"],
            cwd=root,
            timeout=120,
        )
        if fetched.returncode == 0
        else fetched
    )
    if aligned.returncode != 0:
        result.status = "merged_sync_blocked"
        result.error = _detail(aligned, "could not realign the owned Fable worktree")
        return result
    _run(["git", "branch", "--unset-upstream"], cwd=root, timeout=20)

    result.success = True
    result.status = "merged"
    result.next_action = "Legacy trusted Fable finalization completed; shadow closeout remains observational."
    return result


def finalize_fable_git_lifecycle(
    preparation: FableGitPreparation,
    *,
    mode: str,
    task: str,
    worker_summary: str,
    closeout_mode: str = "shadow",
) -> FableGitFinalization:
    """Run trusted Fable finalization with bounded Git/GitHub spans."""

    work_id = (
        f"fable:{preparation.repo}:{preparation.branch}"
        if preparation.repo or preparation.branch
        else "fable-finalization"
    )
    recorder = RuntimeSpanRecorder(work_id=work_id, max_spans=80)
    root_span = recorder.start(
        "fable_finalization",
        phase="closeout",
        attempt_id="finalization-1",
        metadata={
            "operation": "fable_finalization",
            "source": "fable",
        },
    )
    recorder_token = _FABLE_SPAN_RECORDER.set(recorder)
    parent_token = _FABLE_SPAN_PARENT.set(root_span.id)
    try:
        result = _finalize_fable_git_lifecycle_impl(
            preparation,
            mode=mode,
            task=task,
            worker_summary=worker_summary,
            closeout_mode=closeout_mode,
        )
    except Exception:
        recorder.finish(root_span, status="error")
        raise
    finally:
        _FABLE_SPAN_PARENT.reset(parent_token)
        _FABLE_SPAN_RECORDER.reset(recorder_token)
    recorder.finish(
        root_span,
        status="ok" if result.success else "blocked",
        metadata={"outcome": result.status},
    )
    if result.closeout_state:
        telemetry = (
            result.closeout_state.get("telemetry")
            if isinstance(result.closeout_state.get("telemetry"), dict)
            else {}
        )
        telemetry["phase_spans"] = sanitize_runtime_spans(
            [*(telemetry.get("phase_spans") or []), *recorder.export()],
            max_spans=120,
        )
        result.closeout_state["telemetry"] = telemetry
    return result
