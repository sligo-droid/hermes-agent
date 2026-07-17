"""Trusted linked-worktree provisioning and merge-back for coding workers."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


_WORKER_COUNTERS: dict[tuple[str, str], int] = {}
_WORKER_COUNTERS_GUARD = threading.Lock()


@dataclass(frozen=True)
class ParallelWorkerContext:
    group_id: str
    base_cwd: str
    base_root: str
    worker_cwd: str
    worker_root: str
    branch: str


@dataclass(frozen=True)
class _PathSnapshot:
    kind: str
    data: bytes | str | None = None
    mode: int = 0


def _git_bytes(
    cwd: str,
    args: list[str],
    *,
    env: Optional[dict[str, str]] = None,
    input_data: Optional[bytes] = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        input=input_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def _git_output(cwd: str, args: list[str], *, timeout: float = 10.0) -> str:
    proc = _git_bytes(cwd, args, timeout=timeout)
    if proc.returncode != 0:
        detail = (
            bytes(proc.stderr or proc.stdout or b"Git command failed")
            .decode("utf-8", errors="replace")
            .strip()
        )
        raise RuntimeError(detail)
    return bytes(proc.stdout or b"").decode("utf-8", errors="surrogateescape").strip()


def _group_slug(group_id: str) -> str:
    stem = re.sub(r"[^a-zA-Z0-9]+", "-", group_id).strip("-").lower() or "group"
    digest = hashlib.sha256(group_id.encode("utf-8", errors="replace")).hexdigest()[:8]
    return f"{stem[:32].rstrip('-') or 'group'}-{digest}"


def provision_parallel_worker(
    base_cwd: str,
    group_id: str,
    *,
    requested_cwd: Optional[str] = None,
) -> ParallelWorkerContext:
    """Create a unique linked worktree for one member of a parallel group."""
    base_path = Path(base_cwd).expanduser().resolve(strict=True)
    base_root = Path(
        _git_output(str(base_path), ["rev-parse", "--show-toplevel"])
    ).resolve()
    execution_path = (
        Path(requested_cwd).expanduser().resolve(strict=True)
        if requested_cwd
        else base_path
    )
    try:
        relative_cwd = execution_path.relative_to(base_root)
    except ValueError as exc:
        raise RuntimeError(
            f"parallel worker cwd {execution_path} is outside base repository {base_root}"
        ) from exc

    slug = _group_slug(group_id)
    counter_key = (str(base_root), group_id)
    with _WORKER_COUNTERS_GUARD:
        worker_number = _WORKER_COUNTERS.get(counter_key, 0)
        while True:
            worker_number += 1
            worker_root = Path(f"{base_root}-pw-{slug}-{worker_number}")
            if not worker_root.exists():
                break
        _WORKER_COUNTERS[counter_key] = worker_number

    branch = (
        f"hermes-parallel/{slug}-{os.getpid()}-{worker_number}-{uuid.uuid4().hex[:8]}"
    )
    proc = _git_bytes(
        str(base_root),
        ["worktree", "add", "-b", branch, str(worker_root), "HEAD"],
        timeout=60.0,
    )
    if proc.returncode != 0:
        _git_bytes(str(base_root), ["branch", "-D", branch])
        detail = (
            bytes(proc.stderr or proc.stdout or b"git worktree add failed")
            .decode("utf-8", errors="replace")
            .strip()
        )
        raise RuntimeError(detail)

    worker_cwd = (worker_root / relative_cwd).resolve(strict=False)
    if not worker_cwd.is_dir():
        _git_bytes(str(base_root), ["worktree", "remove", "--force", str(worker_root)])
        _git_bytes(str(base_root), ["branch", "-D", branch])
        raise RuntimeError(f"parallel worker cwd was not created: {worker_cwd}")
    return ParallelWorkerContext(
        group_id=group_id,
        base_cwd=str(execution_path),
        base_root=str(base_root),
        worker_cwd=str(worker_cwd),
        worker_root=str(worker_root),
        branch=branch,
    )


def _temporary_index_env(index_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_INDEX_FILE"] = str(index_path)
    return env


def _worker_patch(worker_root: str) -> tuple[bytes, list[str]]:
    with tempfile.TemporaryDirectory(
        prefix="hermes-parallel-worker-index-"
    ) as temp_dir:
        env = _temporary_index_env(Path(temp_dir) / "index")
        for args in (["read-tree", "HEAD"], ["add", "-A", "--", "."]):
            proc = _git_bytes(worker_root, args, env=env)
            if proc.returncode != 0:
                raise RuntimeError(
                    bytes(proc.stderr or proc.stdout or b"could not build worker diff")
                    .decode("utf-8", errors="replace")
                    .strip()
                )
        patch_proc = _git_bytes(
            worker_root,
            ["diff", "--cached", "--binary", "--full-index", "HEAD", "--"],
            env=env,
        )
        paths_proc = _git_bytes(
            worker_root,
            ["diff", "--cached", "--name-only", "-z", "HEAD", "--"],
            env=env,
        )
        if patch_proc.returncode != 0 or paths_proc.returncode != 0:
            proc = patch_proc if patch_proc.returncode != 0 else paths_proc
            raise RuntimeError(
                bytes(proc.stderr or proc.stdout or b"could not read worker diff")
                .decode("utf-8", errors="replace")
                .strip()
            )
        paths = [
            item.decode("utf-8", errors="surrogateescape")
            for item in bytes(paths_proc.stdout or b"").split(b"\0")
            if item
        ]
        return bytes(patch_proc.stdout or b""), sorted(paths)


def _snapshot_path(path: Path) -> _PathSnapshot:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _PathSnapshot("absent")
    if stat.S_ISLNK(info.st_mode):
        return _PathSnapshot("symlink", os.readlink(path), stat.S_IMODE(info.st_mode))
    if stat.S_ISREG(info.st_mode):
        return _PathSnapshot("file", path.read_bytes(), stat.S_IMODE(info.st_mode))
    if stat.S_ISDIR(info.st_mode):
        return _PathSnapshot("directory", mode=stat.S_IMODE(info.st_mode))
    return _PathSnapshot("other", mode=stat.S_IMODE(info.st_mode))


def _remove_path(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _restore_paths(base_root: Path, snapshots: dict[str, _PathSnapshot]) -> None:
    for relative_path, snapshot in snapshots.items():
        path = base_root / relative_path
        if snapshot.kind == "absent":
            _remove_path(path)
            parent = path.parent
            while parent != base_root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
            continue
        _remove_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if snapshot.kind == "file":
            path.write_bytes(snapshot.data if isinstance(snapshot.data, bytes) else b"")
            path.chmod(snapshot.mode)
        elif snapshot.kind == "symlink":
            path.symlink_to(str(snapshot.data or ""))
        elif snapshot.kind == "directory":
            path.mkdir(exist_ok=True)
            path.chmod(snapshot.mode)


def _conflict_paths(output: bytes, fallback: list[str]) -> list[str]:
    text = output.decode("utf-8", errors="replace")
    conflicts: set[str] = set()
    patterns = (
        r"Applied patch to ['\"](.+?)['\"] with conflicts",
        r"error: patch failed: (.+?):\d+",
        r"error: (.+?): (?:does not match index|patch does not apply)",
    )
    for pattern in patterns:
        conflicts.update(re.findall(pattern, text))
    return sorted(conflicts or fallback)


def parallel_recovery_action(
    base_cwd: str,
    worker_cwd: str,
    group_id: str,
    conflicts: list[str],
) -> str:
    paths = ", ".join(conflicts) if conflicts else "the reported conflicting files"
    return (
        "Re-delegate a focused conflict-resolution task with cwd set to the kept worker "
        f"worktree {worker_cwd}. Resolve only {paths} against the current turn workspace, "
        "run focused verification, then have trusted Hermes retry "
        f"merge_parallel_worker_result({base_cwd!r}, {worker_cwd!r}, {group_id!r})."
    )


def _cleanup_worktree(
    base_root: str, worker_root: str, branch: str
) -> tuple[bool, str]:
    remove_proc = _git_bytes(
        base_root,
        ["worktree", "remove", "--force", worker_root],
        timeout=60.0,
    )
    if remove_proc.returncode != 0:
        error = (
            bytes(
                remove_proc.stderr or remove_proc.stdout or b"worktree cleanup failed"
            )
            .decode("utf-8", errors="replace")
            .strip()
        )
        return True, error
    branch_proc = _git_bytes(base_root, ["branch", "-D", branch])
    if branch_proc.returncode != 0:
        error = (
            bytes(branch_proc.stderr or branch_proc.stdout or b"branch cleanup failed")
            .decode("utf-8", errors="replace")
            .strip()
        )
        return Path(worker_root).exists(), error
    return False, ""


def merge_parallel_worker_result_unlocked(
    base_cwd: str,
    worker_cwd: str,
    group_id: str,
) -> dict[str, Any]:
    """Apply one worker diff; the caller must hold the parallel-group lock."""
    base_root = Path(_git_output(base_cwd, ["rev-parse", "--show-toplevel"])).resolve()
    worker_root = Path(
        _git_output(worker_cwd, ["rev-parse", "--show-toplevel"])
    ).resolve()
    base_common = Path(_git_output(str(base_root), ["rev-parse", "--git-common-dir"]))
    worker_common = Path(
        _git_output(str(worker_root), ["rev-parse", "--git-common-dir"])
    )
    if not base_common.is_absolute():
        base_common = (base_root / base_common).resolve()
    if not worker_common.is_absolute():
        worker_common = (worker_root / worker_common).resolve()
    if base_common != worker_common:
        raise RuntimeError(
            "parallel worker and base cwd do not share the same Git repository"
        )

    branch = _git_output(str(worker_root), ["symbolic-ref", "--short", "HEAD"])
    patch, changed_paths = _worker_patch(str(worker_root))
    if not patch:
        worktree_kept, cleanup_error = _cleanup_worktree(
            str(base_root), str(worker_root), branch
        )
        return {
            "group_id": group_id,
            "worker_cwd": worker_cwd,
            "merged": True,
            "merge_conflicts": [],
            "worktree_kept": worktree_kept,
            **({"cleanup_error": cleanup_error} if cleanup_error else {}),
        }

    snapshots = {path: _snapshot_path(base_root / path) for path in changed_paths}
    with tempfile.TemporaryDirectory(prefix="hermes-parallel-base-index-") as temp_dir:
        env = _temporary_index_env(Path(temp_dir) / "index")
        for args in (["read-tree", "HEAD"], ["add", "-A", "--", "."]):
            proc = _git_bytes(str(base_root), args, env=env)
            if proc.returncode != 0:
                raise RuntimeError(
                    bytes(proc.stderr or proc.stdout or b"could not prepare base merge")
                    .decode("utf-8", errors="replace")
                    .strip()
                )

        check_proc = _git_bytes(
            str(base_root),
            ["apply", "--3way", "--check", "--whitespace=nowarn", "-"],
            env=env,
            input_data=patch,
        )
        check_output = bytes(check_proc.stdout or b"") + bytes(check_proc.stderr or b"")
        if check_proc.returncode != 0 or b"with conflicts" in check_output:
            conflicts = _conflict_paths(check_output, changed_paths)
            return {
                "group_id": group_id,
                "worker_cwd": worker_cwd,
                "merged": False,
                "merge_conflicts": conflicts,
                "worktree_kept": True,
                "recovery_required": True,
                "next_action": parallel_recovery_action(
                    base_cwd, worker_cwd, group_id, conflicts
                ),
            }

        apply_proc = _git_bytes(
            str(base_root),
            ["apply", "--3way", "--whitespace=nowarn", "-"],
            env=env,
            input_data=patch,
        )
        apply_output = bytes(apply_proc.stdout or b"") + bytes(apply_proc.stderr or b"")
        unmerged_proc = _git_bytes(str(base_root), ["ls-files", "-u", "-z"], env=env)
        if (
            apply_proc.returncode != 0
            or b"with conflicts" in apply_output
            or bool(unmerged_proc.stdout)
        ):
            _restore_paths(base_root, snapshots)
            conflicts = _conflict_paths(apply_output, changed_paths)
            return {
                "group_id": group_id,
                "worker_cwd": worker_cwd,
                "merged": False,
                "merge_conflicts": conflicts,
                "worktree_kept": True,
                "recovery_required": True,
                "next_action": parallel_recovery_action(
                    base_cwd, worker_cwd, group_id, conflicts
                ),
            }

    worktree_kept, cleanup_error = _cleanup_worktree(
        str(base_root), str(worker_root), branch
    )
    return {
        "group_id": group_id,
        "worker_cwd": worker_cwd,
        "merged": True,
        "merge_conflicts": [],
        "worktree_kept": worktree_kept,
        **({"cleanup_error": cleanup_error} if cleanup_error else {}),
    }
