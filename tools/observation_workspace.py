"""Workspace-bounded path resolution for observational runtime tools."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from gateway.session_context import get_session_env


def _active_workspace_root(task_id: str = "default") -> Path:
    """Return the same best-effort workspace anchor used by file tools."""

    root: Any = None
    try:
        from tools.file_tools import _authoritative_workspace_root

        root = _authoritative_workspace_root(task_id)
    except Exception:
        root = None
    root = (
        root
        or get_session_env("HERMES_SESSION_CWD", "")
        or os.environ.get("TERMINAL_CWD", "")
        or os.getcwd()
    )
    return Path(str(root)).expanduser().resolve(strict=False)


def _workspace_boundary(anchor: Path) -> Path:
    """Allow the current Git repository, otherwise only the cwd subtree."""

    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "rev-parse", "--show-toplevel"],
            cwd=str(anchor),
            capture_output=True,
            timeout=5,
            check=False,
        )
    except Exception:
        result = None
    if result is not None and result.returncode == 0:
        root = Path(result.stdout.decode(errors="replace").strip()).resolve(strict=False)
        try:
            anchor.relative_to(root)
        except ValueError:
            pass
        else:
            return root
    return anchor


def resolve_observation_workdir(
    value: Any,
    *,
    task_id: str = "default",
) -> tuple[Path | None, str | None]:
    """Resolve one directory without granting arbitrary host-wide inspection."""

    anchor = _active_workspace_root(task_id)
    boundary = _workspace_boundary(anchor)
    raw = str(value or "").strip()
    candidate = Path(raw).expanduser() if raw else anchor
    if not candidate.is_absolute():
        candidate = anchor / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(boundary)
    except ValueError:
        return None, (
            "workdir must remain inside the active workspace boundary "
            f"({boundary})"
        )
    if not resolved.is_dir():
        return None, f"workdir is not an existing directory: {resolved}"
    return resolved, None
