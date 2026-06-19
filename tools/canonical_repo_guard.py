"""Guards for protecting canonical main checkouts from agent writes.

The Sligo Labs operating model keeps canonical project checkouts read-only
(``$HERMES_HOME/workspace/<project>`` and the Hermes source checkout itself).
Agents should inspect those roots, then create/use git worktrees for edits.
This module enforces that boundary in the local tool layer.
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

_MAIN_BRANCHES = {"main", "master", "trunk"}
_DISABLE_ENV = "HERMES_DISABLE_CANONICAL_REPO_GUARD"
_ALLOW_ENV = "HERMES_ALLOW_CANONICAL_MAIN_WRITES"
_ROOTS_ENV = "HERMES_CANONICAL_REPO_ROOTS"


@dataclass(frozen=True)
class CanonicalRepoInfo:
    repo_root: Path
    branch: str


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _guard_disabled() -> bool:
    return _truthy(os.getenv(_DISABLE_ENV)) or _truthy(os.getenv(_ALLOW_ENV))


def _run_git(args: Sequence[str], *, cwd: Path, timeout: float = 5.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except Exception:
        return 125, ""
    output = "\n".join(
        part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip()
    )
    return proc.returncode, output.strip()


def _split_env_roots(raw: str) -> Iterable[str]:
    # Accept os.pathsep and comma so config/env snippets are forgiving.
    for part in re.split(r"[," + re.escape(os.pathsep) + r"]", raw):
        value = part.strip()
        if value:
            yield value


def _configured_protected_roots() -> list[Path]:
    raw = os.getenv(_ROOTS_ENV, "")
    roots: list[Path] = []
    if raw.strip():
        candidates = [Path(item).expanduser() for item in _split_env_roots(raw)]
    else:
        candidates = []
        # Active profile workspace root.
        hermes_home = os.getenv("HERMES_HOME", "")
        if hermes_home:
            candidates.append(Path(hermes_home).expanduser() / "workspace")
        # Host-default workspace root. Profile subprocess HOME can differ from
        # the real OS home, so keep this explicit fallback for normal installs.
        candidates.append(Path.home().expanduser() / ".hermes" / "workspace")
        # The checked-out Hermes source tree itself. In the installed private
        # fork this resolves to /home/droid/hermes; feature worktrees live
        # elsewhere and are not under this root.
        try:
            candidates.append(Path(__file__).resolve().parents[1])
        except Exception:
            pass

    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except Exception:
            resolved = candidate
        key = str(resolved)
        if key and key not in seen:
            roots.append(resolved)
            seen.add(key)
    return roots


def _existing_probe_path(path: Path) -> Path:
    probe = path if path.is_dir() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return probe


def _path_from_user(value: str | Path) -> Path:
    p = Path(str(value)).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        return p.resolve(strict=False)
    except Exception:
        return p


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_protected_repo_root(repo_root: Path) -> bool:
    for root in _configured_protected_roots():
        if repo_root == root or _is_under(repo_root, root):
            return True
    return False


def _repo_info_for_path(path: str | Path) -> CanonicalRepoInfo | None:
    resolved = _path_from_user(path)
    probe = _existing_probe_path(resolved)
    if not probe.exists():
        return None
    rc, root_raw = _run_git(["rev-parse", "--show-toplevel"], cwd=probe)
    if rc != 0 or not root_raw:
        return None
    try:
        repo_root = Path(root_raw.splitlines()[-1]).expanduser().resolve()
    except Exception:
        repo_root = Path(root_raw.splitlines()[-1]).expanduser()
    if not _is_protected_repo_root(repo_root):
        return None
    rc, branch_raw = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
    if rc != 0:
        return None
    branch = branch_raw.strip()
    if branch not in _MAIN_BRANCHES:
        return None
    return CanonicalRepoInfo(repo_root=repo_root, branch=branch)


def _is_tracked(repo_root: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(repo_root)
    except ValueError:
        return False
    rc, _ = _run_git(["ls-files", "--error-unmatch", "--", str(rel)], cwd=repo_root)
    return rc == 0


def canonical_main_write_violation(path: str | Path, *, require_tracked: bool = True) -> str | None:
    """Return a block message when *path* targets protected canonical main.

    By default this only blocks tracked files. That catches the damaging class
    that dirties project history while leaving incidental untracked scratch/cache
    writes alone.
    """
    if _guard_disabled():
        return None
    resolved = _path_from_user(path)
    info = _repo_info_for_path(resolved)
    if info is None:
        return None
    if require_tracked and not _is_tracked(info.repo_root, resolved):
        return None
    return _format_write_block(path=resolved, info=info)


def canonical_main_worker_violation(workdir: str | Path) -> str | None:
    """Return a block message when a coding worker is pointed at canonical main."""
    return canonical_main_routing_hint(workdir, action="delegate_coding_task")


def canonical_main_routing_hint(workdir: str | Path, *, action: str) -> str | None:
    """Return routing guidance when *action* targets protected canonical main."""
    if _guard_disabled():
        return None
    info = _repo_info_for_path(workdir)
    if info is None:
        return None
    action_text = str(action or "requested action").strip() or "requested action"
    return (
        f"BLOCKED: {action_text} was pointed at a protected canonical "
        f"checkout on {info.branch}: {info.repo_root}. Canonical checkouts are "
        "inspection-only for agents and must not be mutated. This is an "
        "intentional safety guard; do not disable it for agent work. Create "
        "or use a git worktree under /home/droid/workspaces/ and retry with "
        "cwd set to an absolute worktree path such as "
        "/home/droid/workspaces/<repo-task>."
    )


def _format_write_block(*, path: Path, info: CanonicalRepoInfo) -> str:
    return (
        "BLOCKED: refusing to edit tracked file in protected canonical checkout.\n"
        f"- file: {path}\n"
        f"- repo: {info.repo_root}\n"
        f"- branch: {info.branch}\n"
        "Canonical project checkouts are read-only for agents. Create/use a git "
        "worktree under /home/droid/workspaces/ and retry with an absolute path "
        "inside that worktree. If a human truly needs to bypass this guard, set "
        f"{_ALLOW_ENV}=1 before starting Hermes."
    )


def _simple_command_segments(command: str) -> list[str] | None:
    # Redirection/heredocs can mutate files even when the argv looks read-only.
    if re.search(r"(^|[^<])>(?!>)|>>|<<", command):
        return None
    # Pipelines can hide mutating commands on the right-hand side. Keep the
    # allowlist intentionally boring for protected roots.
    if "|" in command:
        return None
    parts = [part.strip() for part in re.split(r"\s*(?:&&|\|\||;|\n)\s*", command) if part.strip()]
    return parts or [command.strip()]


def _git_subcommand(tokens: Sequence[str]) -> tuple[str | None, list[str]]:
    idx = 1
    while idx < len(tokens):
        token = tokens[idx]
        if token in {"-C", "-c"}:
            idx += 2
            continue
        if token == "--no-pager" or token.startswith("--git-dir") or token.startswith("--work-tree"):
            idx += 1
            continue
        if token.startswith("-"):
            idx += 1
            continue
        return token, list(tokens[idx + 1 :])
    return None, []


def _is_allowed_git_command(tokens: Sequence[str]) -> bool:
    subcommand, rest = _git_subcommand(tokens)
    if not subcommand:
        return False
    if subcommand in {"status", "diff", "log", "show", "rev-parse", "branch", "ls-files", "grep", "remote", "fetch"}:
        return True
    if subcommand == "worktree":
        return bool(rest) and rest[0] in {"add", "list", "prune"}
    return False


def _is_read_only_terminal_command(command: str) -> bool:
    segments = _simple_command_segments(command)
    if segments is None:
        return False
    for segment in segments:
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return False
        if not tokens:
            continue
        base = tokens[0]
        if base == "git" and _is_allowed_git_command(tokens):
            continue
        if base in {"pwd", "date", "true", "false", "whoami", "hostname"} and len(tokens) == 1:
            continue
        if base in {"python", "python3", "node", "npm", "pnpm", "uv"} and any(
            token in {"--version", "-V", "-v"} for token in tokens[1:]
        ):
            continue
        return False
    return True


def canonical_main_terminal_violation(workdir: str | Path, command: str) -> str | None:
    """Return a block message for non-read-only terminal commands on canonical main."""
    if _guard_disabled():
        return None
    info = _repo_info_for_path(workdir)
    if info is None:
        return None
    if _is_read_only_terminal_command(command):
        return None
    return (
        "BLOCKED: refusing to run a non-read-only terminal command from a "
        f"protected canonical checkout on {info.branch}: {info.repo_root}. "
        "Canonical project roots are inspection-only. Create/use a git worktree "
        "under /home/droid/workspaces/ and rerun the command with workdir set to "
        "that worktree. Allowed here: boring read-only git/status commands and "
        "git worktree add/list/prune."
    )
