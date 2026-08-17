"""Deterministic PR merge after an explicit Discord Dev-role approval."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from hermes_cli.closeout_execution import (
    RemoteMutationUncertain,
    run_closeout_command,
)
from hermes_cli.trusted_closeout import (
    enrich_required_check_identities,
    normalize_closeout_state,
    repo_uses_trusted_required_checks,
    summarize_required_checks,
)


_PR_FIELDS = (
    "number,url,state,headRefOid,mergedAt,mergeCommit,mergeStateStatus,"
    "mergeable,isDraft,reviewDecision,statusCheckRollup"
)
_PASS_RECEIPT_STATUSES = frozenset({"passed", "approved", "success"})


@dataclass(frozen=True)
class DevMergeResult:
    outcome: str
    pr_url: str
    message: str


CommandRunner = Callable[..., Any]


def _run(
    runner: CommandRunner,
    args: list[str],
    *,
    root: Path,
    timeout: int = 60,
) -> Any:
    return runner(args, cwd=root, timeout=timeout, github=True)


def _valid_pr_url(value: Any, *, repository: str) -> str:
    url = str(value or "").strip()
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"github.com", "www.github.com"}
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""
    parts = [part for part in parsed.path.split("/") if part]
    if (
        len(parts) != 4
        or parts[2] != "pull"
        or not parts[3].isdigit()
        or "/".join(parts[:2]).lower() != str(repository or "").strip().lower()
    ):
        return ""
    return url


def _receipt_passed(receipt: Mapping[str, Any], *, required: bool, head_sha: str) -> bool:
    if not required:
        return True
    return (
        str(receipt.get("status") or "").strip().lower() in _PASS_RECEIPT_STATUSES
        and str(receipt.get("head_sha") or "").strip().lower() == head_sha
    )


def _persisted_gate_blocker(state: Mapping[str, Any], *, head_sha: str) -> str:
    if str(state.get("status") or "") != "pr_published":
        return "The final response is not attached to a published, fully checked PR."
    policy = state.get("policy") if isinstance(state.get("policy"), Mapping) else {}
    for key, label in (
        ("local_verification", "local verification"),
        ("review", "code review"),
        ("visual_qa", "visual QA"),
    ):
        receipt = state.get(key) if isinstance(state.get(key), Mapping) else {}
        if not _receipt_passed(
            receipt,
            required=policy.get(f"require_{key}") is True,
            head_sha=head_sha,
        ):
            return f"The exact PR head has not passed {label}."

    ci = state.get("ci") if isinstance(state.get("ci"), Mapping) else {}
    if (
        str(ci.get("status") or "").strip().lower() != "passed"
        or str(ci.get("head_sha") or "").strip().lower() != head_sha
    ):
        return "The exact PR head has not passed required CI."

    if policy.get("require_preview") is True:
        preview = state.get("preview") if isinstance(state.get("preview"), Mapping) else {}
        if (
            str(preview.get("status") or "").strip().lower() != "ready"
            or str(preview.get("observed_sha") or "").strip().lower() != head_sha
            or not str(preview.get("url") or "").startswith("https://")
        ):
            return "The exact PR head does not have a ready feature preview."

    pr = state.get("pr") if isinstance(state.get("pr"), Mapping) else {}
    if str(pr.get("review_decision") or "").strip().upper() == "CHANGES_REQUESTED":
        return "The PR has requested review changes."
    return ""


def _read_pr(
    runner: CommandRunner,
    *,
    root: Path,
    repository: str,
    pr_url: str,
) -> tuple[dict[str, Any] | None, str]:
    result = _run(
        runner,
        ["gh", "pr", "view", pr_url, "--repo", repository, "--json", _PR_FIELDS],
        root=root,
    )
    if int(getattr(result, "returncode", 1)) != 0:
        detail = str(getattr(result, "stderr", "") or "GitHub PR lookup failed").strip()
        return None, detail[:500]
    try:
        payload = json.loads(str(getattr(result, "stdout", "") or ""))
    except json.JSONDecodeError:
        return None, "GitHub returned an invalid PR response."
    if not isinstance(payload, dict):
        return None, "GitHub returned an invalid PR response."
    return payload, ""


def _live_gate_blocker(
    payload: Mapping[str, Any],
    *,
    head_sha: str,
    root: Path,
    repository: str,
    runner: CommandRunner,
) -> str:
    live_head = str(payload.get("headRefOid") or "").strip().lower()
    if live_head != head_sha:
        return "The PR head changed after the final response; wait for its new preview and checks."
    if str(payload.get("state") or "").strip().upper() != "OPEN":
        return f"The PR is {str(payload.get('state') or 'not open').lower()}."
    if str(payload.get("reviewDecision") or "").strip().upper() == "CHANGES_REQUESTED":
        return "The PR has requested review changes."

    if repo_uses_trusted_required_checks(root):
        trusted_payload = enrich_required_check_identities(
            payload,
            repo=repository,
            root=root,
            run=runner,
        )
        identity_error = str(trusted_payload.get("_required_check_identity_error") or "").strip()
        if identity_error:
            return identity_error[:500]
        checks = summarize_required_checks(
            trusted_payload.get("statusCheckRollup"),
            head_sha=head_sha,
        )
        if checks.get("status") != "passed" or checks.get("head_sha") != head_sha:
            return "The exact PR head has not passed required CI."

    if payload.get("isDraft") is True:
        return "draft"
    mergeable = payload.get("mergeable")
    if mergeable is not True and str(mergeable or "").strip().upper() != "MERGEABLE":
        return "GitHub does not report the PR as mergeable."
    if str(payload.get("mergeStateStatus") or "").strip().upper() != "CLEAN":
        return "GitHub does not report the PR as clean and ready to merge."
    return ""


def _merged_result(
    payload: Mapping[str, Any],
    pr_url: str,
    *,
    head_sha: str,
) -> DevMergeResult | None:
    if str(payload.get("state") or "").strip().upper() != "MERGED":
        return None
    if str(payload.get("headRefOid") or "").strip().lower() != head_sha:
        return DevMergeResult(
            "blocked",
            pr_url,
            "The merged PR head does not match the reviewed and approved head.",
        )
    return DevMergeResult("already_merged", pr_url, f"Merged: {pr_url}")


def merge_published_pr(
    closeout: Any,
    *,
    run: CommandRunner = run_closeout_command,
) -> DevMergeResult:
    """Merge one exact-head, fully published PR without touching local main."""

    state = normalize_closeout_state(closeout)
    workspace = state["workspace"]
    pr = state["pr"]
    repository = str(workspace.get("repository") or "").strip()
    pr_url = _valid_pr_url(pr.get("url"), repository=repository)
    root_text = str(workspace.get("path") or "").strip()
    head_sha = str(pr.get("head_sha") or "").strip().lower()
    if not pr_url or not repository or not root_text or not head_sha:
        return DevMergeResult(
            "blocked",
            pr_url,
            "The final response is missing trusted PR or workspace metadata.",
        )
    root = Path(root_text).expanduser().resolve(strict=False)
    if not root.is_dir():
        return DevMergeResult("blocked", pr_url, "The trusted PR workspace is unavailable.")

    blocker = _persisted_gate_blocker(state, head_sha=head_sha)
    if blocker:
        return DevMergeResult("blocked", pr_url, blocker)

    payload, error = _read_pr(run, root=root, repository=repository, pr_url=pr_url)
    if payload is None:
        return DevMergeResult("blocked", pr_url, error)
    merged = _merged_result(payload, pr_url, head_sha=head_sha)
    if merged is not None:
        return merged

    blocker = _live_gate_blocker(
        payload,
        head_sha=head_sha,
        root=root,
        repository=repository,
        runner=run,
    )
    if blocker == "draft":
        try:
            ready = _run(
                run,
                ["gh", "pr", "ready", pr_url, "--repo", repository],
                root=root,
            )
        except RemoteMutationUncertain:
            ready = None
        payload, error = _read_pr(run, root=root, repository=repository, pr_url=pr_url)
        if payload is None:
            return DevMergeResult("uncertain", pr_url, error)
        merged = _merged_result(payload, pr_url, head_sha=head_sha)
        if merged is not None:
            return merged
        if payload.get("isDraft") is True:
            if ready is None:
                return DevMergeResult(
                    "uncertain",
                    pr_url,
                    f"Could not confirm whether GitHub marked the PR ready: {pr_url}",
                )
            detail = str(getattr(ready, "stderr", "") or "GitHub did not mark the PR ready").strip()
            return DevMergeResult("blocked", pr_url, detail[:500])
        blocker = _live_gate_blocker(
            payload,
            head_sha=head_sha,
            root=root,
            repository=repository,
            runner=run,
        )

    if blocker:
        return DevMergeResult("blocked", pr_url, blocker)

    merge_uncertain = False
    try:
        result = _run(
            run,
            [
                "gh",
                "pr",
                "merge",
                pr_url,
                "--repo",
                repository,
                "--squash",
                "--match-head-commit",
                head_sha,
            ],
            root=root,
            timeout=120,
        )
    except RemoteMutationUncertain:
        result = None
        merge_uncertain = True

    payload, error = _read_pr(run, root=root, repository=repository, pr_url=pr_url)
    if payload is not None:
        merged = _merged_result(payload, pr_url, head_sha=head_sha)
        if merged is not None:
            if merged.outcome == "already_merged":
                return DevMergeResult("merged", pr_url, merged.message)
            return merged
    if merge_uncertain:
        return DevMergeResult(
            "uncertain",
            pr_url,
            f"The merge outcome could not be confirmed. Check the PR before retrying: {pr_url}",
        )
    if result is not None and int(getattr(result, "returncode", 1)) != 0:
        detail = str(getattr(result, "stderr", "") or "GitHub rejected the merge").strip()
        return DevMergeResult("blocked", pr_url, detail[:500])
    return DevMergeResult("uncertain", pr_url, error or "GitHub did not confirm the merge.")


__all__ = ["DevMergeResult", "merge_published_pr"]
