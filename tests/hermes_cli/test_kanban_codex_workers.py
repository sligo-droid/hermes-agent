from __future__ import annotations

import json
import inspect
import os
import re
import sqlite3
import stat
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest


_TRUSTED_PR_HEAD = "c" * 40
_REQUIRED_CHECK_RUNS = {
    ("Basic Tests", "basic"): ("101", ".github/workflows/tests.yml"),
    ("PR Body Format", "pr body"): ("202", ".github/workflows/pr-body-format.yml"),
}


def _required_check_details_url(repo: str, workflow: str, check: str) -> str:
    run_id, _path = _REQUIRED_CHECK_RUNS[(workflow, check)]
    return f"https://github.com/{repo}/actions/runs/{run_id}/job/{run_id}1"


def _home(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_RUNNER", raising=False)
    monkeypatch.delenv("HERMES_CODING_WORKER_BACKEND", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_REASONING", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_SERVICE_TIER", raising=False)
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SYSTEMD", "0")
    return root


@pytest.fixture(autouse=True)
def _resolve_canonical_sync_in_worker_integration_tests(monkeypatch):
    """Keep PR-finalizer integration fakes focused on lifecycle behavior.

    Protected-root/default-branch validation is covered directly in
    ``test_canonical_checkout_sync.py``. The historical finalizer tests below
    mock only the bounded Git lifecycle, so give that helper a trusted root
    and retain their explicit dirty/fetch/fast-forward/ancestry assertions.
    """
    from hermes_cli import canonical_checkout_sync

    monkeypatch.setattr(
        canonical_checkout_sync,
        "resolve_protected_canonical_checkout",
        lambda project_path, _branch, **_kwargs: (Path(str(project_path)), None),
    )
    # Historical lifecycle fakes use ``abc123`` as a compact placeholder. The
    # production authority boundary requires the real GitHub SHA length; this
    # integration fixture deliberately bypasses that unrelated validation.
    monkeypatch.setattr(
        canonical_checkout_sync,
        "_MERGE_COMMIT_RE",
        re.compile(r"^[0-9a-fA-F]{4,64}$"),
    )


@pytest.fixture(autouse=True)
def _mock_required_check_identity_api(monkeypatch):
    """Serve exact-head REST identity for raw ``gh pr view`` test payloads."""

    from hermes_cli import kanban_codex_worker as worker

    original_run_gh = worker._run_gh
    heads_by_repo: dict[str, str] = {}
    rollups_by_repo_head: dict[tuple[str, str], list[dict]] = {}

    def wrapped(args, *, root, timeout):
        endpoint = args[1] if len(args) > 1 and args[0] == "api" else ""
        check_runs_match = re.fullmatch(
            r"repos/([^/]+/[^/]+)/commits/([0-9a-f]{40}|[0-9a-f]{64})/"
            r"check-runs\?filter=all&per_page=100",
            endpoint,
        )
        if check_runs_match:
            repo, head_sha = check_runs_match.groups()
            heads_by_repo[repo.casefold()] = head_sha
            raw_rollup = rollups_by_repo_head.get((repo.casefold(), head_sha), [])
            check_runs = []
            for index, raw in enumerate(raw_rollup, start=1):
                identity = (
                    str(raw.get("workflowName") or ""),
                    str(raw.get("name") or ""),
                )
                if identity not in _REQUIRED_CHECK_RUNS:
                    continue
                check_runs.append(
                    {
                        "id": raw.get("databaseId") or index,
                        "name": identity[1],
                        "head_sha": head_sha,
                        "status": str(raw.get("status") or ""),
                        "conclusion": str(raw.get("conclusion") or ""),
                        "completed_at": f"2026-07-18T00:00:{index:02d}Z",
                        "details_url": str(raw.get("detailsUrl") or ""),
                        "app": {"slug": "github-actions"},
                    }
                )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"check_runs": check_runs}),
                stderr="",
            )
        workflow_run_match = re.fullmatch(
            r"repos/([^/]+/[^/]+)/actions/runs/(\d+)",
            endpoint,
        )
        if workflow_run_match:
            repo, run_id = workflow_run_match.groups()
            for _identity, (expected_run_id, path) in _REQUIRED_CHECK_RUNS.items():
                if run_id == expected_run_id:
                    return SimpleNamespace(
                        returncode=0,
                        stdout=json.dumps(
                            {
                                "path": path,
                                "head_sha": heads_by_repo.get(
                                    repo.casefold(),
                                    _TRUSTED_PR_HEAD,
                                ),
                            }
                        ),
                        stderr="",
                    )
            return SimpleNamespace(returncode=1, stdout="", stderr="unknown run")
        result = original_run_gh(args, root=root, timeout=timeout)
        if args[:2] == ["pr", "view"] and result.returncode == 0:
            try:
                payload = json.loads(result.stdout or "{}")
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                try:
                    repo = str(args[args.index("--repo") + 1])
                except (ValueError, IndexError):
                    repo = ""
                head_sha = str(payload.get("headRefOid") or "")
                rollup = payload.get("statusCheckRollup")
                if repo and head_sha and isinstance(rollup, list):
                    rollups_by_repo_head[(repo.casefold(), head_sha)] = [
                        dict(item) for item in rollup if isinstance(item, dict)
                    ]
        return result

    monkeypatch.setattr(worker, "_run_gh", wrapped)


def _pr_view_json(
    *,
    number: int = 123,
    repo: str = "sligo-labs/PID",
    state: str = "MERGED",
    merge_state: str = "CLEAN",
    mergeable: str = "MERGEABLE",
    checks: list[dict] | None = None,
    head_sha: str = _TRUSTED_PR_HEAD,
) -> str:
    if checks is None:
        checks = [
            {
                "name": "basic",
                "workflowName": "Basic Tests",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
            {
                "name": "pr body",
                "workflowName": "PR Body Format",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
        ]
    raw_checks = []
    for raw in checks:
        item = dict(raw)
        identity = (str(item.get("workflowName") or ""), str(item.get("name") or ""))
        if identity in _REQUIRED_CHECK_RUNS:
            item.setdefault(
                "detailsUrl",
                _required_check_details_url(repo, *identity),
            )
        raw_checks.append(item)
    return json.dumps(
        {
            "number": number,
            "url": f"https://github.com/{repo}/pull/{number}",
            "state": state,
            "headRefOid": head_sha,
            "mergedAt": "2026-05-26T15:30:17Z" if state == "MERGED" else None,
            "mergeCommit": {"oid": "abc123"} if state == "MERGED" else None,
            "mergeStateStatus": merge_state,
            "mergeable": mergeable,
            "isDraft": False,
            "reviewDecision": "",
            "statusCheckRollup": raw_checks,
        }
    )


def _canonical_sync_result(
    cmd: list[str],
    *,
    head: str = "def456",
    dirty: bool = False,
    pull_failed: bool = False,
    ancestor_failed: bool = False,
) -> SimpleNamespace | None:
    if cmd == ["git", "status", "--porcelain"]:
        return SimpleNamespace(returncode=0, stdout=" M file.py\n" if dirty else "", stderr="")
    if cmd[:4] == ["git", "fetch", "origin", "--prune"]:
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd[:4] == ["git", "rev-parse", "--verify"]:
        return SimpleNamespace(returncode=0, stdout="remotehead\n", stderr="")
    if cmd[:3] == ["git", "cat-file", "-e"]:
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd[:3] == ["git", "merge-base", "--is-ancestor"]:
        if ancestor_failed:
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd[:2] == ["git", "checkout"]:
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd[:3] == ["git", "merge", "--ff-only"]:
        if pull_failed:
            return SimpleNamespace(returncode=1, stdout="", stderr="not fast-forward")
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    if cmd == ["git", "rev-parse", "HEAD"]:
        return SimpleNamespace(returncode=0, stdout=f"{head}\n", stderr="")
    return None


def _bind_reviewer_head(dwb, board: str, head: str = _TRUSTED_PR_HEAD) -> None:
    dwb._update_worker_meta(board, {"review_approved_head": head})


def _claimed_planner(monkeypatch, tmp_path: Path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.set_goal(thread_id="9001", goal="Plan with Codex")
    conn = kanban_db.connect(board=board.slug)
    try:
        task = kanban_db.list_tasks(conn, include_archived=False)[0]
        claimed = kanban_db.claim_task(conn, task.id)
    finally:
        conn.close()
    assert claimed is not None
    return board, claimed


def test_check_rollup_summary_uses_latest_duplicate_check_run():
    from hermes_cli import kanban_codex_worker as worker

    status, total, failed = worker._check_rollup_summary(
        [
            {
                "name": "basic",
                "workflowName": "Basic Tests",
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
                "startedAt": "2026-06-09T15:00:00Z",
            },
            {
                "name": "supply-chain",
                "workflowName": "Supply Chain Audit",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-06-09T15:01:00Z",
            },
            {
                "name": "basic",
                "workflowName": "Basic Tests",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-06-09T15:02:00Z",
            },
        ]
    )

    assert status == "passed"
    assert total == 2
    assert failed == []


def test_required_check_rollup_uses_current_head_skipped_and_latest_rerun():
    from hermes_cli import kanban_codex_worker as worker

    def trusted(workflow, name, *, conclusion, started_at):
        _run_id, path = _REQUIRED_CHECK_RUNS[(workflow, name)]
        return {
            "name": name,
            "workflowName": workflow,
            "status": "COMPLETED",
            "conclusion": conclusion,
            "headSha": _TRUSTED_PR_HEAD,
            "startedAt": started_at,
            "app": {"slug": "github-actions", "name": "GitHub Actions"},
            "workflow": {"path": path},
        }

    status, total, failed, wait_state = worker._required_check_rollup_summary(
        [
            trusted(
                "Basic Tests",
                "basic",
                conclusion="CANCELLED",
                started_at="2026-06-09T15:00:00Z",
            ),
            trusted(
                "Basic Tests",
                "basic",
                conclusion="SUCCESS",
                started_at="2026-06-09T15:02:00Z",
            ),
            trusted(
                "PR Body Format",
                "pr body",
                conclusion="SKIPPED",
                started_at="2026-06-09T15:03:00Z",
            ),
            {
                "name": "unrelated",
                "workflowName": "Other Workflow",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "headSha": _TRUSTED_PR_HEAD,
            },
        ],
        head_sha=_TRUSTED_PR_HEAD,
    )

    assert (status, total, failed, wait_state) == ("passed", 2, [], "")


def test_refresh_pr_status_enriches_raw_checks_and_rejects_spoof_before_canonical(
    monkeypatch,
    tmp_path,
):
    from hermes_cli import kanban_codex_worker as worker

    repo = "sligo-labs/PID"
    spoof_url = f"https://github.com/{repo}/actions/runs/303/job/3031"
    basic_url = _required_check_details_url(repo, "Basic Tests", "basic")
    basic_rerun_url = basic_url.rsplit("/", 1)[0] + "/1012"
    pr_body_url = _required_check_details_url(repo, "PR Body Format", "pr body")
    checks = [
        {
            "name": "basic",
            "workflowName": "Basic Tests",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-06-09T15:05:00Z",
            "detailsUrl": spoof_url,
        },
        {
            "name": "basic",
            "workflowName": "Basic Tests",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-06-09T15:00:00Z",
            "detailsUrl": basic_url,
        },
        {
            "name": "basic",
            "workflowName": "Basic Tests",
            "status": "COMPLETED",
            "conclusion": "FAILURE",
            "startedAt": "2026-06-09T15:02:00Z",
            "detailsUrl": basic_rerun_url,
        },
        {
            "name": "pr body",
            "workflowName": "PR Body Format",
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "startedAt": "2026-06-09T15:03:00Z",
            "detailsUrl": pr_body_url,
        },
    ]

    def fake_gh(args, **_kwargs):
        if args[:2] == ["pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(
                    number=456,
                    state="OPEN",
                    checks=checks,
                ),
                stderr="",
            )
        if args[:1] == ["api"] and "/check-runs?" in args[1]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "check_runs": [
                            {
                                "id": index,
                                "name": item["name"],
                                "head_sha": _TRUSTED_PR_HEAD,
                                "status": item["status"],
                                "conclusion": item["conclusion"],
                                "completed_at": item["startedAt"],
                                "details_url": item["detailsUrl"],
                                "app": {"slug": "github-actions"},
                            }
                            for index, item in enumerate(checks, start=1)
                        ]
                    }
                ),
                stderr="",
            )
        if args[:1] == ["api"] and "/actions/runs/" in args[1]:
            run_id = args[1].rsplit("/", 1)[-1]
            paths = {
                "101": ".github/workflows/tests.yml",
                "202": ".github/workflows/pr-body-format.yml",
                "303": ".github/workflows/spoof-tests.yml",
            }
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {"path": paths[run_id], "head_sha": _TRUSTED_PR_HEAD}
                ),
                stderr="",
            )
        raise AssertionError(args)

    monkeypatch.setattr(worker, "_run_gh", fake_gh)
    state = {"pr_url": f"https://github.com/{repo}/pull/456"}

    worker._refresh_pr_status(state, root=tmp_path, repo=repo)

    assert state["pr_checks_status"] == "failed"
    assert state["pr_checks_total"] == 2
    assert state["pr_checks_failed"] == ["Basic Tests / basic"]


@pytest.mark.parametrize("ordering", ["original", "reversed", "rotated"])
def test_ensure_pr_merge_blocks_newest_failed_rerun_after_many_spoofs(
    monkeypatch,
    tmp_path,
    ordering,
):
    from hermes_cli import kanban_codex_worker as worker

    repo = "sligo-labs/PID"

    def details(run_id, job_id):
        return f"https://github.com/{repo}/actions/runs/{run_id}/job/{job_id}"

    raw_checks = []
    check_runs = []
    for offset in range(9):
        run_id = str(100 + offset)
        url = details(run_id, f"{run_id}1")
        raw_checks.append(
            {"workflowName": "Basic Tests", "name": "basic", "detailsUrl": url}
        )
        check_runs.append(
            {
                "id": 100 + offset,
                "name": "basic",
                "head_sha": _TRUSTED_PR_HEAD,
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "created_at": f"2026-07-17T00:00:{offset:02d}Z",
                "started_at": f"2026-07-17T00:01:{offset:02d}Z",
                "completed_at": f"2026-07-19T00:00:{offset:02d}Z",
                "details_url": url,
                "app": {"slug": "github-actions"},
            }
        )
    for check_id, conclusion, created_at, completed_at in (
        (9001, "SUCCESS", "2026-07-18T00:04:00Z", "2026-07-18T00:05:00Z"),
        (9002, "FAILURE", "2026-07-18T00:09:00Z", "2026-07-18T00:10:00Z"),
    ):
        url = details("900", str(check_id))
        raw_checks.append(
            {"workflowName": "Basic Tests", "name": "basic", "detailsUrl": url}
        )
        check_runs.append(
            {
                "id": check_id,
                "name": "basic",
                "head_sha": _TRUSTED_PR_HEAD,
                "status": "COMPLETED",
                "conclusion": conclusion,
                "created_at": created_at,
                "started_at": created_at,
                "completed_at": completed_at,
                "details_url": url,
                "app": {"slug": "github-actions"},
            }
        )
    pr_body_url = details("901", "9011")
    raw_checks.append(
        {
            "workflowName": "PR Body Format",
            "name": "pr body",
            "detailsUrl": pr_body_url,
        }
    )
    check_runs.append(
        {
            "id": 9011,
            "name": "pr body",
            "head_sha": _TRUSTED_PR_HEAD,
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "created_at": "2026-07-18T00:08:00Z",
            "started_at": "2026-07-18T00:08:00Z",
            "completed_at": "2026-07-18T00:09:00Z",
            "details_url": pr_body_url,
            "app": {"slug": "github-actions"},
        }
    )
    if ordering == "reversed":
        raw_checks.reverse()
        check_runs.reverse()
    elif ordering == "rotated":
        raw_checks[:] = raw_checks[4:] + raw_checks[:4]
        check_runs[:] = check_runs[6:] + check_runs[:6]
    calls = []
    queried_runs = []

    def fake_gh(args, **_kwargs):
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(
                    number=457,
                    state="OPEN",
                    checks=raw_checks,
                ),
                stderr="",
            )
        if args[:1] == ["api"] and "/check-runs?" in args[1]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"check_runs": check_runs}),
                stderr="",
            )
        if args[:1] == ["api"] and "/actions/runs/" in args[1]:
            run_id = args[1].rsplit("/", 1)[-1]
            queried_runs.append(run_id)
            path = {
                "900": ".github/workflows/tests.yml",
                "901": ".github/workflows/pr-body-format.yml",
            }.get(run_id, f".github/workflows/spoof-{run_id}.yml")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"path": path, "head_sha": _TRUSTED_PR_HEAD}),
                stderr="",
            )
        if args[:2] == ["pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(worker, "_run_gh", fake_gh)
    state = {
        "pr_url": f"https://github.com/{repo}/pull/457",
        "review_approved_head": _TRUSTED_PR_HEAD,
    }

    outcome = worker._ensure_pr_merged(state, root=tmp_path, repo=repo)

    assert outcome == worker.PRFinalizationOutcome.FAILED
    assert state["pr_checks_status"] == "failed"
    assert state["pr_checks_failed"] == ["Basic Tests / basic"]
    assert queried_runs == ["900", "901"]
    assert not any(args[:2] == ["pr", "merge"] for args in calls)


def test_required_check_identity_api_failure_is_bounded_blocker(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    repo = "sligo-labs/PID"
    calls = []

    def fake_gh(args, **_kwargs):
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(number=458, state="OPEN"),
                stderr="",
            )
        if args[:1] == ["api"] and "/check-runs?" in args[1]:
            return SimpleNamespace(
                returncode=403,
                stdout="",
                stderr=(
                    "token=supersecret rate limited at "
                    "https://api.github.com/protected "
                    + ("x" * 1000)
                ),
            )
        if args[:2] == ["pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(worker, "_run_gh", fake_gh)
    state = {
        "pr_url": f"https://github.com/{repo}/pull/458",
        "review_approved_head": _TRUSTED_PR_HEAD,
    }

    outcome = worker._ensure_pr_merged(state, root=tmp_path, repo=repo)

    assert outcome == worker.PRFinalizationOutcome.FAILED
    assert state["pr_status_error"].startswith("Required check identity lookup failed")
    assert state["pr_blocker"] == state["pr_status_error"]
    assert len(state["pr_status_error"]) <= 600
    assert "supersecret" not in state["pr_status_error"]
    assert "api.github.com" not in state["pr_status_error"]
    assert not state.get("pr_ci_wait_state")
    assert not any(args[:2] == ["pr", "merge"] for args in calls)


def test_legacy_kanban_fields_normalize_and_dual_write_shared_closeout(tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    head = "a" * 40
    merge_sha = "b" * 40
    flattened = {
        "project_path": str(tmp_path / "canonical"),
        "pr_url": "https://example.invalid/pr/1",
        "pr_number": "1",
        "pr_state": "MERGED",
        "pr_is_draft": False,
        "pr_ci_head_sha": head,
        "pr_merge_commit": merge_sha,
        "pr_merge_state": "CLEAN",
        "pr_mergeable": True,
        "pr_review_decision": "APPROVED",
        "pr_checks_status": "passed",
        "pr_checks_total": 2,
        "pr_checks_failed": [],
        "trusted_local_verification_head": head,
        "review_approved_head": head,
        "green_unmerged_since": 90,
        "green_unmerged_overdue": True,
        "merge_policy": "auto",
        "pr_open_policy": "after_review_approval",
    }
    state = worker._legacy_worker_closeout_state(
        flattened,
        board="board-1",
        workspace=str(tmp_path / "worktree"),
        repo="owner/repo",
        branch="worker/branch",
        base="main",
        config={
            "mode": "shadow",
            "early_draft_pr": True,
            "post_merge_requirements": {"ci": True},
        },
    )

    assert state["source"] == "kanban"
    assert state["workspace"]["path"] == str(tmp_path / "worktree")
    assert state["workspace"]["canonical_path"] == str(tmp_path / "canonical")
    assert state["pr"]["head_sha"] == head
    assert state["pr"]["merge_sha"] == merge_sha
    assert state["ci"]["status"] == "passed"
    assert state["policy"]["early_draft_pr"] is True
    assert state["policy"]["post_merge_requirements"]["ci"] is True
    assert state["policy"]["require_visual_qa"] is False
    assert state["visual_qa"] == {"status": "not_required"}
    assert state["review"] == {"status": "approved", "head_sha": head}
    assert state["telemetry"]["green_unmerged_since"] == 90
    assert state["telemetry"]["green_unmerged_overdue"] is True

    state["status"] = "post_merge_pending"
    state["next_due_at"] = 123
    state["telemetry"]["green_unmerged_since"] = 100
    state["telemetry"]["green_unmerged_overdue"] = True
    worker._dual_write_closeout_to_worker(flattened, state)

    assert flattened["closeout"]["status"] == "post_merge_pending"
    assert flattened["pr_ci_head_sha"] == head
    assert flattened["pr_merge_commit"] == merge_sha
    assert flattened["pr_ci_next_poll_at"] == 123
    assert flattened["green_unmerged_since"] == 100
    assert flattened["green_unmerged_overdue"] is True


def test_kanban_visual_gate_uses_structured_requirement_and_exact_checkpoint_head(tmp_path):
    from agent.visual_qa import normalize_visual_requirement, visual_requirement_id
    from hermes_cli import kanban_codex_worker as worker

    head = "a" * 40
    requirement = normalize_visual_requirement(
        {
            "level": "surface",
            "target": "responsive dashboard",
            "assertions": ["dashboard has no horizontal overflow"],
        }
    )
    assertion_id = requirement["assertions"][0]["id"]
    trusted_receipt = {
        "requirement_id": visual_requirement_id(requirement),
        "contract_id": "vac_" + ("b" * 24),
        "assertion_ids": [assertion_id],
        "status": "passed",
        "attempts": 1,
        "vision_calls": 0,
        "duration_ms": 20,
        "diagnostic_codes": ["no_horizontal_overflow_satisfied"],
        "order": 3,
    }
    flattened = {
        "project_path": str(tmp_path / "canonical"),
        "pr_ci_head_sha": head,
        "project_context": {
            "visual_qa_requirement": requirement,
            "visual_qa_receipts": [trusted_receipt],
            "visual_qa_min_receipt_order": 3,
        },
    }

    state = worker._legacy_worker_closeout_state(
        flattened,
        board="board-1",
        workspace=str(tmp_path / "worktree"),
        repo="owner/repo",
        branch="worker/branch",
        base="main",
        config={"mode": "enforce", "early_draft_pr": True},
    )

    assert state["policy"]["require_visual_qa"] is True
    assert state["visual_qa"] == {"status": "passed", "head_sha": head}

    later_head = "c" * 40
    flattened["closeout"] = state
    flattened["pr_ci_head_sha"] = later_head
    later = worker._legacy_worker_closeout_state(
        flattened,
        board="board-1",
        workspace=str(tmp_path / "worktree"),
        repo="owner/repo",
        branch="worker/branch",
        base="main",
        config={"mode": "enforce", "early_draft_pr": True},
    )
    assert later["visual_qa"] == {"status": "pending", "head_sha": later_head}
    assert flattened["trusted_visual_qa_receipt_binding"] == {
        "receipt_key": (
            f"{trusted_receipt['requirement_id']}:"
            f"{trusted_receipt['contract_id']}:3"
        ),
        "head_sha": head,
    }


def test_shadow_kanban_closeout_cannot_take_ownership_from_legacy_finalizer(
    monkeypatch,
    tmp_path,
):
    from hermes_cli import discord_worker_boards as boards
    from hermes_cli import kanban_codex_worker as worker

    workspace = tmp_path / "worktree"
    workspace.mkdir()
    stored = {
        "worker_branch": "worker/feature",
        "base_branch": "main",
        "pr_open_policy": "never",
        "merge_policy": "never",
    }
    observations = []
    monkeypatch.setattr(
        worker,
        "_kanban_closeout_config",
        lambda: {"mode": "shadow", "surfaces": {"kanban": True}},
    )
    monkeypatch.setattr(
        worker,
        "_reconcile_kanban_closeout",
        lambda *_args, **_kwargs: observations.append("repair_required")
        or worker.PRFinalizationOutcome.FAILED,
    )
    monkeypatch.setattr(
        worker.kanban_db,
        "read_board_metadata",
        lambda _board: {"discord_worker": dict(stored)},
    )
    monkeypatch.setattr(
        boards,
        "effective_pr_policy_for_worker",
        lambda _value: {"pr_open_policy": "never", "merge_policy": "never"},
    )
    monkeypatch.setattr(
        boards,
        "_update_worker_meta",
        lambda _board, updates: stored.update(updates) or {},
    )
    monkeypatch.setattr(worker, "_resolve_github_repo", lambda *_args: "owner/repo")

    outcome = worker._ensure_pr("board-1", str(workspace))

    assert observations == ["repair_required"]
    assert outcome == worker.PRFinalizationOutcome.MERGED
    assert stored["pr_state"] == "not_needed"
    assert stored["pr_blocker"] == ""


def test_first_learned_kanban_pr_head_does_not_promote_unbound_evidence(tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.trusted_closeout import normalize_closeout_state

    first_head = "a" * 40
    later_head = "b" * 40
    existing = normalize_closeout_state(
        {
            "source": "kanban",
            "mode": "shadow",
            "workspace": {"path": str(tmp_path / "worktree")},
            "local_verification": {"status": "passed"},
            "review": {"status": "approved"},
        }
    )
    flattened = {
        "closeout": existing,
        "pr_ci_head_sha": first_head,
        "pr_checks_status": "passed",
    }

    first = worker._legacy_worker_closeout_state(
        flattened,
        board="board-1",
        workspace=str(tmp_path / "worktree"),
        repo="owner/repo",
        branch="worker/branch",
        base="main",
        config={"mode": "shadow"},
    )
    assert first["local_verification"] == {"status": "passed"}
    assert first["review"] == {"status": "approved"}

    flattened["closeout"] = first
    flattened["pr_ci_head_sha"] = later_head
    later = worker._legacy_worker_closeout_state(
        flattened,
        board="board-1",
        workspace=str(tmp_path / "worktree"),
        repo="owner/repo",
        branch="worker/branch",
        base="main",
        config={"mode": "shadow"},
    )
    assert later["pr"]["head_sha"] == later_head
    assert later["local_verification"] == {"status": "passed"}
    assert later["review"] == {"status": "approved"}


def test_early_draft_checkpoint_pushes_exact_head_before_review(monkeypatch, tmp_path):
    from hermes_cli import discord_worker_boards as boards
    from hermes_cli import kanban_codex_worker as worker

    root = tmp_path / "worktree"
    root.mkdir()
    head = "a" * 40
    stored = {
        "kind": "discord_worker_board",
        "worker_branch": "worker/feature",
        "base_branch": "main",
        "project_path": str(tmp_path / "canonical"),
        "pr_open_policy": "after_review_approval",
        "merge_policy": "auto",
        "project_context": {
            "visual_qa_requirement": {
                "level": "surface",
                "target": "responsive dashboard",
                "assertions": ["dashboard has no horizontal overflow"],
            }
        },
    }
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(
        worker,
        "_kanban_closeout_config",
        lambda: {
            "mode": "enforce",
            "surfaces": {"kanban": True},
            "early_draft_pr": True,
        },
    )
    monkeypatch.setattr(worker.kanban_db, "read_board_metadata", lambda _board: {"discord_worker": dict(stored)})
    monkeypatch.setattr(boards, "effective_pr_policy_for_worker", lambda value: {})
    monkeypatch.setattr(boards, "_update_worker_meta", lambda _board, updates: stored.update(updates) or {})
    monkeypatch.setattr(worker, "_resolve_github_repo", lambda *_args: "owner/repo")

    def fake_run(args, **_kwargs):
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=head + "\n", stderr="")
        if args[:2] in (["git", "diff"], ["git", "status"]):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    opened = []

    def fake_open(state, **kwargs):
        opened.append(kwargs)
        state.update(
            {
                "pr_url": "https://example.invalid/pr/1",
                "pr_state": "OPEN",
                "pr_is_draft": True,
                "pr_ci_head_sha": head,
                "pr_checks_status": "pending",
            }
        )
        return True

    monkeypatch.setattr(worker, "_ensure_pr_open", fake_open)

    result = worker._ensure_early_draft_pr("board-1", str(root))

    assert result == {"status": "opened", "head_sha": head}
    assert opened[0]["draft"] is True
    assert opened[0]["allow_draft"] is True
    assert stored["early_draft_pushed_head_sha"] == head
    assert "trusted_local_verification_head" not in stored
    assert stored["closeout"]["local_verification"] == {"status": "pending"}
    assert stored["closeout"]["review"] == {
        "status": "pending",
        "head_sha": head,
    }
    assert stored["closeout"]["policy"]["require_visual_qa"] is True
    assert stored["closeout"]["visual_qa"] == {
        "status": "pending",
        "head_sha": head,
    }


def test_early_draft_followup_head_keeps_prior_review_stale(monkeypatch, tmp_path):
    from hermes_cli import discord_worker_boards as boards
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.trusted_closeout import normalize_closeout_state

    root = tmp_path / "worktree"
    root.mkdir()
    old_head = "a" * 40
    new_head = "b" * 40
    stored = {
        "kind": "discord_worker_board",
        "worker_branch": "worker/feature",
        "base_branch": "main",
        "project_path": str(tmp_path / "canonical"),
        "pr_open_policy": "after_review_approval",
        "merge_policy": "auto",
        "pr_url": "https://example.invalid/pr/1",
        "pr_state": "OPEN",
        "pr_is_draft": True,
        "pr_ci_head_sha": old_head,
        "early_draft_pushed_head_sha": old_head,
        "trusted_local_verification_head": old_head,
        "review_approved_head": old_head,
        "closeout": normalize_closeout_state(
            {
                "mode": "enforce",
                "pr": {"head_sha": old_head},
                "local_verification": {"status": "passed", "head_sha": old_head},
                "review": {"status": "approved", "head_sha": old_head},
            }
        ),
    }
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(
        worker,
        "_kanban_closeout_config",
        lambda: {
            "mode": "enforce",
            "surfaces": {"kanban": True},
            "early_draft_pr": True,
        },
    )
    monkeypatch.setattr(worker.kanban_db, "read_board_metadata", lambda _board: {"discord_worker": dict(stored)})
    monkeypatch.setattr(boards, "effective_pr_policy_for_worker", lambda value: {})
    monkeypatch.setattr(boards, "_update_worker_meta", lambda _board, updates: stored.update(updates) or {})
    monkeypatch.setattr(worker, "_resolve_github_repo", lambda *_args: "owner/repo")

    def fake_run(args, **_kwargs):
        if args == ["git", "rev-parse", "HEAD"]:
            return SimpleNamespace(returncode=0, stdout=new_head + "\n", stderr="")
        if args[:2] in (["git", "diff"], ["git", "status"]):
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    def fake_open(state, **_kwargs):
        state["pr_ci_head_sha"] = new_head
        state["pr_checks_status"] = "pending"
        return True

    monkeypatch.setattr(worker, "_ensure_pr_open", fake_open)

    result = worker._ensure_early_draft_pr("board-1", str(root))

    assert result == {"status": "opened", "head_sha": new_head}
    assert stored["trusted_local_verification_head"] == old_head
    assert stored["closeout"]["local_verification"] == {"status": "pending"}
    assert stored["closeout"]["review"] == {
        "status": "pending",
        "head_sha": new_head,
    }
    assert stored["review_approved_head"] == old_head


def test_worker_retry_operation_names_are_backend_neutral():
    from hermes_cli import kanban_codex_worker as worker

    source = inspect.getsource(worker)

    assert 'operation_name="coding_worker.initial_get_task"' in source
    assert 'operation_name="coding_worker.recovery_get_task"' in source
    assert 'operation_name="coding_worker.recovery_confirm_get_task"' in source
    assert 'operation_name="coding_worker.recorded_result_get_task"' in source
    assert 'operation_name="codex_worker.' not in source


def test_backend_child_env_bridges_profile_cli_paths(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    hermes_home = tmp_path / "hermes-home"
    profile_home = hermes_home / "home"
    profile_local_bin = profile_home / ".local" / "bin"
    profile_foundry_bin = profile_home / ".foundry" / "bin"
    profile_cargo_bin = profile_home / ".cargo" / "bin"
    profile_local_bin.mkdir(parents=True)
    profile_foundry_bin.mkdir(parents=True)
    profile_cargo_bin.mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", "/home/operator")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TERMINAL_HOME_MODE", "profile")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "task-control-state")

    env = worker._backend_child_env({"HERMES_KANBAN_WORKSPACE": "/workspace"})

    assert env["HOME"] == str(profile_home)
    assert env["HERMES_HOME"] == str(hermes_home)
    assert "HERMES_KANBAN_TASK" not in env
    assert env["HERMES_KANBAN_WORKSPACE"] == "/workspace"
    assert env["PATH"].split(os.pathsep)[:3] == [
        str(profile_local_bin),
        str(profile_foundry_bin),
        str(profile_cargo_bin),
    ]


def test_backend_child_env_respects_explicit_path(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    hermes_home = tmp_path / "hermes-home"
    (hermes_home / "home" / ".foundry" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", "/home/operator")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("TERMINAL_HOME_MODE", "profile")

    env = worker._backend_child_env({"PATH": "/explicit/bin"})

    assert env["HOME"] == str(hermes_home / "home")
    assert env["PATH"] == "/explicit/bin"


def test_backend_child_env_auto_keeps_real_home_on_host(monkeypatch, tmp_path):
    import hermes_constants
    from hermes_cli import kanban_codex_worker as worker

    hermes_home = tmp_path / "hermes-home"
    (hermes_home / "home" / ".foundry" / "bin").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HOME", "/home/runner")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("TERMINAL_HOME_MODE", raising=False)
    monkeypatch.setattr(hermes_constants, "is_container", lambda: False)

    env = worker._backend_child_env()

    assert env["HOME"] == "/home/runner"
    assert env["HERMES_HOME"] == str(hermes_home)
    assert env["PATH"] == "/usr/bin:/bin"


def test_run_gh_bridges_real_gh_config_dir_when_home_is_isolated(monkeypatch, tmp_path):
    from hermes_cli import github_remote
    from hermes_cli import kanban_codex_worker as worker

    isolated_home = tmp_path / "hermes-home" / "home"
    real_gh_config = tmp_path / "real-home" / ".config" / "gh"
    real_gh_config.mkdir(parents=True)
    captured: dict[str, object] = {}

    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        github_remote,
        "get_github_cli_config_dir",
        lambda env: str(real_gh_config) if env.get("HOME") == str(isolated_home) else "",
    )

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["env"] = kwargs.get("env")
        captured["cwd"] = kwargs.get("cwd")
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    result = worker._run_gh(["auth", "status"], root=tmp_path, timeout=5)

    assert result.returncode == 0
    assert captured["args"] == ["gh", "auth", "status"]
    assert captured["cwd"] == tmp_path
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["HOME"] == str(isolated_home)
    assert env["GH_CONFIG_DIR"] == str(real_gh_config)
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env


def _write_codex_auth(path: Path, *, access: str, refresh: str, id_token: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": access,
                    "refresh_token": refresh,
                    "id_token": id_token,
                },
            }
        ),
        encoding="utf-8",
    )


def _write_pool_auth(hermes_home: Path, entries: list[dict]) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {"openai-codex": entries},
            }
        ),
        encoding="utf-8",
    )


def test_dev_role_prompt_includes_autoreview_closeout_contract(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV, ROLE_PLANNER

    board = "discord-worker-autoreview-closeout"
    kanban_db.create_board(board, name="Autoreview closeout")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    conn = kanban_db.connect(board=board)
    try:
        dev_task_id = kanban_db.create_task(
            conn,
            title="Implement parser fix",
            body="Goal: fix parser\nSuccess means: tests pass\nStop when: local verification recorded",
            assignee=ROLE_DEV,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        planner_task_id = kanban_db.create_task(
            conn,
            title="Plan parser fix",
            assignee=ROLE_PLANNER,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        dev_prompt = worker._build_prompt(conn, dev_task_id, ROLE_DEV)
        planner_prompt = worker._build_prompt(conn, planner_task_id, ROLE_PLANNER)
    finally:
        conn.close()

    assert "Autoreview closeout contract for dev workers" in dev_prompt
    assert ".agents/skills/autoreview/scripts/autoreview --mode local" in dev_prompt
    assert "Hermes materializes this repo-local helper" in dev_prompt
    assert "Record the autoreview command/result" in dev_prompt
    assert "Autoreview closeout contract for dev workers" not in planner_prompt


def test_dev_worker_prompt_requires_bounded_visual_qa_or_explicit_na(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV, ROLE_PLANNER, ROLE_REVIEWER

    board = "discord-worker-visual-qa-handoff"
    kanban_db.create_board(board, name="Visual QA handoff")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    conn = kanban_db.connect(board=board)
    try:
        dev_task_id = kanban_db.create_task(
            conn,
            title="Implement visual change",
            assignee=ROLE_DEV,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        dev_prompt = worker._build_prompt(conn, dev_task_id, ROLE_DEV)
    finally:
        conn.close()

    assert "Visual-QA handoff contract" in dev_prompt
    assert "Visual QA: required" in dev_prompt
    assert "Visual QA: N/A" in dev_prompt
    assert "run one assertion-driven rendered check" in dev_prompt
    assert "`handoff.visual_qa`" in dev_prompt
    assert "passed, failed, or blocked" in dev_prompt
    assert "not_applicable" in dev_prompt
    assert "do not launch visual tooling" in dev_prompt
    assert '"visual_qa"' in worker._schema_instructions(ROLE_DEV)
    planner_schema = worker._schema_instructions(ROLE_PLANNER)
    assert "Visual QA: required" in planner_schema
    assert "Visual QA: N/A" in planner_schema
    assert "missing `handoff.visual_qa` receipt" in worker._schema_instructions(ROLE_REVIEWER)
    assert worker._visual_qa_handoff_prompt(ROLE_PLANNER) == ""


def test_worker_prompt_renders_structured_dev_first_inspection_contract(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as boards
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV

    candidates = [
        {
            "url": "http://127.0.0.1:5173/",
            "environment": "development",
            "location": "local",
        },
        {
            "url": "https://dev.example.test/",
            "environment": "development",
            "location": "external",
        },
        {
            "url": "https://prod.example.test/",
            "environment": "production",
            "location": "external",
        },
    ]
    board = boards.start_direct_goal(
        thread_id="worker-inspection-contract",
        goal="Implement the responsive dashboard",
        project_context={
            "project_inspection_candidates": candidates,
            "visual_qa_requirement": {
                "level": "surface",
                "target": "responsive dashboard",
                "assertions": ["dashboard has no horizontal overflow"],
            },
        },
    )
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Implement visual change",
            assignee=ROLE_DEV,
            tenant=board.slug,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        prompt = worker._build_prompt(conn, task_id, ROLE_DEV)
    finally:
        conn.close()

    assert prompt.index(candidates[0]["url"]) < prompt.index(candidates[1]["url"])
    assert prompt.index(candidates[1]["url"]) < prompt.index(candidates[2]["url"])
    assert "only when connection, DNS, or navigation is unavailable" in prompt
    assert "Do not switch to production" in prompt
    assert "Structured board visual-QA requirement: required" in prompt


def test_worker_prompt_includes_dashboard_qa_auth_without_secret(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV

    board = "discord-worker-dashboard-qa-auth"
    kanban_db.create_board(board, name="Dashboard QA auth")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "super-secret-dashboard-password")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Smoke protected dashboard",
            body="Goal: browser QA\nSuccess means: protected route checked\nStop when: verification recorded",
            assignee=ROLE_DEV,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        prompt = worker._build_prompt(conn, task_id, ROLE_DEV)
    finally:
        conn.close()

    assert "Protected dashboard/browser QA auth contract" in prompt
    assert "hermes_qa" in prompt
    assert "pnpm --dir dashboard qa:auth" in prompt
    assert "PID_QA_USERNAME" in prompt
    assert "role `admin_viewer`" in prompt
    assert "inspect privileged read surfaces" in prompt
    assert "every mutation must remain denied" in prompt
    assert "mutation testing as blocked" in prompt
    assert "HERMES_DASHBOARD_PASSWORD" in prompt
    assert "Never print, log" in prompt
    assert "super-secret-dashboard-password" not in prompt


def test_dev_role_prompt_force_loads_board_worker_skill_hints(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY, ROLE_DEV, ROLE_PLANNER

    board = "discord-worker-solidity-skill"
    kanban_db.create_board(board, name="Solidity skill")
    meta = kanban_db.read_board_metadata(board)
    meta.pop("db_path", None)
    meta[DISCORD_WORKER_META_KEY] = {
        "kind": "discord_worker_board",
        "project_context": {"worker_skill_hints": ["reserve-solidity-style"]},
    }
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_git_summary", lambda _workspace: "clean")

    loaded: list[tuple[list[str], str, str]] = []

    def fake_build_automatic_skills_message(names, user_text="", task_id=None, source_label="", **_kwargs):
        loaded.append((list(names), task_id, source_label))
        return (
            f"LOADED-SKILL {','.join(names)} for {task_id}",
            list(names),
            [],
        )

    monkeypatch.setattr(
        "agent.skill_commands.build_automatic_skills_message",
        fake_build_automatic_skills_message,
    )

    conn = kanban_db.connect(board=board)
    try:
        dev_task_id = kanban_db.create_task(
            conn,
            title="Implement Reserve contract amend",
            body="Goal: update Solidity PR\nSuccess means: follows Reserve style\nStop when: tests recorded",
            assignee=ROLE_DEV,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            tenant=board,
        )
        planner_task_id = kanban_db.create_task(
            conn,
            title="Plan Reserve amend",
            assignee=ROLE_PLANNER,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            tenant=board,
        )
        dev_prompt = worker._build_prompt(conn, dev_task_id, ROLE_DEV)
        planner_prompt = worker._build_prompt(conn, planner_task_id, ROLE_PLANNER)
    finally:
        conn.close()

    assert loaded == [
        (
            ["reserve-solidity-style"],
            dev_task_id,
            "Kanban dev worker task/board worker_skill_hints",
        )
    ]
    assert "Force-loaded implementation skills for this dev worker" in dev_prompt
    assert f"LOADED-SKILL reserve-solidity-style for {dev_task_id}" in dev_prompt
    assert "reserve-solidity-style" not in planner_prompt


def test_dev_role_prompt_force_loads_task_skills_and_deduplicates(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV

    board = "discord-worker-task-skills"
    kanban_db.create_board(board, name="Task skills")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_git_summary", lambda _workspace: "clean")

    captured: list[list[str]] = []

    def fake_build_automatic_skills_message(names, user_text="", task_id=None, source_label="", **_kwargs):
        captured.append(list(names))
        return ("\n".join(f"SKILL {name}" for name in names), list(names), [])

    monkeypatch.setattr(
        "agent.skill_commands.build_automatic_skills_message",
        fake_build_automatic_skills_message,
    )

    conn = kanban_db.connect(board=board)
    try:
        dev_task_id = kanban_db.create_task(
            conn,
            title="Implement skilled task",
            body="Goal: implement\nSuccess means: done\nStop when: verified",
            assignee=ROLE_DEV,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
            tenant=board,
            skills=["general-coding", "reserve-solidity-style", "general-coding"],
        )
        dev_prompt = worker._build_prompt(conn, dev_task_id, ROLE_DEV)
    finally:
        conn.close()

    assert dev_prompt.count("SKILL general-coding") == 1
    assert dev_prompt.count("SKILL reserve-solidity-style") == 1
    assert captured == [["general-coding", "reserve-solidity-style"]]


def test_autoreview_helper_materializes_in_hermes_and_pid_like_workspaces(tmp_path):
    from hermes_cli.worker_autoreview import AUTOREVIEW_RELATIVE_HELPER, AUTOREVIEW_RELATIVE_SKILL
    from hermes_cli.worker_autoreview import materialize_autoreview_helper

    for name in ("hermes-worker", "pid-worker"):
        workspace = tmp_path / name
        workspace.mkdir()

        helper = materialize_autoreview_helper(workspace)

        assert helper == workspace / AUTOREVIEW_RELATIVE_HELPER
        assert helper.exists()
        assert stat.S_IMODE(helper.stat().st_mode) & stat.S_IXUSR
        assert (workspace / AUTOREVIEW_RELATIVE_SKILL).read_text(encoding="utf-8").startswith("---\nname: autoreview")
        assert "advisory_not_model_review" in helper.read_text(encoding="utf-8")


def test_autoreview_helper_is_excluded_from_git_status(tmp_path):
    from hermes_cli.worker_autoreview import materialize_autoreview_helper

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)

    materialize_autoreview_helper(tmp_path)

    status = subprocess.run(
        ["git", "status", "--short"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert ".agents/skills/autoreview" not in status


def test_autoreview_helper_preserves_existing_repo_helper(tmp_path):
    from hermes_cli.worker_autoreview import AUTOREVIEW_RELATIVE_HELPER, AUTOREVIEW_RELATIVE_SKILL
    from hermes_cli.worker_autoreview import materialize_autoreview_helper

    helper = tmp_path / AUTOREVIEW_RELATIVE_HELPER
    skill = tmp_path / AUTOREVIEW_RELATIVE_SKILL
    helper.parent.mkdir(parents=True)
    skill.parent.mkdir(parents=True, exist_ok=True)
    helper.write_text("custom helper", encoding="utf-8")
    skill.write_text("custom skill", encoding="utf-8")

    materialize_autoreview_helper(tmp_path)

    assert helper.read_text(encoding="utf-8") == "custom helper"
    assert skill.read_text(encoding="utf-8") == "custom skill"


def test_checkpoint_commit_commits_dirty_repo(tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "worker@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Worker"], cwd=tmp_path, check=True)
    (tmp_path / "changed.txt").write_text("worker progress\n", encoding="utf-8")

    assert worker._checkpoint_commit(str(tmp_path), "task-123", "implemented checkpoint") is None
    log = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "checkpoint task-123: implemented checkpoint"


def test_checkpoint_commit_returns_and_logs_commit_failure(monkeypatch, tmp_path, caplog):
    from hermes_cli import kanban_codex_worker as worker

    def fake_run(cmd, **kwargs):
        if cmd == ["git", "status", "--porcelain"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=" M file.py\n", stderr="")
        if cmd == ["git", "add", "-A"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:2] == ["git", "commit"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="hook rejected")
        raise AssertionError(cmd)

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    caplog.set_level("WARNING")

    err = worker._checkpoint_commit(str(tmp_path), "task-123", "implemented checkpoint")

    assert err is not None
    assert "hook rejected" in err
    assert "checkpoint commit failed for task task-123" in caplog.text
    assert "hook rejected" in caplog.text


def test_checkpoint_commit_clean_repo_is_noop(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    # ``tmp_path`` itself contains the autouse fixture's ``hermes_test``
    # directory. Keep the Git fixture in a child so the repository is truly
    # clean and this test exercises the no-op branch rather than committing
    # fixture state.
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "worker@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Worker"], cwd=repo, check=True)
    (repo / "tracked.txt").write_text("already committed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repo, check=True, capture_output=True, text=True)

    real_run = subprocess.run
    calls: list[list[str]] = []

    def recording_run(cmd, **kwargs):
        calls.append(cmd)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(worker.subprocess, "run", recording_run)

    assert worker._checkpoint_commit(str(repo), "task-123", "nothing changed") is None
    assert calls == [["git", "status", "--porcelain"]]


def test_dev_completion_records_checkpoint_commit_error_and_completes(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV

    board = "checkpoint-error-board"
    kanban_db.create_board(board, name="Checkpoint Error Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Dev ticket",
            assignee=ROLE_DEV,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        monkeypatch.setattr(worker, "_checkpoint_commit", lambda *args, **kwargs: "hook rejected")

        worker._apply_role_output(
            conn,
            task_id,
            ROLE_DEV,
            {"status": "completed", "summary": "Implemented.", "changed_files": ["file.py"]},
            board=board,
            workspace=str(tmp_path),
            expected_run_id=claimed.current_run_id,
        )
        task = kanban_db.get_task(conn, task_id)
        run = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()

    assert task is not None
    assert task.status == "done"
    assert run is not None
    assert run.outcome == "completed"
    assert isinstance(run.metadata, dict)
    assert run.metadata["checkpoint_commit_error"] == "hook rejected"
    assert run.metadata["changed_files"] == ["file.py"]


def test_worker_pr_body_adds_project_state_justification_for_operational_changes():
    from hermes_cli import kanban_codex_worker as worker
    from scripts.check_pr_body_format import check_project_state_requirement

    body = worker._worker_pr_body(
        {"public_url": "https://discord/thread", "root_goal": "Fix worker PR hygiene"},
        board="discord-board",
        changed_files=["hermes_cli/kanban_codex_worker.py"],
    )

    assert "Board: https://discord/thread" in body
    assert "## Summary\n" in body
    assert "## Verification\n" in body
    assert "Goal:\nFix worker PR hygiene" not in body
    assert worker._WORKER_PROJECT_STATE_JUSTIFICATION in body
    ok, _message = check_project_state_requirement(body, ["hermes_cli/kanban_codex_worker.py"])
    assert ok is True


def test_worker_pr_body_skips_project_state_justification_when_state_changed():
    from hermes_cli import kanban_codex_worker as worker

    body = worker._worker_pr_body(
        {"root_goal": "Update project ledger"},
        board="discord-board",
        changed_files=["hermes_cli/kanban_codex_worker.py", "docs/project-state.md"],
    )

    assert worker._WORKER_PROJECT_STATE_JUSTIFICATION not in body


def test_changed_files_for_pr_body_includes_branch_and_worktree_changes(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "diff", "--name-only", "origin/main...HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="gateway/run.py\n", stderr="")
        if cmd == ["git", "diff", "--name-only", "--cached"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd == ["git", "diff", "--name-only"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="docs/project-state.md\ngateway/run.py\n", stderr="")
        if cmd == ["git", "ls-files", "--others", "--exclude-standard"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="hermes_cli/kanban.py\n", stderr="")
        raise AssertionError(cmd)

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._changed_files_for_pr_body(tmp_path, base="main") == [
        "gateway/run.py",
        "docs/project-state.md",
        "hermes_cli/kanban.py",
    ]
    assert calls == [
        ["git", "diff", "--name-only", "origin/main...HEAD"],
        ["git", "diff", "--name-only", "--cached"],
        ["git", "diff", "--name-only"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]


def test_ensure_existing_worker_pr_body_appends_project_state_justification(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    calls = []
    worker_meta = {"pr_url": "https://github.com/sligo-labs/hermes-agent/pull/123", "root_goal": "Fix worker PR hygiene"}

    def fake_run_gh(args, *, root, timeout):
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return subprocess.CompletedProcess(args, 0, stdout="Board: board\n\nGoal:\nFix worker PR hygiene\n", stderr="")
        if args[:2] == ["pr", "edit"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(args)

    monkeypatch.setattr(worker, "_run_gh", fake_run_gh)

    worker._ensure_worker_pr_body_hygiene(
        worker_meta,
        root=tmp_path,
        repo="sligo-labs/hermes-agent",
        board="discord-board",
        changed_files=["hermes_cli/kanban_codex_worker.py"],
    )

    assert calls[0][:4] == ["pr", "view", worker_meta["pr_url"], "--repo"]
    assert calls[1][:4] == ["pr", "edit", worker_meta["pr_url"], "--repo"]
    assert calls[1][-1].endswith(worker._WORKER_PROJECT_STATE_JUSTIFICATION)
    assert "pr_body_update_error" not in worker_meta


def test_ensure_existing_worker_pr_body_does_not_edit_when_requirement_satisfied(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    calls = []
    body = f"Board: board\n\nGoal:\nFix worker PR hygiene\n\n{worker._WORKER_PROJECT_STATE_JUSTIFICATION}"

    def fake_run_gh(args, *, root, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=body, stderr="")

    monkeypatch.setattr(worker, "_run_gh", fake_run_gh)

    worker._ensure_worker_pr_body_hygiene(
        {"pr_url": "https://github.com/sligo-labs/hermes-agent/pull/123"},
        root=tmp_path,
        repo="sligo-labs/hermes-agent",
        board="discord-board",
        changed_files=["hermes_cli/kanban_codex_worker.py"],
    )

    assert len(calls) == 1
    assert calls[0][:2] == ["pr", "view"]


def test_role_workers_materialize_autoreview_before_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV, ROLE_PLANNER, ROLE_REVIEWER

    calls: list[Path] = []

    def fake_materialize(workspace):
        calls.append(Path(workspace))
        return Path(workspace) / ".agents/skills/autoreview/scripts/autoreview"

    monkeypatch.setattr(worker, "materialize_autoreview_helper", fake_materialize)
    monkeypatch.setattr(worker, "_role_uses_opencode", lambda role, task: False)
    monkeypatch.setattr(worker, "_run_codex", lambda *args, **kwargs: SimpleNamespace(final_text="{}", error=None))

    worker._run_role_backend("prompt", str(tmp_path), ROLE_DEV, task=SimpleNamespace(), task_id="t1", board="b1")
    worker._run_role_backend("prompt", str(tmp_path), ROLE_PLANNER, task=SimpleNamespace(), task_id="t2", board="b1")
    worker._run_role_backend("prompt", str(tmp_path), ROLE_REVIEWER, task=SimpleNamespace(), task_id="t3", board="b1")

    assert calls == [tmp_path, tmp_path, tmp_path]


def test_role_worker_reports_autoreview_materialization_failure_to_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV

    prompts: list[str] = []

    monkeypatch.setattr(
        worker,
        "materialize_autoreview_helper",
        lambda _workspace: (_ for _ in ()).throw(RuntimeError("readonly workspace")),
    )
    monkeypatch.setattr(worker, "_role_uses_opencode", lambda role, task: False)
    monkeypatch.setattr(
        worker,
        "_run_codex",
        lambda prompt, *args, **kwargs: prompts.append(prompt) or SimpleNamespace(final_text="{}", error=None),
    )

    worker._run_role_backend("prompt", str(tmp_path), ROLE_DEV, task=SimpleNamespace(), task_id="t1", board="b1")

    assert "Autoreview helper materialization failed before dev worker start: readonly workspace" in prompts[0]


def test_coding_worker_activity_heartbeat_rate_limits_and_uses_run_id(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker

    calls: list[tuple[str, int | None]] = []

    class Conn:
        def close(self):
            pass

    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "42")
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t1")
    monkeypatch.setattr(worker.kanban_db, "connect", lambda board=None: Conn())
    monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "claimer-lock")
    monkeypatch.setattr(
        worker.kanban_db,
        "heartbeat_claim",
        lambda conn, task_id, claimer=None: calls.append(("claim", claimer)) or True,
    )
    monkeypatch.setattr(
        worker.kanban_db,
        "heartbeat_worker",
        lambda conn, task_id, note=None, expected_run_id=None: calls.append(("worker", expected_run_id)) or True,
    )
    monkeypatch.setattr(worker.time, "monotonic", lambda: 100.0)
    worker._last_activity_heartbeat_at.clear()

    worker._heartbeat_worker_activity("t1", board="b1")
    worker._heartbeat_worker_activity("t1", board="b1")

    assert calls == [("claim", "claimer-lock"), ("worker", 42)]

    worker._heartbeat_worker_activity("t1", board="b1", force=True)
    assert calls == [
        ("claim", "claimer-lock"),
        ("worker", 42),
        ("claim", "claimer-lock"),
        ("worker", 42),
    ]


def test_coding_worker_activity_heartbeat_is_best_effort(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker

    worker._last_activity_heartbeat_at.clear()
    monkeypatch.setattr(worker.kanban_db, "connect", lambda board=None: (_ for _ in ()).throw(RuntimeError("db locked")))

    worker._heartbeat_worker_activity("t1", board="b1", force=True)


def test_role_completion_recovers_when_run_pointer_rotates_but_claim_is_owned(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = "discord-race"
    kanban_db.create_board(board, name="Race board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Dev ticket",
            assignee="dev",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        original_run_id = claimed.current_run_id
        replacement = conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, "
            "claim_expires, started_at) VALUES (?, ?, 'running', ?, ?, ?)",
            (task_id, "dev", claimed.claim_lock, claimed.claim_expires, int(time.time())),
        )
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (replacement.lastrowid, task_id),
        )
        conn.commit()
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", claimed.claim_lock)

        completed = worker._complete_role_task(
            conn,
            task_id,
            summary="Implemented in checkpoint commit.",
            metadata={"raw": {"status": "completed"}},
            expected_run_id=original_run_id,
        )

        task = kanban_db.get_task(conn, task_id)
        latest = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()

    assert completed is True
    assert task is not None
    assert task.status == "done"
    assert latest is not None
    assert latest.outcome == "completed"
    assert latest.summary == "Implemented in checkpoint commit."


def test_role_completion_does_not_recover_when_claim_is_not_owned(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = "discord-race-unowned"
    kanban_db.create_board(board, name="Race board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Dev ticket", assignee="dev")
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        conn.execute(
            "UPDATE tasks SET current_run_id = current_run_id + 1 WHERE id = ?",
            (task_id,),
        )
        conn.commit()
        monkeypatch.setenv("HERMES_KANBAN_CLAIM_LOCK", "someone-else")

        completed = worker._complete_role_task(
            conn,
            task_id,
            summary="Should not complete.",
            metadata={"raw": {"status": "completed"}},
            expected_run_id=claimed.current_run_id,
        )

        task = kanban_db.get_task(conn, task_id)
    finally:
        conn.close()

    assert completed is False
    assert task is not None
    assert task.status == "running"


def test_role_worker_exits_zero_after_recording_backend_blocker(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = "discord-worker-error"
    kanban_db.create_board(board, name="Worker error board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Review", assignee=ROLE_REVIEWER)
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
    finally:
        conn.close()

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_CODEX_WORKER_ROLE", ROLE_REVIEWER)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_build_prompt", lambda _conn, _task_id, _role: "prompt")
    monkeypatch.setattr(
        worker,
        "_run_role_backend",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("backend exploded")),
    )
    monkeypatch.setattr(worker, "mark_dispatch_dirty", lambda **_kwargs: None)

    assert worker.main() == 0

    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        latest = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    assert task.status == "blocked"
    assert latest is not None
    assert latest.outcome == "blocked"
    assert "backend exploded" in (latest.summary or "")


def test_role_worker_recovers_completed_json_after_transient_apply_failure(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = "discord-worker-recover-result"
    kanban_db.create_board(board, name="Worker recovery board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Review", assignee=ROLE_REVIEWER)
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
    finally:
        conn.close()

    payload = {
        "status": "blocked",
        "summary": "Reviewer could not finish.",
        "findings": [],
        "new_tasks": [],
        "criteria_assessment": {},
        "blocker": "Need operator input.",
    }
    real_apply = worker._apply_role_output
    calls = {"count": 0}

    def flaky_apply(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("transient sqlite lock after result")
        return real_apply(*args, **kwargs)

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_CODEX_WORKER_ROLE", ROLE_REVIEWER)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_build_prompt", lambda _conn, _task_id, _role: "prompt")
    monkeypatch.setattr(
        worker,
        "_run_role_backend",
        lambda *args, **kwargs: SimpleNamespace(final_text=json.dumps(payload), error=None),
    )
    monkeypatch.setattr(worker, "_apply_role_output", flaky_apply)
    monkeypatch.setattr(worker, "mark_dispatch_dirty", lambda **_kwargs: None)

    assert worker.main() == 0
    assert calls["count"] == 2

    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        latest = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    assert task.status == "blocked"
    assert latest is not None
    assert latest.outcome == "blocked"
    assert latest.summary == "Need operator input."


def test_role_worker_recovers_completed_json_with_fresh_connection_after_poisoned_conn(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV

    board = "discord-worker-fresh-recover-result"
    kanban_db.create_board(board, name="Fresh recovery board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Dev",
            assignee=ROLE_DEV,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
    finally:
        conn.close()

    payload = {
        "status": "completed",
        "summary": "Dev finished with valid JSON.",
        "changed_files": ["hermes_cli/kanban_codex_worker.py"],
        "tests": [
            {
                "command": "scripts/run_tests.sh tests/hermes_cli/test_kanban_codex_workers.py",
                "result": "passed",
                "output": "ok",
            }
        ],
        "handoff": {
            "changed_files": ["hermes_cli/kanban_codex_worker.py"],
            "tests": [],
            "verification": [],
            "preview": {"url": "", "command": "", "status": "not_run"},
            "smoke_routes": [],
            "known_warnings": [],
            "notes": "",
        },
        "blocker": None,
        "pr_ready": False,
    }
    real_apply = worker._apply_role_output
    calls = {"count": 0}

    def poison_first_connection(*args, **kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            args[0].close()
            raise RuntimeError("sqlite connection died after model result")
        return real_apply(*args, **kwargs)

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_CODEX_WORKER_ROLE", ROLE_DEV)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_build_prompt", lambda _conn, _task_id, _role: "prompt")
    monkeypatch.setattr(
        worker,
        "_run_role_backend",
        lambda *args, **kwargs: SimpleNamespace(final_text=json.dumps(payload), error=None),
    )
    monkeypatch.setattr(worker, "_apply_role_output", poison_first_connection)
    monkeypatch.setattr(worker, "_checkpoint_commit", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "mark_dispatch_dirty", lambda **_kwargs: None)

    assert worker.main() == 0
    assert calls["count"] == 2

    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        latest = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    assert task.status == "done"
    assert latest is not None
    assert latest.outcome == "completed"
    assert latest.summary == "Dev finished with valid JSON."


def test_role_worker_closes_poisoned_conn_before_fresh_result_recovery(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = "discord-worker-close-poisoned-recover-result"
    kanban_db.create_board(board, name="Close poisoned recovery board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(conn, title="Review", assignee=ROLE_REVIEWER)
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
    finally:
        conn.close()

    payload = {
        "status": "blocked",
        "summary": "Reviewer reached a valid blocker.",
        "findings": [],
        "new_tasks": [],
        "criteria_assessment": {},
        "blocker": "Authenticated protected-route rendering requires a valid Agora session.",
    }
    real_connect = kanban_db.connect
    real_get_task_with_retry = kanban_db.get_task_with_transient_retry
    real_apply = worker._apply_role_output
    first_conn = {"proxy": None}
    calls = {"apply": 0, "fresh_before_close": 0}

    class ConnProxy:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.closed = False

        def close(self):
            self.closed = True
            return self.wrapped.close()

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

    def connect_with_poison_guard(*args, **kwargs):
        raw = real_connect(*args, **kwargs)
        if first_conn["proxy"] is None:
            first_conn["proxy"] = ConnProxy(raw)
            return first_conn["proxy"]
        if not first_conn["proxy"].closed:
            calls["fresh_before_close"] += 1
            raw.close()
            raise sqlite3.OperationalError("disk I/O error")
        return raw

    def get_task_with_poisoned_original(conn, *args, **kwargs):
        if conn is first_conn["proxy"] and kwargs.get("operation_name") == "coding_worker.recovery_get_task":
            raise sqlite3.OperationalError("disk I/O error")
        return real_get_task_with_retry(conn, *args, **kwargs)

    def fail_first_apply(*args, **kwargs):
        calls["apply"] += 1
        if calls["apply"] == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real_apply(*args, **kwargs)

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_CODEX_WORKER_ROLE", ROLE_REVIEWER)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_build_prompt", lambda _conn, _task_id, _role: "prompt")
    monkeypatch.setattr(
        worker,
        "_run_role_backend",
        lambda *args, **kwargs: SimpleNamespace(final_text=json.dumps(payload), error=None),
    )
    monkeypatch.setattr(kanban_db, "connect", connect_with_poison_guard)
    monkeypatch.setattr(kanban_db, "get_task_with_transient_retry", get_task_with_poisoned_original)
    monkeypatch.setattr(worker, "_apply_role_output", fail_first_apply)
    monkeypatch.setattr(worker, "mark_dispatch_dirty", lambda **_kwargs: None)

    assert worker.main() == 0
    assert calls == {"apply": 2, "fresh_before_close": 0}
    assert first_conn["proxy"] is not None
    assert first_conn["proxy"].closed is True

    conn = real_connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        latest = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    assert task.status == "blocked"
    assert latest is not None
    assert latest.outcome == "blocked"
    assert latest.summary == "Authenticated protected-route rendering requires a valid Agora session."


def test_role_worker_recovers_recorded_json_when_backend_raises_after_recording(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV

    board = "discord-worker-recorded-recover-result"
    kanban_db.create_board(board, name="Recorded recovery board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Dev",
            assignee=ROLE_DEV,
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
    finally:
        conn.close()

    payload = {
        "status": "completed",
        "summary": "Dev result was recorded before cleanup failed.",
        "changed_files": ["hermes_cli/kanban_codex_worker.py"],
        "tests": [],
        "handoff": {
            "changed_files": ["hermes_cli/kanban_codex_worker.py"],
            "tests": [],
            "verification": [],
            "preview": {"url": "", "command": "", "status": "not_run"},
            "smoke_routes": [],
            "known_warnings": [],
            "notes": "",
        },
        "blocker": None,
        "pr_ready": False,
    }

    def backend_records_then_raises(*args, **kwargs):
        result = SimpleNamespace(
            final_text=json.dumps(payload),
            error=None,
            backend="opencode",
            exit_code=0,
        )
        worker.record_codex_worker_result(task_id, board=board, result=result)
        raise RuntimeError("post-result cleanup failed")

    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", board)
    monkeypatch.setenv("HERMES_CODEX_WORKER_ROLE", ROLE_DEV)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr(worker, "_build_prompt", lambda _conn, _task_id, _role: "prompt")
    monkeypatch.setattr(worker, "_run_role_backend", backend_records_then_raises)
    monkeypatch.setattr(worker, "_checkpoint_commit", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "mark_dispatch_dirty", lambda **_kwargs: None)

    assert worker.main() == 0

    conn = kanban_db.connect(board=board)
    try:
        task = kanban_db.get_task(conn, task_id)
        latest = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    assert task.status == "done"
    assert latest is not None
    assert latest.outcome == "completed"
    assert latest.summary == "Dev result was recorded before cleanup failed."


def test_codex_role_worker_defaults_to_host_runner(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import discord_worker_read

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    real_home = tmp_path / "real-home"
    gh_dir = real_home / ".config" / "gh"
    gh_dir.mkdir(parents=True)
    (gh_dir / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-token")
    monkeypatch.setenv("DISCORD_ADMIN_ACTIONS", "delete,pin")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "dashboard-secret")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "parent-codex-home"))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "parent-cred")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        discord_worker_read,
        "start_read_broker",
        lambda token: ("http://127.0.0.1:9", "broker-secret"),
    )
    captured = {}
    specialist = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update(
            {
                "cmd": cmd,
                "cwd": cwd,
                "env": env,
                "start_new_session": start_new_session,
            }
        )
        stdout.write(b"host worker launched\n")
        stdout.flush()
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    pid = workers.spawn_codex_worker(task, str(workspace), board=board.slug)

    assert pid == 4321
    assert captured["cmd"] == workers._host_worker_cmd()
    assert captured["cwd"] == str(workspace.resolve())
    assert captured["env"]["HERMES_CODEX_WORKER_ROLE"] == "planner"
    assert captured["env"]["HERMES_CODEX_WORKER_REASONING"] == "high"
    assert captured["env"]["HERMES_CODEX_WORKER_SERVICE_TIER"] == "normal"
    assert captured["env"]["HERMES_KANBAN_BOARD"] == board.slug
    assert captured["env"]["HERMES_DISCORD_WORKER_READ_URL"] == "http://127.0.0.1:9"
    assert captured["env"]["HERMES_DISCORD_WORKER_READ_TOKEN"] == "broker-secret"
    assert captured["env"]["HERMES_DISCORD_WORKER_CONTROL_URL"] == "http://127.0.0.1:9"
    assert captured["env"]["HERMES_DISCORD_WORKER_CONTROL_TOKEN"] == "broker-secret"
    assert captured["env"]["HERMES_DASHBOARD_USERNAME"] == "hermes_qa"
    assert captured["env"]["HERMES_DASHBOARD_PASSWORD"] == "dashboard-secret"
    assert "DISCORD_BOT_TOKEN" not in captured["env"]
    assert "DISCORD_ADMIN_ACTIONS" not in captured["env"]
    assert captured["env"]["CODEX_HOME"].endswith("/homes/" + task.id)
    assert captured["env"]["CODEX_HOME"] != str(tmp_path / "parent-codex-home")
    assert captured["env"].get("HERMES_CODEX_WORKER_CREDENTIAL_ID") != "parent-cred"
    assert captured["env"]["GH_CONFIG_DIR"] == str(gh_dir)
    assert captured["start_new_session"] is True


def test_worker_env_preserves_explicit_dashboard_username(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    monkeypatch.setenv("HERMES_DASHBOARD_USERNAME", "operator")
    monkeypatch.setenv("HERMES_DASHBOARD_PASSWORD", "dashboard-secret")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    env = workers._worker_env()

    assert env["HERMES_DASHBOARD_USERNAME"] == "operator"
    assert env["HERMES_DASHBOARD_PASSWORD"] == "dashboard-secret"


def test_worker_env_defaults_pid_qa_to_readonly_and_drops_admin_mode(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    monkeypatch.setenv("PID_QA_USERNAME", "hermes_qa")
    monkeypatch.setenv("PID_QA_PASSWORD", "pid-qa-secret")
    monkeypatch.setenv("PID_QA_EXPECT_ADMIN", "true")
    monkeypatch.setenv("PID_QA_EXPECT_READONLY", "false")
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)

    env = workers._worker_env()

    assert env["PID_QA_USERNAME"] == "hermes_qa"
    assert env["PID_QA_PASSWORD"] == "pid-qa-secret"
    assert env["PID_QA_EXPECT_READONLY"] == "true"
    assert "PID_QA_EXPECT_ADMIN" not in env


def test_pid_qa_credentials_are_explicitly_allowed_for_container_workers():
    from hermes_cli import kanban_codex_workers as workers

    assert workers._forward_env_to_worker_container("PID_QA_USERNAME")
    assert workers._forward_env_to_worker_container("PID_QA_PASSWORD")
    assert workers._forward_env_to_worker_container("PID_QA_EXPECT_READONLY")
    assert not workers._forward_env_to_worker_container("PID_QA_EXPECT_ADMIN")
    assert not workers._forward_env_to_worker_container("PID_SUPABASE_SERVICE_ROLE_KEY")


def test_worker_env_loads_dashboard_password_from_config_env(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    monkeypatch.delenv("HERMES_DASHBOARD_USERNAME", raising=False)
    monkeypatch.delenv("HERMES_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    values = {
        "HERMES_DASHBOARD_USERNAME": "configured-qa",
        "HERMES_DASHBOARD_PASSWORD": "configured-secret",
    }
    monkeypatch.setattr(workers, "_config_env_value", lambda key: values.get(key, ""))

    env = workers._worker_env()

    assert env["HERMES_DASHBOARD_USERNAME"] == "configured-qa"
    assert env["HERMES_DASHBOARD_PASSWORD"] == "configured-secret"


def test_codex_role_worker_pythonpath_prefers_runtime_venv_owner(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    board, task = _claimed_planner(monkeypatch, tmp_path)
    runtime_root = tmp_path / "canonical-hermes"
    project_worktree = tmp_path / "workspaces" / "hermes-discord-old-branch"
    (runtime_root / "hermes_cli").mkdir(parents=True)
    (project_worktree / "hermes_cli").mkdir(parents=True)
    python = runtime_root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(project_worktree))
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"cmd": cmd, "cwd": cwd, "env": env})
        return Proc()

    monkeypatch.setattr(workers.sys, "executable", str(python))
    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers, "_write_minimal_codex_home", lambda path: None)
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    pid = workers.spawn_codex_worker(task, str(project_worktree), board=board.slug)

    assert pid == 4321
    pythonpath = captured["env"]["PYTHONPATH"].split(os.pathsep)
    assert pythonpath[0] == str(runtime_root)
    assert str(project_worktree) in pythonpath[1:]
    assert captured["cwd"] == str(project_worktree.resolve())


def test_codex_role_worker_rejects_excluded_workspace(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "workspaces" / "quarantined"
    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"excluded_workspaces": [str(workspace)]},
    )

    with pytest.raises(RuntimeError, match="workspace is quarantined"):
        workers.spawn_codex_worker(task, str(workspace), board=board.slug)


def test_codex_role_worker_uses_systemd_worker_handle_when_enabled(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SYSTEMD", "1")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-token")
    captured = {}

    def fake_spawn_systemd_worker(*, cmd, workspace, env, log_path, unit_name):
        captured.update(
            {
                "cmd": cmd,
                "workspace": workspace,
                "env": env,
                "log_path": log_path,
                "unit_name": unit_name,
            }
        )
        return kanban_db._SpawnHandle(pid=2468, unit=f"{unit_name}.service")

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(
        workers,
        "_configure_discord_read_broker",
        lambda env: env.update(
            {
                "HERMES_DISCORD_WORKER_READ_URL": "http://127.0.0.1:9",
                "HERMES_DISCORD_WORKER_READ_TOKEN": "broker-secret",
                "HERMES_DISCORD_WORKER_CONTROL_URL": "http://127.0.0.1:9",
                "HERMES_DISCORD_WORKER_CONTROL_TOKEN": "broker-secret",
            }
        ),
    )
    monkeypatch.setattr(kanban_db, "_should_use_systemd_worker", lambda: True)
    monkeypatch.setattr(kanban_db, "_spawn_systemd_worker", fake_spawn_systemd_worker)
    monkeypatch.setattr(
        workers.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("direct Popen fallback should not run"),
    )

    handle = workers.spawn_codex_worker(task, str(workspace), board=board.slug)

    assert isinstance(handle, kanban_db._SpawnHandle)
    assert handle.pid == 2468
    assert handle.unit == f"{captured['unit_name']}.service"
    assert captured["cmd"] == workers._host_worker_cmd()
    assert captured["workspace"] == str(workspace.resolve())
    assert captured["env"]["HERMES_CODEX_WORKER_ROLE"] == "planner"
    assert captured["env"]["HERMES_CODING_WORKER_BACKEND"] == "codex"
    assert captured["env"]["HERMES_DISCORD_WORKER_READ_TOKEN"] == "broker-secret"
    assert captured["env"]["CODEX_HOME"].endswith("/homes/" + task.id)


def test_systemd_worker_env_keeps_role_worker_runtime_keys(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_db

    filtered = kanban_db._systemd_worker_env(
        {
            "HERMES_KANBAN_TASK": "task-1",
            "HERMES_CODEX_WORKER_ROLE": "planner",
            "HERMES_CODEX_WORKER_REASONING": "xhigh",
            "HERMES_CODEX_WORKER_SERVICE_TIER": "fast",
            "HERMES_CODEX_WORKER_CREDENTIAL_ID": "cred-1",
            "HERMES_CODING_WORKER_BACKEND": "opencode",
            "HERMES_DASHBOARD_USERNAME": "hermes_qa",
            "HERMES_DASHBOARD_PASSWORD": "dashboard-secret",
            "HERMES_DISCORD_WORKER_READ_URL": "http://127.0.0.1:9",
            "HERMES_DISCORD_WORKER_READ_TOKEN": "broker-secret",
            "HERMES_DISCORD_WORKER_CONTROL_URL": "http://127.0.0.1:9",
            "HERMES_DISCORD_WORKER_CONTROL_TOKEN": "broker-secret",
            "CODEX_HOME": str(tmp_path / "codex-home"),
            "DISCORD_BOT_TOKEN": "discord-token",
            "OPENAI_API_KEY": "openai-secret",
        }
    )

    assert filtered["HERMES_KANBAN_TASK"] == "task-1"
    assert filtered["HERMES_CODEX_WORKER_ROLE"] == "planner"
    assert filtered["HERMES_CODEX_WORKER_REASONING"] == "xhigh"
    assert filtered["HERMES_CODEX_WORKER_SERVICE_TIER"] == "fast"
    assert filtered["HERMES_CODEX_WORKER_CREDENTIAL_ID"] == "cred-1"
    assert filtered["HERMES_CODING_WORKER_BACKEND"] == "opencode"
    assert filtered["HERMES_DASHBOARD_USERNAME"] == "hermes_qa"
    assert filtered["HERMES_DASHBOARD_PASSWORD"] == "dashboard-secret"
    assert filtered["HERMES_DISCORD_WORKER_READ_TOKEN"] == "broker-secret"
    assert filtered["HERMES_DISCORD_WORKER_CONTROL_TOKEN"] == "broker-secret"
    assert filtered["CODEX_HOME"] == str(tmp_path / "codex-home")
    assert "DISCORD_BOT_TOKEN" not in filtered
    assert "OPENAI_API_KEY" not in filtered


def test_repo_root_falls_back_to_imported_checkout_outside_venv(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    python = tmp_path / "python"
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(workers.sys, "executable", str(python))

    assert workers._repo_root() == Path(workers.__file__).resolve().parent.parent


def test_host_worker_cmd_uses_absolute_runtime_script(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    runtime_root = tmp_path / "canonical-hermes"
    (runtime_root / "hermes_cli").mkdir(parents=True)
    python = runtime_root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    monkeypatch.setattr(workers.sys, "executable", str(python))

    assert workers._host_worker_cmd() == [
        str(python),
        str(runtime_root / "hermes_cli" / "kanban_codex_worker.py"),
    ]


def test_codex_role_worker_falls_back_to_direct_spawn_when_systemd_launch_fails(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    monkeypatch.setenv("HERMES_KANBAN_WORKER_SYSTEMD", "1")
    captured = {}

    class Proc:
        pid = 5432

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update(
            {"cmd": cmd, "cwd": cwd, "env": env, "start_new_session": start_new_session}
        )
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(kanban_db, "_should_use_systemd_worker", lambda: True)
    monkeypatch.setattr(
        kanban_db,
        "_spawn_systemd_worker",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("no user manager")),
    )
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    pid = workers.spawn_codex_worker(task, str(workspace), board=board.slug)

    assert pid == 5432
    assert captured["cwd"] == str(workspace.resolve())
    assert captured["start_new_session"] is True
    log = kanban_db.read_worker_log(task.id, board=board.slug)
    assert log is not None
    assert "systemd-run role worker launch failed" in log
    assert "falling back to direct spawn: no user manager" in log


def test_codex_role_worker_inherits_available_pool_credential(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "cred-1",
                            "label": "primary",
                            "auth_type": "oauth",
                            "priority": 0,
                            "source": "manual:device_code",
                            "access_token": "access-1",
                            "refresh_token": "refresh-1",
                            "id_token": "id-1",
                            "last_status": "exhausted",
                            "last_status_at": time.time(),
                            "last_error_code": 429,
                            "last_error_reset_at": time.time() + 5 * 3600,
                        },
                        {
                            "id": "cred-2",
                            "label": "secondary",
                            "auth_type": "oauth",
                            "priority": 1,
                            "source": "manual:device_code",
                            "access_token": "access-2",
                            "refresh_token": "refresh-2",
                            "id_token": "id-2",
                        },
                    ]
                },
            }
        )
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "parent-codex-home"))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "parent-cred")
    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"env": env})
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(workspace), board=board.slug)

    codex_home = Path(captured["env"]["CODEX_HOME"])
    payload = json.loads((codex_home / "auth.json").read_text())
    assert captured["env"]["HERMES_CODEX_WORKER_CREDENTIAL_ID"] == "cred-2"
    assert captured["env"]["CODEX_HOME"] != str(tmp_path / "parent-codex-home")
    assert codex_home.is_symlink()
    assert payload["tokens"]["access_token"] == "access-2"
    assert payload["tokens"]["refresh_token"] == "refresh-2"


def test_codex_worker_refreshes_pool_credential_missing_id_token(monkeypatch, tmp_path):
    from agent import credential_pool
    from agent.codex_worker_auth import prepare_codex_worker_home

    hermes_home = tmp_path / "hermes-home"
    _write_pool_auth(
        hermes_home,
        [
            {
                "id": "cred-1",
                "label": "primary",
                "auth_type": "oauth",
                "priority": 0,
                "source": "manual:device_code",
                "access_token": "access-old",
                "refresh_token": "refresh-old",
            },
        ],
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    calls = []

    def fake_refresh(access_token, refresh_token):
        calls.append((access_token, refresh_token))
        return {
            "access_token": "access-new",
            "refresh_token": "refresh-new",
            "id_token": "id-new",
            "last_refresh": "now",
        }

    monkeypatch.setattr(credential_pool.auth_mod, "refresh_codex_oauth_pure", fake_refresh)

    codex_home = tmp_path / "worker-codex-home"
    credential_id = prepare_codex_worker_home(
        codex_home,
        source_env={"CODEX_HOME": str(tmp_path / "source-codex-home")},
        allow_fallback=False,
    )

    payload = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    entry = credential_pool.load_pool("openai-codex").entries()[0]
    assert credential_id == "cred-1"
    assert calls == [("access-old", "refresh-old")]
    assert payload["tokens"]["access_token"] == "access-new"
    assert payload["tokens"]["refresh_token"] == "refresh-new"
    assert payload["tokens"]["id_token"] == "id-new"
    assert entry.id_token == "id-new"


def test_cleanup_codex_worker_home_allows_child_of_explicit_root(monkeypatch, tmp_path):
    from agent.codex_worker_auth import cleanup_codex_worker_home

    root = tmp_path / "codex-worker-homes"
    child = root / "task-1"
    child.mkdir(parents=True)
    (child / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("HERMES_CODEX_WORKER_CLEANUP_ROOT", str(root))

    cleanup_codex_worker_home(child)

    assert root.exists()
    assert not child.exists()


def test_codex_role_worker_does_not_copy_inherited_worker_codex_home(monkeypatch, tmp_path):
    from agent import credential_pool
    from hermes_cli import kanban_codex_workers as workers

    parent_codex_home = tmp_path / "parent-worker-codex-home"
    _write_codex_auth(
        parent_codex_home,
        access="parent-worker-access",
        refresh="parent-worker-refresh",
        id_token="parent-worker-id",
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home-without-codex-auth"))
    monkeypatch.setenv("CODEX_HOME", str(parent_codex_home))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "parent-worker-cred")
    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: None)
    board, task = _claimed_planner(monkeypatch, tmp_path)
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"env": env})
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    codex_home = Path(captured["env"]["CODEX_HOME"])
    assert captured["env"].get("HERMES_CODEX_WORKER_CREDENTIAL_ID") != "parent-worker-cred"
    assert captured["env"]["CODEX_HOME"] != str(parent_codex_home)
    assert not (codex_home / "auth.json").exists()


def test_codex_role_worker_does_not_copy_external_codex_home(monkeypatch, tmp_path):
    from agent import credential_pool
    from hermes_cli import kanban_codex_workers as workers

    source_codex_home = tmp_path / "source-codex-home"
    _write_codex_auth(
        source_codex_home,
        access="source-access",
        refresh="source-refresh",
        id_token="source-id",
    )
    monkeypatch.setenv("CODEX_HOME", str(source_codex_home))
    monkeypatch.delenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", raising=False)
    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: None)
    board, task = _claimed_planner(monkeypatch, tmp_path)
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"env": env})
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    codex_home = Path(captured["env"]["CODEX_HOME"])
    assert captured["env"].get("HERMES_CODEX_WORKER_CREDENTIAL_ID") is None
    assert captured["env"]["CODEX_HOME"] != str(source_codex_home)
    assert not (codex_home / "auth.json").exists()


def test_role_worker_logs_named_tier_and_runtime_sources(monkeypatch, tmp_path):
    from agent import opencode_worker as ow
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    board, task = _claimed_planner(monkeypatch, tmp_path)

    class Proc:
        pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {
            "codex_home_root": str(tmp_path / "homes"),
            "roles": {
                "planner": {
                    "model_tier": "advanced",
                    "reasoning": "minimal",
                    "service_tier": "fast",
                }
            },
        },
    )
    monkeypatch.setattr(ow, "check_opencode_binary", lambda: (True, "/bin/opencode"))
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", lambda *args, **kwargs: Proc())

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    log = kanban_db.read_worker_log(task.id, board=board.slug)
    assert log is not None
    assert (
        "[kanban dispatcher] scheduled Codex role worker: "
        "role=planner reasoning=medium mode=fast "
        "model=gpt-5.6-sol tier=advanced tier_source=role "
        "reasoning_source=model_tier service_tier=fast "
        "service_tier_source=explicit"
    ) in log


def test_planner_worker_env_carries_effective_opencode_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db
    from agent import opencode_worker as ow

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "parent-codex-home"))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "parent-cred")
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"env": env})
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "opencode", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(ow, "check_opencode_binary", lambda: (True, "/bin/opencode"))
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(workspace), board=board.slug)

    assert captured["env"]["HERMES_CODING_WORKER_BACKEND"] == "opencode"
    assert "CODEX_HOME" not in captured["env"]
    assert "HERMES_CODEX_WORKER_CREDENTIAL_ID" not in captured["env"]
    log = kanban_db.read_worker_log(task.id, board=board.slug)
    assert log is not None
    assert "scheduled OpenCode role worker: role=planner reasoning=high mode=normal" in log


def test_command_center_repair_foreman_uses_codex_with_codex_config(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    _home(monkeypatch, tmp_path)
    board = "repair-codex-board"
    kanban_db.create_board(board, name="Repair Codex Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Repair blocked board",
            assignee="foreman",
            created_by="command-center-repair",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kanban_db.claim_task(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"env": env})
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {"backend": "codex", "codex_home_root": str(tmp_path / "homes")},
    )
    monkeypatch.setattr(workers, "_write_minimal_codex_home", lambda path: None)
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path), board=board)

    assert captured["env"]["HERMES_CODING_WORKER_BACKEND"] == "codex"
    assert captured["env"]["CODEX_HOME"].endswith(f"/homes/{task.id}")
    log = kanban_db.read_worker_log(task.id, board=board)
    assert log is not None
    assert "scheduled Codex role worker: role=foreman reasoning=high mode=normal" in log


def test_foreman_worker_env_defaults_to_codex(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db
    from agent import opencode_worker as ow

    _home(monkeypatch, tmp_path)
    board = "repair-opencode-board"
    kanban_db.create_board(board, name="Repair OpenCode Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Repair blocked board",
            assignee="foreman",
            created_by="command-center-repair",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        task = kanban_db.claim_task(conn, task_id)
    finally:
        conn.close()
    assert task is not None
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"env": env})
        return Proc()

    monkeypatch.setattr(workers, "_worker_config", lambda: {})
    monkeypatch.setattr(ow, "check_opencode_binary", lambda: (True, "/bin/opencode"))
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path), board=board)

    assert captured["env"]["HERMES_CODING_WORKER_BACKEND"] == "codex"
    assert captured["env"]["CODEX_HOME"].endswith(f"/{task.id}")
    log = kanban_db.read_worker_log(task.id, board=board)
    assert log is not None
    assert "scheduled Codex role worker: role=foreman reasoning=high mode=normal" in log


def test_role_extra_args_use_scheduled_runtime_env(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker

    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "low")
    monkeypatch.setenv("HERMES_CODEX_WORKER_SERVICE_TIER", "fast")

    assert worker._role_extra_args("planner") == [
        "-c", 'model_reasoning_effort="low"',
        "-c", 'service_tier="fast"',
    ]


def test_dev_runtime_auto_uses_fast_medium_for_simple_task(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    monkeypatch.delenv("HERMES_CODEX_WORKER_REASONING", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_SERVICE_TIER", raising=False)
    task = SimpleNamespace(
        title="Fix typo",
        body="Correct a README typo",
        result=None,
        last_failure_error=None,
        consecutive_failures=0,
        created_by="planner",
    )

    settings = workers._role_runtime_settings("dev", {}, task)

    assert settings["reasoning"] == "medium"
    assert settings["reasoning_source"] == "adaptive"
    assert settings["service_tier"] == "fast"
    assert settings["service_tier_source"] == "adaptive"


def test_dev_runtime_auto_keeps_risky_and_retry_work_high(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    monkeypatch.delenv("HERMES_CODEX_WORKER_REASONING", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_SERVICE_TIER", raising=False)
    risky = SimpleNamespace(
        title="Fix auth migration performance regression",
        body="Production auth path is slow after schema migration",
        result=None,
        last_failure_error=None,
        consecutive_failures=0,
        created_by="planner",
    )
    retry = SimpleNamespace(
        title="Fix parser",
        body="Small parser fix",
        result=None,
        last_failure_error="previous worker crashed",
        consecutive_failures=1,
        created_by="planner",
    )

    risky_settings = workers._role_runtime_settings("dev", {}, risky)
    retry_settings = workers._role_runtime_settings("dev", {}, retry)

    assert risky_settings["reasoning"] == "high"
    assert risky_settings["service_tier"] == "normal"
    assert retry_settings["reasoning"] == "high"
    assert retry_settings["service_tier"] == "normal"


def test_runtime_explicit_config_and_env_override_auto(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    task = SimpleNamespace(title="Fix typo", body="", consecutive_failures=0)
    cfg = {
        "roles": {"dev": {"reasoning": "low", "service_tier": "normal"}},
        "service_tier": "auto",
    }

    settings = workers._role_runtime_settings("dev", cfg, task)
    assert settings["reasoning"] == "low"
    assert settings["service_tier"] == "normal"
    assert settings["reasoning_source"] == "explicit"
    assert settings["service_tier_source"] == "explicit"

    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "xhigh")
    monkeypatch.setenv("HERMES_CODEX_WORKER_SERVICE_TIER", "fast")

    settings = workers._role_runtime_settings("dev", cfg, task)
    assert settings["reasoning"] == "low"
    assert settings["service_tier"] == "normal"

    cfg = {
        "roles": {"dev": {"reasoning": "auto", "service_tier": "auto"}},
        "service_tier": "normal",
    }
    settings = workers._role_runtime_settings("dev", cfg, task)
    assert settings["reasoning"] == "high"
    assert settings["service_tier"] == "fast"


def test_only_reviewer_auto_remains_xhigh(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    monkeypatch.delenv("HERMES_CODEX_WORKER_REASONING", raising=False)
    task = SimpleNamespace(title="Plan work", body="", consecutive_failures=0)

    assert workers._role_runtime_settings("planner", {}, task)["reasoning"] == "high"
    assert workers._role_runtime_settings("reviewer", {}, task)["reasoning"] == "xhigh"


def test_role_runtime_caps_explicit_max_reasoning_for_planner(monkeypatch):
    from hermes_cli import kanban_codex_workers as workers

    monkeypatch.delenv("HERMES_CODEX_WORKER_REASONING", raising=False)
    settings = workers._role_runtime_settings(
        "planner",
        {"roles": {"planner": {"reasoning": "max", "service_tier": "normal"}}},
    )

    assert settings["reasoning"] == "high"
    assert settings["reasoning_source"] == "review_only_cap"


def test_dispatch_recovers_recorded_role_result_before_dead_pid_crash(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER
    from hermes_cli.discord_worker_state import record_codex_worker_result

    board = dwb.start_direct_goal(thread_id="recover-review", goal="Review completed result")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Review",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
            workspace_kind="dir",
            workspace_path=str(tmp_path / "repo"),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (999_999_999, task_id),
        )
        conn.execute(
            "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
            (999_999_999, claimed.current_run_id),
        )
        conn.commit()
        record_codex_worker_result(
            task_id,
            board=board.slug,
            result=SimpleNamespace(
                backend="codex",
                final_text=json.dumps(
                    {
                        "status": "approved",
                        "summary": "Reviewer approved.",
                        "findings": [],
                        "new_tasks": [],
                    }
                ),
                error=None,
            ),
        )
        monkeypatch.setattr(kanban_db, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kanban_db, "_classify_worker_exit", lambda _pid: ("unknown", None))

        result = kanban_db.dispatch_once(conn, board=board.slug, max_spawn=0)
        task = kanban_db.get_task(conn, task_id)
        runs = kanban_db.list_runs(conn, task_id)
    finally:
        conn.close()

    assert result.crashed == []
    assert result.auto_blocked == []
    assert task is not None
    assert task.status == "done"
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
    assert runs[-1].outcome == "completed"
    assert runs[-1].summary == "Reviewer approved."


def test_dispatch_recovers_recorded_role_result_before_stale_reclaim(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER
    from hermes_cli.discord_worker_state import record_codex_worker_result

    board = dwb.start_direct_goal(thread_id="recover-stale-review", goal="Review completed stale result")
    now = int(time.time())
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Review",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
            workspace_kind="dir",
            workspace_path=str(tmp_path / "repo"),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        conn.execute(
            """
            UPDATE tasks
               SET worker_pid = ?, last_heartbeat_at = ?, claim_expires = ?
             WHERE id = ?
            """,
            (999_999_999, now - 500, now + 3600, task_id),
        )
        conn.execute(
            """
            UPDATE task_runs
               SET worker_pid = ?, started_at = ?, last_heartbeat_at = ?
             WHERE id = ?
            """,
            (999_999_999, now - 500, now - 500, claimed.current_run_id),
        )
        conn.commit()
        record_codex_worker_result(
            task_id,
            board=board.slug,
            result=SimpleNamespace(
                backend="codex",
                final_text=json.dumps(
                    {
                        "status": "approved",
                        "summary": "Reviewer approved stale run.",
                        "findings": [],
                        "new_tasks": [],
                    }
                ),
                error=None,
            ),
        )
        terminations = []
        monkeypatch.setattr(
            kanban_db,
            "_terminate_reclaimed_worker",
            lambda *args, **kwargs: terminations.append(args) or {"pid_terminated": False},
        )
        monkeypatch.setattr(kanban_db, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kanban_db, "_classify_worker_exit", lambda _pid: ("unknown", None))

        result = kanban_db.dispatch_once(
            conn,
            board=board.slug,
            max_spawn=0,
            stale_timeout_seconds=60,
        )
        task = kanban_db.get_task(conn, task_id)
        runs = kanban_db.list_runs(conn, task_id)
    finally:
        conn.close()

    assert result.stale == []
    assert result.crashed == []
    assert terminations == []
    assert task is not None
    assert task.status == "done"
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
    assert runs[-1].outcome == "completed"
    assert runs[-1].summary == "Reviewer approved stale run."


def test_dispatch_recovers_recorded_role_result_before_expired_claim_reclaim(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER
    from hermes_cli.discord_worker_state import record_codex_worker_result

    board = dwb.start_direct_goal(thread_id="recover-expired-review", goal="Review expired result")
    now = int(time.time())
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Review",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
            workspace_kind="dir",
            workspace_path=str(tmp_path / "repo"),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        conn.execute(
            """
            UPDATE tasks
               SET worker_pid = ?, last_heartbeat_at = ?, claim_expires = ?
             WHERE id = ?
            """,
            (999_999_999, now - 500, now - 1, task_id),
        )
        conn.execute(
            """
            UPDATE task_runs
               SET worker_pid = ?, started_at = ?, last_heartbeat_at = ?, claim_expires = ?
             WHERE id = ?
            """,
            (999_999_999, now - 500, now - 500, now - 1, claimed.current_run_id),
        )
        conn.commit()
        record_codex_worker_result(
            task_id,
            board=board.slug,
            result=SimpleNamespace(
                backend="codex",
                final_text=json.dumps(
                    {
                        "status": "approved",
                        "summary": "Reviewer approved expired run.",
                        "findings": [],
                        "new_tasks": [],
                    }
                ),
                error=None,
            ),
        )
        terminations = []
        monkeypatch.setattr(
            kanban_db,
            "_terminate_reclaimed_worker",
            lambda *args, **kwargs: terminations.append(args) or {"pid_terminated": False},
        )
        monkeypatch.setattr(kanban_db, "_pid_alive", lambda _pid: False)
        monkeypatch.setattr(kanban_db, "_classify_worker_exit", lambda _pid: ("unknown", None))

        result = kanban_db.dispatch_once(conn, board=board.slug, max_spawn=0)
        task = kanban_db.get_task(conn, task_id)
        runs = kanban_db.list_runs(conn, task_id)
    finally:
        conn.close()

    assert result.reclaimed == 0
    assert result.crashed == []
    assert terminations == []
    assert task is not None
    assert task.status == "done"
    assert task.consecutive_failures == 0
    assert task.last_failure_error is None
    assert runs[-1].outcome == "completed"
    assert runs[-1].summary == "Reviewer approved expired run."


def test_dispatch_recovers_blocked_dead_pid_recorded_role_result(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER
    from hermes_cli.discord_worker_state import record_codex_worker_result

    board = dwb.start_direct_goal(thread_id="recover-blocked-review", goal="Review blocked result")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Review",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
            workspace_kind="dir",
            workspace_path=str(tmp_path / "repo"),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        conn.execute(
            """
            UPDATE tasks
               SET status = 'blocked',
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL,
                   current_run_id = ?,
                   consecutive_failures = 2,
                   last_failure_error = ?
             WHERE id = ?
            """,
            (claimed.current_run_id, "pid 4141493 not alive", task_id),
        )
        conn.execute(
            """
            UPDATE task_runs
               SET status = 'crashed', outcome = 'crashed', error = ?
             WHERE id = ?
            """,
            ("pid 4141493 not alive", claimed.current_run_id),
        )
        conn.commit()
        record_codex_worker_result(
            task_id,
            board=board.slug,
            result=SimpleNamespace(
                backend="codex",
                final_text=json.dumps(
                    {
                        "status": "approved",
                        "summary": "Reviewer approved after blocked reclaim.",
                        "findings": [],
                        "new_tasks": [],
                    }
                ),
                error=None,
            ),
        )

        result = kanban_db.dispatch_once(conn, board=board.slug, max_spawn=0)
        task = kanban_db.get_task(conn, task_id)
        runs = kanban_db.list_runs(conn, task_id)
    finally:
        conn.close()

    assert result.crashed == []
    assert result.auto_blocked == []
    assert task is not None
    assert task.status == "done"
    assert task.last_failure_error is None
    assert runs[-1].outcome == "completed"
    assert runs[-1].summary == "Reviewer approved after blocked reclaim."


def test_dispatch_does_not_recover_blocked_dead_pid_from_stale_sidecar(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER
    from hermes_cli.discord_worker_state import record_codex_worker_result, write_codex_worker_state

    board = dwb.start_direct_goal(thread_id="recover-blocked-stale", goal="Review stale result")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Review",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
            workspace_kind="dir",
            workspace_path=str(tmp_path / "repo"),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        conn.execute(
            "UPDATE task_runs SET started_at = ? WHERE id = ?",
            (int(time.time()) + 60, claimed.current_run_id),
        )
        conn.execute(
            """
            UPDATE tasks
               SET status = 'blocked',
                   claim_lock = NULL,
                   claim_expires = NULL,
                   worker_pid = NULL,
                   current_run_id = ?,
                   consecutive_failures = 2,
                   last_failure_error = ?
             WHERE id = ?
            """,
            (claimed.current_run_id, "pid 4141493 not alive", task_id),
        )
        conn.execute(
            """
            UPDATE task_runs
               SET status = 'crashed', outcome = 'crashed', error = ?
             WHERE id = ?
            """,
            ("pid 4141493 not alive", claimed.current_run_id),
        )
        conn.commit()
        record_codex_worker_result(
            task_id,
            board=board.slug,
            result=SimpleNamespace(
                backend="codex",
                final_text=json.dumps(
                    {
                        "status": "approved",
                        "summary": "Stale reviewer approval.",
                        "findings": [],
                        "new_tasks": [],
                    }
                ),
                error=None,
            ),
        )
        write_codex_worker_state(task_id, board=board.slug, update={"updated_at": int(time.time()) - 60})

        result = kanban_db.dispatch_once(conn, board=board.slug, max_spawn=0)
        task = kanban_db.get_task(conn, task_id)
        runs = kanban_db.list_runs(conn, task_id)
    finally:
        conn.close()

    assert result.crashed == []
    assert task is not None
    assert task.status == "blocked"
    assert runs[-1].outcome == "crashed"


def test_opencode_adaptive_dev_reasoning_does_not_override_raw_config(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import config as config_mod

    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "medium")
    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING_SOURCE", "adaptive")
    monkeypatch.setattr(config_mod, "read_raw_config", lambda: {})

    assert worker._scheduled_opencode_worker_config() == {
        "simple_build_reasoning_level": "medium",
        "complex_build_reasoning_level": "medium",
    }

    monkeypatch.setattr(
        config_mod,
        "read_raw_config",
        lambda: {"coding_worker": {"simple_build_reasoning_level": "xhigh"}},
    )
    assert worker._scheduled_opencode_worker_config() is None


def test_role_extra_args_default_reasoning_by_role(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker

    monkeypatch.delenv("HERMES_CODEX_WORKER_REASONING", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_SERVICE_TIER", raising=False)

    assert worker._role_extra_args("planner")[1] == 'model_reasoning_effort="high"'
    assert worker._role_extra_args("reviewer")[1] == 'model_reasoning_effort="xhigh"'
    assert worker._role_extra_args("foreman")[1] == 'model_reasoning_effort="high"'
    assert worker._role_extra_args("dev")[1] == 'model_reasoning_effort="medium"'


def test_scheduled_runtime_metadata_attaches_to_worker_result(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV

    result = SimpleNamespace()
    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "low")
    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING_SOURCE", "model_tier")
    monkeypatch.setenv("HERMES_CODEX_WORKER_MODEL", "gpt-5.6-terra")
    monkeypatch.setenv("HERMES_CODEX_WORKER_MODEL_TIER", "intermediate")
    monkeypatch.setenv("HERMES_CODEX_WORKER_MODEL_TIER_SOURCE", "role")
    monkeypatch.setenv("HERMES_CODEX_WORKER_SERVICE_TIER", "fast")
    monkeypatch.setenv("HERMES_CODEX_WORKER_SERVICE_TIER_SOURCE", "explicit")

    worker._attach_scheduled_runtime(result, ROLE_DEV)

    assert result.service_tier == "fast"
    assert result.fast_mode is True
    assert result.run_profile == {
        "kind": "one_pass_build",
        "label": "1-pass build",
        "pass_count": 1,
        "plan_used": False,
        "passes": [
            {
                "name": "build",
                "agent": "dev",
                "reasoning": "low",
                "reasoning_source": "model_tier",
                "model": "gpt-5.6-terra",
                "model_tier": "intermediate",
                "model_tier_source": "role",
                "service_tier": "fast",
                "service_tier_source": "explicit",
            }
        ],
    }


def test_dev_role_uses_opencode_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV
    from agent import opencode_worker as ow

    monkeypatch.setenv("HERMES_CODING_WORKER_BACKEND", "opencode")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    calls = []

    def fake_run(prompt, workspace, **kwargs):
        calls.append((prompt, workspace, kwargs))
        return SimpleNamespace(
            final_text='{"status":"completed","summary":"ok","changed_files":[],"tests":[]}',
            error=None,
            interrupted=False,
            timed_out=False,
            should_retire=False,
            tool_iterations=1,
            thread_id="ses-build",
            turn_id="ses-build",
            backend="opencode",
            agents=["build"],
            plan_text="",
            exit_code=0,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = worker._run_role_backend(
        "prompt",
        str(tmp_path),
        ROLE_DEV,
        task=SimpleNamespace(id="t_dev", title="Fix bug", body="Fix parser bug"),
        task_id="t_dev",
        board=None,
    )

    assert result.backend == "opencode"
    assert calls
    assert calls[0][1] == str(tmp_path)
    assert calls[0][2]["force_plan"] is False


def test_planner_role_uses_opencode_plan_agent(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER
    from agent import opencode_worker as ow

    monkeypatch.setenv("HERMES_CODING_WORKER_BACKEND", "opencode")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ow,
        "load_opencode_config",
        lambda: {"plan_agent": "plan", "complex_plan_reasoning_level": "xhigh"},
    )
    monkeypatch.setattr(
        ow,
        "run_opencode_task",
        lambda *args, **kwargs: pytest.fail("planner must not use build wrapper"),
    )
    calls = []

    def fake_single_pass(prompt, workspace, **kwargs):
        calls.append((prompt, workspace, kwargs))
        return SimpleNamespace(
            final_text=(
                '{"status":"planned","summary":"ok",'
                '"acceptance_criteria":["answer box is simplified"],'
                '"tasks":[{"title":"Clean answer box","body":"Do it",'
                '"priority":10,"parents":[]}],"blocker":null}'
            ),
            error=None,
            interrupted=False,
            timed_out=False,
            should_retire=False,
            tool_iterations=1,
            thread_id="ses-plan",
            turn_id="ses-plan",
            backend="opencode",
            agents=["plan"],
            plan_text="",
            exit_code=0,
        )

    monkeypatch.setattr(ow, "run_opencode_single_pass", fake_single_pass)

    result = worker._run_role_backend(
        "prompt",
        str(tmp_path),
        ROLE_PLANNER,
        task=SimpleNamespace(id="t_plan", title="Plan work", body=""),
        task_id="t_plan",
        board=None,
    )

    assert result.backend == "opencode"
    assert calls
    assert calls[0][1] == str(tmp_path)
    assert calls[0][2]["agent"] == "plan"
    assert calls[0][2]["reasoning_level"] == "xhigh"


def test_opencode_role_receives_sanitized_env(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV
    from agent import opencode_worker as ow

    control_values = {
        "HERMES_KANBAN_DB": str(tmp_path / "live" / "kanban.db"),
        "HERMES_KANBAN_BOARD": "discord-1512532369897160735",
        "HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "HERMES_KANBAN_TASK": "task-1",
        "HERMES_KANBAN_RUN_ID": "run-1",
        "HERMES_KANBAN_CLAIM_LOCK": "claim-1",
        "HERMES_KANBAN_ROOT": str(tmp_path / "kanban-root"),
    }
    for key, value in control_values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HERMES_CODING_WORKER_BACKEND", "opencode")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("OPENAI_API_KEY", "credential-survives")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    calls = []

    def fake_run(prompt, workspace, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            final_text='{"status":"completed","summary":"ok","changed_files":[],"tests":[]}',
            error=None,
            interrupted=False,
            timed_out=False,
            should_retire=False,
            tool_iterations=1,
            thread_id="ses-build",
            turn_id="ses-build",
            backend="opencode",
            agents=["build"],
            plan_text="",
            exit_code=0,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = worker._run_role_backend(
        "prompt",
        str(tmp_path),
        ROLE_DEV,
        task=SimpleNamespace(id="t_dev", title="Fix bug", body="Fix parser bug"),
        task_id="t_dev",
        board=None,
    )

    child_env = calls[0]["env"]
    assert result.backend == "opencode"
    for key in control_values:
        assert key not in child_env
        assert os.environ[key] == control_values[key]
    assert child_env["HERMES_HOME"] == str(tmp_path / "hermes-home")
    assert child_env["OPENAI_API_KEY"] == "credential-survives"
    assert child_env["HERMES_DISCORD_WORKER_READ_ONLY"] == "1"


def test_reviewer_role_uses_configured_opencode_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER
    from agent import opencode_worker as ow

    monkeypatch.setenv("HERMES_CODING_WORKER_BACKEND", "opencode")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    calls = []

    def fake_run(prompt, workspace, **kwargs):
        calls.append((prompt, workspace, kwargs))
        return SimpleNamespace(
            final_text='{"status":"approved","summary":"ok","findings":[],"new_tasks":[]}',
            error=None,
            interrupted=False,
            timed_out=False,
            should_retire=False,
            tool_iterations=1,
            thread_id="ses-review",
            turn_id="ses-review",
            backend="opencode",
            agents=["build"],
            plan_text="",
            exit_code=0,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)
    monkeypatch.setattr(
        worker,
        "_run_codex",
        lambda *args, **kwargs: pytest.fail("reviewer should use OpenCode when configured"),
    )

    result = worker._run_role_backend(
        "prompt",
        str(tmp_path),
        ROLE_REVIEWER,
        task=SimpleNamespace(id="t_review", title="Review", body=""),
        task_id="t_review",
        board=None,
    )

    assert result.backend == "opencode"
    assert result.error is None
    assert calls


def test_command_center_repair_foreman_runtime_uses_codex_with_codex_backend(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_FOREMAN
    from agent import opencode_worker as ow

    monkeypatch.setenv("HERMES_CODING_WORKER_BACKEND", "codex")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    calls = []

    def fake_codex(prompt, workspace, *args, **kwargs):
        calls.append((prompt, workspace, kwargs))
        return SimpleNamespace(
            final_text=(
                '{"status":"completed","summary":"ok",'
                '"actions":[],"verification":[],"changed_tasks":[]}'
            ),
            error=None,
            interrupted=False,
            timed_out=False,
            should_retire=False,
            tool_iterations=1,
            thread_id="ses-repair",
            turn_id="ses-repair",
            backend="codex",
            agents=["build"],
            plan_text="",
            exit_code=0,
        )

    monkeypatch.setattr(
        ow,
        "run_opencode_task",
        lambda *args, **kwargs: pytest.fail("repair foreman should use Codex when configured"),
    )
    monkeypatch.setattr(worker, "_run_codex", fake_codex)

    result = worker._run_role_backend(
        "prompt",
        str(tmp_path),
        ROLE_FOREMAN,
        task=SimpleNamespace(
            id="t_repair",
            title="Repair blocked board",
            body="Recover board",
            assignee=ROLE_FOREMAN,
            created_by="command-center-repair",
        ),
        task_id="t_repair",
        board=None,
    )

    assert result.backend == "codex"
    assert calls


def test_opencode_planner_output_creates_dev_ticket(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_PLANNER
    from agent import opencode_worker as ow

    board, task = _claimed_planner(monkeypatch, tmp_path)
    monkeypatch.setenv("HERMES_CODING_WORKER_BACKEND", "opencode")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        ow,
        "load_opencode_config",
        lambda: {"plan_agent": "plan", "complex_plan_reasoning_level": "xhigh"},
    )
    monkeypatch.setattr(
        ow,
        "run_opencode_single_pass",
        lambda *args, **kwargs: SimpleNamespace(
            final_text=(
                '{"status":"planned","summary":"ok",'
                '"acceptance_criteria":["answer box is simplified"],'
                '"tasks":[{"title":"Clean answer box","body":"Do it",'
                '"priority":10,"parents":[]}],"blocker":null}'
            ),
            error=None,
            backend="opencode",
            agents=["plan"],
            tool_iterations=1,
            thread_id="ses-plan",
            turn_id="ses-plan",
        ),
    )

    result = worker._run_role_backend(
        "prompt",
        str(tmp_path / "repo"),
        ROLE_PLANNER,
        task=task,
        task_id=task.id,
        board=board.slug,
    )
    payload = worker._parse_json(result.final_text)

    conn = kanban_db.connect(board=board.slug)
    try:
        worker._apply_role_output(
            conn,
            task.id,
            ROLE_PLANNER,
            payload,
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=task.current_run_id,
        )
        dev_tasks = [
            item for item in kanban_db.list_tasks(conn, include_archived=False)
            if item.assignee == "dev"
        ]
    finally:
        conn.close()

    assert [item.title for item in dev_tasks] == ["R1: Clean answer box"]


def test_planner_output_replaces_bootstrap_request_criteria(monkeypatch, tmp_path):
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    old_request_body = (
        "Act as the planner for this board. Implement the approval activation so "
        "the Discord worker board starts in planning and produces dev tickets."
    )
    dwb._update_worker_meta(
        board.slug,
        {
            **board.worker,
            "criteria": [{"text": old_request_body, "active": True}],
        },
    )
    payload = {
        "status": "planned",
        "summary": "Planned one step.",
        "acceptance_criteria": [
            "Answer box is simplified.",
            "Answer box is simplified.",
            "Verification is recorded.",
        ],
        "tasks": [{"title": "Clean answer box", "body": "Do it.", "priority": 20}],
    }

    conn = kanban_db.connect(board=board.slug)
    try:
        worker._apply_role_output(
            conn,
            task.id,
            ROLE_PLANNER,
            payload,
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=task.current_run_id,
        )
    finally:
        conn.close()

    worker_meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert worker_meta["criteria"] == [
        {"text": "Answer box is simplified.", "active": True},
        {"text": "Verification is recorded.", "active": True},
    ]
    assert worker_meta["criteria_source"] == "planner"
    assert old_request_body not in json.dumps(worker_meta["criteria"])


def test_docker_runner_logs_immediate_registry_failure(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    board, task = _claimed_planner(monkeypatch, tmp_path)

    class Proc:
        pid = 9876

        def poll(self):
            return 125

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        stdout.write(b"docker: error from registry: denied\n")
        stdout.flush()
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {
            "backend": "codex",
            "runner": "docker",
            "docker_image": "ghcr.io/nousresearch/hermes-codex-worker:latest",
            "codex_home_root": str(tmp_path / "homes"),
        },
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="exited immediately with code 125"):
        workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    log = kanban_db.read_worker_log(task.id, board=board.slug)
    assert log is not None
    assert "ghcr.io/nousresearch/hermes-codex-worker:latest" in log
    assert "error from registry: denied" in log


def test_docker_runner_mounts_gh_config_read_only(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    board, task = _claimed_planner(monkeypatch, tmp_path)
    real_home = tmp_path / "real-home"
    gh_dir = real_home / ".config" / "gh"
    gh_dir.mkdir(parents=True)
    (gh_dir / "hosts.yml").write_text("github.com:\n", encoding="utf-8")
    captured = {}

    class Proc:
        pid = 9876

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"cmd": cmd, "env": env})
        return Proc()

    monkeypatch.setenv("HOME", str(real_home))
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {
            "backend": "codex",
            "runner": "docker",
            "docker_image": "ghcr.io/nousresearch/hermes-codex-worker:latest",
            "codex_home_root": str(tmp_path / "homes"),
        },
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    assert captured["env"]["GH_CONFIG_DIR"] == "/gh-config"
    assert "-v" in captured["cmd"]
    assert f"{gh_dir.resolve()}:/gh-config:ro" in captured["cmd"]
    assert "-e" in captured["cmd"]
    assert "GH_CONFIG_DIR" in captured["cmd"]
    assert "GH_CONFIG_DIR=/gh-config" not in captured["cmd"]


def test_docker_runner_uses_absolute_runtime_script(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    board, task = _claimed_planner(monkeypatch, tmp_path)
    captured = {}

    class Proc:
        pid = 9876

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"cmd": cmd, "env": env, "cwd": cwd})
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {
            "backend": "codex",
            "runner": "docker",
            "docker_image": "ghcr.io/nousresearch/hermes-codex-worker:latest",
            "codex_home_root": str(tmp_path / "homes"),
        },
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    assert captured["cmd"][-3:] == [
        "ghcr.io/nousresearch/hermes-codex-worker:latest",
        "python",
        "/hermes/hermes_cli/kanban_codex_worker.py",
    ]
    assert captured["env"]["PYTHONPATH"] == "/hermes"


def test_docker_runner_forwards_public_frontend_env_only(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    board, task = _claimed_planner(monkeypatch, tmp_path)
    captured = {}

    class Proc:
        pid = 9876

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"cmd": cmd, "env": env})
        return Proc()

    monkeypatch.setenv("VITE_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("VITE_SUPABASE_ANON_KEY", "public-anon-key")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "private-service-role")
    monkeypatch.setenv("DATABASE_URL", "postgres://private-db")
    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {
            "backend": "codex",
            "runner": "docker",
            "docker_image": "ghcr.io/nousresearch/hermes-codex-worker:latest",
            "codex_home_root": str(tmp_path / "homes"),
        },
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    assert "-e" in captured["cmd"]
    assert "VITE_SUPABASE_URL" in captured["cmd"]
    assert "VITE_SUPABASE_ANON_KEY" in captured["cmd"]
    assert "VITE_SUPABASE_URL=https://example.supabase.co" not in captured["cmd"]
    assert "VITE_SUPABASE_ANON_KEY=public-anon-key" not in captured["cmd"]
    assert "SUPABASE_SERVICE_ROLE_KEY" not in captured["cmd"]
    assert "DATABASE_URL" not in captured["cmd"]
    assert "private-service-role" not in captured["cmd"]
    assert "postgres://private-db" not in captured["cmd"]


def test_docker_runner_uses_read_broker_without_discord_credentials(monkeypatch, tmp_path):
    from hermes_cli import discord_worker_read
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    board, task = _claimed_planner(monkeypatch, tmp_path)
    captured = {}

    class Proc:
        pid = 9876

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update({"cmd": cmd, "env": env})
        return Proc()

    monkeypatch.setenv("DISCORD_BOT_TOKEN", "discord-token")
    monkeypatch.setenv("DISCORD_ADMIN_ACTIONS", "delete,pin")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "parent-codex-home"))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "parent-cred")
    monkeypatch.setattr(
        discord_worker_read,
        "start_read_broker",
        lambda token: ("http://127.0.0.1:9", "broker-secret"),
    )
    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {
            "backend": "codex",
            "runner": "docker",
            "docker_image": "ghcr.io/nousresearch/hermes-codex-worker:latest",
            "codex_home_root": str(tmp_path / "homes"),
        },
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    assert "DISCORD_BOT_TOKEN" not in captured["env"]
    assert "DISCORD_ADMIN_ACTIONS" not in captured["env"]
    assert captured["env"]["CODEX_HOME"] == "/codex-home"
    assert captured["env"].get("HERMES_CODEX_WORKER_CREDENTIAL_ID") != "parent-cred"
    assert captured["env"]["HERMES_DISCORD_WORKER_READ_URL"] == "http://127.0.0.1:9"
    assert captured["env"]["HERMES_DISCORD_WORKER_READ_TOKEN"] == "broker-secret"
    assert captured["env"]["HERMES_DISCORD_WORKER_CONTROL_URL"] == "http://127.0.0.1:9"
    assert captured["env"]["HERMES_DISCORD_WORKER_CONTROL_TOKEN"] == "broker-secret"
    assert "HERMES_DISCORD_WORKER_READ_URL" in captured["cmd"]
    assert "HERMES_DISCORD_WORKER_READ_TOKEN" in captured["cmd"]
    assert "HERMES_DISCORD_WORKER_CONTROL_URL" in captured["cmd"]
    assert "HERMES_DISCORD_WORKER_CONTROL_TOKEN" in captured["cmd"]
    assert "broker-secret" not in captured["cmd"]
    assert "discord-token" not in captured["cmd"]
    assert "parent-cred" not in captured["cmd"]
    assert "DISCORD_BOT_TOKEN" not in captured["cmd"]
    assert "DISCORD_ADMIN_ACTIONS" not in captured["cmd"]
    log = kanban_db.read_worker_log(task.id, board=board.slug)
    assert log is not None
    assert "discord-token" not in log
    assert "broker-secret" not in log
    assert "parent-cred" not in log


def test_worker_prompt_is_read_only_for_normal_roles(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV, ROLE_PLANNER, ROLE_REVIEWER

    monkeypatch.setattr(worker.kanban_db, "build_worker_context", lambda _conn, _task_id: "{}")
    monkeypatch.setattr(worker, "_build_reviewer_context", lambda _conn, _task_id: "reviewer compact")
    monkeypatch.setattr(worker, "_git_summary", lambda _workspace: "clean")

    for role in (ROLE_PLANNER, ROLE_DEV, ROLE_REVIEWER):
        prompt = worker._build_prompt(object(), "task-1", role)
        assert "python -m hermes_cli.discord_worker_read fetch-message" in prompt
        assert "python -m hermes_cli.discord_worker_read fetch-messages" in prompt
        assert "read-only" in prompt.lower()
        assert "finalizer/operator owns board and Discord mutation" in prompt
        assert "python -m hermes_cli.discord_worker_read discord-request" not in prompt
        assert "python -m hermes_cli.discord_worker_read update-board" not in prompt
        assert "python -m hermes_cli.discord_worker_read task-status" not in prompt
        assert "python -m hermes_cli.discord_worker_read sync-summary" not in prompt
        assert "exact host:port" in prompt
        assert "worker_frontend_smoke" in prompt
        assert "python -m hermes_cli.browser_preflight chromium" in prompt
        assert "do not install browsers" in prompt


def test_planner_output_links_parent_dependencies(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    payload = {
        "status": "planned",
        "summary": "Planned two steps.",
        "acceptance_criteria": ["done"],
        "tasks": [
            {
                "title": "R1: Build foundation",
                "body": "Do first.",
                "priority": 20,
                "parents": [],
            },
            {"title": "Wire feature", "body": "Do second.", "priority": 10, "parents": [0]},
        ],
    }

    conn = kanban_db.connect(board=board.slug)
    try:
        worker._apply_role_output(
            conn,
            task.id,
            ROLE_PLANNER,
            payload,
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=task.current_run_id,
        )
        tasks = kanban_db.list_tasks(conn, include_archived=False)
        dev_tasks = [item for item in tasks if item.assignee == "dev"]
        assert len(dev_tasks) == 2
        first = next(item for item in dev_tasks if item.title == "R1: Build foundation")
        second = next(item for item in dev_tasks if item.title == "R1: Wire feature")
        assert first.status == "ready"
        assert second.status == "todo"
        assert second.id in kanban_db.child_ids(conn, first.id)
    finally:
        conn.close()


def test_planner_output_cleans_created_tasks_when_completion_fails(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    payload = {
        "status": "planned",
        "summary": "Planned one step.",
        "tasks": [{"title": "Build foundation", "body": "Do it.", "priority": 20}],
    }

    def fail_complete(*args, **kwargs):
        raise RuntimeError("completion failed")

    monkeypatch.setattr(kanban_db, "complete_task", fail_complete)
    conn = kanban_db.connect(board=board.slug)
    try:
        with pytest.raises(RuntimeError, match="completion failed"):
            worker._apply_role_output(
                conn,
                task.id,
                ROLE_PLANNER,
                payload,
                board=board.slug,
                workspace=str(tmp_path / "repo"),
                expected_run_id=task.current_run_id,
            )
        dev_tasks = [
            item for item in kanban_db.list_tasks(conn, include_archived=False)
            if item.assignee == "dev"
        ]
    finally:
        conn.close()

    assert dev_tasks == []


def test_planner_output_persists_requirements_and_adds_dev_context_header(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    metadata = kanban_db.read_board_metadata(board.slug)
    board_worker = dict(metadata["discord_worker"])
    artifact_path = tmp_path / "plans" / "004-worker-pid-identity.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("# Worker PID identity plan\n", encoding="utf-8")
    board_worker.update(
        {
            "context_pack_path": str(tmp_path / "context-pack.json"),
            "context_pack_markdown_path": str(tmp_path / "context-pack.md"),
            "discord_plan_artifacts": [
                {
                    "artifact_path": str(artifact_path),
                    "content_sha256": "sha256-plan",
                    "kind": "local_plan",
                }
            ],
        }
    )
    from hermes_cli import discord_worker_boards as dwb

    dwb._update_worker_meta(board.slug, board_worker)
    payload = {
        "status": "planned",
        "summary": "Planned one step.",
        "acceptance_criteria": ["done"],
        "requirements": [
            {
                "id": "REQ-1",
                "text": "Preserve Discord context",
                "source_message_ids": ["123456789012345678"],
                "owner_task_indices": [0],
                "required": True,
            }
        ],
        "tasks": [
            {
                "title": "Build context pack",
                "body": "Goal: implement.\nSuccess means: done.\nStop when: verified.",
                "priority": 20,
                "requirement_ids": ["REQ-1"],
            }
        ],
    }

    conn = kanban_db.connect(board=board.slug)
    try:
        worker._apply_role_output(
            conn,
            task.id,
            ROLE_PLANNER,
            payload,
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=task.current_run_id,
        )
        dev_task = [item for item in kanban_db.list_tasks(conn, include_archived=False) if item.assignee == "dev"][0]
    finally:
        conn.close()

    assert dev_task.body is not None
    assert "Context pack:" in dev_task.body
    assert str(tmp_path / "context-pack.md") in dev_task.body
    assert "Durable Discord plan artifact paths:" in dev_task.body
    assert str(artifact_path) in dev_task.body
    assert "Requirement IDs: REQ-1" in dev_task.body
    worker_meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert worker_meta["requirements"][0]["id"] == "REQ-1"
    assert worker_meta["requirements"][0]["owner_task_ids"] == [dev_task.id]


def test_reviewer_output_creates_next_round_dev_ticket(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = dwb.start_direct_goal(thread_id="review-followup", goal="Ship it")
    artifact_path = tmp_path / "plans" / "reviewer-followup.md"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("# Reviewer follow-up plan\n", encoding="utf-8")
    dwb._update_worker_meta(
        board.slug,
        {
            **board.worker,
            "review_loop_count": 1,
            "discord_plan_artifacts": [
                {
                    "artifact_path": str(artifact_path),
                    "content_sha256": "reviewer-sha",
                    "kind": "local_plan",
                }
            ],
        },
    )
    conn = kanban_db.connect(board=board.slug)
    try:
        reviewer_id = kanban_db.create_task(
            conn,
            title="R1: Review Discord implementation",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
        )
        claimed = kanban_db.claim_task(conn, reviewer_id)
        assert claimed is not None

        worker._apply_role_output(
            conn,
            claimed.id,
            ROLE_REVIEWER,
            {
                "status": "changes_requested",
                "summary": "Needs follow-up.",
                "new_tasks": [
                    {"title": "R1: Fix follow-up", "body": "Do it.", "priority": 10}
                ],
            },
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=claimed.current_run_id,
        )
        dev_tasks = [
            item for item in kanban_db.list_tasks(conn, include_archived=False)
            if item.assignee == "dev"
        ]
    finally:
        conn.close()

    assert [item.title for item in dev_tasks] == ["R2: Fix follow-up"]
    assert dev_tasks[0].body is not None
    assert "Durable Discord plan artifact paths:" in dev_tasks[0].body
    assert str(artifact_path) in dev_tasks[0].body


def test_reviewer_pr_lifecycle_task_hands_off_without_dev_ticket(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = dwb.start_direct_goal(thread_id="review-pr-chore", goal="Ship it")
    dwb._update_worker_meta(board.slug, {"phase": "reviewing", "goal_status": "active"})
    dispatch_requests = []
    monkeypatch.setattr(
        worker,
        "_ensure_pr",
        lambda *args, **kwargs: pytest.fail("role worker must not finalize the PR"),
    )
    monkeypatch.setattr(
        worker,
        "mark_dispatch_dirty",
        lambda **kwargs: dispatch_requests.append(kwargs),
    )
    conn = kanban_db.connect(board=board.slug)
    try:
        reviewer_id = kanban_db.create_task(
            conn,
            title="R2: Review Discord implementation",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
        )
        claimed = kanban_db.claim_task(conn, reviewer_id)
        assert claimed is not None

        worker._apply_role_output(
            conn,
            claimed.id,
            ROLE_REVIEWER,
            {
                "status": "changes_requested",
                "summary": "Implementation is fine; PR branch is stale.",
                "new_tasks": [
                    {
                        "title": "R3: Update PR 239 with final branch state",
                        "body": "Push the worker branch, run gh pr checks --watch, and confirm the PR view is current.",
                        "priority": 10,
                    }
                ],
            },
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=claimed.current_run_id,
        )
        dev_tasks = [
            item for item in kanban_db.list_tasks(conn, include_archived=False)
            if item.assignee == "dev"
        ]
        reviewer = kanban_db.get_task(conn, reviewer_id)
        runs = kanban_db.list_runs(conn, reviewer_id)
    finally:
        conn.close()

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert dev_tasks == []
    assert reviewer is not None
    assert reviewer.status == "done"
    assert runs[-1].metadata["filtered_pr_lifecycle_tasks"]
    assert dispatch_requests == [
        {"board": board.slug, "reason": "reviewer-approval-persisted"}
    ]
    assert meta["phase"] == "reviewing"
    assert meta["goal_status"] == "active"


def test_reviewer_pr_lifecycle_filter_keeps_live_provenance_followup(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = dwb.start_direct_goal(thread_id="review-live-provenance", goal="Ship live cron pickup")
    conn = kanban_db.connect(board=board.slug)
    try:
        reviewer_id = kanban_db.create_task(
            conn,
            title="R2: Review Discord implementation",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
        )
        claimed = kanban_db.claim_task(conn, reviewer_id)
        assert claimed is not None

        worker._apply_role_output(
            conn,
            claimed.id,
            ROLE_REVIEWER,
            {
                "status": "changes_requested",
                "summary": "Live provenance is missing.",
                "new_tasks": [
                    {
                        "title": "Verify live pickup provenance",
                        "body": "Goal: verify and record the active runtime path, source of truth, and live pickup evidence.",
                        "priority": 10,
                    }
                ],
            },
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=claimed.current_run_id,
        )
        dev_tasks = [
            item for item in kanban_db.list_tasks(conn, include_archived=False)
            if item.assignee == "dev"
        ]
    finally:
        conn.close()

    assert [item.title for item in dev_tasks] == ["R1: Verify live pickup provenance"]


def test_reviewer_pr_lifecycle_task_filter_keeps_real_code_followup(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = dwb.start_direct_goal(thread_id="review-mixed-followup", goal="Ship it")
    conn = kanban_db.connect(board=board.slug)
    try:
        reviewer_id = kanban_db.create_task(
            conn,
            title="R2: Review Discord implementation",
            assignee=ROLE_REVIEWER,
            tenant=board.slug,
        )
        claimed = kanban_db.claim_task(conn, reviewer_id)
        assert claimed is not None

        worker._apply_role_output(
            conn,
            claimed.id,
            ROLE_REVIEWER,
            {
                "status": "changes_requested",
                "summary": "Needs one code fix and one PR chore.",
                "new_tasks": [
                    {"title": "Update PR 239", "body": "Push branch and wait for checks.", "priority": 10},
                    {"title": "Fix failing CI test", "body": "Goal: repair the failing unit test.", "priority": 9},
                ],
            },
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=claimed.current_run_id,
        )
        dev_tasks = [
            item for item in kanban_db.list_tasks(conn, include_archived=False)
            if item.assignee == "dev"
        ]
    finally:
        conn.close()

    assert [item.title for item in dev_tasks] == ["R1: Fix failing CI test"]


def test_reviewer_approval_hands_off_pr_finalization_to_dispatcher(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board = dwb.start_direct_goal(thread_id="review-pr-failed", goal="Ship it")
    dwb._update_worker_meta(board.slug, {"phase": "reviewing", "goal_status": "active"})
    dispatch_requests = []
    conn = kanban_db.connect(board=board.slug)
    try:
        reviewer_id = kanban_db.create_task(
            conn,
            title="Review",
            assignee=ROLE_REVIEWER,
            created_by="test",
            tenant=board.slug,
        )
        reviewer = kanban_db.claim_task(conn, reviewer_id)
        assert reviewer is not None
        monkeypatch.setattr(
            worker,
            "_ensure_pr",
            lambda *args, **kwargs: pytest.fail("role worker must not finalize the PR"),
        )
        monkeypatch.setattr(
            worker,
            "mark_dispatch_dirty",
            lambda **kwargs: dispatch_requests.append(kwargs),
        )

        worker._apply_role_output(
            conn,
            reviewer_id,
            ROLE_REVIEWER,
            {"status": "approved", "summary": "Looks good."},
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=reviewer.current_run_id,
        )
        completed = kanban_db.get_task(conn, reviewer_id)
        runs = kanban_db.list_runs(conn, reviewer_id)
    finally:
        conn.close()

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert completed is not None
    assert completed.status == "done"
    assert runs[-1].summary == "Looks good."
    assert dispatch_requests == [
        {"board": board.slug, "reason": "reviewer-approval-persisted"}
    ]
    assert meta["phase"] == "reviewing"
    assert meta["goal_status"] == "active"


def test_dev_blocked_output_marks_discord_board_blocked(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV

    board = dwb.start_direct_goal(thread_id="dev-blocked", goal="Ship it")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Implement change",
            assignee=ROLE_DEV,
            tenant=board.slug,
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None

        worker._apply_role_output(
            conn,
            claimed.id,
            ROLE_DEV,
            {
                "status": "blocked",
                "summary": "Workspace unavailable.",
                "blocker": "Workspace is not a git repository.",
            },
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=claimed.current_run_id,
        )

        blocked = kanban_db.get_task(conn, task_id)
    finally:
        conn.close()

    worker_meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert blocked is not None
    assert blocked.status == "blocked"
    assert worker_meta["goal_status"] == "blocked"
    assert worker_meta["phase"] == "blocked"


def test_dev_output_persists_handoff_manifest(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV

    board = dwb.start_direct_goal(thread_id="dev-handoff", goal="Ship it")
    conn = kanban_db.connect(board=board.slug)
    try:
        task_id = kanban_db.create_task(conn, title="Implement", assignee=ROLE_DEV, tenant=board.slug)
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        handoff = {
            "changed_files": ["app/page.tsx"],
            "tests": [{"command": "pnpm test", "result": "passed", "output": "ok"}],
            "verification": ["inspected UI"],
            "preview": {"url": "http://127.0.0.1:4173", "command": "pnpm preview --port 4173", "status": "passed"},
            "smoke_routes": ["/"],
            "known_warnings": ["none"],
            "notes": "ready for review",
        }
        worker._apply_role_output(
            conn,
            claimed.id,
            ROLE_DEV,
            {
                "status": "completed",
                "summary": "Done.",
                "changed_files": ["app/page.tsx"],
                "tests": [],
                "handoff": handoff,
            },
            board=board.slug,
            workspace=str(tmp_path / "repo"),
            expected_run_id=claimed.current_run_id,
        )
        run = kanban_db.list_runs(conn, task_id)[-1]
    finally:
        conn.close()

    assert run.metadata["handoff"] == handoff


def test_reviewer_prompt_uses_compact_context_and_parent_handoff(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_DEV, ROLE_REVIEWER

    board = dwb.start_direct_goal(
        thread_id="reviewer-compact",
        goal="Root goal with SECRET_FULL_BODY_SHOULD_NOT_APPEAR",
    )
    metadata = kanban_db.read_board_metadata(board.slug)
    board_worker = dict(metadata["discord_worker"])
    board_worker["criteria"] = ["Smoke exact preview port"]
    board_worker["requirements"] = [{"id": "REQ-1", "text": "Use handoff manifests"}]
    board_worker["context_pack_markdown_path"] = str(tmp_path / "context-pack.md")
    board_worker["context_pack_path"] = str(tmp_path / "context-pack.json")
    dwb._update_worker_meta(board.slug, board_worker)
    conn = kanban_db.connect(board=board.slug)
    try:
        dev_id = kanban_db.create_task(
            conn,
            title="Dev",
            body="FULL DEV BODY SHOULD NOT APPEAR " * 100,
            assignee=ROLE_DEV,
            tenant=board.slug,
        )
        dev = kanban_db.claim_task(conn, dev_id)
        assert dev is not None
        kanban_db.complete_task(
            conn,
            dev_id,
            summary="Dev complete.",
            metadata={"handoff": {"changed_files": ["src/app.ts"], "smoke_routes": ["/"]}},
            expected_run_id=dev.current_run_id,
        )
        reviewer_id = kanban_db.create_task(
            conn,
            title="Review",
            body="FULL REVIEW TASK BODY SHOULD NOT APPEAR " * 100,
            assignee=ROLE_REVIEWER,
            parents=[dev_id],
            tenant=board.slug,
        )
        prompt = worker._build_prompt(conn, reviewer_id, ROLE_REVIEWER)
    finally:
        conn.close()

    assert "Parent task handoff manifests" in prompt
    assert "src/app.ts" in prompt
    assert "Smoke exact preview port" in prompt
    assert "context-pack.md" in prompt
    assert "FULL DEV BODY SHOULD NOT APPEAR" not in prompt
    assert "FULL REVIEW TASK BODY SHOULD NOT APPEAR" not in prompt
    assert "Use parent task handoff manifests" in prompt


def test_planner_schema_uses_parents_not_depends_on():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    schema = worker._schema_instructions(ROLE_PLANNER)
    assert '"parents"' in schema
    assert "depends_on" not in schema
    assert "fewest coherent dev tickets" in schema
    assert "Fold normal discovery, audit, polish, and verification" in schema
    assert "detailed, self-contained implementation brief" in schema
    assert "opens with Goal, Success means, and Stop when" in schema
    assert "ticket-specific acceptance criteria" in schema
    assert "include board-level criteria only when that ticket owns the whole outcome" in schema
    assert "Set Stop when to the concrete handoff point" in schema
    assert "deduplicated canonical board-level list" in schema


def test_planner_prompt_defaults_simple_jobs_to_one_dev_ticket():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    frame = worker._role_outcome_frame(ROLE_PLANNER)
    schema = worker._schema_instructions(ROLE_PLANNER)

    assert "Simple or single-surface Hermes/Discord jobs default to exactly one dev ticket" in frame
    assert "For simple or single-surface Hermes/Discord jobs, default to exactly one dev ticket" in schema
    assert "Fold docs, runbook notes, migration verification" in schema
    assert "active-path evidence" in schema
    assert "owning implementation ticket" in schema


def test_planner_prompt_limits_standalone_tickets():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    schema = worker._schema_instructions(ROLE_PLANNER)

    assert "Create standalone dev tickets only for truly independent implementation slices" in schema
    assert "user-requested separate deliverables" in schema
    assert "shared blockers/prerequisites affecting multiple tickets" in schema
    assert "Do not create extra assertion, telemetry, debug, hardening, or PR-check tickets" in schema
    assert "concrete unmet acceptance criterion" in schema


def test_reviewer_prompt_approves_when_only_optional_followups_remain():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    frame = worker._role_outcome_frame(ROLE_REVIEWER)
    schema = worker._schema_instructions(ROLE_REVIEWER)

    assert "If requirements are satisfied and only optional improvements remain" in frame
    assert "approve with empty new_tasks" in frame
    assert "Do not emit new_tasks for optional hardening, extra tests" in schema
    assert "PR lifecycle, docs polish, telemetry" in schema
    assert "routine active-path/code-island checks" in schema
    assert "nice-to-have cleanup when requirements are satisfied" in schema
    assert "set status to approved, keep new_tasks empty" in schema


def test_reviewer_prompt_preserves_real_gap_followups():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    schema = worker._schema_instructions(ROLE_REVIEWER)

    assert "Request a new round only for concrete acceptance gaps" in schema
    assert "evidenced regressions" in schema
    assert "real defects" in schema
    assert "requested behavior that is unmet" in schema
    assert "part of the requested behavior or acceptance criteria" in schema
    assert "follow-up dev task must explicitly ask dev to verify" in schema


def test_worker_role_frames_are_outcome_first():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV, ROLE_PLANNER, ROLE_REVIEWER

    dev_frame = worker._role_outcome_frame(ROLE_DEV)
    reviewer_frame = worker._role_outcome_frame(ROLE_REVIEWER)
    reviewer_schema = worker._schema_instructions(ROLE_REVIEWER)

    assert "Goal: Complete the assigned Kanban ticket" in dev_frame
    assert "Success means:" in dev_frame
    assert "Stop when: Return the JSON completion" in dev_frame
    assert "Goal: Decide whether the board work satisfies" in reviewer_frame
    assert "Success means:" in reviewer_frame
    assert "Stop when: Return the JSON review verdict." in reviewer_frame
    assert "new_tasks body must be a self-contained follow-up brief" in reviewer_schema
    assert "opens with Goal, Success means, and Stop when" in reviewer_schema
    assert "Do not create dev tickets whose goal is to push a branch" in worker._schema_instructions(ROLE_PLANNER)
    assert "Do not emit new_tasks for pure PR lifecycle chores" in reviewer_schema
    assert "pre_review_readiness advisory only as evidence" in reviewer_schema
    assert "active runtime paths" in reviewer_schema
    assert "source of truth" in reviewer_schema
    assert "Never push to a remote branch" in worker._schema_instructions(ROLE_DEV)


def test_worker_pr_mutation_guard_blocks_push_and_pr_mutation(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_DEV

    monkeypatch.setattr(worker.shutil, "which", lambda binary: "/bin/true")
    guard_env, guard_dir = worker._role_pr_mutation_guard_env(ROLE_DEV)
    assert guard_dir is not None
    env = os.environ.copy()
    env.update(guard_env)
    try:
        git_push = subprocess.run(["git", "push"], env=env, capture_output=True, text=True, timeout=10)
        gh_create = subprocess.run(["gh", "pr", "create"], env=env, capture_output=True, text=True, timeout=10)
        gh_repo_create = subprocess.run(
            ["gh", "--repo", "sligo-labs/PID", "pr", "create"],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        git_status = subprocess.run(["git", "status"], env=env, capture_output=True, text=True, timeout=10)
    finally:
        worker._cleanup_pr_mutation_guard(guard_dir)

    assert git_push.returncode == 126
    assert gh_create.returncode == 126
    assert gh_repo_create.returncode == 126
    assert "deterministic finalizer" in git_push.stderr
    assert "deterministic finalizer" in gh_create.stderr
    assert git_status.returncode == 0


def test_worker_prompt_mentions_discord_read_helper(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    conn = kanban_db.connect(board=board.slug)
    try:
        prompt = worker._build_prompt(conn, task.id, ROLE_PLANNER)
    finally:
        conn.close()

    assert "finalizer/operator owns board and Discord mutation" in prompt
    assert "Outcome frame:" in prompt
    assert "Goal: Convert the Kanban context into the smallest coherent implementation plan" in prompt
    assert "Success means:" in prompt
    assert "Stop when: Return the JSON plan or a concise blocker." in prompt
    assert "python -m hermes_cli.discord_worker_read fetch-message" in prompt
    assert "python -m hermes_cli.discord_worker_read fetch-messages" in prompt
    assert "python -m hermes_cli.discord_worker_read update-board" not in prompt
    assert "python -m hermes_cli.discord_worker_read discord-request" not in prompt


def test_foreman_role_prompt_and_guards_allow_repair_mutation(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_FOREMAN

    board = "foreman-repair-board"
    kanban_db.create_board(board, name="Foreman Repair Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Repair blocked board",
            body="Recover blocked worker-board tickets.",
            assignee=ROLE_FOREMAN,
            created_by="command-center-repair",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        prompt = worker._build_prompt(conn, task_id, ROLE_FOREMAN)
    finally:
        conn.close()

    assert "You are the Discord Kanban foreman worker" in prompt
    assert "safely mutate Kanban board/task state" in prompt
    assert "mark dispatch dirty" in prompt
    assert "retry, unblock, close, reassign" in prompt
    assert "Use Discord worker read/control broker access only when necessary" in prompt
    assert "not subject to the planner/dev/reviewer read-only" in prompt
    assert "Do not create code-change PRs" in prompt
    assert "follow_up_proposals" in prompt
    assert "Command Center self-improvement proposal/job" in prompt
    assert "durable repo fix discovered during repair" in prompt
    assert "Keep secrets" in prompt
    assert "Do not call mutation helpers" not in prompt
    assert worker._role_pr_mutation_guard_env(ROLE_FOREMAN) == ({}, None)
    assert worker._role_read_only_discord_env(ROLE_FOREMAN) == {}


def test_foreman_runtime_defaults_to_high_normal():
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli.discord_worker_boards import ROLE_FOREMAN

    os.environ.pop("HERMES_CODEX_WORKER_REASONING", None)
    os.environ.pop("HERMES_CODEX_WORKER_SERVICE_TIER", None)
    settings = workers._role_runtime_settings(ROLE_FOREMAN, {}, None)

    assert settings["reasoning"] == "high"
    assert settings["service_tier"] == "normal"
    assert worker._worker_reasoning_effort(ROLE_FOREMAN) == "high"


def test_native_codex_worker_caps_max_reasoning_override_for_foreman(monkeypatch):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_FOREMAN

    monkeypatch.setenv("HERMES_CODEX_WORKER_REASONING", "max")

    assert worker._worker_reasoning_effort(ROLE_FOREMAN) == "high"
    assert worker._role_extra_args(ROLE_FOREMAN)[1] == 'model_reasoning_effort="high"'


def test_foreman_completed_output_completes_repair_task_without_dev_checkpoint(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import ROLE_FOREMAN

    board = "foreman-output-board"
    kanban_db.create_board(board, name="Foreman Output Board")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Repair blocked board",
            assignee=ROLE_FOREMAN,
            created_by="command-center-repair",
            workspace_kind="dir",
            workspace_path=str(tmp_path),
        )
        claimed = kanban_db.claim_task(conn, task_id)
        assert claimed is not None
        checkpoint_calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(worker, "_checkpoint_commit", lambda workspace, task_id, summary: checkpoint_calls.append((workspace, task_id, summary)))
        payload = {
            "status": "completed",
            "summary": "Recovered board.",
            "actions": ["unblocked t1", "marked dispatch dirty"],
            "verification": ["dispatch picked t1"],
            "changed_tasks": [{"id": "t1", "action": "unblock", "status": "ready"}],
        }

        worker._apply_role_output(
            conn,
            task_id,
            ROLE_FOREMAN,
            payload,
            board=board,
            workspace=str(tmp_path),
            expected_run_id=claimed.current_run_id,
        )
        task = kanban_db.get_task(conn, task_id)
        run = kanban_db.latest_run(conn, task_id)
    finally:
        conn.close()

    assert checkpoint_calls == []
    assert task is not None
    assert task.status == "done"
    assert run is not None
    assert run.outcome == "completed"
    assert run.metadata["raw"] == payload
    assert run.metadata["actions"] == payload["actions"]
    assert run.metadata["verification"] == payload["verification"]
    assert run.metadata["changed_tasks"] == payload["changed_tasks"]


def test_run_codex_records_app_server_state(monkeypatch, tmp_path):
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()

    session_envs = []

    class FakeSession:
        def __init__(self, **kwargs):
            session_envs.append(dict(kwargs["env"]))
            self.on_event = kwargs["on_event"]

        def run_turn(self, prompt, turn_timeout):
            self.on_event(
                {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "commandExecution",
                            "cwd": "/home/droid/secret",
                            "command": "cat /home/droid/secret/.env",
                        }
                    },
                }
            )
            return SimpleNamespace(
                final_text='{"status":"planned","summary":"ok","tasks":[]}',
                error=None,
                interrupted=False,
                timed_out=False,
                should_retire=False,
                tool_iterations=1,
                turn_id="turn-1",
                thread_id="thread-1",
            )

        def close(self):
            pass

    monkeypatch.setattr(worker, "CodexAppServerSession", FakeSession)

    result = worker._run_codex(
        "prompt",
        str(workspace),
        ROLE_PLANNER,
        task_id=task.id,
        board=board.slug,
    )

    assert result.turn_id == "turn-1"
    assert session_envs[0]["HERMES_DISCORD_WORKER_READ_ONLY"] == "1"
    assert session_envs[0]["HERMES_DISCORD_WORKER_CONTROL_URL"] == ""
    assert session_envs[0]["HERMES_DISCORD_WORKER_CONTROL_TOKEN"] == ""
    state = dwb.ticket_state_for_session("9001", task.id)["codex_state"]
    rendered = str(state)
    assert state["result"]["thread_id"] == "thread-1"
    assert state["events"][0]["item_type"] == "commandExecution"
    assert "/home/droid/secret" not in rendered
    assert "[REDACTED_PATH]" in rendered


def test_dev_role_backend_uses_trusted_model_tier_for_planner_ui_route(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import config as config_mod
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.config import DEFAULT_CONFIG
    from hermes_cli.discord_worker_boards import ROLE_DEV

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    monkeypatch.setattr(config_mod, "load_config", lambda: cfg)
    monkeypatch.setenv("HERMES_CODEX_WORKER_MODEL_TIER", "trivial")
    monkeypatch.setenv("HERMES_CODEX_WORKER_MODEL_TIER_SOURCE", "role")
    monkeypatch.setattr(worker, "_role_uses_opencode", lambda role, task: False)
    monkeypatch.setattr(worker, "_materialize_role_autoreview", lambda workspace, role: "")
    events = []
    captured = {}

    def fake_event(task_id, *, board, event):
        events.append(event)

    def fake_run_codex(prompt, workspace, role, *, task_id, board, ui_work_route=None):
        captured.update(
            {
                "prompt": prompt,
                "workspace": workspace,
                "role": role,
                "task_id": task_id,
                "board": board,
                "ui_work_route": ui_work_route,
            }
        )
        return SimpleNamespace(final_text="{}", error=None)

    monkeypatch.setattr(worker, "record_codex_worker_event", fake_event)
    monkeypatch.setattr(worker, "_run_codex", fake_run_codex)

    task = SimpleNamespace(
        title="R1: Smoke ui_visual_specialist route with tiny Command Center visual polish",
        body='Recorded planner route decision for this ticket: {"route":"ui_visual_specialist","model_tier":"advanced","rationale":"Command Center visual polish smoke"}',
        result=None,
    )

    worker._run_role_backend("prompt", str(tmp_path), ROLE_DEV, task=task, task_id="t-ui", board="b-ui")

    route = events[0]["params"]["route"]
    assert route["selected_route"] == "ui_visual_specialist"
    assert route["selected_provider"] == ""
    assert route["selected_model"] == ""
    assert route["route_decision_source"] == "planner"
    assert route["model_tier"] == "trivial"
    assert "worker_tier" not in route
    assert route["recommended_skills"] == ["taste-skill"]
    assert "selected_route: ui_visual_specialist" in captured["prompt"]
    assert "recommended_skills: taste-skill" in captured["prompt"]
    assert captured["ui_work_route"].selected_route == "ui_visual_specialist"
    assert events[0]["method"] == "ui_work_route/decision"


def test_run_role_backend_keeps_configured_codex_for_ui_specialist(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.config import DEFAULT_CONFIG
    from hermes_cli.discord_worker_boards import ROLE_DEV
    from hermes_cli.ui_work_routing import resolve_ui_work_route

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    decision = resolve_ui_work_route(
        DEFAULT_CONFIG,
        task="Implement Command Center visual polish.",
        backend="codex",
        route_decision={"route": "ui_visual_specialist", "rationale": "visual polish"},
    )
    seen = {}

    def fake_run_codex(prompt, workspace, role, *, task_id, board, ui_work_route=None):
        seen.update(
            prompt=prompt,
            workspace=workspace,
            role=role,
            route=ui_work_route,
        )
        return SimpleNamespace(final_text="ok", error=None)

    monkeypatch.setattr(worker, "_resolve_task_ui_work_route", lambda *args, **kwargs: decision)
    monkeypatch.setattr(worker, "_role_uses_opencode", lambda role, task: False)
    monkeypatch.setattr(worker, "_materialize_role_autoreview", lambda *args: "")
    monkeypatch.setattr(worker, "_run_codex", fake_run_codex)

    result = worker._run_role_backend(
        "prompt",
        str(workspace),
        ROLE_DEV,
        task=task,
        task_id=task.id,
        board=board.slug,
    )

    assert result.final_text == "ok"
    assert seen["workspace"] == str(workspace)
    assert seen["route"].selected_route == "ui_visual_specialist"
    assert seen["route"].selected_provider == ""
    assert seen["route"].selected_model == ""
    assert "UI specialist skill loading" in seen["prompt"]


def test_run_role_backend_keeps_configured_opencode_for_ui_specialist(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.config import DEFAULT_CONFIG
    from hermes_cli.discord_worker_boards import ROLE_DEV
    from hermes_cli.ui_work_routing import resolve_ui_work_route

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    decision = resolve_ui_work_route(
        DEFAULT_CONFIG,
        task="Implement Command Center visual polish.",
        backend="opencode",
        route_decision={"route": "ui_visual_specialist", "rationale": "visual polish"},
    )
    seen = {}

    def fake_run_opencode(
        prompt,
        workspace,
        role,
        *,
        task,
        task_id,
        board,
        ui_work_route=None,
    ):
        seen.update(
            prompt=prompt,
            workspace=workspace,
            role=role,
            route=ui_work_route,
        )
        return SimpleNamespace(final_text="ok", error=None)

    monkeypatch.setattr(worker, "_resolve_task_ui_work_route", lambda *args, **kwargs: decision)
    monkeypatch.setattr(worker, "_role_uses_opencode", lambda role, task: True)
    monkeypatch.setattr(worker, "_materialize_role_autoreview", lambda *args: "")
    monkeypatch.setattr(worker, "_run_opencode", fake_run_opencode)

    result = worker._run_role_backend(
        "prompt",
        str(workspace),
        ROLE_DEV,
        task=task,
        task_id=task.id,
        board=board.slug,
    )

    assert result.final_text == "ok"
    assert seen["workspace"] == str(workspace)
    assert seen["route"].selected_route == "ui_visual_specialist"
    assert seen["route"].backend == "opencode"
    assert "UI specialist skill loading" in seen["prompt"]


def test_kanban_backend_child_env_scrubs_control_vars_without_mutating_role_env(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    control_values = {
        "HERMES_KANBAN_DB": str(tmp_path / "live" / "kanban.db"),
        "HERMES_KANBAN_BOARD": "discord-1512532369897160735",
        "HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "HERMES_KANBAN_TASK": "task-1",
        "HERMES_KANBAN_RUN_ID": "run-1",
        "HERMES_KANBAN_CLAIM_LOCK": "claim-1",
        "HERMES_KANBAN_ROOT": str(tmp_path / "kanban-root"),
    }
    for key, value in control_values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")
    monkeypatch.setenv("OPENAI_API_KEY", "credential-survives")
    monkeypatch.setenv("VITE_PUBLIC_URL", "https://example.test")

    child_env = worker._backend_child_env({"HERMES_DISABLE_MCP": "1"})

    for key in control_values:
        assert key not in child_env
        assert os.environ[key] == control_values[key]
    assert child_env["HERMES_HOME"] == str(tmp_path / "hermes-home")
    assert child_env["PATH"] == "/usr/local/bin:/usr/bin"
    assert child_env["OPENAI_API_KEY"] == "credential-survives"
    assert child_env["VITE_PUBLIC_URL"] == "https://example.test"
    assert child_env["HERMES_DISABLE_MCP"] == "1"


def test_run_codex_passes_sanitized_replacement_env_to_app_server(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_REVIEWER

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    control_values = {
        "HERMES_KANBAN_DB": str(tmp_path / "live" / "kanban.db"),
        "HERMES_KANBAN_BOARD": board.slug,
        "HERMES_KANBAN_WORKSPACES_ROOT": str(tmp_path / "workspaces"),
        "HERMES_KANBAN_TASK": task.id,
        "HERMES_KANBAN_RUN_ID": "run-1",
        "HERMES_KANBAN_CLAIM_LOCK": "claim-1",
        "HERMES_KANBAN_ROOT": str(tmp_path / "kanban-root"),
    }
    for key, value in control_values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("OPENAI_API_KEY", "credential-survives")
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)
    sessions = []

    class FakeSession:
        def __init__(self, **kwargs):
            sessions.append(kwargs)

        def run_turn(self, prompt, turn_timeout):
            return SimpleNamespace(
                final_text='{"status":"approved","summary":"ok","findings":[]}',
                error=None,
                interrupted=False,
                timed_out=False,
                should_retire=False,
                tool_iterations=1,
                turn_id="turn-1",
                thread_id="thread-1",
            )

        def close(self):
            pass

    monkeypatch.setattr(worker, "CodexAppServerSession", FakeSession)

    result = worker._run_codex(
        "prompt",
        str(workspace),
        ROLE_REVIEWER,
        task_id=task.id,
        board=board.slug,
    )

    child_env = sessions[0]["env"]
    assert result.turn_id == "turn-1"
    assert sessions[0]["replace_env"] is True
    for key in control_values:
        assert key not in child_env
        assert os.environ[key] == control_values[key]
    assert child_env["HERMES_HOME"] == str(tmp_path / "hermes-home")
    assert child_env["OPENAI_API_KEY"] == "credential-survives"
    assert child_env["HERMES_DISCORD_WORKER_READ_ONLY"] == "1"
    assert child_env["HERMES_KANBAN_WORKSPACE"] == str(workspace)
    assert child_env["HERMES_DISABLE_MCP"] == "1"


def test_run_codex_retries_auth_failure_with_next_pool_credential(monkeypatch, tmp_path):
    from agent.credential_pool import STATUS_EXHAUSTED, load_pool
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli.discord_worker_boards import ROLE_PLANNER

    hermes_home = tmp_path / "hermes-home"
    _write_pool_auth(
        hermes_home,
        [
            {
                "id": "cred-1",
                "label": "primary",
                "auth_type": "oauth",
                "priority": 0,
                "source": "manual:device_code",
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "id_token": "id-1",
            },
            {
                "id": "cred-2",
                "label": "secondary",
                "auth_type": "oauth",
                "priority": 1,
                "source": "manual:device_code",
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "id_token": "id-2",
            },
        ],
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    codex_home = tmp_path / "worker-codex-home"
    _write_codex_auth(codex_home, access="access-1", refresh="refresh-1", id_token="id-1")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "cred-1")
    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    workspace.mkdir()
    session_access_tokens = []

    class FakeSession:
        def __init__(self, **kwargs):
            payload = json.loads((Path(kwargs["codex_home"]) / "auth.json").read_text(encoding="utf-8"))
            session_access_tokens.append(payload["tokens"]["access_token"])

        def run_turn(self, prompt, turn_timeout):
            if len(session_access_tokens) == 1:
                return SimpleNamespace(
                    final_text="",
                    error="Codex authentication failed: refresh token was revoked.",
                    auth_failed=True,
                    interrupted=False,
                    timed_out=False,
                    should_retire=True,
                    tool_iterations=0,
                    turn_id=None,
                    thread_id=None,
                )
            return SimpleNamespace(
                final_text='{"status":"planned","summary":"ok","tasks":[]}',
                error=None,
                auth_failed=False,
                interrupted=False,
                timed_out=False,
                should_retire=False,
                tool_iterations=1,
                turn_id="turn-2",
                thread_id="thread-2",
            )

        def close(self):
            pass

    monkeypatch.setattr(worker, "CodexAppServerSession", FakeSession)
    monkeypatch.setattr(worker, "record_codex_worker_result", lambda *args, **kwargs: None)

    result = worker._run_codex(
        "prompt",
        str(workspace),
        ROLE_PLANNER,
        task_id=task.id,
        board=board.slug,
    )

    pool_entries = {entry.id: entry for entry in load_pool("openai-codex").entries()}
    payload = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
    assert result.turn_id == "turn-2"
    assert session_access_tokens == ["access-1", "access-2"]
    assert os.environ["HERMES_CODEX_WORKER_CREDENTIAL_ID"] == "cred-2"
    assert payload["tokens"]["access_token"] == "access-2"
    assert pool_entries["cred-1"].last_status == STATUS_EXHAUSTED
    assert pool_entries["cred-1"].last_error_code == 401


def test_rotate_codex_worker_credential_uses_child_home_for_container_mount(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    hermes_home = tmp_path / "hermes-home"
    _write_pool_auth(
        hermes_home,
        [
            {
                "id": "cred-1",
                "label": "primary",
                "auth_type": "oauth",
                "priority": 0,
                "source": "manual:device_code",
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "id_token": "id-1",
            },
            {
                "id": "cred-2",
                "label": "secondary",
                "auth_type": "oauth",
                "priority": 1,
                "source": "manual:device_code",
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "id_token": "id-2",
            },
        ],
    )
    codex_mount = tmp_path / "codex-mount"
    _write_codex_auth(codex_mount, access="access-1", refresh="refresh-1", id_token="id-1")
    (codex_mount / "sentinel.txt").write_text("keep", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("CODEX_HOME", str(codex_mount))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "cred-1")
    monkeypatch.setenv("HERMES_CODEX_WORKER_CONTAINER_CODEX_HOME", "1")

    rotated = worker._rotate_codex_worker_credential_after_auth_failure(
        SimpleNamespace(error="Codex authentication failed")
    )

    next_home = codex_mount / ".rotated-credential-home"
    original_payload = json.loads((codex_mount / "auth.json").read_text(encoding="utf-8"))
    next_payload = json.loads((next_home / "auth.json").read_text(encoding="utf-8"))
    assert rotated is True
    assert os.environ["CODEX_HOME"] == str(next_home)
    assert (codex_mount / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert original_payload["tokens"]["access_token"] == "access-1"
    assert next_payload["tokens"]["access_token"] == "access-2"
    assert not next_home.is_symlink()


def test_rotate_codex_worker_credential_disables_fallback_auth_copy(monkeypatch, tmp_path):
    from agent import codex_worker_auth
    from hermes_cli import kanban_codex_worker as worker

    captured = {}
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "worker-codex-home"))
    monkeypatch.setenv("HERMES_CODEX_WORKER_CREDENTIAL_ID", "cred-1")
    monkeypatch.setattr(
        codex_worker_auth,
        "mark_codex_worker_credential_auth_failed",
        lambda credential_id, *, message=None: True,
    )

    def fake_prepare(codex_home, **kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(codex_worker_auth, "prepare_codex_worker_home", fake_prepare)

    rotated = worker._rotate_codex_worker_credential_after_auth_failure(
        SimpleNamespace(error="Codex authentication failed")
    )

    assert rotated is False
    assert captured["allow_fallback"] is False


def test_update_phase_refreshes_worker_updated_at(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board, _task = _claimed_planner(monkeypatch, tmp_path)
    monkeypatch.setattr(worker.time, "time", lambda: 12345)

    worker._update_phase(board.slug, "complete", goal_status="done")

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["phase"] == "complete"
    assert meta["goal_status"] == "done"
    assert meta["updated_at"] == 12345
    assert meta["terminal_reaction_sync_pending"] is True
    assert meta["terminal_summary_sync_pending"] is True
    assert meta["terminal_completion_message_pending"] is True


def test_pr_policy_defaults_auto_but_explicit_do_not_merge_sets_never(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    auto_board = dwb.start_direct_goal(thread_id="auto-pr-policy", goal="Implement and ship it")
    never_board = dwb.start_direct_goal(
        thread_id="never-pr-policy",
        goal="Implement this, open a PR at the end, but DO NOT merge it.",
    )

    auto_meta = kanban_db.read_board_metadata(auto_board.slug)["discord_worker"]
    never_meta = kanban_db.read_board_metadata(never_board.slug)["discord_worker"]
    assert auto_meta["pr_open_policy"] == "after_review_approval"
    assert auto_meta["merge_policy"] == "auto"
    assert never_meta["pr_open_policy"] == "after_review_approval"
    assert never_meta["merge_policy"] == "never"


def test_pr_policy_explicit_local_only_disables_pr_lifecycle(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="local-only-policy",
        goal=(
            "Dev work stops at a local verified branch state; it does not open "
            "pull requests, push remote branches, wait on remote checks, or merge."
        ),
    )

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_open_policy"] == "never"
    assert meta["merge_policy"] == "never"


def test_ensure_pr_honors_local_only_criteria_over_stale_pr_metadata(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="local-only-stale-metadata",
        goal="Implement and verify locally.",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    dwb._update_worker_meta(
        board.slug,
        {
            "criteria": [
                {
                    "text": (
                        "Dev work stops at a local verified branch state; it does not open "
                        "pull requests, push remote branches, wait on remote checks, or merge."
                    ),
                    "active": True,
                }
            ],
            "pr_open_policy": "after_review_approval",
            "merge_policy": "auto",
        },
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_open_policy"] == "never"
    assert meta["merge_policy"] == "never"
    assert meta["pr_state"] == "not_needed"
    assert meta["pr_checks_status"] == "passed"
    assert meta["pr_blocker"] == ""
    assert meta["pr_merge_skipped_reason"] == "pr_open_policy_never"
    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)
    assert not any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls)


def test_ensure_pr_never_policy_opens_without_merging(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="open-only-pr",
        goal="Open a PR at the end but DO NOT merge it.",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/321\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(
                    number=321,
                    state="OPEN",
                    merge_state="UNSTABLE",
                    checks=[{"name": "ci", "status": "IN_PROGRESS", "conclusion": ""}],
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert ["git", "push", "-u", "origin", "discord/open-only-pr"] in calls
    assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)
    assert meta["merge_policy"] == "never"
    assert meta["pr_state"] == "OPEN"
    assert meta["pr_checks_status"] == "pending"
    assert meta["pr_merge_skipped"] is True
    assert meta["pr_merge_skipped_reason"] == "never"
    assert meta["pr_blocker"] == ""
    assert "canonical_sync_state" not in meta


def test_ensure_pr_open_uses_sanitized_github_env_for_push_and_gh(
    monkeypatch, tmp_path
):
    from hermes_cli import github_remote
    from hermes_cli import kanban_codex_worker as worker

    isolated_home = tmp_path / "hermes-home" / "home"
    real_gh_config = tmp_path / "real-home" / ".config" / "gh"
    real_gh_config.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
    monkeypatch.setattr(
        github_remote,
        "get_github_cli_config_dir",
        lambda env: str(real_gh_config) if env.get("HOME") == str(isolated_home) else "",
    )
    calls: list[tuple[list[str], dict | None]] = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs.get("env")))
        if cmd == ["git", "remote", "-v"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "push"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(
                returncode=0,
                stdout="https://github.com/sligo-labs/PID/pull/321\n",
                stderr="",
            )
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(number=321, state="OPEN"),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr_open(
        {"summary": "Opened finalizer PR."},
        root=tmp_path,
        repo="sligo-labs/PID",
        branch="feature/pr-env",
        base="main",
        board="discord-pr-env",
    )

    env_by_cmd = {tuple(cmd[:3]): env for cmd, env in calls if env}
    for key in (("git", "push", "-u"), ("gh", "pr", "create")):
        env = env_by_cmd[key]
        assert env["GH_CONFIG_DIR"] == str(real_gh_config)
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env


def test_ensure_pr_open_blocks_pr_amend_when_origin_is_upstream_repo(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "-v"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "origin\tgit@github.com:reserve-protocol/reserve-index-dtf.git (fetch)\n"
                    "origin\tgit@github.com:reserve-protocol/reserve-index-dtf.git (push)\n"
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    data = {
        "project_context": {
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
            }
        }
    }

    assert not worker._ensure_pr_open(
        data,
        root=tmp_path,
        repo="sligo-droid/reserve-index-dtf",
        branch="discord/pr-amend-target",
        base="feat/irrevocable-fee-recipients",
        board="discord-pr-amend-target",
    )

    assert data["pr_error"] == (
        "PR-amend checkout origin mismatch: origin repo reserve-protocol/reserve-index-dtf is not finalizer target repo "
        "sligo-droid/reserve-index-dtf. Upstream/base repo reserve-protocol/reserve-index-dtf is source/review "
        "context only; target repo must be PR head repo sligo-droid/reserve-index-dtf. Fix checkout origin before finalization."
    )
    assert not any(cmd[:3] == ["git", "push", "-u"] for cmd in calls)
    assert not any(cmd[:2] == ["gh", "pr"] for cmd in calls)


def test_ensure_pr_open_blocks_pr_amend_when_origin_fetch_is_head_but_push_is_upstream(
    monkeypatch, tmp_path
):
    from hermes_cli import kanban_codex_worker as worker

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "-v"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "origin\tgit@github.com:sligo-droid/reserve-index-dtf.git (fetch)\n"
                    "origin\tgit@github.com:reserve-protocol/reserve-index-dtf.git (push)\n"
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    data = {
        "project_context": {
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
            }
        }
    }

    assert not worker._ensure_pr_open(
        data,
        root=tmp_path,
        repo="sligo-droid/reserve-index-dtf",
        branch="discord/pr-amend-target",
        base="feat/irrevocable-fee-recipients",
        board="discord-pr-amend-target",
    )

    assert (
        "origin repo reserve-protocol/reserve-index-dtf is not finalizer target repo sligo-droid/reserve-index-dtf"
        in data["pr_error"]
    )
    assert not any(cmd[:3] == ["git", "push", "-u"] for cmd in calls)
    assert not any(cmd[:2] == ["gh", "pr"] for cmd in calls)


def test_ensure_pr_open_allows_pr_amend_when_origin_is_head_repo(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "-v"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "origin\tgit@github.com:sligo-droid/reserve-index-dtf.git (fetch)\n"
                    "origin\tgit@github.com:sligo-droid/reserve-index-dtf.git (push)\n"
                ),
                stderr="",
            )
        if cmd[:3] == ["git", "push", "-u"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-droid/reserve-index-dtf/pull/7\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(
                    number=7,
                    repo="sligo-droid/reserve-index-dtf",
                    state="OPEN",
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    data = {
        "summary": "Opened finalizer PR.",
        "project_context": {
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
            }
        },
    }

    assert worker._ensure_pr_open(
        data,
        root=tmp_path,
        repo="sligo-droid/reserve-index-dtf",
        branch="discord/pr-amend-target",
        base="feat/irrevocable-fee-recipients",
        board="discord-pr-amend-target",
    )

    assert ["git", "push", "-u", "origin", "discord/pr-amend-target"] in calls
    pr_create = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "create"])
    assert pr_create[pr_create.index("--repo") + 1] == "sligo-droid/reserve-index-dtf"
    assert pr_create[pr_create.index("--base") + 1] == "feat/irrevocable-fee-recipients"


def test_ensure_pr_uses_explicit_repo_base_and_head_from_project_context(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="explicit-pr",
        goal="Ship explicit PR context",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git", "base_branch": "develop"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    _bind_reviewer_head(dwb, board.slug)
    calls = []
    view_states = ["OPEN", "MERGED"]

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/123\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            state = view_states.pop(0)
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=123, state=state), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED

    pr_list = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "list"])
    pr_create = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "create"])
    assert pr_list[pr_list.index("--repo") + 1] == "sligo-labs/PID"
    assert pr_list[pr_list.index("--base") + 1] == "develop"
    assert pr_list[pr_list.index("--head") + 1] == "discord/explicit-pr"
    assert pr_create[pr_create.index("--repo") + 1] == "sligo-labs/PID"
    assert pr_create[pr_create.index("--base") + 1] == "develop"
    assert pr_create[pr_create.index("--head") + 1] == "discord/explicit-pr"
    pr_merge = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "merge"])
    assert pr_merge[pr_merge.index("--repo") + 1] == "sligo-labs/PID"
    assert pr_merge[pr_merge.index("--match-head-commit") + 1] == _TRUSTED_PR_HEAD
    assert ["git", "push", "-u", "origin", "discord/explicit-pr"] in calls
    remote_ref = "refs/remotes/origin/develop"
    sync_commands = [
        ["git", "status", "--porcelain"],
        [
            "git",
            "fetch",
            "origin",
            "--prune",
            f"+refs/heads/develop:{remote_ref}",
        ],
        ["git", "rev-parse", "--verify", remote_ref],
        ["git", "cat-file", "-e", "abc123^{commit}"],
        ["git", "merge-base", "--is-ancestor", "abc123", remote_ref],
        ["git", "checkout", "develop"],
        ["git", "merge", "--ff-only", remote_ref],
        ["git", "rev-parse", "HEAD"],
        ["git", "merge-base", "--is-ancestor", "abc123", "HEAD"],
    ]
    assert [cmd for cmd in calls if cmd in sync_commands] == sync_commands
    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_url"] == "https://github.com/sligo-labs/PID/pull/123"
    assert meta["pr_state"] == "MERGED"
    assert meta["pr_merge_commit"] == "abc123"
    assert meta["canonical_sync_state"] == "synced"
    assert meta["canonical_sync_error"] == ""
    assert meta["canonical_sync_path"] == str(project_path)
    assert meta["canonical_sync_branch"] == "develop"
    assert meta["canonical_sync_head"] == "def456"
    assert meta["canonical_sync_merge_commit"] == "abc123"
    assert meta["canonical_synced_at"]


def test_ensure_pr_syncs_existing_worktree_when_base_branch_already_checked_out(
    monkeypatch, tmp_path
):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="existing-worktree-pr",
        goal="Ship explicit PR context",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git", "base_branch": "develop"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    existing_path = tmp_path / "develop-worktree"
    workspace.mkdir()
    project_path.mkdir()
    existing_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    _bind_reviewer_head(dwb, board.slug)
    calls = []
    view_states = ["OPEN", "MERGED"]

    def fake_run(cmd, **kwargs):
        cwd = Path(kwargs.get("cwd") or project_path)
        calls.append((cmd, cwd))
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/123\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            state = view_states.pop(0)
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=123, state=state), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == ["git", "status", "--porcelain"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:4] == ["git", "fetch", "origin", "--prune"]:
            assert cwd == project_path
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:4] == ["git", "rev-parse", "--verify"]:
            assert cwd == project_path
            return SimpleNamespace(returncode=0, stdout="remotehead\n", stderr="")
        if cmd[:3] == ["git", "cat-file", "-e"]:
            assert cwd == project_path
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == [
            "git",
            "merge-base",
            "--is-ancestor",
            "abc123",
            "refs/remotes/origin/develop",
        ]:
            assert cwd == project_path
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == ["git", "checkout", "develop"]:
            return SimpleNamespace(
                returncode=128,
                stdout="",
                stderr=f"fatal: 'develop' is already used by worktree at '{existing_path}'",
            )
        if cmd == ["git", "worktree", "list", "--porcelain"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"worktree {project_path}\n"
                    "HEAD aaaaaa\n"
                    "branch refs/heads/main\n\n"
                    f"worktree {existing_path}\n"
                    "HEAD existinghead\n"
                    "branch refs/heads/develop\n"
                ),
                stderr="",
            )
        if cmd == ["git", "merge", "--ff-only", "refs/remotes/origin/develop"]:
            assert cwd == existing_path
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd == ["git", "rev-parse", "HEAD"]:
            assert cwd == existing_path
            return SimpleNamespace(returncode=0, stdout="existinghead\n", stderr="")
        if cmd == ["git", "merge-base", "--is-ancestor", "abc123", "HEAD"]:
            assert cwd == existing_path
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert (["git", "checkout", "develop"], project_path) in calls
    assert (
        ["git", "merge", "--ff-only", "refs/remotes/origin/develop"],
        existing_path,
    ) in calls
    assert not any(cmd[:2] == ["git", "pull"] for cmd, _cwd in calls)
    assert meta["pr_state"] == "MERGED"
    assert meta["pr_merge_commit"] == "abc123"
    assert meta["canonical_sync_state"] == "synced_existing_worktree"
    assert meta["canonical_sync_error"] == ""
    assert meta["canonical_sync_path"] == str(existing_path)
    assert meta["canonical_sync_branch"] == "develop"
    assert meta["canonical_sync_head"] == "existinghead"
    assert meta["canonical_sync_merge_commit"] == "abc123"
    assert meta["canonical_synced_at"]


def test_ensure_pr_create_uses_concise_title_and_body(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    giant_goal = (
        "Goal: implement concise PR copy.\n"
        "Acceptance criteria:\n- preserve this long list in Kanban only\n"
        "Rollback plan:\n- keep this section out of GitHub copy\n"
        + "full task body details " * 80
    )
    title, body = worker._build_worker_pr_copy(
        {
            "public_url": "https://dashboard.example.test/boards/concise-pr",
            "root_goal": giant_goal,
            "summary": "Implemented short PR copy from worker metadata.",
            "changed_files": ["agent/prompt_builder.py", "hermes_cli/kanban_codex_worker.py"],
            "tests": [{"command": "scripts/run_tests.sh tests/tools/test_kanban_tools.py", "result": "passed"}],
        },
        board="discord-concise-pr",
    )
    assert "\n" not in title
    assert len(title) <= 80
    assert title == "Discord worker: implement concise PR copy."
    assert "Acceptance criteria" not in title
    assert "Rollback" not in title
    assert body.startswith("Board: https://dashboard.example.test/boards/concise-pr\n\n")
    assert "## Summary\n" in body
    assert "## Verification\n" in body
    assert "scripts/run_tests.sh tests/tools/test_kanban_tools.py passed" in body
    assert "Acceptance criteria" not in body
    assert "Risk/rollback" not in body
    assert "full task body details" not in body


def test_ensure_pr_syncs_already_merged_pr(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="already-merged-pr",
        goal="Ship already merged PR",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(
        board.slug,
        {"project_path": str(project_path), "pr_url": "https://github.com/sligo-labs/PID/pull/123"},
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=123, state="MERGED"), stderr="")
        sync_result = _canonical_sync_result(cmd, head="fedcba")
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED

    assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)
    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)
    assert ["git", "status", "--porcelain"] in calls
    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_state"] == "MERGED"
    assert meta["canonical_sync_state"] == "synced"
    assert meta["canonical_sync_head"] == "fedcba"


@pytest.mark.parametrize(
    ("case", "project_exists", "sync_kwargs", "expected"),
    [
        ("missing", False, {}, "Canonical checkout missing or invalid"),
        ("dirty", True, {"dirty": True}, "Canonical checkout is dirty"),
        ("merge", True, {"pull_failed": True}, "Canonical checkout fast-forward merge failed"),
        (
            "ancestor",
            True,
            {"ancestor_failed": True},
            "Canonical checkout remote ref does not contain PR merge commit",
        ),
    ],
)
def test_ensure_pr_blocks_terminal_completion_when_canonical_sync_fails_after_merge(
    monkeypatch, tmp_path, case, project_exists, sync_kwargs, expected
):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id=f"sync-blocked-{case}",
        goal="Ship sync blocked PR",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    if project_exists:
        project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    _bind_reviewer_head(dwb, board.slug)
    calls = []
    view_states = ["OPEN", "MERGED"]

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/123\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=123, state=view_states.pop(0)), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        sync_result = _canonical_sync_result(cmd, **sync_kwargs)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.FAILED

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_state"] == "MERGED"
    assert meta["canonical_sync_state"] == "blocked"
    assert expected in meta["canonical_sync_error"]
    assert expected in meta["pr_error"]
    assert expected in meta["pr_blocker"]
    if not project_exists:
        assert ["git", "status", "--porcelain"] not in calls


def test_ensure_pr_records_merge_checks_and_blocker(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="blocked-pr",
        goal="Ship PR blocker facts",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/125\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(
                    number=125,
                    state="OPEN",
                    merge_state="BLOCKED",
                    mergeable="CONFLICTING",
                    checks=[
                        {
                            "name": "basic",
                            "workflowName": "Basic Tests",
                            "status": "COMPLETED",
                            "conclusion": "FAILURE",
                        },
                        {
                            "name": "pr body",
                            "workflowName": "PR Body Format",
                            "status": "COMPLETED",
                            "conclusion": "SUCCESS",
                        },
                    ],
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.FAILED

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_url"] == "https://github.com/sligo-labs/PID/pull/125"
    assert meta["pr_number"] == "125"
    assert meta["pr_state"] == "OPEN"
    assert meta["pr_merge_state"] == "BLOCKED"
    assert meta["pr_mergeable"] == "CONFLICTING"
    assert meta["pr_checks_status"] == "failed"
    assert meta["pr_checks_total"] == 2
    assert meta["pr_checks_failed"] == ["Basic Tests / basic"]
    assert meta["pr_blocker"] == "checks failed: Basic Tests / basic"
    assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)


def test_legacy_merge_rejects_reviewer_approval_for_stale_pr_head(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    approved_head = "a" * 40
    current_head = "b" * 40
    calls: list[list[str]] = []

    def fake_gh(args, **_kwargs):
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(
                    number=126,
                    state="OPEN",
                    merge_state="CLEAN",
                    head_sha=current_head,
                ),
                stderr="",
            )
        if args[:1] == ["api"] and "/check-runs?" in args[1]:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "check_runs": [
                            {
                                "id": index,
                                "name": check,
                                "head_sha": current_head,
                                "status": "COMPLETED",
                                "conclusion": "SUCCESS",
                                "completed_at": f"2026-07-18T00:00:0{index}Z",
                                "details_url": _required_check_details_url(
                                    "sligo-labs/PID",
                                    workflow,
                                    check,
                                ),
                                "app": {"slug": "github-actions"},
                            }
                            for index, (workflow, check) in enumerate(
                                _REQUIRED_CHECK_RUNS,
                                start=1,
                            )
                        ]
                    }
                ),
                stderr="",
            )
        if args[:1] == ["api"] and "/actions/runs/" in args[1]:
            run_id = args[1].rsplit("/", 1)[-1]
            path = next(
                path
                for expected_run_id, path in _REQUIRED_CHECK_RUNS.values()
                if run_id == expected_run_id
            )
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"path": path, "head_sha": current_head}),
                stderr="",
            )
        raise AssertionError(args)

    monkeypatch.setattr(worker, "_run_gh", fake_gh)
    state = {
        "pr_url": "https://github.com/sligo-labs/PID/pull/126",
        "review_approved_head": approved_head,
    }

    outcome = worker._ensure_pr_merged(
        state,
        root=tmp_path,
        repo="sligo-labs/PID",
    )

    assert outcome == worker.PRFinalizationOutcome.FAILED
    assert "missing or stale" in state["pr_blocker"]
    assert not any(args[:2] == ["pr", "merge"] for args in calls)


def test_ensure_pr_waits_for_checks_before_merging(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="pending-pr",
        goal="Ship pending PR",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/126\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(
                    number=126,
                    state="OPEN",
                    merge_state="UNSTABLE",
                    checks=[
                        {
                            "name": "basic",
                            "workflowName": "Basic Tests",
                            "status": "IN_PROGRESS",
                            "conclusion": "",
                        },
                        {
                            "name": "pr body",
                            "workflowName": "PR Body Format",
                            "status": "QUEUED",
                            "conclusion": "",
                        },
                    ],
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    monkeypatch.setattr(worker.time, "sleep", lambda *_args: pytest.fail("CI polling must not sleep"))

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.WAITING_FOR_CI

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_state"] == "OPEN"
    assert meta["pr_checks_status"] == "pending"
    assert meta["pr_blocker"] == ""
    assert meta["pr_ci_wait_state"] == "running"
    assert meta["pr_ci_next_poll_at"] - meta["pr_ci_wait_started_at"] >= 10
    assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)

    views_before_next_tick = len([cmd for cmd in calls if cmd[:3] == ["gh", "pr", "view"]])
    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.WAITING_FOR_CI
    assert len([cmd for cmd in calls if cmd[:3] == ["gh", "pr", "view"]]) == views_before_next_tick


def test_ensure_pr_waits_when_stable_checks_are_missing(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="no-check-pr",
        goal="Ship PR without checks",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []
    view_states = iter(
        [
            _pr_view_json(number=129, state="OPEN", merge_state="CLEAN", checks=[]),
        ]
    )

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/129\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=next(view_states), stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.WAITING_FOR_CI

    assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)
    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_state"] == "OPEN"
    assert meta["pr_checks_status"] == "pending"
    assert meta["pr_checks_total"] == 0
    assert meta["pr_blocker"] == ""
    assert meta["pr_ci_wait_state"] == "queued"


def test_ensure_pr_merges_after_pending_gate_checks_pass(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker

    board = dwb.start_direct_goal(
        thread_id="pending-then-passed-pr",
        goal="Ship CI-gated PR",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    _bind_reviewer_head(dwb, board.slug)
    calls: list[list[str]] = []
    views = iter(
        [
            _pr_view_json(
                number=130,
                state="OPEN",
                merge_state="UNSTABLE",
                checks=[
                    {
                        "name": "basic",
                        "workflowName": "Basic Tests",
                        "status": "IN_PROGRESS",
                        "conclusion": "",
                    },
                    {
                        "name": "pr body",
                        "workflowName": "PR Body Format",
                        "status": "QUEUED",
                        "conclusion": "",
                    },
                ],
            ),
            _pr_view_json(number=130, state="OPEN", merge_state="CLEAN"),
            _pr_view_json(number=130, state="MERGED", merge_state="CLEAN"),
        ]
    )

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/130\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=next(views), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    monkeypatch.setattr(worker.time, "sleep", lambda *_args: pytest.fail("CI polling must not sleep"))

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.WAITING_FOR_CI
    dwb._update_worker_meta(board.slug, {"pr_ci_next_poll_at": 0})
    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED
    assert any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)



def test_ensure_pr_polls_passed_unstable_nonblockingly_before_merging(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="unstable-pr",
        goal="Ship unstable PR",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    _bind_reviewer_head(dwb, board.slug)
    calls = []
    view_states = iter(
        [
            _pr_view_json(number=127, state="OPEN", merge_state="UNSTABLE"),
            _pr_view_json(number=127, state="OPEN", merge_state="CLEAN"),
            _pr_view_json(number=127, state="MERGED", merge_state="CLEAN"),
        ]
    )

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/127\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=next(view_states), stderr="")
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.WAITING_FOR_CI
    assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)

    dwb._update_worker_meta(board.slug, {"pr_ci_next_poll_at": 0})
    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED

    merge_index = next(index for index, cmd in enumerate(calls) if cmd[:3] == ["gh", "pr", "merge"])
    view_indices = [index for index, cmd in enumerate(calls) if cmd[:3] == ["gh", "pr", "view"]]
    clean_view_index = view_indices[1]
    assert clean_view_index < merge_index
    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_state"] == "MERGED"
    assert meta["pr_merge_state"] == "CLEAN"
    assert meta["pr_checks_status"] == "passed"
    assert meta["pr_blocker"] == ""


def test_ensure_pr_falls_back_to_origin_remote_for_repo(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker

    board = dwb.start_direct_goal(thread_id="remote-pr", goal="Ship remote fallback")
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=0, stdout="git@github.com:sligo-labs/PID.git\n", stderr="")
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/124\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=124), stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED

    pr_create = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "create"])
    assert pr_create[pr_create.index("--repo") + 1] == "sligo-labs/PID"
    assert pr_create[pr_create.index("--base") + 1] == "main"
    assert pr_create[pr_create.index("--head") + 1] == "discord/remote-pr"


def test_ensure_pr_blocks_local_only_remote_before_pr_creation(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="local-remote-pr",
        goal="Ship remote preflight",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=0, stdout="/home/droid/hermes\n", stderr="")
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return SimpleNamespace(returncode=0, stdout="1\n", stderr="")
        if cmd == ["git", "remote", "-v"]:
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "origin\t/home/droid/hermes (fetch)\n"
                    "origin\t/home/droid/hermes (push)\n"
                ),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.FAILED

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert "only local/file remotes" in meta["pr_error"]
    assert "/home/droid/hermes" in meta["pr_error"]
    assert "not a GitHub token/auth problem" in meta["pr_error"]
    assert "git remote set-url origin" in meta["pr_error"]
    assert meta["pr_blocker"] == meta["pr_error"]
    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)
    assert not any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls)


def test_ensure_pr_prefers_checkout_remote_over_stale_project_context(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker

    board = dwb.start_direct_goal(
        thread_id="stale-context-pr",
        goal="Ship remote override",
        project_context={"project_github_url": "https://github.com/sligo-droid/PID"},
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=0, stdout="git@github.com:sligo-labs/PID.git\n", stderr="")
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-labs/PID/pull/124\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(returncode=0, stdout=_pr_view_json(number=124), stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED

    pr_list = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "list"])
    pr_create = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "create"])
    assert pr_list[pr_list.index("--repo") + 1] == "sligo-labs/PID"
    assert pr_create[pr_create.index("--repo") + 1] == "sligo-labs/PID"


def test_ensure_pr_explicit_target_repo_overrides_checkout_remote(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker

    board = dwb.start_direct_goal(
        thread_id="pr-amend-target",
        goal="Amend upstream PR through fork branch",
        project_context={
            "github_pr_target_repo": "sligo-droid/reserve-index-dtf",
            "base_branch": "feat/irrevocable-fee-recipients",
            "project_github_url": "https://github.com/reserve-protocol/reserve-index-dtf",
        },
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=0, stdout="git@github.com:reserve-protocol/reserve-index-dtf.git\n", stderr="")
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-droid/reserve-index-dtf/pull/7\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(
                    number=7,
                    repo="sligo-droid/reserve-index-dtf",
                ),
                stderr="",
            )
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED

    pr_create = next(cmd for cmd in calls if cmd[:3] == ["gh", "pr", "create"])
    assert pr_create[pr_create.index("--repo") + 1] == "sligo-droid/reserve-index-dtf"
    assert pr_create[pr_create.index("--base") + 1] == "feat/irrevocable-fee-recipients"
    assert pr_create[pr_create.index("--head") + 1] == "discord/pr-amend-target"


def test_ensure_pr_amend_uses_head_repo_and_never_upstream_for_pr_commands(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker

    board = dwb.start_direct_goal(
        thread_id="pr-amend-finalizer-target",
        goal="Amend upstream PR through fork branch",
        project_context={
            "github_pr_target_repo": "sligo-droid/reserve-index-dtf",
            "base_branch": "feat/irrevocable-fee-recipients",
            "project_github_url": "https://github.com/reserve-protocol/reserve-index-dtf",
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "upstream_pr_number": "182",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
                "head_sha": "a" * 40,
                "source_kind": "issue_comment",
            },
        },
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=0, stdout="git@github.com:reserve-protocol/reserve-index-dtf.git\n", stderr="")
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-droid/reserve-index-dtf/pull/7\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(
                    number=7,
                    repo="sligo-droid/reserve-index-dtf",
                ),
                stderr="",
            )
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED

    gh_pr_calls = [cmd for cmd in calls if cmd[:2] == ["gh", "pr"]]
    assert gh_pr_calls
    assert all(cmd[cmd.index("--repo") + 1] == "sligo-droid/reserve-index-dtf" for cmd in gh_pr_calls if "--repo" in cmd)
    assert all("reserve-protocol/reserve-index-dtf" not in cmd for cmd in gh_pr_calls)
    pr_create = next(cmd for cmd in gh_pr_calls if cmd[:3] == ["gh", "pr", "create"])
    assert pr_create[pr_create.index("--base") + 1] == "feat/irrevocable-fee-recipients"


@pytest.mark.parametrize("invalid_sha", ["a" * 7, "a" * 41, "a" * 63])
def test_pr_amend_rejects_non_exact_trigger_sha_without_querying_github(
    monkeypatch,
    tmp_path,
    invalid_sha,
):
    from hermes_cli import kanban_codex_worker as worker

    calls = []
    monkeypatch.setattr(worker, "_run_gh", lambda *args, **kwargs: calls.append(args))
    state = {
        "project_context": {
            "github_pr_amend": {
                "upstream_repo": "acme/upstream",
                "upstream_pr_number": "7",
                "head_sha": invalid_sha,
                "requires_head_sha_advance": True,
            }
        }
    }

    assert worker._verify_pr_amend_head_advanced(state, root=tmp_path) is False
    assert calls == []
    assert "missing exact upstream PR head SHA" in state["pr_blocker"]


def test_pr_amend_rejects_invalid_refreshed_head_sha(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_worker as worker

    monkeypatch.setattr(
        worker,
        "_run_gh",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=("b" * 63) + "\n",
            stderr="",
        ),
    )
    state = {
        "project_context": {
            "github_pr_amend": {
                "upstream_repo": "acme/upstream",
                "upstream_pr_number": "7",
                "head_sha": "a" * 40,
                "requires_head_sha_advance": True,
            }
        }
    }

    assert worker._verify_pr_amend_head_advanced(state, root=tmp_path) is False
    assert state["pr_amend_head_advanced"] is False
    assert state["pr_blocker"] == (
        "PR-amend completion blocked: upstream PR returned an invalid head SHA."
    )


def test_ensure_pr_amend_blocks_when_upstream_head_sha_does_not_advance(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="pr-amend-unchanged-head",
        goal="Amend upstream PR through fork branch",
        project_context={
            "github_pr_target_repo": "sligo-droid/reserve-index-dtf",
            "base_branch": "feat/irrevocable-fee-recipients",
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "upstream_pr_number": "182",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
                "head_sha": "a" * 40,
                "source_kind": "review",
                "review_state": "CHANGES_REQUESTED",
                "requires_head_sha_advance": True,
            },
        },
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-droid/reserve-index-dtf/pull/7\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"] and "headRefOid" in cmd:
            return SimpleNamespace(returncode=0, stdout=("a" * 40) + "\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            raise AssertionError("PR lifecycle must not run before amendment-head advancement")
        if cmd[:3] == ["gh", "pr", "merge"]:
            raise AssertionError("merge must not run before amendment-head advancement")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    monkeypatch.setattr(
        worker,
        "_kanban_closeout_config",
        lambda: {"mode": "enforce", "surfaces": {"kanban": True}},
    )
    monkeypatch.setattr(
        worker,
        "_reconcile_kanban_closeout",
        lambda *_a, **_k: pytest.fail(
            "merge-capable shared closeout must not run before amendment-head advancement"
        ),
    )

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.FAILED

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert not any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)
    assert str(meta.get("pr_state") or "").upper() != "MERGED"
    assert meta["pr_amend_head_advanced"] is False
    assert meta["pr_amend_upstream_head_sha"] == "a" * 40
    assert meta["pr_amend_trigger_head_sha"] == "a" * 40
    assert meta["pr_blocker"] == "PR-amend completion blocked: upstream PR head SHA did not advance from triggering review commit."


def test_ensure_pr_amend_succeeds_when_upstream_head_sha_advances(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="pr-amend-advanced-head",
        goal="Amend upstream PR through fork branch",
        project_context={
            "github_pr_target_repo": "sligo-droid/reserve-index-dtf",
            "base_branch": "feat/irrevocable-fee-recipients",
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "upstream_pr_number": "182",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
                "head_sha": "a" * 40,
                "source_kind": "review",
                "review_state": "CHANGES_REQUESTED",
                "requires_head_sha_advance": True,
            },
        },
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(board.slug, {"project_path": str(project_path)})
    _bind_reviewer_head(dwb, board.slug)
    view_states = ["OPEN", "MERGED"]

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-droid/reserve-index-dtf/pull/7\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"] and "headRefOid" in cmd:
            return SimpleNamespace(returncode=0, stdout=("b" * 40) + "\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(
                    number=7,
                    repo="sligo-droid/reserve-index-dtf",
                    state=view_states.pop(0),
                ),
                stderr="",
            )
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_state"] == "MERGED"
    assert meta["pr_amend_head_advanced"] is True
    assert meta["pr_amend_upstream_head_sha"] == "b" * 40
    assert meta["pr_blocker"] == ""


def test_ensure_pr_amend_finalizer_ignores_dev_worker_no_pr_text(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="pr-amend-dev-text",
        goal="Amend upstream PR through fork branch",
        project_context={
            "github_pr_target_repo": "sligo-droid/reserve-index-dtf",
            "base_branch": "feat/irrevocable-fee-recipients",
            "github_pr_amend": {
                "upstream_repo": "reserve-protocol/reserve-index-dtf",
                "upstream_pr_number": "182",
                "head_repo": "sligo-droid/reserve-index-dtf",
                "head_ref": "feat/irrevocable-fee-recipients",
                "head_sha": "a" * 40,
                "requires_head_sha_advance": True,
            },
        },
    )
    workspace = tmp_path / "repo"
    project_path = tmp_path / "canonical"
    workspace.mkdir()
    project_path.mkdir()
    dwb._update_worker_meta(
        board.slug,
        {
            "project_path": str(project_path),
            "latest_planner_request": "Dev workers do not open PRs/push/merge. Do not merge the upstream PR.",
        },
    )
    _bind_reviewer_head(dwb, board.slug)
    view_states = ["OPEN", "MERGED"]
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["gh", "pr", "create"]:
            return SimpleNamespace(returncode=0, stdout="https://github.com/sligo-droid/reserve-index-dtf/pull/7\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"] and "headRefOid" in cmd:
            return SimpleNamespace(returncode=0, stdout=("b" * 40) + "\n", stderr="")
        if cmd[:3] == ["gh", "pr", "view"]:
            return SimpleNamespace(
                returncode=0,
                stdout=_pr_view_json(
                    number=7,
                    repo="sligo-droid/reserve-index-dtf",
                    state=view_states.pop(0),
                ),
                stderr="",
            )
        if cmd[:3] == ["gh", "pr", "merge"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        sync_result = _canonical_sync_result(cmd)
        if sync_result is not None:
            return sync_result
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_open_policy"] == dwb.PR_OPEN_POLICY_AFTER_REVIEW_APPROVAL
    assert meta["merge_policy"] == dwb.MERGE_POLICY_AUTO
    assert meta.get("pr_skipped_no_changes") is not True
    assert meta["pr_amend_head_advanced"] is True
    assert any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls)
    assert any(cmd[:3] == ["gh", "pr", "merge"] for cmd in calls)


def test_ensure_pr_skips_foreman_no_change_branch(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="foreman-no-change",
        goal="Foreman escalation: resolve a Discord worker issue.",
        project_context={
            "project_github_url": "https://github.com/sligo-labs/PID.git"
        },
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="no remote")
        if cmd[:3] == ["git", "rev-list", "--count"]:
            return SimpleNamespace(returncode=0, stdout="0\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.MERGED

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_skipped_no_changes"] is True
    assert meta["pr_state"] == "not_needed"
    assert meta["pr_checks_status"] == "passed"
    assert meta["pr_blocker"] == ""
    assert not any(cmd[:3] == ["gh", "pr", "list"] for cmd in calls)
    assert not any(cmd[:2] == ["git", "push"] for cmd in calls)
    assert not any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls)


def test_ensure_pr_rejects_kanban_role_worker_context(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY

    board = dwb.start_direct_goal(
        thread_id="role-worker-pr-guard",
        goal="Finalize only from dispatcher",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_TASK", "review-task")
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("role-worker guard must run before git or gh"),
    )

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.FAILED

    meta = kanban_db.read_board_metadata(board.slug)[DISCORD_WORKER_META_KEY]
    assert meta["pr_finalizer_guard"] == "role_worker_context"
    assert meta["pr_checks_status"] == "not checked"
    assert meta["pr_merge_state"] == "not attempted"
    assert "dispatcher reconciliation must finalize" in meta["pr_error"]
    assert meta["pr_blocker"] == meta["pr_error"]


def test_ensure_pr_records_error_when_repo_or_head_missing(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import DISCORD_WORKER_META_KEY
    from utils import atomic_json_write

    board = dwb.start_direct_goal(thread_id="missing-pr", goal="Cannot resolve")
    workspace = tmp_path / "repo"
    workspace.mkdir()
    metadata = kanban_db.read_board_metadata(board.slug)
    metadata[DISCORD_WORKER_META_KEY]["worker_branch"] = ""
    metadata.pop("db_path", None)
    atomic_json_write(kanban_db.board_metadata_path(board.slug), metadata, indent=2)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ["git", "remote", "get-url", "origin"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="no remote")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.FAILED

    meta = kanban_db.read_board_metadata(board.slug)[DISCORD_WORKER_META_KEY]
    assert meta["pr_error"] == "Cannot create PR: missing GitHub repository, worker branch"
    assert meta["pr_blocker"] == meta["pr_error"]
    assert meta["pr_checks_status"] == "not checked"
    assert meta["pr_merge_state"] == "unknown"
    assert not any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls)


def test_ensure_pr_records_push_failure_before_create(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_codex_worker as worker
    from hermes_cli import kanban_db

    board = dwb.start_direct_goal(
        thread_id="push-failed",
        goal="Cannot push",
        project_context={"github_url": "https://github.com/sligo-labs/PID.git"},
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["git", "push"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="permission denied")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(worker.subprocess, "run", fake_run)

    assert worker._ensure_pr(board.slug, str(workspace)) == worker.PRFinalizationOutcome.FAILED

    meta = kanban_db.read_board_metadata(board.slug)["discord_worker"]
    assert meta["pr_error"] == "permission denied"
    assert meta["pr_blocker"] == "permission denied"
    assert not any(cmd[:3] == ["gh", "pr", "create"] for cmd in calls)
