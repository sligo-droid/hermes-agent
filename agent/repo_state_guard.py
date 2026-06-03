"""Repository state preflight helpers for autonomous coding workers.

The guard is intentionally non-destructive: it inspects git state and produces a
small prompt block that tells workers when they are standing on a dirty, stale,
or divergent base. Parent Hermes still owns branch/PR lifecycle decisions.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Optional


_MAIN_BRANCHES = {"main", "master", "trunk"}


def _run_git(args: list[str], *, cwd: Path, timeout: float = 10.0) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            encoding="utf-8",
            errors="replace",
        )
    except Exception:
        return 125, ""
    output = "\n".join(
        part.strip() for part in (proc.stdout, proc.stderr) if part and part.strip()
    )
    return proc.returncode, output.strip()


def _short_lines(text: str, *, limit: int) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if len(line) > 180:
            line = line[:177].rstrip() + "..."
        lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def repo_state_preflight(workspace: str | Path, *, max_dirty_files: int = 12) -> Optional[dict[str, Any]]:
    """Return compact git state for *workspace*, or ``None`` outside git.

    The result deliberately contains paths/status only, never file contents.
    """
    try:
        cwd = Path(workspace).expanduser().resolve()
    except Exception:
        cwd = Path(str(workspace)).expanduser()
    if not cwd.exists():
        return None

    rc, root_raw = _run_git(["rev-parse", "--show-toplevel"], cwd=cwd)
    if rc != 0 or not root_raw:
        return None
    root = Path(root_raw.splitlines()[-1]).expanduser()

    branch = ""
    rc, branch_raw = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if rc == 0:
        branch = branch_raw.strip()

    head = ""
    rc, head_raw = _run_git(["rev-parse", "--short", "HEAD"], cwd=root)
    if rc == 0:
        head = head_raw.strip()

    upstream = ""
    ahead = 0
    behind = 0
    rc, upstream_raw = _run_git(
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=root,
    )
    if rc == 0:
        upstream = upstream_raw.strip()
        rc, counts_raw = _run_git(["rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=root)
        if rc == 0:
            parts = counts_raw.split()
            if len(parts) >= 2:
                try:
                    ahead = int(parts[0])
                    behind = int(parts[1])
                except ValueError:
                    ahead = behind = 0

    rc, status_raw = _run_git(["status", "--porcelain=v1", "--untracked-files=normal"], cwd=root)
    dirty_files = _short_lines(status_raw if rc == 0 else "", limit=max_dirty_files)
    dirty_count = len([line for line in (status_raw if rc == 0 else "").splitlines() if line.strip()])

    concerns: list[str] = []
    branch_name = branch if branch != "HEAD" else "detached HEAD"
    if dirty_count:
        concerns.append(f"dirty worktree ({dirty_count} entr{'y' if dirty_count == 1 else 'ies'})")
    if behind:
        concerns.append(f"behind upstream by {behind} commit{'s' if behind != 1 else ''}")
    if ahead:
        concerns.append(f"ahead of upstream by {ahead} commit{'s' if ahead != 1 else ''}")
    if branch in _MAIN_BRANCHES and (dirty_count or ahead or behind):
        concerns.append("mainline branch is not a clean production base")
    if branch == "HEAD":
        concerns.append("detached HEAD")

    severity = "ok"
    if concerns:
        severity = "warning"
    if branch in _MAIN_BRANCHES and (dirty_count or behind):
        severity = "high"

    return {
        "repo_root": str(root),
        "workspace": str(cwd),
        "branch": branch_name,
        "head": head,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "dirty_count": dirty_count,
        "dirty_files": dirty_files,
        "concerns": concerns,
        "severity": severity,
    }


def format_repo_state_preflight(preflight: Optional[dict[str, Any]]) -> str:
    """Format a repo-state prompt block for a coding worker."""
    if not preflight:
        return ""
    concerns = preflight.get("concerns") or []
    if not concerns and not preflight.get("dirty_count"):
        return ""

    upstream = preflight.get("upstream") or "none"
    branch = preflight.get("branch") or "unknown"
    head = preflight.get("head") or "unknown"
    lines = [
        "Repository state preflight:",
        f"- repo: {preflight.get('repo_root')}",
        f"- cwd: {preflight.get('workspace')}",
        f"- branch: {branch}; head: {head}; upstream: {upstream}; "
        f"ahead={preflight.get('ahead', 0)} behind={preflight.get('behind', 0)}",
        f"- severity: {preflight.get('severity', 'unknown')}",
    ]
    if concerns:
        lines.append("- concerns: " + "; ".join(str(item) for item in concerns))
    dirty_files = preflight.get("dirty_files") or []
    dirty_count = int(preflight.get("dirty_count") or 0)
    if dirty_count:
        lines.append(f"- dirty entries shown: {len(dirty_files)} of {dirty_count}")
        for line in dirty_files:
            lines.append(f"  - {line}")
    lines.append(
        "- instruction: preserve unrelated changes. If this dirty/stale/divergent "
        "state conflicts with the task, stop and report the preflight instead of "
        "guessing or overwriting work."
    )
    return "\n".join(lines)
