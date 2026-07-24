"""Read-only adoption of an agent-completed GitHub lifecycle."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from hermes_cli.trusted_closeout import normalize_closeout_state


_PR_URL_RE = re.compile(
    r"https://github\.com/(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)"
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SUCCESS_CONCLUSIONS = {"SUCCESS", "SKIPPED", "NEUTRAL"}


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


def _run(
    args: list[str],
    *,
    cwd: Path,
    timeout: float = 60,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _successful_check_rollup(value: Any) -> tuple[bool, int, list[dict[str, str]]]:
    if not isinstance(value, list) or not value:
        return False, 0, []
    latest: dict[tuple[str, str, str], tuple[tuple[str, int], dict[str, Any]]] = {}
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            return False, 0, []
        kind = str(raw.get("__typename") or "")
        name = str(raw.get("name") or raw.get("context") or kind or "check")[:160]
        workflow = str(raw.get("workflowName") or "")[:160]
        timestamps = [
            str(raw.get(field) or "")
            for field in ("completedAt", "startedAt")
            if str(raw.get(field) or "") not in {"", "0001-01-01T00:00:00Z"}
        ]
        rank = (max(timestamps, default=""), index)
        identity = (kind.casefold(), workflow.casefold(), name.casefold())
        previous = latest.get(identity)
        if previous is None or rank >= previous[0]:
            latest[identity] = (rank, raw)

    checks: list[dict[str, str]] = []
    for _rank, raw in latest.values():
        kind = str(raw.get("__typename") or "")
        name = str(raw.get("name") or raw.get("context") or kind or "check")[:160]
        workflow = str(raw.get("workflowName") or "")[:160]
        if kind == "StatusContext":
            successful = str(raw.get("state") or "").upper() == "SUCCESS"
        else:
            successful = (
                str(raw.get("status") or "").upper() == "COMPLETED"
                and str(raw.get("conclusion") or "").upper() in _SUCCESS_CONCLUSIONS
            )
        checks.append({"workflow": workflow, "check": name})
        if not successful:
            return False, len(checks), checks
    return True, len(checks), checks


def observe_merged_direct_closeout(
    item: dict[str, Any],
    final_response: str,
    *,
    run: CommandRunner = _run,
) -> dict[str, Any] | None:
    """Return a terminal closeout only after exact local and GitHub checks pass."""

    closeout = item.get("closeout") if isinstance(item.get("closeout"), dict) else None
    if not closeout or item.get("closeout_authoritative") is True:
        return None
    state = normalize_closeout_state(closeout)
    if state["mode"] != "enforce" or state["source"] not in {"direct", "fable", "opus"}:
        return None
    workspace = state["workspace"]
    root_text = str(workspace.get("path") or "").strip()
    repository = str(workspace.get("repository") or "").strip()
    branch = str(workspace.get("branch") or "").strip()
    base_branch = str(workspace.get("base_branch") or "main").strip() or "main"
    if not root_text or not repository or not branch:
        return None
    root = Path(root_text).expanduser().resolve(strict=False)
    if not root.is_dir():
        return None

    match = next(
        (
            candidate
            for candidate in _PR_URL_RE.finditer(str(final_response or ""))
            if candidate.group("repo").casefold() == repository.casefold()
        ),
        None,
    )
    if match is None:
        return None
    pr_number = match.group("number")
    viewed = run(
        [
            "gh",
            "pr",
            "view",
            pr_number,
            "--repo",
            repository,
            "--json",
            (
                "number,url,state,headRefOid,headRefName,baseRefName,mergedAt,"
                "mergeCommit,mergeStateStatus,mergeable,isDraft,reviewDecision,"
                "statusCheckRollup"
            ),
        ],
        cwd=root,
        timeout=60,
    )
    if viewed.returncode != 0:
        return None
    try:
        payload = json.loads(viewed.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or str(payload.get("state") or "").upper() != "MERGED":
        return None
    head_sha = str(payload.get("headRefOid") or "").strip().lower()
    merge_commit = payload.get("mergeCommit") if isinstance(payload.get("mergeCommit"), dict) else {}
    merge_sha = str(merge_commit.get("oid") or "").strip().lower()
    if (
        not _SHA_RE.fullmatch(head_sha)
        or not _SHA_RE.fullmatch(merge_sha)
        or str(payload.get("headRefName") or "") != branch
        or str(payload.get("baseRefName") or "") != base_branch
    ):
        return None

    for args in (
        ["git", "rev-parse", f"refs/heads/{branch}"],
        ["git", "status", "--porcelain"],
        ["git", "diff", "--check"],
    ):
        result = run(args, cwd=root, timeout=30)
        if result.returncode != 0:
            return None
        if args[1] == "rev-parse" and str(result.stdout or "").strip().lower() != head_sha:
            return None
        if args[1] == "status" and str(result.stdout or "").strip():
            return None

    checks_green, check_count, checks = _successful_check_rollup(
        payload.get("statusCheckRollup")
    )
    if not checks_green:
        return None

    state["local_verification"] = {
        "status": "passed",
        "head_sha": head_sha,
        "source": "exact_checkout_and_green_pr",
    }
    state["pr"].update(
        {
            "url": str(payload.get("url") or match.group(0))[:1200],
            "number": str(payload.get("number") or pr_number)[:32],
            "state": "MERGED",
            "is_draft": payload.get("isDraft") is True,
            "head_sha": head_sha,
            "merge_sha": merge_sha,
            "merge_state": str(payload.get("mergeStateStatus") or "UNKNOWN").upper()[:48],
            "mergeable": payload.get("mergeable", "unknown"),
            "review_decision": str(payload.get("reviewDecision") or "UNKNOWN").upper()[:48],
        }
    )
    state["ci"] = {
        "head_sha": head_sha,
        "status": "passed",
        "total": check_count,
        "failed": [],
        "wait_state": "complete",
        "required": checks,
    }
    from hermes_cli.post_merge_receipts import initialize_post_merge_receipts

    state["post_merge"] = initialize_post_merge_receipts(
        state,
        target_sha=merge_sha,
    )
    state["canonical_sync"] = dict(state["post_merge"]["canonical_sync"])
    required_post_merge = state["policy"]["post_merge_requirements"]
    if any(required_post_merge.values()):
        state["status"] = "post_merge_pending"
        state["next_due_at"] = 0.0
    else:
        state["status"] = "post_merge_complete"
        state["next_due_at"] = None
    state["telemetry"]["last_transition"] = "observed_direct_completion"
    return state
