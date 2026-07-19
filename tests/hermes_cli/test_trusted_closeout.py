from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hermes_cli import trusted_closeout as closeout


OLD_SHA = "1" * 40
HEAD_SHA = "2" * 40
MERGE_SHA = "3" * 40


def _state(tmp_path: Path, **updates):
    value = {
        "id": "closeout-1",
        "source": "fable",
        "mode": "enforce",
        "workspace": {
            "path": str(tmp_path),
            "canonical_path": str(tmp_path / "canonical"),
            "repository": "acme/example",
            "branch": "feature/test",
            "base_branch": "main",
        },
        "policy": {
            "merge": "auto",
            "pr_open": "after_review_approval",
            "require_local_verification": True,
            "require_review": True,
            "require_visual_qa": True,
            "post_merge_requirements": {},
        },
        "local_verification": {"status": "passed", "head_sha": HEAD_SHA},
        "review": {"status": "passed", "head_sha": HEAD_SHA},
        "visual_qa": {"status": "passed", "head_sha": HEAD_SHA},
        "pr": {
            "url": "https://github.com/acme/example/pull/7",
            "head_sha": HEAD_SHA,
        },
    }
    for key, item in updates.items():
        value[key] = item
    return value


def test_closeout_normalization_accepts_only_exact_sha_lengths(tmp_path):
    for invalid in ("a" * 7, "a" * 41, "a" * 63):
        state = _state(tmp_path)
        state["pr"].update(
            head_sha=invalid,
            merge_sha=invalid,
            merge_attempted_head_sha=invalid,
        )
        state["ci"] = {"head_sha": invalid}
        state["post_merge"] = {"target_sha": invalid}
        normalized = closeout.normalize_closeout_state(state)
        assert normalized["pr"]["head_sha"] == ""
        assert normalized["pr"]["merge_sha"] == ""
        assert normalized["pr"]["merge_attempted_head_sha"] == ""
        assert normalized["ci"]["head_sha"] == ""
        assert normalized["post_merge"]["target_sha"] == ""

    exact = _state(tmp_path)
    exact["pr"].update(
        head_sha="a" * 64,
        merge_sha="b" * 64,
        merge_attempted_head_sha="c" * 64,
    )
    exact["ci"] = {"head_sha": "d" * 64}
    exact["post_merge"] = {"target_sha": "e" * 64}
    normalized = closeout.normalize_closeout_state(exact)
    assert normalized["pr"]["head_sha"] == "a" * 64
    assert normalized["pr"]["merge_sha"] == "b" * 64
    assert normalized["pr"]["merge_attempted_head_sha"] == "c" * 64
    assert normalized["ci"]["head_sha"] == "d" * 64
    assert normalized["post_merge"]["target_sha"] == "e" * 64


def _check(workflow, name, *, conclusion="SUCCESS", head_sha=HEAD_SHA, run=1):
    return {
        "workflowName": workflow,
        "name": name,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "headSha": head_sha,
        "databaseId": run,
        "completedAt": f"2026-07-17T00:00:{run:02d}Z",
    }


def _pr_payload(
    *,
    head_sha=HEAD_SHA,
    draft=False,
    state="OPEN",
    merge_sha="",
    checks=None,
    merge_state="CLEAN",
    mergeable="MERGEABLE",
    review_decision="APPROVED",
):
    return {
        "number": 7,
        "url": "https://github.com/acme/example/pull/7",
        "state": state,
        "headRefOid": head_sha,
        "mergedAt": "2026-07-17T00:01:00Z" if state == "MERGED" else None,
        "mergeCommit": {"oid": merge_sha} if merge_sha else None,
        "mergeStateStatus": merge_state,
        "mergeable": mergeable,
        "isDraft": draft,
        "reviewDecision": review_decision,
        "statusCheckRollup": checks
        if checks is not None
        else [
            _check("Basic Tests", "basic"),
            _check("PR Body Format", "pr body"),
        ],
    }


def _completed(args, returncode=0, *, stdout="", stderr=""):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr=stderr)


def _patch_repo_boundary(monkeypatch):
    monkeypatch.setattr(closeout, "github_remote_preflight_error", lambda *_a, **_k: None)
    monkeypatch.setattr(closeout, "github_origin_repo", lambda *_a, **_k: "acme/example")


def test_normalize_closeout_state_is_bounded_and_additive(tmp_path):
    state = closeout.normalize_closeout_state(
        {
            "mode": "bogus",
            "workspace": {"path": str(tmp_path)},
            "policy": {"merge": "bogus", "post_merge_requirements": {"deployment": True}},
            "errors": [
                {
                    "code": "bad",
                    "message": "token=secret https://protected.invalid/page " + ("x" * 1000),
                }
            ]
            * 20,
            "unknown": "discarded",
        }
    )

    assert state["schema_version"] == 1
    assert state["mode"] == "off"
    assert state["policy"]["merge"] == "auto"
    assert state["policy"]["post_merge_requirements"]["deployment"] is True
    assert len(state["errors"]) == 8
    assert "secret" not in state["errors"][0]["message"]
    assert "protected.invalid" not in state["errors"][0]["message"]
    assert "unknown" not in state


def test_required_checks_use_current_head_and_newest_logical_rerun():
    checks = [
        _check("Basic Tests", "basic", conclusion="FAILURE", run=1),
        _check("Basic Tests", "basic", conclusion="SUCCESS", run=2),
        _check("PR Body Format", "pr body", conclusion="SUCCESS", head_sha=OLD_SHA, run=5),
        _check("PR Body Format", "pr body", conclusion="SUCCESS", head_sha=HEAD_SHA, run=4),
        _check("Unrelated", "lint", conclusion="FAILURE", run=99),
    ]

    summary = closeout.summarize_required_checks(checks, head_sha=HEAD_SHA)

    assert summary == {
        "head_sha": HEAD_SHA,
        "status": "passed",
        "total": 2,
        "failed": [],
        "wait_state": "complete",
        "required": [
            {"workflow": "Basic Tests", "check": "basic"},
            {"workflow": "PR Body Format", "check": "pr body"},
        ],
    }


def test_required_checks_classify_all_terminal_failure_conclusions():
    for conclusion in ("STALE", "STARTUP_FAILURE", "ERROR"):
        summary = closeout.summarize_required_checks(
            [
                _check("Basic Tests", "basic", conclusion=conclusion),
                _check("PR Body Format", "pr body"),
            ],
            head_sha=HEAD_SHA,
        )
        assert summary["status"] == "failed"
        assert summary["wait_state"] == "rerun_required"
        assert summary["failed"] == ["Basic Tests / basic"]


def test_head_change_atomically_invalidates_head_bound_receipts(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    state = _state(tmp_path)
    state["pr"]["head_sha"] = OLD_SHA
    state["local_verification"] = {"status": "passed", "head_sha": OLD_SHA}
    state["review"] = {"status": "passed", "head_sha": OLD_SHA}
    state["visual_qa"] = {"status": "passed", "head_sha": OLD_SHA}
    state["ci"] = {"status": "passed", "head_sha": OLD_SHA}
    state["pr"]["ready_at"] = 10
    state["pr"]["merge_attempted_head_sha"] = OLD_SHA

    def run(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload()))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    assert transition.outcome == "waiting_for_gates"
    assert transition.next_due_at == 130
    assert transition.state["pr"]["head_sha"] == HEAD_SHA
    assert transition.state["ci"]["head_sha"] == HEAD_SHA
    for key in ("local_verification", "review", "visual_qa"):
        assert transition.state[key] == {"status": "stale"}
    assert transition.state["pr"]["ready_at"] is None
    assert transition.state["pr"]["merge_attempted_head_sha"] == ""


def test_failed_current_head_ci_requires_repair_without_polling(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    failed_checks = [
        _check("Basic Tests", "basic", conclusion="FAILURE"),
        _check("PR Body Format", "pr body", conclusion="SUCCESS"),
    ]

    def run(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(
                args,
                stdout=json.dumps(_pr_payload(checks=failed_checks)),
            )
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(
        _state(tmp_path),
        now=100,
        run=run,
    )

    assert transition.outcome == "repair_required"
    assert transition.terminal is True
    assert transition.next_due_at is None
    assert transition.state["status"] == "repair_required"
    assert transition.state["errors"][-1]["code"] == "required_checks_failed"
    assert closeout.closeout_terminal_eligible(transition.state) is False


def test_shadow_ci_failure_is_diagnostic_only(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    failed_checks = [
        _check("Basic Tests", "basic", conclusion="FAILURE"),
        _check("PR Body Format", "pr body"),
    ]

    def run(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload(checks=failed_checks)))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(
        _state(tmp_path, mode="shadow"),
        now=100,
        run=run,
    )

    assert transition.outcome == "waiting_for_ci"
    assert transition.terminal is False
    assert transition.next_due_at == 130
    assert transition.state["errors"][-1]["code"] == "required_checks_failed"
    assert closeout.closeout_terminal_eligible(transition.state) is False


def test_shadow_is_read_only_and_pending_is_not_terminal(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    calls = []
    state = _state(tmp_path, mode="shadow")
    state["pr"] = {}

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "list"]:
            return _completed(args, stdout="null")
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    assert transition.outcome == "pr_pending"
    assert transition.terminal is False
    assert closeout.closeout_terminal_eligible(transition.state) is False
    assert not any(args[:2] == ["git", "push"] for args in calls)
    assert not any(args[:3] == ["gh", "pr", "create"] for args in calls)


def test_shadow_never_readies_or_merges(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload(draft=True)))
        raise AssertionError(args)

    draft = closeout.reconcile_trusted_closeout(
        _state(tmp_path, mode="shadow"),
        now=100,
        run=run,
    )

    assert draft.outcome == "ready_pending"
    assert closeout.closeout_terminal_eligible(draft.state) is False
    assert not any(args[:3] == ["gh", "pr", "ready"] for args in calls)
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)

    calls.clear()

    def run_ready(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload()))
        raise AssertionError(args)

    merge = closeout.reconcile_trusted_closeout(
        _state(tmp_path, mode="shadow"),
        now=101,
        run=run_ready,
    )

    assert merge.outcome == "pending"
    assert closeout.closeout_terminal_eligible(merge.state) is False
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


def test_draft_becomes_ready_only_after_current_head_gates(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload(draft=True)))
        if args[:3] == ["gh", "pr", "ready"]:
            return _completed(args)
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(_state(tmp_path), now=100, run=run)

    assert transition.outcome == "ready_pending"
    assert transition.next_due_at == 100
    assert transition.wake_immediately is True
    assert transition.state["pr"]["is_draft"] is False
    assert transition.state["pr"]["ready_at"] == 100
    assert sum(args[:3] == ["gh", "pr", "ready"] for args in calls) == 1
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


def test_merge_is_one_pass_then_exact_sha_sync_on_refresh(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    calls = []

    def run_open(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload()))
        if args[:3] == ["gh", "pr", "merge"]:
            return _completed(args)
        raise AssertionError(args)

    first = closeout.reconcile_trusted_closeout(_state(tmp_path), now=100, run=run_open)

    assert first.outcome == "pending"
    assert first.next_due_at == 100
    assert first.state["pr"]["merge_attempted_head_sha"] == HEAD_SHA
    assert first.state["telemetry"]["green_unmerged_since"] == 100
    merge_calls = [args for args in calls if args[:3] == ["gh", "pr", "merge"]]
    assert len(merge_calls) == 1
    assert merge_calls[0][-2:] == ["--match-head-commit", HEAD_SHA]
    assert sum(args[:3] == ["gh", "pr", "view"] for args in calls) == 2

    sync_calls = []

    def run_merged(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(
                args,
                stdout=json.dumps(_pr_payload(state="MERGED", merge_sha=MERGE_SHA)),
            )
        raise AssertionError(args)

    def sync(path, branch, sha):
        sync_calls.append((path, branch, sha))
        return {"state": "synced", "head": sha, "merge_commit": sha}

    second = closeout.reconcile_trusted_closeout(
        first.state,
        now=101,
        run=run_merged,
        sync_canonical=sync,
    )

    assert second.outcome == "post_merge_pending"
    assert second.state["post_merge"]["target_sha"] == MERGE_SHA
    assert second.state["canonical_sync"] == {"status": "pending"}
    assert second.state["telemetry"]["green_unmerged_since"] is None
    assert second.state["telemetry"]["green_unmerged_overdue"] is False
    assert sync_calls == []

    third = closeout.reconcile_trusted_closeout(
        second.state,
        now=102,
        run=run_merged,
        sync_canonical=sync,
    )

    assert third.outcome == "post_merge_complete"
    assert third.terminal is True
    assert third.state["canonical_sync"] == {
        "status": "passed",
        "observed_sha": MERGE_SHA,
        "checked_at": 102,
    }
    assert sync_calls == [(str(tmp_path / "canonical"), "main", MERGE_SHA)]


def test_premerge_refresh_accepts_concurrent_external_merge(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    calls = []
    views = iter(
        [
            _pr_payload(),
            _pr_payload(state="MERGED", merge_sha=MERGE_SHA),
        ]
    )

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(next(views)))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(_state(tmp_path), now=100, run=run)

    assert transition.outcome == "post_merge_pending"
    assert transition.terminal is False
    assert transition.wake_immediately is True
    assert transition.state["pr"]["state"] == "MERGED"
    assert transition.state["pr"]["merge_sha"] == MERGE_SHA
    assert transition.state["post_merge"]["target_sha"] == MERGE_SHA
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


def test_premerge_head_change_invalidates_gates_and_prevents_merge(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    calls = []
    new_sha = "4" * 40
    views = iter(
        [
            _pr_payload(),
            _pr_payload(head_sha=new_sha),
        ]
    )

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(next(views)))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(_state(tmp_path), now=100, run=run)

    assert transition.outcome == "waiting_for_gates"
    assert transition.state["pr"]["head_sha"] == new_sha
    for key in ("local_verification", "review", "visual_qa"):
        assert transition.state[key] == {"status": "stale"}
    assert transition.state["pr"]["merge_attempted_head_sha"] == ""
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


@pytest.mark.parametrize(
    ("change", "expected_outcome"),
    [
        ("closed", "blocked"),
        ("draft", "ready_pending"),
        ("mergeability", "blocked"),
        ("review", "blocked"),
        ("stale_check", "repair_required"),
        ("startup_failure", "repair_required"),
    ],
)
def test_same_head_premerge_refresh_reapplies_every_gate_and_prevents_merge(
    monkeypatch,
    tmp_path,
    change,
    expected_outcome,
):
    _patch_repo_boundary(monkeypatch)
    calls = []
    second = _pr_payload()
    if change == "closed":
        second = _pr_payload(state="CLOSED")
    elif change == "draft":
        second = _pr_payload(draft=True)
    elif change == "mergeability":
        second = _pr_payload(mergeable="CONFLICTING")
    elif change == "review":
        second = _pr_payload(review_decision="CHANGES_REQUESTED")
    elif change == "stale_check":
        second = _pr_payload(
            checks=[
                _check("Basic Tests", "basic", conclusion="STALE", run=8),
                _check("PR Body Format", "pr body", run=8),
            ]
        )
    elif change == "startup_failure":
        second = _pr_payload(
            checks=[
                _check("Basic Tests", "basic", conclusion="STARTUP_FAILURE", run=9),
                _check("PR Body Format", "pr body", run=9),
            ]
        )
    views = iter([_pr_payload(), second])

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(next(views)))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(_state(tmp_path), now=100, run=run)

    assert transition.outcome == expected_outcome
    assert transition.state["pr"]["head_sha"] == HEAD_SHA
    assert transition.state["pr"]["merge_attempted_head_sha"] == ""
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)
    premerge_view = [args for args in calls if args[:3] == ["gh", "pr", "view"]][-1]
    requested = premerge_view[premerge_view.index("--json") + 1]
    assert "mergeable" in requested
    assert "reviewDecision" in requested
    assert "statusCheckRollup" in requested


def test_required_post_merge_receipts_return_distinct_pending_outcome(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    state = _state(tmp_path)
    state["policy"]["post_merge_requirements"] = {"ci": True, "deployment": True}

    def run(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(
                args,
                stdout=json.dumps(_pr_payload(state="MERGED", merge_sha=MERGE_SHA)),
            )
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(
        state,
        now=100,
        run=run,
        sync_canonical=lambda *_a: {"state": "synced"},
    )

    assert transition.outcome == "post_merge_pending"
    assert transition.next_due_at == 100
    assert transition.wake_immediately is True
    assert closeout.closeout_terminal_eligible(transition.state) is False


@pytest.mark.parametrize(
    ("receipt_status", "expected_outcome", "error_code"),
    [
        ("failed", "repair_required", "post_merge_receipt_failed"),
        ("blocked", "blocked", "post_merge_receipt_blocked"),
    ],
)
def test_terminal_required_post_merge_receipt_does_not_poll_forever(
    monkeypatch,
    tmp_path,
    receipt_status,
    expected_outcome,
    error_code,
):
    from hermes_cli import post_merge_receipts

    _patch_repo_boundary(monkeypatch)
    state = _state(tmp_path)
    state["policy"]["post_merge_requirements"] = {"deployment": True}
    state["post_merge"] = post_merge_receipts.initialize_post_merge_receipts(
        state,
        target_sha=MERGE_SHA,
    )

    gathered = dict(state["post_merge"])
    gathered["deployment"] = {
        "status": receipt_status,
        "checked_at": 100,
        "diagnostic_code": f"deployment_{receipt_status}",
    }
    monkeypatch.setattr(
        post_merge_receipts,
        "collect_post_merge_receipts",
        lambda *_args, **_kwargs: gathered,
    )

    def run(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(
                args,
                stdout=json.dumps(_pr_payload(state="MERGED", merge_sha=MERGE_SHA)),
            )
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    assert transition.outcome == expected_outcome
    assert transition.next_due_at is None
    assert transition.terminal is True
    assert transition.state["errors"][-1]["code"] == error_code


def test_manual_merge_policy_allows_terminal_open_pr(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    state = _state(tmp_path)
    state["policy"]["merge"] = "manual"

    def run(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload()))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    assert transition.outcome == "pr_open"
    assert transition.terminal is True
    assert transition.state["telemetry"]["green_unmerged_since"] is None
    assert transition.state["telemetry"]["green_unmerged_overdue"] is False
    assert closeout.closeout_terminal_eligible(transition.state) is True


def test_green_unmerged_telemetry_starts_immediately_and_becomes_overdue(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)

    def run(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(
                args,
                stdout=json.dumps(_pr_payload(merge_state="UNKNOWN")),
            )
        raise AssertionError(args)

    first = closeout.reconcile_trusted_closeout(
        _state(tmp_path),
        now=100,
        run=run,
        green_unmerged_overdue_seconds=60,
    )

    assert first.outcome == "waiting_for_mergeability"
    assert first.wake_immediately is True
    assert first.next_due_at == 100
    assert first.state["telemetry"]["green_unmerged_since"] == 100
    assert first.state["telemetry"]["green_unmerged_overdue"] is False

    second = closeout.reconcile_trusted_closeout(
        first.state,
        now=161,
        run=run,
        green_unmerged_overdue_seconds=60,
    )

    assert second.outcome == "waiting_for_mergeability"
    assert second.wake_immediately is False
    assert second.next_due_at == 191
    assert second.state["telemetry"]["green_unmerged_since"] == 100
    assert second.state["telemetry"]["green_unmerged_overdue"] is True


def test_draft_and_incomplete_gates_suppress_green_unmerged_telemetry(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    state = _state(tmp_path)
    state["telemetry"] = {
        "green_unmerged_since": 50,
        "green_unmerged_overdue": True,
    }
    state["review"] = {"status": "pending", "head_sha": HEAD_SHA}

    def run(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload(draft=True)))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(
        state,
        now=100,
        run=run,
        green_unmerged_overdue_seconds=60,
    )

    assert transition.outcome == "waiting_for_gates"
    assert transition.state["telemetry"]["green_unmerged_since"] is None
    assert transition.state["telemetry"]["green_unmerged_overdue"] is False


def test_one_pass_never_sleeps_and_sanitizes_command_errors(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)

    def run(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(
                args,
                returncode=1,
                stderr="Authorization: bearer ghp_secret https://github.com/private/page",
            )
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(_state(tmp_path), now=100, run=run)

    assert transition.outcome == "pending"
    assert transition.next_due_at == 130
    error = transition.state["errors"][-1]
    assert error["code"] == "github_auth_unavailable"
    assert "ghp_secret" not in error["message"]
    assert "private/page" not in error["message"]


def test_closeout_records_sanitized_git_github_and_transition_spans(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    state = _state(tmp_path)
    state["policy"]["merge"] = "manual"

    def run(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload()))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    spans = transition.state["telemetry"]["phase_spans"]
    assert {span["phase"] for span in spans} >= {"closeout", "github"}
    assert all(str(span.get("work_id") or "").startswith("wrk_") for span in spans)
    rendered = json.dumps(spans)
    assert "closeout-1" not in rendered
    assert "https://" not in rendered
    assert "--json" not in rendered
    assert "statusCheckRollup" not in rendered
    assert transition.state["telemetry"]["last_transition"] == "pr_open"


def test_shadow_post_merge_finishes_without_mutating_collectors(monkeypatch, tmp_path):
    from hermes_cli import post_merge_receipts

    _patch_repo_boundary(monkeypatch)
    calls = []
    post_merge_receipts.register_deployment_adapter(
        "shadow-forbidden",
        lambda **_kwargs: calls.append("adapter")
        or {"status": "passed", "observed_sha": MERGE_SHA},
    )
    state = _state(tmp_path, mode="shadow")
    state["policy"]["post_merge_requirements"] = {
        "canonical_sync": True,
        "deployment": True,
    }

    def run(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(
                args,
                stdout=json.dumps(
                    _pr_payload(state="MERGED", merge_sha=MERGE_SHA)
                ),
            )
        raise AssertionError(args)

    first = closeout.reconcile_trusted_closeout(state, now=100, run=run)
    assert first.outcome == "post_merge_pending"
    second = closeout.reconcile_trusted_closeout(
        first.state,
        now=101,
        run=run,
        sync_canonical=lambda *_args: (_ for _ in ()).throw(
            AssertionError("shadow canonical sync")
        ),
        post_merge_config={
            "repositories": {
                "acme/example": {"deployment_adapter": "shadow-forbidden"}
            }
        },
    )

    assert calls == []
    assert second.outcome == "post_merge_complete"
    assert second.state["post_merge"]["canonical_sync"]["status"] == "not_configured"
    assert second.state["post_merge"]["deployment"]["status"] == "not_configured"
    assert closeout.closeout_terminal_eligible(second.state) is False


def test_existing_post_merge_receipts_must_match_exact_target_sha():
    state = closeout.normalize_closeout_state(
        {
            "mode": "enforce",
            "status": "post_merge_complete",
            "policy": {"post_merge_requirements": {"ci": True}},
            "post_merge": {
                "target_sha": MERGE_SHA,
                "ci": {"status": "passed", "observed_sha": HEAD_SHA},
            },
        }
    )

    assert closeout.closeout_terminal_eligible(state) is False
    state["post_merge"]["ci"]["observed_sha"] = MERGE_SHA
    assert closeout.closeout_terminal_eligible(state) is True
