#!/usr/bin/env python3
"""Resolve a bounded, trustworthy CI change range.

Reliable ranges emit changed paths on stdout. Any invalid SHA, missing history,
force-push ancestry break, or bounded-fetch uncertainty deliberately emits an
empty path stream so ``classify_changes.py`` enables every lane.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, NamedTuple


_FULL_SHA_RE = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_ZERO_SHA_RE = re.compile(r"^(?:0{40}|0{64})$")
_DEFAULT_DEEPEN_STEPS = (50, 200)
GitRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class RangeResolution(NamedTuple):
    reliable: bool
    paths: tuple[str, ...] = ()
    reason: str = ""
    base_sha: str = ""
    head_sha: str = ""


def _default_runner(root: Path) -> GitRunner:
    def run(args: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )

    return run


def _valid_sha(value: str, *, allow_zero: bool = False) -> bool:
    if not _FULL_SHA_RE.fullmatch(value or ""):
        return False
    return allow_zero or not _ZERO_SHA_RE.fullmatch(value)


def _commit_exists(run: GitRunner, sha: str) -> bool:
    return run(["cat-file", "-e", f"{sha}^{{commit}}"]).returncode == 0


def _fetch_exact(run: GitRunner, sha: str, *, remote: str) -> bool:
    result = run(["fetch", "--no-tags", "--depth=1", remote, sha])
    return result.returncode == 0 and _commit_exists(run, sha)


def _ensure_commits(run: GitRunner, shas: tuple[str, str], *, remote: str) -> bool:
    for sha in shas:
        if _commit_exists(run, sha):
            continue
        if not _fetch_exact(run, sha, remote=remote):
            return False
    return True


def _prove_pr_merge_base(run: GitRunner, base: str, head: str) -> bool:
    result = run(["merge-base", base, head])
    if result.returncode != 0:
        return False
    merge_base = result.stdout.strip().lower()
    return _valid_sha(merge_base) and _commit_exists(run, merge_base)


def _prove_push_ancestry(run: GitRunner, base: str, head: str) -> bool:
    return run(["merge-base", "--is-ancestor", base, head]).returncode == 0


def _deepen_bounded(
    run: GitRunner,
    *,
    remote: str,
    base: str,
    head: str,
    steps: tuple[int, ...],
    prove: Callable[[], bool],
) -> bool:
    if prove():
        return True
    for raw_step in steps:
        step = max(1, min(1000, int(raw_step)))
        run(["fetch", "--no-tags", f"--deepen={step}", remote, base, head])
        if _ensure_commits(run, (base, head), remote=remote) and prove():
            return True
    return False


def resolve_changed_range(
    root: Path,
    *,
    event_name: str,
    base_sha: str,
    head_sha: str,
    remote: str = "origin",
    deepen_steps: tuple[int, ...] = _DEFAULT_DEEPEN_STEPS,
    run: GitRunner | None = None,
) -> RangeResolution:
    """Return changed paths only after bounded history and ancestry proof."""

    event = str(event_name or "").strip().lower()
    base = str(base_sha or "").strip().lower()
    head = str(head_sha or "").strip().lower()
    if event not in {"pull_request", "push"}:
        return RangeResolution(False, reason="unsupported_event")
    if not _valid_sha(base, allow_zero=event == "push") or not _valid_sha(head):
        return RangeResolution(False, reason="invalid_full_sha")
    if event == "push" and _ZERO_SHA_RE.fullmatch(base):
        return RangeResolution(False, reason="initial_push_has_no_reliable_base", base_sha=base, head_sha=head)

    runner = run or _default_runner(root)
    try:
        if not _ensure_commits(runner, (base, head), remote=remote):
            return RangeResolution(False, reason="commit_unavailable", base_sha=base, head_sha=head)

        if event == "pull_request":
            proven = _deepen_bounded(
                runner,
                remote=remote,
                base=base,
                head=head,
                steps=deepen_steps,
                prove=lambda: _prove_pr_merge_base(runner, base, head),
            )
            diff_spec = f"{base}...{head}"
            reason = "merge_base_unproven"
        else:
            proven = _deepen_bounded(
                runner,
                remote=remote,
                base=base,
                head=head,
                steps=deepen_steps,
                prove=lambda: _prove_push_ancestry(runner, base, head),
            )
            diff_spec = f"{base}..{head}"
            reason = "push_ancestry_unproven"
        if not proven:
            return RangeResolution(False, reason=reason, base_sha=base, head_sha=head)

        diff = runner(["diff", "--name-only", "--no-renames", diff_spec, "--"])
        if diff.returncode != 0:
            return RangeResolution(False, reason="diff_failed", base_sha=base, head_sha=head)
        paths = tuple(line.strip() for line in diff.stdout.splitlines() if line.strip())
        return RangeResolution(True, paths=paths, base_sha=base, head_sha=head)
    except (subprocess.TimeoutExpired, OSError):
        return RangeResolution(False, reason="runner_unavailable", base_sha=base, head_sha=head)
    except Exception:
        # Injected runners may fail in repository- or platform-specific ways.
        # CI must fail open to all lanes rather than fail the classifier job.
        return RangeResolution(False, reason="runner_exception", base_sha=base, head_sha=head)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-name", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args(argv)
    resolution = resolve_changed_range(
        Path(args.root).resolve(),
        event_name=args.event_name,
        base_sha=args.base,
        head_sha=args.head,
        remote=args.remote,
    )
    if resolution.reliable:
        for path in resolution.paths:
            print(path)
    else:
        print(f"resolve_changed_range: fail-open ({resolution.reason})", file=sys.stderr)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
