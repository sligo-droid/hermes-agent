import json
import subprocess

import pytest

from hermes_cli.closeout_execution import RemoteMutationUncertain
from hermes_cli.dev_pr_merge import merge_published_pr


HEAD = "a" * 40
PR_URL = "https://github.com/acme/example/pull/7"


def _check(workflow, name, path, *, conclusion="SUCCESS"):
    return {
        "workflowName": workflow,
        "name": name,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "headSha": HEAD,
        "databaseId": 1,
        "completedAt": "2026-08-10T00:00:00Z",
        "app": {"slug": "github-actions"},
        "workflow": {"path": path},
    }


def _payload(*, state="OPEN", head=HEAD, draft=False, merge_state="CLEAN", mergeable="MERGEABLE"):
    return {
        "number": 7,
        "url": PR_URL,
        "state": state,
        "headRefOid": head,
        "mergedAt": "2026-08-10T00:01:00Z" if state == "MERGED" else None,
        "mergeCommit": {"oid": "b" * 40} if state == "MERGED" else None,
        "mergeStateStatus": merge_state,
        "mergeable": mergeable,
        "isDraft": draft,
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            _check("Basic Tests", "basic", ".github/workflows/tests.yml"),
            _check("PR Body Format", "pr body", ".github/workflows/pr-body-format.yml"),
        ],
    }


def _closeout(tmp_path):
    return {
        "id": "work-1",
        "mode": "enforce",
        "status": "pr_published",
        "workspace": {
            "path": str(tmp_path),
            "repository": "acme/example",
            "branch": "feature/example",
            "base_branch": "main",
        },
        "policy": {
            "merge": "never",
            "require_preview": True,
            "require_local_verification": True,
            "require_review": True,
            "require_visual_qa": True,
        },
        "local_verification": {"status": "passed", "head_sha": HEAD},
        "review": {"status": "passed", "head_sha": HEAD},
        "visual_qa": {"status": "passed", "head_sha": HEAD},
        "pr": {
            "url": PR_URL,
            "number": "7",
            "state": "OPEN",
            "head_sha": HEAD,
            "review_decision": "APPROVED",
        },
        "ci": {"status": "passed", "head_sha": HEAD},
        "preview": {
            "status": "ready",
            "observed_sha": HEAD,
            "url": "https://example-feature.vercel.app",
        },
    }


class FakeRun:
    def __init__(
        self,
        payloads,
        *,
        uncertain_merge=False,
        merge_returncode=0,
        merge_stderr="",
    ):
        self.payloads = list(payloads)
        self.uncertain_merge = uncertain_merge
        self.merge_returncode = merge_returncode
        self.merge_stderr = merge_stderr
        self.calls = []

    def __call__(self, args, **_kwargs):
        self.calls.append(list(args))
        if args[:3] == ["gh", "pr", "view"]:
            payload = self.payloads.pop(0)
            return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")
        if args[:3] == ["gh", "pr", "merge"] and self.uncertain_merge:
            raise RemoteMutationUncertain("github_pr_merge", "timeout")
        if args[:3] == ["gh", "pr", "merge"]:
            return subprocess.CompletedProcess(
                args,
                self.merge_returncode,
                stdout="",
                stderr=self.merge_stderr,
            )
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


@pytest.fixture(autouse=True)
def _trust_fixture_checks(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.dev_pr_merge.enrich_required_check_identities",
        lambda payload, **_kwargs: dict(payload),
    )


def test_merges_exact_published_head(tmp_path):
    run = FakeRun([_payload(), _payload(state="MERGED")])

    result = merge_published_pr(_closeout(tmp_path), run=run)

    assert result.outcome == "merged"
    assert result.message == f"Merged: {PR_URL}"
    merge_call = next(call for call in run.calls if call[:3] == ["gh", "pr", "merge"])
    assert "--squash" in merge_call
    assert "--delete-branch" not in merge_call
    assert merge_call[-2:] == ["--match-head-commit", HEAD]
    assert not any(call[:2] == ["git", "checkout"] for call in run.calls)


def test_external_repo_without_hermes_checks_uses_published_ci_gate(tmp_path):
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "insights.yml").write_text("name: insights\n", encoding="utf-8")
    open_payload = _payload()
    open_payload["statusCheckRollup"] = []
    run = FakeRun([open_payload, _payload(state="MERGED")])

    result = merge_published_pr(_closeout(tmp_path), run=run)

    assert result.outcome == "merged"


def test_marks_draft_ready_before_merging(tmp_path):
    run = FakeRun(
        [_payload(draft=True), _payload(draft=False), _payload(state="MERGED")]
    )

    result = merge_published_pr(_closeout(tmp_path), run=run)

    assert result.outcome == "merged"
    assert [call[:3] for call in run.calls if call[:2] == ["gh", "pr"]] == [
        ["gh", "pr", "view"],
        ["gh", "pr", "ready"],
        ["gh", "pr", "view"],
        ["gh", "pr", "merge"],
        ["gh", "pr", "view"],
    ]


def test_blocks_when_head_advanced(tmp_path):
    run = FakeRun([_payload(head="c" * 40)])

    result = merge_published_pr(_closeout(tmp_path), run=run)

    assert result.outcome == "blocked"
    assert "head changed" in result.message
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in run.calls)


def test_match_head_fence_blocks_advance_between_view_and_merge(tmp_path):
    run = FakeRun(
        [_payload(), _payload(head="c" * 40)],
        merge_returncode=1,
        merge_stderr="head branch changed",
    )

    result = merge_published_pr(_closeout(tmp_path), run=run)

    merge_call = next(call for call in run.calls if call[:3] == ["gh", "pr", "merge"])
    assert merge_call[-2:] == ["--match-head-commit", HEAD]
    assert result.outcome == "blocked"
    assert result.message == "head branch changed"


def test_blocks_when_persisted_visual_qa_is_not_green(tmp_path):
    state = _closeout(tmp_path)
    state["visual_qa"] = {"status": "failed", "head_sha": HEAD}
    run = FakeRun([])

    result = merge_published_pr(state, run=run)

    assert result.outcome == "blocked"
    assert "visual QA" in result.message
    assert run.calls == []


def test_blocks_when_live_required_ci_is_not_green(tmp_path):
    payload = _payload()
    payload["statusCheckRollup"][0]["conclusion"] = "FAILURE"
    run = FakeRun([payload])

    result = merge_published_pr(_closeout(tmp_path), run=run)

    assert result.outcome == "blocked"
    assert "required CI" in result.message


def test_already_merged_is_idempotent(tmp_path):
    run = FakeRun([_payload(state="MERGED")])

    result = merge_published_pr(_closeout(tmp_path), run=run)

    assert result.outcome == "already_merged"
    assert result.message == f"Merged: {PR_URL}"
    assert not any(call[:3] == ["gh", "pr", "merge"] for call in run.calls)


def test_already_merged_mismatched_head_is_rejected(tmp_path):
    run = FakeRun([_payload(state="MERGED", head="c" * 40)])

    result = merge_published_pr(_closeout(tmp_path), run=run)

    assert result.outcome == "blocked"
    assert "does not match" in result.message


def test_rejects_pr_url_from_different_repository(tmp_path):
    state = _closeout(tmp_path)
    state["pr"]["url"] = "https://github.com/other/repository/pull/7"
    run = FakeRun([])

    result = merge_published_pr(state, run=run)

    assert result.outcome == "blocked"
    assert run.calls == []


def test_uncertain_merge_reobserves_success(tmp_path):
    run = FakeRun([_payload(), _payload(state="MERGED")], uncertain_merge=True)

    result = merge_published_pr(_closeout(tmp_path), run=run)

    assert result.outcome == "merged"


def test_uncertain_merge_rejects_merged_advanced_head(tmp_path):
    run = FakeRun(
        [_payload(), _payload(state="MERGED", head="c" * 40)],
        uncertain_merge=True,
    )

    result = merge_published_pr(_closeout(tmp_path), run=run)

    assert result.outcome == "blocked"
    assert "does not match" in result.message


def test_uncertain_merge_fails_closed_when_still_open(tmp_path):
    run = FakeRun([_payload(), _payload()], uncertain_merge=True)

    result = merge_published_pr(_closeout(tmp_path), run=run)

    assert result.outcome == "uncertain"
    assert "could not be confirmed" in result.message
