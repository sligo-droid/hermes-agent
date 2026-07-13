"""Bounded, read-only repository state for PR/worktree decisions.

The raw ``git worktree list --porcelain`` inventory is useful to Git but is a
poor conversational result in a workspace with hundreds of worktrees. This
module parses it internally and exposes only the current checkout, requested
PR branches, warning records, and bounded counts/samples.

It deliberately performs no fetch, prune, checkout, or cleanup operation.
Run it with ``python -m hermes_cli.pr_workflow_preflight``.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlsplit, urlunsplit


DEFAULT_MAX_OUTPUT_CHARS = 4_096
_MAX_ERROR_CHARS = 240
_MAX_WARNING_SAMPLES = 6
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)([?&](?:access[_-]?token|api[_-]?key|auth|password|secret|token)=)[^&\s]+"
)

GitRunner = Callable[..., subprocess.CompletedProcess[str]]


def _default_git_runner(
    args: list[str],
    *,
    cwd: Path,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _clip(value: Any, max_chars: int) -> str:
    text = str(value or "").replace("\x00", " ").replace("\r", " ").replace("\n", " ")
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3] + "..."


def _redact_url(url: str) -> str:
    """Remove URL userinfo and token-like query values before rendering."""
    raw = str(url or "")
    try:
        parsed = urlsplit(raw)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            host = parsed.hostname or ""
            if parsed.port:
                host = f"{host}:{parsed.port}"
            netloc = f"<redacted>@{host}" if parsed.username else host
            raw = urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    except Exception:
        pass
    return _SENSITIVE_QUERY_RE.sub(r"\1<redacted>", raw)


def _redact_text(text: str) -> str:
    return _clip(_redact_url(str(text or "")), _MAX_ERROR_CHARS)


def _branch_ref(branch: str | None) -> str | None:
    if not branch:
        return None
    value = str(branch).strip()
    if value.startswith("refs/heads/"):
        return value
    return f"refs/heads/{value}"


def _branch_name(branch: str | None) -> str | None:
    ref = _branch_ref(branch)
    return ref.removeprefix("refs/heads/") if ref else None


def _parse_worktree_porcelain(output: str) -> list[dict[str, Any]]:
    """Parse Git's porcelain worktree records without retaining raw lines."""
    records: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None

    def finish() -> None:
        nonlocal current
        if current and current.get("path"):
            current.setdefault("head", "")
            current.setdefault("branch", None)
            current.setdefault("detached", False)
            current.setdefault("bare", False)
            current.setdefault("locked", None)
            current.setdefault("prunable", None)
            records.append(current)
        current = None

    for raw_line in str(output or "").splitlines():
        line = raw_line.rstrip("\r")
        if not line.strip():
            finish()
            continue
        if line.startswith("worktree "):
            finish()
            current = {"path": line[len("worktree ") :]}
            continue
        if current is None:
            # Ignore malformed leading text rather than exposing it in output.
            continue
        if line.startswith("HEAD "):
            current["head"] = line[len("HEAD ") :].strip()
        elif line.startswith("branch "):
            current["branch"] = line[len("branch ") :].strip()
        elif line == "detached":
            current["detached"] = True
        elif line == "bare":
            current["bare"] = True
        elif line.startswith("locked"):
            current["locked"] = line[len("locked") :].strip() or True
        elif line.startswith("prunable"):
            current["prunable"] = line[len("prunable") :].strip() or True
    finish()
    return records


def find_worktree_for_branch(
    branch: str,
    *,
    cwd: Path | str,
    run_git: GitRunner | None = None,
) -> Optional[Path]:
    """Return the existing checkout for ``branch`` when it is usable."""
    runner = run_git or _default_git_runner
    wanted = _branch_ref(branch)
    if not wanted:
        return None
    try:
        result = runner(
            ["worktree", "list", "--porcelain"],
            cwd=Path(cwd),
            timeout=20,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    for record in _parse_worktree_porcelain(result.stdout or ""):
        if record.get("branch") != wanted:
            continue
        path = Path(str(record.get("path") or ""))
        if path.is_dir() and not record.get("prunable"):
            return path
    return None


def _record_state(record: dict[str, Any], *, current: bool = False) -> str:
    flags: list[str] = []
    if current:
        flags.append("current")
    if record.get("detached"):
        flags.append("detached")
    if record.get("bare"):
        flags.append("bare")
    if record.get("locked"):
        flags.append("locked")
    if record.get("prunable"):
        flags.append("prunable")
    path = Path(str(record.get("path") or ""))
    if not path.exists():
        flags.append("missing")
    return ",".join(flags) or "healthy"


def _serialize_record(record: dict[str, Any], *, current: bool = False) -> dict[str, Any]:
    return {
        "path": str(record.get("path") or ""),
        "branch": _branch_name(record.get("branch")) or None,
        "head": _clip(record.get("head"), 16),
        "state": _record_state(record, current=current),
        "locked": _clip(record.get("locked"), 120) if record.get("locked") else None,
        "prunable": _clip(record.get("prunable"), 120) if record.get("prunable") else None,
    }


def collect_pr_workflow_preflight(
    repo_path: str | Path = ".",
    *,
    base_branch: str | None = None,
    head_branch: str | None = None,
    max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    run_git: GitRunner | None = None,
) -> dict[str, Any]:
    """Collect bounded PR/worktree facts from a local checkout."""
    runner = run_git or _default_git_runner
    requested_path = Path(repo_path).expanduser()
    try:
        requested_path = requested_path.resolve()
    except Exception:
        requested_path = Path(repo_path)

    errors: list[str] = []

    def git_text(
        args: list[str],
        label: str,
        *,
        required: bool = False,
        timeout: int = 20,
    ) -> str:
        try:
            result = runner(args, cwd=requested_path, timeout=timeout)
        except Exception as exc:
            if required:
                errors.append(f"{label}: {_redact_text(exc)}")
            return ""
        if result.returncode != 0:
            if required:
                detail = result.stderr or result.stdout or "command failed"
                errors.append(f"{label}: {_redact_text(detail)}")
            return ""
        return str(result.stdout or "").strip()

    repo_root = git_text(["rev-parse", "--show-toplevel"], "repo root", required=True)
    canonical_path = repo_root or str(requested_path)

    current_branch = git_text(["branch", "--show-current"], "current branch", required=True)
    status_output = git_text(
        ["status", "--porcelain", "--untracked-files=normal"],
        "working tree status",
        required=True,
    )
    changed_lines = [line for line in status_output.splitlines() if line.strip()]

    upstream = git_text(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], "upstream")
    ahead = behind = None
    divergence = git_text(["rev-list", "--left-right", "--count", "@{u}...HEAD"], "divergence")
    if divergence:
        fields = divergence.split()
        if len(fields) >= 2 and all(field.isdigit() for field in fields[:2]):
            behind, ahead = int(fields[0]), int(fields[1])

    remote_output = git_text(["remote", "-v"], "remotes")
    remotes: dict[str, dict[str, str]] = {}
    for line in remote_output.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        remote, url, direction = fields[0], fields[1], fields[2].strip("()")
        entry = remotes.setdefault(remote, {})
        entry.setdefault(direction, _redact_url(url))

    relevant_remote_names = {"origin", "upstream"}
    if upstream and "/" in upstream:
        relevant_remote_names.add(upstream.split("/", 1)[0])
    selected_remotes = {
        name: values
        for name, values in remotes.items()
        if name in relevant_remote_names
    }
    if not selected_remotes and remotes:
        # A repository may use a differently named fork remote. Preserve the
        # one actually associated with @{u} when no conventional names exist.
        selected_names = (
            {upstream.split("/", 1)[0]}
            if upstream and "/" in upstream
            else {next(iter(remotes))}
        )
        selected_remotes = {
            name: values for name, values in remotes.items() if name in selected_names
        }

    default_ref = git_text(["symbolic-ref", "refs/remotes/origin/HEAD"], "default branch")
    default_branch = None
    if default_ref.startswith("refs/remotes/origin/"):
        default_branch = default_ref.removeprefix("refs/remotes/origin/") or None

    worktree_output = git_text(
        ["worktree", "list", "--porcelain"],
        "worktree inventory",
        required=True,
        timeout=30,
    )
    records = _parse_worktree_porcelain(worktree_output)
    current_resolved = Path(canonical_path)
    try:
        current_resolved = current_resolved.resolve()
    except Exception:
        pass

    requested_branches = {
        branch
        for branch in (_branch_name(current_branch), _branch_name(base_branch), _branch_name(head_branch))
        if branch
    }
    relevant: list[tuple[dict[str, Any], bool]] = []
    warning_records: list[tuple[dict[str, Any], bool]] = []
    healthy_unrelated = 0
    for record in records:
        path = Path(str(record.get("path") or ""))
        try:
            is_current = path.resolve() == current_resolved
        except Exception:
            is_current = str(path) == str(current_resolved)
        is_relevant = is_current or _branch_name(record.get("branch")) in requested_branches
        warning = bool(record.get("locked") or record.get("prunable") or not path.exists())
        if is_relevant:
            relevant.append((record, is_current))
        elif warning:
            warning_records.append((record, False))
        else:
            healthy_unrelated += 1

    shown: list[dict[str, Any]] = []
    shown_keys: set[tuple[str, str | None]] = set()
    for record, is_current in [*relevant, *warning_records[:_MAX_WARNING_SAMPLES]]:
        key = (str(record.get("path") or ""), record.get("branch"))
        if key in shown_keys:
            continue
        shown_keys.add(key)
        shown.append(_serialize_record(record, current=is_current))

    counts = {
        "total": len(records),
        "prunable": sum(1 for record in records if record.get("prunable")),
        "locked": sum(1 for record in records if record.get("locked")),
        "detached": sum(1 for record in records if record.get("detached")),
        "missing": sum(1 for record in records if not Path(str(record.get("path") or "")).exists()),
        "stale": sum(
            1
            for record in records
            if record.get("prunable") or not Path(str(record.get("path") or "")).exists()
        ),
        "healthy_unrelated_omitted": healthy_unrelated,
        "warning_samples_omitted": max(0, len(warning_records) - _MAX_WARNING_SAMPLES),
    }

    result = {
        "success": not errors,
        "canonical_path": canonical_path,
        "current": {
            "branch": current_branch or None,
            "clean": not changed_lines,
            "changed_count": len(changed_lines),
            "upstream": upstream or None,
            "ahead": ahead,
            "behind": behind,
        },
        "default_branch": default_branch,
        "remotes": [
            {"name": name, **values}
            for name, values in sorted(selected_remotes.items())
        ],
        "requested_branches": sorted(requested_branches),
        "worktrees": {"counts": counts, "shown": shown},
        "errors": errors,
        "max_output_chars": max(1, int(max_output_chars or DEFAULT_MAX_OUTPUT_CHARS)),
    }
    result["output"] = render_pr_workflow_preflight(
        result,
        max_chars=result["max_output_chars"],
    )
    return result


def render_pr_workflow_preflight(
    summary: dict[str, Any],
    *,
    max_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
) -> str:
    """Render a human-readable summary with a hard character cap."""
    limit = max(1, int(max_chars or DEFAULT_MAX_OUTPUT_CHARS))
    current = summary.get("current") or {}
    counts = (summary.get("worktrees") or {}).get("counts") or {}
    status = "OK" if summary.get("success") else "NEEDS ATTENTION"
    branch = current.get("branch") or "(detached/unknown)"
    clean = "clean" if current.get("clean") else f"dirty ({current.get('changed_count', 0)} changed)"
    upstream = current.get("upstream") or "(none)"
    divergence = "unknown"
    if current.get("ahead") is not None and current.get("behind") is not None:
        divergence = f"ahead {current['ahead']}, behind {current['behind']}"

    lines = [
        f"PR/worktree preflight: {status}",
        f"canonical: {_clip(summary.get('canonical_path'), 420)}",
        f"checkout: branch={_clip(branch, 160)}; {clean}; upstream={_clip(upstream, 180)}; {divergence}",
        f"default branch: {_clip(summary.get('default_branch') or 'unknown', 120)}",
    ]

    remotes = summary.get("remotes") or []
    if remotes:
        remote_text = ", ".join(
            f"{_clip(remote.get('name'), 60)}={_clip(remote.get('fetch') or remote.get('push') or '', 220)}"
            for remote in remotes
        )
        lines.append(f"remotes: {remote_text}")

    lines.append(
        "worktrees: "
        f"total={counts.get('total', 0)}, prunable={counts.get('prunable', 0)}, "
        f"locked={counts.get('locked', 0)}, detached={counts.get('detached', 0)}, "
        f"missing={counts.get('missing', 0)}, stale={counts.get('stale', 0)}, "
        f"healthy-unrelated-omitted={counts.get('healthy_unrelated_omitted', 0)}"
    )

    shown = ((summary.get("worktrees") or {}).get("shown") or [])
    for record in shown:
        branch_text = record.get("branch") or "(detached)"
        state = record.get("state") or "unknown"
        lines.append(
            f"  - {_clip(record.get('path'), 360)} "
            f"[{_clip(branch_text, 140)}] {state}"
        )

    omitted_warnings = counts.get("warning_samples_omitted", 0)
    if omitted_warnings:
        lines.append(f"warning samples omitted: {omitted_warnings}")
    errors = summary.get("errors") or []
    for error in errors:
        lines.append(f"error: {_clip(error, 420)}")

    rendered = "\n".join(lines)
    if len(rendered) <= limit:
        return rendered
    suffix = f"\n[… preflight output capped at {limit} chars …]"
    if limit <= len(suffix):
        return rendered[:limit]
    return rendered[: limit - len(suffix)] + suffix


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render bounded PR/worktree preflight state.")
    parser.add_argument("--repo", default=".", help="Repository/worktree path (default: current directory)")
    parser.add_argument("--base", dest="base_branch", help="PR base branch to include")
    parser.add_argument("--head", dest="head_branch", help="PR head branch to include")
    parser.add_argument(
        "--max-chars",
        type=int,
        default=DEFAULT_MAX_OUTPUT_CHARS,
        help=f"Maximum rendered output characters (default: {DEFAULT_MAX_OUTPUT_CHARS})",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    summary = collect_pr_workflow_preflight(
        args.repo,
        base_branch=args.base_branch,
        head_branch=args.head_branch,
        max_output_chars=args.max_chars,
    )
    print(summary["output"])
    return 0 if summary["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
