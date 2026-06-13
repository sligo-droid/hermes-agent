"""Materialize the local autoreview helper for coding worker workspaces."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


AUTOREVIEW_RELATIVE_HELPER = Path(".agents/skills/autoreview/scripts/autoreview")
AUTOREVIEW_RELATIVE_SKILL = Path(".agents/skills/autoreview/SKILL.md")


_HELPER_TEXT = """#!/usr/bin/env python3
\"\"\"Lightweight local autoreview closeout helper materialized by Hermes.

This helper is deterministic and advisory. It does not run a model review or
claim OpenClaw approval; it gives workers a stable closeout command that records
local repository evidence and reminds them how to handle findings.
\"\"\"

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _run(args: list[str], root: Path) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            args,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except FileNotFoundError:
        return 127, "", f"{args[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{' '.join(args)} timed out"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes local autoreview closeout helper")
    parser.add_argument("--mode", default="local", choices=["local"], help="review mode")
    args = parser.parse_args()
    root = Path.cwd()

    print("autoreview helper: local deterministic closeout")
    print(f"mode: {args.mode}")
    print("status: advisory_not_model_review")
    print("note: This helper does not replace human/model review; verify concrete findings in code.")

    code, inside, err = _run(["git", "rev-parse", "--is-inside-work-tree"], root)
    if code != 0 or inside.lower() != "true":
        print(f"git: unavailable ({err or 'not a git worktree'})")
        return 0

    _, branch, _ = _run(["git", "branch", "--show-current"], root)
    _, status, _ = _run(["git", "status", "--short"], root)
    _, diff_stat, _ = _run(["git", "diff", "--stat"], root)

    print(f"branch: {branch or '(detached)'}")
    print("working_tree:")
    print(status or "  clean")
    print("diff_stat:")
    print(diff_stat or "  no unstaged diff")
    print("closeout:")
    print("- Run focused tests/checks for touched code before this helper.")
    print("- Treat any review findings as advisory; fix only verified in-scope issues.")
    print("- If you edit after review, rerun affected checks and this helper.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
"""


_SKILL_TEXT = """---
name: autoreview
description: Run deterministic local closeout review checks.
---

# Autoreview

Use this worker-local skill after non-trivial code edits and focused checks.

## How to Run

Run:

```bash
.agents/skills/autoreview/scripts/autoreview --mode local
```

The helper is deterministic and advisory. It reports local git evidence and
closeout instructions; it does not claim that a model or human reviewer ran.

## Procedure

- Run focused checks for the files you changed before invoking the helper.
- Treat output and later review findings as advisory.
- Verify any actionable finding in the real code path before fixing it.
- Fix only concrete in-scope issues.
- Rerun affected checks and this helper after review-triggered edits.
"""


def materialize_autoreview_helper(workspace: str | os.PathLike[str]) -> Path:
    """Ensure a deterministic local autoreview helper exists in a worker workspace."""

    root = Path(workspace).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"worker workspace does not exist: {root}")

    helper_path = root / AUTOREVIEW_RELATIVE_HELPER
    skill_path = root / AUTOREVIEW_RELATIVE_SKILL
    helper_path.parent.mkdir(parents=True, exist_ok=True)
    skill_path.parent.mkdir(parents=True, exist_ok=True)

    if not helper_path.exists():
        helper_path.write_text(_HELPER_TEXT, encoding="utf-8")
    if not skill_path.exists():
        skill_path.write_text(_SKILL_TEXT, encoding="utf-8")
    _exclude_materialized_autoreview(root)

    mode = helper_path.stat().st_mode
    helper_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return helper_path


def _exclude_materialized_autoreview(root: Path) -> None:
    """Hide the generated helper from git status without touching tracked files."""

    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:
        return
    if proc.returncode != 0:
        return
    exclude_text = proc.stdout.strip()
    if not exclude_text:
        return
    exclude_path = Path(exclude_text)
    if not exclude_path.is_absolute():
        exclude_path = root / exclude_path
    try:
        exclude_path.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
        entry = "/.agents/skills/autoreview/"
        if entry not in existing.splitlines():
            separator = "" if not existing or existing.endswith("\n") else "\n"
            exclude_path.write_text(f"{existing}{separator}{entry}\n", encoding="utf-8")
    except Exception:
        return


def autoreview_prompt_note(helper_path: Path | None) -> str:
    """Return compact worker prompt text for the materialized helper."""

    if helper_path is None:
        return (
            "Autoreview helper materialization failed before worker start. "
            "Report the unavailable helper explicitly in closeout notes."
        )
    return (
        "Autoreview helper is expected to be available in this worker workspace at "
        f"`{AUTOREVIEW_RELATIVE_HELPER}`. Run `.agents/skills/autoreview/scripts/autoreview --mode local` "
        "after focused checks for non-trivial code edits. The helper is deterministic "
        "and advisory; do not report it as a model review."
    )
