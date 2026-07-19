"""Safely fast-forward a trusted canonical checkout after a verified merge.

This module is deliberately independent of worker/board state.  It is for
orchestrators that have already established a trusted project path, base
branch, and merge commit, and need to make that local checkout reflect the
remote branch without performing destructive recovery.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import re
import subprocess
from typing import Any, Callable

from hermes_cli.pr_workflow_preflight import find_worktree_for_branch


GitRunner = Callable[..., subprocess.CompletedProcess[str]]


_MERGE_COMMIT_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")


def _control_cancelled(control: Any | None) -> bool:
    mutation_allowed = getattr(control, "mutation_allowed", None)
    if callable(mutation_allowed):
        try:
            return not bool(mutation_allowed())
        except Exception:
            return True
    checker = getattr(control, "cancelled", None)
    if not callable(checker):
        return False
    try:
        return bool(checker())
    except Exception:
        return True


def _remaining_timeout(control: Any | None, requested: int | float) -> int | float:
    remaining = getattr(control, "remaining", None)
    if not callable(remaining):
        return requested
    try:
        return max(0.001, min(float(requested), float(remaining())))
    except Exception:
        return 0.001


@dataclass(frozen=True)
class CanonicalCheckoutSyncResult:
    """Outcome of one canonical checkout synchronization attempt."""

    state: str
    error: str
    path: str
    branch: str
    head: str
    merge_commit: str
    synced_at: str

    def as_worker_metadata(self) -> dict[str, str]:
        """Return the established worker metadata names for a caller to persist."""
        return {
            "canonical_sync_state": self.state,
            "canonical_sync_error": self.error,
            "canonical_sync_path": self.path,
            "canonical_sync_branch": self.branch,
            "canonical_sync_head": self.head,
            "canonical_sync_merge_commit": self.merge_commit,
            "canonical_synced_at": self.synced_at,
        }

    def as_dict(self) -> dict[str, str]:
        """Return the neutral result field names for non-worker callers."""
        return {
            "state": self.state,
            "error": self.error,
            "path": self.path,
            "branch": self.branch,
            "head": self.head,
            "merge_commit": self.merge_commit,
            "synced_at": self.synced_at,
        }


def _default_run_git(
    args: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run a direct, bounded Git command without shell or GitHub CLI helpers."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _command_detail(result: subprocess.CompletedProcess[str] | None) -> str:
    if result is None:
        return ""
    return str(result.stderr or result.stdout or "").strip()


def _blocked_result(
    project_path: Path | str,
    branch: str,
    merge_commit: str,
    error: str,
) -> CanonicalCheckoutSyncResult:
    """Build a normalized no-mutation result for boundary validation failures."""
    return CanonicalCheckoutSyncResult(
        state="blocked",
        error=error,
        path=str(project_path or ""),
        branch=str(branch or "").strip(),
        head="",
        merge_commit=str(merge_commit or "").strip(),
        synced_at="",
    )


def resolve_protected_canonical_checkout(
    project_path: Path | str,
    branch: str,
    *,
    run_git: GitRunner | None = None,
    control: Any | None = None,
) -> tuple[Path | None, str | None]:
    """Resolve a protected canonical root on its repository default branch.

    This is the authority boundary shared by the explicit orchestrator tool and
    the dispatcher finalizer.  It deliberately accepts repository default
    branch names beyond ``main``/``master``/``trunk``: some protected project
    checkouts use ``develop``.  A checkout must nevertheless be inside the
    configured protected roots, be named exactly by the caller, currently be
    on the requested branch, and have ``origin/HEAD`` pointing at that branch.
    """
    raw_path = str(project_path or "").strip()
    wanted_branch = str(branch or "").strip()
    if not raw_path:
        return None, "project_path is required"
    if not wanted_branch:
        return None, "branch is required"
    try:
        requested = Path(raw_path).expanduser().resolve(strict=False)
    except Exception:
        requested = Path(raw_path).expanduser()
    if not requested.is_dir():
        return None, f"Canonical checkout missing or invalid: {raw_path}"

    runner = run_git or _default_run_git

    def run(
        args: list[str],
        *,
        timeout: int,
    ) -> subprocess.CompletedProcess[str] | None:
        try:
            return runner(args, cwd=requested, timeout=_remaining_timeout(control, timeout))
        except Exception:
            return None

    root_result = run(["rev-parse", "--show-toplevel"], timeout=20)
    if root_result is None or root_result.returncode != 0:
        detail = _command_detail(root_result)
        return None, f"Canonical checkout root lookup failed{': ' + detail if detail else ''}"
    root_raw = str(root_result.stdout or "").strip()
    if not root_raw:
        return None, "Canonical checkout root lookup failed"
    try:
        repo_root = Path(root_raw).expanduser().resolve(strict=False)
    except Exception:
        repo_root = Path(root_raw).expanduser()
    if requested != repo_root:
        return None, "project_path must name the canonical repository root, not a subdirectory"

    try:
        from tools.canonical_repo_guard import _is_protected_repo_root
    except Exception:
        return None, "Canonical checkout protection configuration is unavailable"
    if not _is_protected_repo_root(repo_root):
        return None, "project_path must be a protected canonical checkout"

    current_result = run(["rev-parse", "--abbrev-ref", "HEAD"], timeout=20)
    if current_result is None or current_result.returncode != 0:
        detail = _command_detail(current_result)
        return None, f"Canonical checkout branch lookup failed{': ' + detail if detail else ''}"
    current_branch = str(current_result.stdout or "").strip()
    if current_branch != wanted_branch:
        return None, f"branch must match checked-out canonical branch {current_branch!r}"

    default_result = run(
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        timeout=20,
    )
    if default_result is None or default_result.returncode != 0:
        detail = _command_detail(default_result)
        return None, f"Canonical checkout default branch lookup failed{': ' + detail if detail else ''}"
    default_ref = str(default_result.stdout or "").strip()
    default_branch = default_ref.removeprefix("refs/remotes/origin/").removeprefix("origin/")
    if not default_branch:
        return None, "Canonical checkout default branch lookup failed"
    if default_branch != wanted_branch:
        return None, f"branch must match origin default branch {default_branch!r}"
    return repo_root, None


def sync_protected_canonical_checkout(
    project_path: Path | str,
    branch: str,
    merge_commit: str,
    *,
    run_git: GitRunner | None = None,
    control: Any | None = None,
) -> CanonicalCheckoutSyncResult:
    """Synchronize only an authority-checked protected canonical checkout."""
    expected_merge_commit = str(merge_commit or "").strip()
    if _control_cancelled(control):
        return _blocked_result(
            project_path,
            branch,
            expected_merge_commit,
            "Canonical checkout synchronization cancelled before mutation",
        )
    if not _MERGE_COMMIT_RE.fullmatch(expected_merge_commit):
        return _blocked_result(
            project_path,
            branch,
            expected_merge_commit,
            "merge_commit must be an exact 40- or 64-character hexadecimal Git SHA",
        )
    root, validation_error = resolve_protected_canonical_checkout(
        project_path,
        branch,
        run_git=run_git,
        control=control,
    )
    if validation_error:
        return _blocked_result(project_path, branch, expected_merge_commit, validation_error)
    assert root is not None
    return sync_canonical_checkout(
        root,
        branch,
        expected_merge_commit,
        run_git=run_git,
        control=control,
    )


def sync_canonical_checkout(
    project_path: Path | str,
    branch: str,
    merge_commit: str,
    *,
    run_git: GitRunner | None = None,
    control: Any | None = None,
) -> CanonicalCheckoutSyncResult:
    """Fetch and fast-forward a clean checkout, then prove it contains a merge.

    The function never resets, stashes, merges, or invokes ``gh``.  If the
    requested branch is attached to another worktree, Git refuses checkout in
    ``project_path``; in that case the clean existing worktree is synchronized
    instead.
    """
    raw_path = str(project_path or "").strip()
    canonical_path = Path(raw_path) if raw_path else Path()
    wanted_branch = str(branch or "").strip()
    expected_merge_commit = str(merge_commit or "").strip()
    runner = run_git or _default_run_git

    def result(
        state: str,
        error: str = "",
        *,
        path: Path | None = None,
        head: str = "",
    ) -> CanonicalCheckoutSyncResult:
        return CanonicalCheckoutSyncResult(
            state=state,
            error=error,
            path=str(path or canonical_path),
            branch=wanted_branch,
            head=head,
            merge_commit=expected_merge_commit,
            synced_at=(
                datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                if state.startswith("synced")
                else ""
            ),
        )

    def fail(
        message: str,
        command: subprocess.CompletedProcess[str] | None = None,
        *,
        path: Path | None = None,
    ) -> CanonicalCheckoutSyncResult:
        detail = _command_detail(command)
        return result("blocked", f"{message}: {detail}" if detail else message, path=path)

    def cancelled(*, path: Path | None = None) -> CanonicalCheckoutSyncResult | None:
        if not _control_cancelled(control):
            return None
        return fail("Canonical checkout synchronization cancelled before mutation", path=path)

    if not raw_path or not canonical_path.is_dir():
        return fail(f"Canonical checkout missing or invalid: {raw_path or '(missing)'}")
    if not wanted_branch:
        return fail("Canonical checkout branch is missing")
    if not expected_merge_commit:
        return fail("Canonical checkout merge commit is missing")

    def run(
        args: list[str],
        *,
        cwd: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        return runner(args, cwd=cwd, timeout=_remaining_timeout(control, timeout))

    def require_clean(sync_path: Path, *, label: str) -> CanonicalCheckoutSyncResult | None:
        try:
            status = run(["status", "--porcelain"], cwd=sync_path, timeout=20)
        except Exception as exc:
            return fail(f"{label} status failed: {exc}", path=sync_path)
        if status.returncode != 0:
            return fail(f"{label} status failed", status, path=sync_path)
        if str(status.stdout or "").strip():
            return fail(f"{label} is dirty", path=sync_path)
        return None

    def verify(sync_path: Path, *, state: str) -> CanonicalCheckoutSyncResult:
        try:
            head_result = run(["rev-parse", "HEAD"], cwd=sync_path, timeout=20)
        except Exception as exc:
            return fail(f"Canonical checkout HEAD lookup failed: {exc}", path=sync_path)
        if head_result.returncode != 0:
            return fail("Canonical checkout HEAD lookup failed", head_result, path=sync_path)
        head = str(head_result.stdout or "").strip()
        try:
            ancestor = run(
                ["merge-base", "--is-ancestor", expected_merge_commit, "HEAD"],
                cwd=sync_path,
                timeout=20,
            )
        except Exception as exc:
            return fail(f"Canonical checkout merge commit verification failed: {exc}", path=sync_path)
        if ancestor.returncode != 0:
            return fail("Canonical checkout does not contain PR merge commit", ancestor, path=sync_path)
        return result(state, path=sync_path, head=head)

    remote_ref = f"refs/remotes/origin/{wanted_branch}"

    blocked = require_clean(canonical_path, label="Canonical checkout")
    if blocked is not None:
        return blocked

    # One normal fetch refreshes the exact remote-tracking ref used below. A
    # bounded exact-object fallback is permitted only when the expected merge
    # commit is still unavailable after that primary fetch.
    fetch_refspec = f"+refs/heads/{wanted_branch}:{remote_ref}"
    cancellation = cancelled(path=canonical_path)
    if cancellation is not None:
        return cancellation
    try:
        fetched = run(
            ["fetch", "origin", "--prune", fetch_refspec],
            cwd=canonical_path,
            timeout=120,
        )
    except Exception as exc:
        return fail(f"Canonical checkout fetch failed: {exc}")
    if fetched.returncode != 0:
        return fail("Canonical checkout fetch failed", fetched)

    try:
        remote = run(["rev-parse", "--verify", remote_ref], cwd=canonical_path, timeout=20)
    except Exception as exc:
        return fail(f"Canonical checkout remote ref verification failed: {exc}")
    if remote.returncode != 0:
        return fail("Canonical checkout remote ref verification failed", remote)

    try:
        commit_available = run(
            ["cat-file", "-e", f"{expected_merge_commit}^{{commit}}"],
            cwd=canonical_path,
            timeout=20,
        )
    except Exception as exc:
        return fail(f"Canonical checkout merge commit lookup failed: {exc}")
    if commit_available.returncode != 0:
        cancellation = cancelled(path=canonical_path)
        if cancellation is not None:
            return cancellation
        try:
            exact_fetch = run(
                ["fetch", "origin", expected_merge_commit],
                cwd=canonical_path,
                timeout=120,
            )
        except Exception as exc:
            return fail(f"Canonical checkout exact commit fetch failed: {exc}")
        if exact_fetch.returncode != 0:
            return fail("Canonical checkout exact commit fetch failed", exact_fetch)
        commit_available = run(
            ["cat-file", "-e", f"{expected_merge_commit}^{{commit}}"],
            cwd=canonical_path,
            timeout=20,
        )
        if commit_available.returncode != 0:
            return fail("Canonical checkout merge commit remains unavailable", commit_available)

    try:
        remote_contains_merge = run(
            ["merge-base", "--is-ancestor", expected_merge_commit, remote_ref],
            cwd=canonical_path,
            timeout=20,
        )
    except Exception as exc:
        return fail(f"Canonical checkout remote ancestry verification failed: {exc}")
    if remote_contains_merge.returncode != 0:
        return fail("Canonical checkout remote ref does not contain PR merge commit", remote_contains_merge)

    cancellation = cancelled(path=canonical_path)
    if cancellation is not None:
        return cancellation
    try:
        checkout = run(["checkout", wanted_branch], cwd=canonical_path, timeout=60)
    except Exception as exc:
        return fail(f"Canonical checkout branch checkout failed: {exc}")
    if checkout.returncode == 0:
        sync_path = canonical_path
        state = "synced"
        label = "Canonical checkout"
    else:
        existing = find_worktree_for_branch(wanted_branch, cwd=canonical_path, run_git=runner)
        if existing is None:
            return fail("Canonical checkout branch checkout failed", checkout)
        blocked = require_clean(existing, label="Existing branch worktree")
        if blocked is not None:
            return blocked
        sync_path = existing
        state = "synced_existing_worktree"
        label = "Existing branch worktree"

    cancellation = cancelled(path=sync_path)
    if cancellation is not None:
        return cancellation
    try:
        merged = run(["merge", "--ff-only", remote_ref], cwd=sync_path, timeout=120)
    except Exception as exc:
        return fail(f"{label} fast-forward merge failed: {exc}", path=sync_path)
    if merged.returncode != 0:
        return fail(f"{label} fast-forward merge failed", merged, path=sync_path)
    return verify(sync_path, state=state)
