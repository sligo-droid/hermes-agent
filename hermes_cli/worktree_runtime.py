"""Shared dependency reuse and conservative Git worktree cleanup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional


_SCAN_SKIP_DIRS = {
    ".git",
    ".hermes",
    ".next",
    ".svelte-kit",
    ".turbo",
    ".venv",
    "build",
    "dist",
    "node_modules",
    "venv",
}
_PYTHON_LOCK_FILES = (
    "uv.lock",
    "poetry.lock",
    "pdm.lock",
    "requirements.txt",
)


@dataclass(frozen=True)
class WorktreeRecord:
    path: str
    branch: str = ""
    locked: bool = False


@dataclass(frozen=True)
class CleanupDecision:
    path: str
    repo_root: str
    primary_path: str
    branch: str
    age_days: float
    eligible: bool
    reasons: tuple[str, ...]


def _run_git(
    cwd: Path | str,
    args: list[str],
    *,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _git_output(cwd: Path | str, args: list[str], *, timeout: float = 15.0) -> str:
    result = _run_git(cwd, args, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "git command failed").strip())
    return result.stdout.strip()


def _parse_worktree_records(output: str) -> list[WorktreeRecord]:
    records: list[WorktreeRecord] = []
    current: dict[str, Any] = {}
    for raw_line in str(output or "").splitlines():
        line = raw_line.rstrip()
        if not line:
            if current.get("path"):
                records.append(WorktreeRecord(**current))
            current = {}
            continue
        if line.startswith("worktree "):
            if current.get("path"):
                records.append(WorktreeRecord(**current))
            current = {"path": line[len("worktree ") :]}
        elif line.startswith("branch "):
            branch = line[len("branch ") :]
            current["branch"] = branch.removeprefix("refs/heads/")
        elif line == "locked" or line.startswith("locked "):
            current["locked"] = True
    if current.get("path"):
        records.append(WorktreeRecord(**current))
    return records


def git_worktree_records(repo_root: Path | str) -> list[WorktreeRecord]:
    try:
        output = _git_output(repo_root, ["worktree", "list", "--porcelain"], timeout=10)
    except Exception:
        return []
    return _parse_worktree_records(output)


def repo_root_for_path(path: Path | str) -> Optional[Path]:
    try:
        return Path(_git_output(path, ["rev-parse", "--show-toplevel"], timeout=5)).resolve()
    except Exception:
        return None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_roots(
    workdir: Path,
    *,
    lock_names: tuple[str, ...],
    require_package_json: bool = False,
    max_depth: int = 4,
) -> list[Path]:
    roots: list[Path] = []

    def consider(path: Path, files: Optional[set[str]] = None) -> None:
        names = files if files is not None else {item.name for item in path.iterdir() if item.is_file()}
        if not any(name in names for name in lock_names):
            return
        if require_package_json and "package.json" not in names:
            return
        resolved = path.resolve(strict=False)
        if resolved not in roots:
            roots.append(resolved)

    try:
        consider(workdir)
    except Exception:
        pass
    for current, dirs, files in os.walk(workdir):
        current_path = Path(current)
        try:
            depth = len(current_path.relative_to(workdir).parts)
        except Exception:
            depth = 0
        dirs[:] = [name for name in dirs if name not in _SCAN_SKIP_DIRS]
        if depth >= max_depth:
            dirs[:] = []
        consider(current_path, set(files))
    return roots


def pnpm_package_roots(workdir: Path | str, *, max_depth: int = 4) -> list[Path]:
    return _package_roots(
        Path(workdir).resolve(strict=False),
        lock_names=("pnpm-lock.yaml",),
        require_package_json=True,
        max_depth=max_depth,
    )


def javascript_package_roots(workdir: Path | str, *, max_depth: int = 4) -> list[Path]:
    return _package_roots(
        Path(workdir).resolve(strict=False),
        lock_names=("pnpm-lock.yaml", "package-lock.json", "yarn.lock"),
        require_package_json=True,
        max_depth=max_depth,
    )


def _scope_overlaps_path(scope: PurePosixPath, target: PurePosixPath) -> bool:
    common = min(len(scope.parts), len(target.parts))
    return scope.parts[:common] == target.parts[:common]


def dependency_reuse_for_scopes(
    workdir: Path | str,
    scope_paths: Optional[list[str]],
) -> tuple[bool, bool]:
    """Return safe pnpm/Python reuse gates for one coding-worker mutation scope."""

    if scope_paths is None:
        return False, False
    if not scope_paths:
        return True, True
    scopes: list[PurePosixPath] = []
    for raw_scope in scope_paths:
        value = str(raw_scope or "").strip().replace("\\", "/")
        scope = PurePosixPath(value)
        if not value or scope.is_absolute() or ".." in scope.parts:
            return False, False
        scopes.append(scope)

    root = Path(workdir).expanduser().resolve(strict=False)
    repo_root = repo_root_for_path(root)
    if repo_root is None:
        return False, False
    js_targets: list[PurePosixPath] = []
    for package_root in pnpm_package_roots(root):
        try:
            relative = package_root.relative_to(repo_root)
        except ValueError:
            continue
        prefix = PurePosixPath(relative.as_posix())
        js_targets.extend((prefix / "package.json", prefix / "pnpm-lock.yaml"))
    python_targets = [
        PurePosixPath(name)
        for name in ("pyproject.toml", *_PYTHON_LOCK_FILES)
        if (repo_root / name).is_file()
    ]
    pnpm_safe = not any(
        _scope_overlaps_path(scope, target)
        for scope in scopes
        for target in js_targets
    )
    python_safe = not any(
        _scope_overlaps_path(scope, target)
        for scope in scopes
        for target in python_targets
    )
    return pnpm_safe, python_safe


def _config(config: Optional[dict[str, Any]]) -> dict[str, Any]:
    if isinstance(config, dict):
        return config
    try:
        from hermes_cli.config import load_config_readonly

        loaded = load_config_readonly() or {}
        return loaded if isinstance(loaded, dict) else {}
    except Exception:
        return {}


def _reuse_enabled(config: Optional[dict[str, Any]], key: str) -> bool:
    env_key = {
        "pnpm": "HERMES_CODING_WORKER_PNPM_LINKS",
        "python_venv": "HERMES_WORKTREE_PYTHON_VENV_LINKS",
    }[key]
    raw_env = os.getenv(env_key)
    if raw_env is not None:
        return raw_env.strip().lower() in {"1", "true", "yes", "on"}
    cfg = _config(config)
    worktrees = cfg.get("worktrees") if isinstance(cfg.get("worktrees"), dict) else {}
    reuse = (
        worktrees.get("dependency_reuse")
        if isinstance(worktrees.get("dependency_reuse"), dict)
        else {}
    )
    return reuse.get(key, True) is not False


def _same_lock(left: Path, right: Path, name: str) -> bool:
    left_lock = left / name
    right_lock = right / name
    if not left_lock.is_file() or not right_lock.is_file():
        return False
    try:
        return _hash_file(left_lock) == _hash_file(right_lock)
    except Exception:
        return False


def _python_lock_signature(root: Path) -> Optional[str]:
    selected = [name for name in _PYTHON_LOCK_FILES if (root / name).is_file()]
    if not selected:
        return None
    digest = hashlib.sha256()
    for name in ("pyproject.toml", *selected):
        path = root / name
        if not path.is_file():
            continue
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_hash_file(path).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _venv_has_python(venv: Path) -> bool:
    return (venv / "bin" / "python").is_file() or (
        venv / "Scripts" / "python.exe"
    ).is_file()


def _link_directory(link: Path, target: Path, *, replace_existing: bool) -> bool:
    if link.is_symlink():
        return link.resolve(strict=False) == target.resolve(strict=False)
    if link.exists():
        if not replace_existing or not link.is_dir():
            return False
        backup = link.with_name(f".{link.name}.hermes-replaced-{os.getpid()}")
        if backup.exists() or backup.is_symlink():
            return False
        link.rename(backup)
        try:
            link.symlink_to(target.resolve(strict=False), target_is_directory=True)
        except Exception:
            backup.rename(link)
            raise
        shutil.rmtree(backup)
        return True
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target.resolve(strict=False), target_is_directory=True)
    return True


def prepare_worktree_dependency_links(
    workdir: Path | str,
    config: Optional[dict[str, Any]] = None,
    *,
    replace_python_venv: bool = False,
    dry_run: bool = False,
    include_pnpm: bool = True,
    include_python_venv: bool = True,
) -> list[str]:
    """Reuse exact-lock dependencies from the repository's primary worktree."""

    root = Path(workdir).expanduser().resolve(strict=False)
    repo_root = repo_root_for_path(root)
    if repo_root is None:
        return []
    records = git_worktree_records(repo_root)
    if not records:
        return []
    primary = Path(records[0].path).resolve(strict=False)
    if primary == repo_root:
        return []

    notes: list[str] = []
    if include_pnpm and _reuse_enabled(config, "pnpm"):
        for package_root in pnpm_package_roots(root):
            try:
                relative = package_root.relative_to(repo_root)
            except ValueError:
                continue
            primary_package = primary / relative
            modules = package_root / "node_modules"
            primary_modules = primary_package / "node_modules"
            if modules.exists() or modules.is_symlink():
                continue
            if not primary_modules.is_dir() or not _same_lock(
                package_root, primary_package, "pnpm-lock.yaml"
            ):
                continue
            note = (
                f"linked {modules} -> {primary_modules} "
                "(exact lock; unlink before running an install or changing dependencies)"
            )
            try:
                if dry_run or _link_directory(
                    modules,
                    primary_modules,
                    replace_existing=False,
                ):
                    notes.append(note)
            except OSError:
                continue

    if include_python_venv and _reuse_enabled(config, "python_venv"):
        signature = _python_lock_signature(repo_root)
        primary_signature = _python_lock_signature(primary)
        venv = repo_root / ".venv"
        primary_venv = primary / ".venv"
        if (
            signature
            and signature == primary_signature
            and _venv_has_python(primary_venv)
            and not venv.is_symlink()
            and (replace_python_venv or not venv.exists())
        ):
            note = (
                f"linked {venv} -> {primary_venv} "
                "(exact lock; unlink before syncing or changing Python dependencies)"
            )
            try:
                if dry_run or _link_directory(
                    venv,
                    primary_venv,
                    replace_existing=replace_python_venv,
                ):
                    notes.append(note)
            except OSError:
                pass
    return notes


def _active_process_cwds() -> tuple[Path, ...]:
    roots: set[Path] = {Path.cwd().resolve(strict=False)}
    proc = Path("/proc")
    if not proc.is_dir():
        return tuple(roots)
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            roots.add((entry / "cwd").resolve(strict=True))
        except Exception:
            continue
    return tuple(roots)


def _contains_path(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _worktree_activity_timestamp(path: Path) -> float:
    timestamps: list[float] = []
    for candidate in (path, path / ".git"):
        try:
            timestamps.append(candidate.lstat().st_mtime)
        except OSError:
            pass
    try:
        git_dir = Path(_git_output(path, ["rev-parse", "--git-dir"], timeout=5))
        if not git_dir.is_absolute():
            git_dir = path / git_dir
        for name in ("index", "HEAD", "gitdir"):
            try:
                timestamps.append((git_dir / name).stat().st_mtime)
            except OSError:
                pass
    except Exception:
        pass
    return max(timestamps or [0.0])


def inspect_cleanup_candidate(
    path: Path | str,
    *,
    older_than_days: float,
    active_cwds: Optional[Iterable[Path]] = None,
    now: Optional[float] = None,
) -> CleanupDecision:
    target = Path(path).expanduser().resolve(strict=False)
    reasons: list[str] = []
    repo_root = repo_root_for_path(target)
    if repo_root is None or repo_root != target:
        return CleanupDecision(str(target), "", "", "", 0.0, False, ("not_worktree_root",))

    records = git_worktree_records(repo_root)
    record = next(
        (item for item in records if Path(item.path).resolve(strict=False) == target),
        None,
    )
    if record is None:
        return CleanupDecision(str(target), str(repo_root), "", "", 0.0, False, ("unregistered",))
    primary = Path(records[0].path).resolve(strict=False)
    if target == primary:
        reasons.append("primary_worktree")
    if record.locked:
        reasons.append("locked")

    active = tuple(active_cwds) if active_cwds is not None else _active_process_cwds()
    if any(_contains_path(target, cwd.resolve(strict=False)) for cwd in active):
        reasons.append("active_process")

    current_time = time.time() if now is None else float(now)
    activity = _worktree_activity_timestamp(target)
    age_days = max(0.0, (current_time - activity) / 86400) if activity else 0.0
    if age_days < float(older_than_days):
        reasons.append("recent")

    status = _run_git(target, ["status", "--porcelain", "--untracked-files=all"], timeout=20)
    if status.returncode != 0:
        reasons.append("status_unknown")
    elif status.stdout.strip():
        reasons.append("dirty")

    remote_refs = _run_git(
        target,
        ["for-each-ref", "--format=%(refname)", "refs/remotes"],
        timeout=10,
    )
    if remote_refs.returncode != 0 or not remote_refs.stdout.strip():
        reasons.append("no_remote_baseline")
    else:
        unpushed = _run_git(
            target,
            ["log", "--format=%H", "HEAD", "--not", "--remotes"],
            timeout=20,
        )
        if unpushed.returncode != 0:
            reasons.append("unpushed_unknown")
        elif unpushed.stdout.strip():
            reasons.append("unpushed_commits")

    return CleanupDecision(
        path=str(target),
        repo_root=str(repo_root),
        primary_path=str(primary),
        branch=record.branch,
        age_days=age_days,
        eligible=not reasons,
        reasons=tuple(reasons),
    )


def discover_worktree_roots(roots: Iterable[Path | str]) -> list[Path]:
    found: list[Path] = []
    for raw_root in roots:
        root = Path(raw_root).expanduser().resolve(strict=False)
        if not root.is_dir():
            continue
        for child in root.iterdir():
            try:
                if child.is_dir() and (child / ".git").exists():
                    found.append(child.resolve(strict=False))
            except OSError:
                continue
    return sorted(set(found), key=str)


def cleanup_worktrees(
    paths: Iterable[Path | str],
    *,
    older_than_days: float,
    apply: bool,
    limit: int = 0,
    excludes: Iterable[Path | str] = (),
) -> dict[str, Any]:
    active_cwds = _active_process_cwds()
    excluded = tuple(Path(path).expanduser().resolve(strict=False) for path in excludes)
    decisions: list[CleanupDecision] = []
    removed: list[str] = []
    errors: list[dict[str, str]] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve(strict=False)
        if any(path == item or _contains_path(item, path) for item in excluded):
            decisions.append(
                CleanupDecision(str(path), "", "", "", 0.0, False, ("excluded",))
            )
            continue
        decision = inspect_cleanup_candidate(
            path,
            older_than_days=older_than_days,
            active_cwds=active_cwds,
        )
        decisions.append(decision)
        if not decision.eligible or not apply:
            continue
        if limit and len(removed) >= limit:
            continue
        result = _run_git(
            decision.primary_path,
            ["worktree", "remove", decision.path],
            timeout=60,
        )
        if result.returncode == 0:
            removed.append(decision.path)
        else:
            errors.append(
                {
                    "path": decision.path,
                    "error": (result.stderr or result.stdout or "remove failed").strip()[:500],
                }
            )
    return {
        "scanned": len(decisions),
        "eligible": sum(1 for item in decisions if item.eligible),
        "removed": removed,
        "errors": errors,
        "reasons": dict(Counter(reason for item in decisions for reason in item.reasons)),
        "decisions": [asdict(item) for item in decisions],
    }


def maybe_cleanup_terminal_action_worktrees(
    ledger: Any,
    config: Optional[dict[str, Any]],
    action_root: Path | str,
) -> dict[str, Any]:
    """Run one bounded cleanup pass over old terminal Discord action worktrees."""

    cfg = _config(config)
    worktrees = cfg.get("worktrees") if isinstance(cfg.get("worktrees"), dict) else {}
    cleanup = worktrees.get("cleanup") if isinstance(worktrees.get("cleanup"), dict) else {}
    if cleanup.get("enabled", True) is False:
        return {"skipped": "disabled"}
    action_retention = cleanup.get("action_retention_minutes")
    try:
        retention_days = (
            max(1.0, float(action_retention)) / (24 * 60)
            if action_retention is not None
            else max(1.0, float(cleanup.get("retention_days", 7)))
        )
    except (TypeError, ValueError):
        retention_days = 15.0 / (24 * 60)
    action_interval = cleanup.get("action_min_interval_minutes")
    try:
        min_interval_seconds = (
            max(1.0, float(action_interval)) * 60
            if action_interval is not None
            else max(1.0, float(cleanup.get("min_interval_hours", 24))) * 3600
        )
    except (TypeError, ValueError):
        min_interval_seconds = 5 * 60
    try:
        max_per_run = max(1, int(cleanup.get("max_per_run", 25)))
    except (TypeError, ValueError):
        max_per_run = 25

    from hermes_constants import get_hermes_home

    marker = get_hermes_home() / "cache" / "worktree_cleanup.json"
    now = time.time()
    try:
        prior = json.loads(marker.read_text(encoding="utf-8"))
        if now - float(prior.get("completed_at") or 0) < min_interval_seconds:
            return {"skipped": "interval"}
    except Exception:
        pass

    cutoff = now - retention_days * 86400
    raw_paths = ledger.terminal_action_worktree_paths(older_than=cutoff)
    root = Path(action_root).expanduser().resolve(strict=False)
    paths = []
    for raw_path in raw_paths:
        path = Path(raw_path).expanduser().resolve(strict=False)
        if _contains_path(root, path) and "-discord-action-" in path.name:
            paths.append(path)
        if len(paths) >= max_per_run * 4:
            break
    result = cleanup_worktrees(
        paths,
        older_than_days=retention_days,
        apply=True,
        limit=max_per_run,
    )
    try:
        from utils import atomic_json_write

        marker.parent.mkdir(parents=True, exist_ok=True)
        atomic_json_write(
            marker,
            {
                "completed_at": now,
                "scanned": result.get("scanned", 0),
                "removed": len(result.get("removed") or []),
                "errors": len(result.get("errors") or []),
            },
            indent=2,
        )
    except Exception:
        pass
    return result


def terminal_action_cleanup_interval_seconds(
    config: Optional[dict[str, Any]],
) -> float:
    """Return the bounded gateway janitor cadence for action worktrees."""

    cfg = _config(config)
    worktrees = cfg.get("worktrees") if isinstance(cfg.get("worktrees"), dict) else {}
    cleanup = worktrees.get("cleanup") if isinstance(worktrees.get("cleanup"), dict) else {}
    raw = cleanup.get("action_min_interval_minutes")
    if raw is None:
        try:
            return max(60.0, float(cleanup.get("min_interval_hours", 24)) * 3600)
        except (TypeError, ValueError):
            return 24 * 3600.0
    try:
        return max(60.0, min(24 * 3600.0, float(raw) * 60))
    except (TypeError, ValueError):
        return 5 * 60.0


def dedupe_python_venvs(
    paths: Iterable[Path | str],
    *,
    apply: bool,
    excludes: Iterable[Path | str] = (),
    limit: int = 0,
) -> dict[str, Any]:
    path_list = list(paths)
    active = _active_process_cwds()
    excluded = tuple(Path(path).expanduser().resolve(strict=False) for path in excludes)
    linked: list[str] = []
    skipped = Counter()
    errors: list[dict[str, str]] = []
    for raw_path in path_list:
        path = Path(raw_path).expanduser().resolve(strict=False)
        if any(path == item or _contains_path(item, path) for item in excluded):
            skipped["excluded"] += 1
            continue
        if any(_contains_path(path, cwd) for cwd in active):
            skipped["active_process"] += 1
            continue
        repo_root = repo_root_for_path(path)
        records = git_worktree_records(repo_root) if repo_root is not None else []
        record = next(
            (
                item
                for item in records
                if Path(item.path).resolve(strict=False) == path
            ),
            None,
        )
        if record is not None and record.locked:
            skipped["locked"] += 1
            continue
        venv = path / ".venv"
        if not venv.is_dir() or venv.is_symlink():
            skipped["no_local_venv"] += 1
            continue
        try:
            notes = prepare_worktree_dependency_links(
                path,
                replace_python_venv=True,
                dry_run=not apply,
                include_pnpm=False,
            )
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)[:500]})
            continue
        python_notes = [note for note in notes if "/.venv -> " in note]
        if python_notes:
            linked.append(str(path))
        else:
            skipped["no_exact_primary_match"] += 1
        if limit and len(linked) >= limit:
            break
    return {
        "scanned": len(path_list),
        "eligible": len(linked),
        "linked": linked,
        "errors": errors,
        "reasons": dict(skipped),
    }


def _print_result(result: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result, indent=2, sort_keys=True))
        return
    print(f"Scanned:  {result.get('scanned', 0)}")
    print(f"Eligible: {result.get('eligible', 0)}")
    if "removed" in result:
        print(f"Removed:  {len(result.get('removed') or [])}")
    if "linked" in result:
        print(f"Linked:   {len(result.get('linked') or [])}")
    reasons = result.get("reasons") or {}
    if reasons:
        print("Skipped:  " + ", ".join(f"{key}={value}" for key, value in sorted(reasons.items())))
    for path in (result.get("removed") or result.get("linked") or [])[:50]:
        print(f"  {path}")
    for item in (result.get("errors") or [])[:20]:
        print(f"ERROR {item.get('path')}: {item.get('error')}")


def _roots(args: argparse.Namespace) -> list[Path]:
    values = list(args.root or [])
    if not values:
        values = [str(Path.cwd().parent)]
    return [Path(value).expanduser() for value in values]


def cmd_prepare(args: argparse.Namespace) -> int:
    notes = prepare_worktree_dependency_links(args.path or Path.cwd())
    if notes:
        for note in notes:
            print(note)
    else:
        print("No compatible primary-worktree dependencies were available.")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    paths = discover_worktree_roots(_roots(args))
    result = cleanup_worktrees(
        paths,
        older_than_days=args.older_than_days,
        apply=args.apply,
        limit=args.limit,
        excludes=args.exclude or (),
    )
    _print_result(result, json_output=args.json)
    if not args.apply and result["eligible"]:
        print("Dry run only; pass --apply to remove eligible worktrees.")
    return 2 if result["errors"] else 0


def cmd_dedupe(args: argparse.Namespace) -> int:
    paths = discover_worktree_roots(_roots(args))
    result = dedupe_python_venvs(
        paths,
        apply=args.apply,
        excludes=args.exclude or (),
        limit=args.limit,
    )
    _print_result(result, json_output=args.json)
    if not args.apply and result["eligible"]:
        print("Dry run only; pass --apply to replace local .venv directories with symlinks.")
    return 2 if result["errors"] else 0


def register_cli(parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(func=lambda args: parser.print_help())
    commands = parser.add_subparsers(dest="worktrees_command", metavar="COMMAND")

    prepare = commands.add_parser("prepare", help="Reuse exact-lock dependencies from the primary worktree")
    prepare.add_argument("path", nargs="?", help="Worktree path (default: current directory)")
    prepare.set_defaults(func=cmd_prepare)

    cleanup = commands.add_parser("cleanup", help="Remove only clean, inactive, fully-pushed worktrees")
    cleanup.add_argument("--root", action="append", help="Root whose direct children are worktrees; repeatable")
    cleanup.add_argument("--older-than-days", type=float, default=7.0)
    cleanup.add_argument("--exclude", action="append", default=[])
    cleanup.add_argument("--limit", type=int, default=0, help="Maximum removals; 0 means unlimited")
    cleanup.add_argument("--apply", action="store_true", help="Perform removals; default is a dry run")
    cleanup.add_argument("--json", action="store_true")
    cleanup.set_defaults(func=cmd_cleanup)

    dedupe = commands.add_parser("dedupe", help="Replace exact-lock local .venv copies with primary-worktree symlinks")
    dedupe.add_argument("--root", action="append", help="Root whose direct children are worktrees; repeatable")
    dedupe.add_argument("--exclude", action="append", default=[])
    dedupe.add_argument("--limit", type=int, default=0, help="Maximum links; 0 means unlimited")
    dedupe.add_argument("--apply", action="store_true", help="Replace environments; default is a dry run")
    dedupe.add_argument("--json", action="store_true")
    dedupe.set_defaults(func=cmd_dedupe)
