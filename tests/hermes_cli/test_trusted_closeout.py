from __future__ import annotations

import base64
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
            "merge": "never",
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
            pending_push_head_sha=invalid,
        )
        state["ci"] = {"head_sha": invalid}
        state["post_merge"] = {"target_sha": invalid}
        normalized = closeout.normalize_closeout_state(state)
        assert normalized["pr"]["head_sha"] == ""
        assert normalized["pr"]["merge_sha"] == ""
        assert normalized["pr"]["merge_attempted_head_sha"] == ""
        assert normalized["pr"]["pending_push_head_sha"] == ""
        assert normalized["ci"]["head_sha"] == ""
        assert normalized["post_merge"]["target_sha"] == ""

    exact = _state(tmp_path)
    exact["pr"].update(
        head_sha="a" * 64,
        merge_sha="b" * 64,
        merge_attempted_head_sha="c" * 64,
        pending_push_head_sha="f" * 64,
    )
    exact["ci"] = {"head_sha": "d" * 64}
    exact["post_merge"] = {"target_sha": "e" * 64}
    normalized = closeout.normalize_closeout_state(exact)
    assert normalized["pr"]["head_sha"] == "a" * 64
    assert normalized["pr"]["merge_sha"] == "b" * 64
    assert normalized["pr"]["merge_attempted_head_sha"] == "c" * 64
    assert normalized["pr"]["pending_push_head_sha"] == "f" * 64
    assert normalized["ci"]["head_sha"] == "d" * 64
    assert normalized["post_merge"]["target_sha"] == "e" * 64


def _check(
    workflow,
    name,
    *,
    conclusion="SUCCESS",
    head_sha=HEAD_SHA,
    run=1,
    app="GitHub Actions",
    workflow_path=None,
):
    if workflow_path is None:
        workflow_path = {
            "Basic Tests": ".github/workflows/tests.yml",
            "PR Body Format": ".github/workflows/pr-body-format.yml",
        }.get(workflow, f".github/workflows/{workflow.lower().replace(' ', '-')}.yml")
    return {
        "workflowName": workflow,
        "name": name,
        "status": "COMPLETED",
        "conclusion": conclusion,
        "headSha": head_sha,
        "databaseId": run,
        "completedAt": f"2026-07-17T00:00:{run:02d}Z",
        "app": {"name": app},
        "workflow": {"path": workflow_path},
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


def _preview_run(
    calls,
    *,
    preview_state="success",
    preview_url="https://example-a1b2c3-acme.vercel.app",
):
    draft = [True]
    branch_url = "https://example-git-feature-test-acme.vercel.app"
    inspector_url = "https://vercel.com/acme/example/deployment-1"
    comment_payload = base64.b64encode(
        json.dumps(
            {
                "projects": [
                    {
                        "name": "example",
                        "inspectorUrl": inspector_url,
                        "previewUrl": branch_url.removeprefix("https://"),
                        "nextCommitStatus": "DEPLOYED",
                    }
                ],
                "requestReviewUrl": (
                    "https://vercel.com/vercel-agent/request-review"
                    "?owner=acme&repo=example&pr=7"
                ),
            }
        ).encode()
    ).decode()

    def run(args, **_kwargs):
        calls.append(args)
        if args[:2] == ["vercel", "inspect"]:
            return _completed(
                args,
                stdout=json.dumps(
                    {
                        "id": "dpl_deployment-1",
                        "name": "example",
                        "url": "example-a1b2c3-acme.vercel.app",
                        "target": "preview",
                        "readyState": "READY",
                        "aliases": ["example-git-feature-test-acme.vercel.app"],
                    }
                ),
            )
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload(draft=draft[0])))
        if args[:3] == ["gh", "pr", "ready"]:
            draft[0] = False
            return _completed(args)
        if args[:2] == ["gh", "api"] and "/deployments?" in args[2]:
            return _completed(
                args,
                stdout=json.dumps(
                    [
                        {
                            "id": 42,
                            "sha": HEAD_SHA,
                            "ref": "feature/test",
                            "environment": "Preview",
                            "creator": {"login": "vercel[bot]", "type": "Bot"},
                            "created_at": "2026-07-17T00:00:00Z",
                            "updated_at": "2026-07-17T00:00:01Z",
                        }
                    ]
                ),
            )
        if args[:2] == ["gh", "api"] and "/deployments/42/statuses?" in args[2]:
            return _completed(
                args,
                stdout=json.dumps(
                    [
                        {
                            "state": preview_state,
                            "environment_url": preview_url,
                            "creator": {"login": "vercel[bot]", "type": "Bot"},
                            "created_at": "2026-07-17T00:00:01Z",
                            "updated_at": "2026-07-17T00:00:01Z",
                        }
                    ]
                ),
            )
        if args[:2] == ["gh", "api"] and "/comments?" in args[2]:
            return _completed(
                args,
                stdout=json.dumps(
                    [
                        {
                            "user": {"login": "vercel[bot]", "type": "Bot"},
                            "performed_via_github_app": {
                                "id": 8329,
                                "slug": "vercel",
                            },
                            "updated_at": "2026-07-17T00:00:02Z",
                            "body": (
                                f"[vc]: #signature:{comment_payload}\n"
                                "[example](https://vercel.com/acme/example) "
                                f"[Ready]({inspector_url}) "
                                f"[Preview]({branch_url})"
                            ),
                        }
                    ]
                ),
            )
        raise AssertionError(args)

    return run


def test_preview_is_published_before_visual_qa_completes(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
    calls = []
    state = _state(tmp_path)
    state["policy"]["require_preview"] = True
    state["visual_qa"] = {"status": "pending", "head_sha": HEAD_SHA}

    transition = closeout.reconcile_trusted_closeout(
        state,
        now=100,
        run=_preview_run(calls),
    )

    assert transition.outcome == "waiting_for_gates"
    assert transition.state["preview"] == {
        "provider": "vercel",
        "status": "ready",
        "observed_sha": HEAD_SHA,
        "url": "https://example-git-feature-test-acme.vercel.app",
        "deployment_id": "42",
    }
    assert not any(args[:3] in (["gh", "pr", "ready"], ["gh", "pr", "merge"]) for args in calls)

    completed = dict(transition.state)
    completed["visual_qa"] = {"status": "passed", "head_sha": HEAD_SHA}
    final = closeout.reconcile_trusted_closeout(
        completed,
        now=130,
        run=_preview_run(calls),
    )

    assert final.outcome == "pr_published"
    assert closeout.closeout_terminal_eligible(final.state) is True
    assert final.state["pr"]["is_draft"] is False
    assert final.state["pr"]["ready_at"] == 130
    assert sum(args[:3] == ["gh", "pr", "ready"] for args in calls) == 1
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


def test_failed_vercel_preview_requires_repair(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
    state = _state(tmp_path)
    state["policy"]["require_preview"] = True

    transition = closeout.reconcile_trusted_closeout(
        state,
        now=100,
        run=_preview_run([], preview_state="failure", preview_url=""),
    )

    assert transition.outcome == "repair_required"
    assert transition.state["preview"]["status"] == "failed"


def test_failed_ci_waits_for_required_preview_before_terminal_repair(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
    state = _state(tmp_path)
    state["policy"]["require_preview"] = True
    failed_checks = [
        _check("Basic Tests", "basic", conclusion="FAILURE"),
        _check("PR Body Format", "pr body"),
    ]

    def run(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(
                args,
                stdout=json.dumps(_pr_payload(draft=True, checks=failed_checks)),
            )
        if args[:2] == ["gh", "api"] and "/deployments?" in args[2]:
            return _completed(
                args,
                stdout=json.dumps(
                    [
                        {
                            "id": 44,
                            "sha": HEAD_SHA,
                            "ref": "feature/test",
                            "environment": "Preview",
                            "creator": {"login": "vercel[bot]", "type": "Bot"},
                        }
                    ]
                ),
            )
        if args[:2] == ["gh", "api"] and "/deployments/44/statuses?" in args[2]:
            return _completed(args, stdout=json.dumps([{"state": "pending"}]))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    assert transition.outcome == "waiting_for_preview"
    assert transition.terminal is False
    assert transition.next_due_at == 130
    assert transition.state["ci"]["status"] == "failed"
    assert not any(
        error["code"] == "required_checks_failed"
        for error in transition.state["errors"]
    )


def _raw_required_check(
    workflow,
    name,
    run_id,
    *,
    check_id=None,
    conclusion="SUCCESS",
    created_at="2026-07-18T00:00:00Z",
    completed_at="2026-07-18T00:01:00Z",
):
    details_url = (
        f"https://github.com/acme/example/actions/runs/{run_id}/job/"
        f"{check_id or run_id}1"
    )
    return (
        {
            "workflowName": workflow,
            "name": name,
            "detailsUrl": details_url,
        },
        {
            "id": check_id or int(run_id),
            "name": name,
            "head_sha": HEAD_SHA,
            "status": "COMPLETED",
            "conclusion": conclusion,
            "created_at": created_at,
            "started_at": created_at,
            "completed_at": completed_at,
            "details_url": details_url,
            "app": {"slug": "github-actions"},
        },
    )


def _unrelated_check_runs(count=100, *, start=1):
    return [
        {
            "id": check_id,
            "name": f"unrelated-{check_id}",
            "head_sha": HEAD_SHA,
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "created_at": f"2026-07-17T00:{(check_id // 60) % 60:02d}:{check_id % 60:02d}Z",
            "completed_at": f"2026-07-17T01:{(check_id // 60) % 60:02d}:{check_id % 60:02d}Z",
            "details_url": (
                f"https://github.com/acme/example/actions/runs/{check_id}/"
                f"job/{check_id}1"
            ),
            "app": {"slug": "github-actions"},
        }
        for check_id in range(start, start + count)
    ]


def _patch_repo_boundary(monkeypatch):
    monkeypatch.setattr(closeout, "github_remote_preflight_error", lambda *_a, **_k: None)
    monkeypatch.setattr(closeout, "github_origin_repo", lambda *_a, **_k: "acme/example")


def _patch_identity_passthrough(monkeypatch):
    monkeypatch.setattr(
        closeout,
        "enrich_required_check_identities",
        lambda payload, **_kwargs: dict(payload),
    )


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
    assert state["policy"]["merge"] == "never"
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


@pytest.mark.parametrize("spoof_first", [False, True])
@pytest.mark.parametrize(
    ("workflow", "check", "spoof_kwargs"),
    [
        ("Basic Tests", "basic", {"app": "Other CI"}),
        ("Basic Tests", "basic", {"workflow_path": ".github/workflows/spoof-tests.yml"}),
        ("PR Body Format", "pr body", {"app": "Other CI"}),
        (
            "PR Body Format",
            "pr body",
            {"workflow_path": ".github/workflows/spoof-pr-body.yml"},
        ),
    ],
)
def test_required_checks_do_not_replace_canonical_github_actions_identity(
    workflow,
    check,
    spoof_kwargs,
    spoof_first,
):
    checks = [
        _check("Basic Tests", "basic", run=1),
        _check("PR Body Format", "pr body", run=1),
    ]
    for item in checks:
        if item["workflowName"] == workflow:
            item["conclusion"] = "FAILURE"
    spoof = _check(workflow, check, conclusion="SUCCESS", run=9, **spoof_kwargs)
    if spoof_first:
        checks.insert(0, spoof)
    else:
        checks.append(spoof)

    summary = closeout.summarize_required_checks(checks, head_sha=HEAD_SHA)

    assert summary["status"] == "failed"
    assert summary["total"] == 2
    assert summary["failed"] == [f"{workflow} / {check}"]


def test_required_checks_enrich_actual_gh_rollup_with_trusted_rest_identity(tmp_path):
    repo = "acme/example"

    def raw_check(workflow, name, run_id, *, conclusion="SUCCESS"):
        item = _check(workflow, name, conclusion=conclusion)
        item.pop("app")
        item.pop("workflow")
        item["databaseId"] = int(run_id)
        item["detailsUrl"] = f"https://github.com/{repo}/actions/runs/{run_id}/job/{run_id}1"
        return item

    checks = [
        raw_check("Basic Tests", "basic", "333"),
        raw_check("Basic Tests", "basic", "111", conclusion="FAILURE"),
        raw_check("PR Body Format", "pr body", "222"),
    ]
    check_runs = {
        "check_runs": [
            {
                "id": item["databaseId"],
                "name": item["name"],
                "head_sha": HEAD_SHA,
                "status": item["status"],
                "conclusion": item["conclusion"],
                "started_at": item.get("startedAt"),
                "completed_at": item.get("completedAt"),
                "details_url": item["detailsUrl"],
                "app": {"slug": "github-actions"},
            }
            for item in checks
        ]
    }
    workflow_runs = {
        "111": {"path": ".github/workflows/tests.yml", "head_sha": HEAD_SHA},
        "222": {"path": ".github/workflows/pr-body-format.yml", "head_sha": HEAD_SHA},
        "333": {"path": ".github/workflows/spoof-tests.yml", "head_sha": HEAD_SHA},
    }

    def run(args, **_kwargs):
        endpoint = args[2]
        if "/check-runs?" in endpoint:
            return _completed(args, stdout=json.dumps(check_runs))
        run_id = endpoint.rsplit("/", 1)[-1]
        return _completed(args, stdout=json.dumps(workflow_runs[run_id]))

    payload = _pr_payload(checks=checks)
    enriched = closeout.enrich_required_check_identities(
        payload,
        repo=repo,
        root=tmp_path,
        run=run,
    )
    summary = closeout.summarize_required_checks(
        enriched["statusCheckRollup"],
        head_sha=HEAD_SHA,
    )

    assert summary["status"] == "failed"
    assert summary["total"] == 2
    assert summary["failed"] == ["Basic Tests / basic"]


def test_required_check_enrichment_rejects_prepopulated_identity_without_rest(tmp_path):
    payload = _pr_payload()
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        return _completed(args, stdout=json.dumps({"check_runs": []}))

    enriched = closeout.enrich_required_check_identities(
        payload,
        repo="acme/example",
        root=tmp_path,
        run=run,
    )
    summary = closeout.summarize_required_checks(
        enriched["statusCheckRollup"],
        head_sha=HEAD_SHA,
    )

    assert len(calls) == 1
    assert summary["status"] == "pending"
    assert summary["total"] == 0


@pytest.mark.parametrize("ordering", ["original", "reversed", "rotated"])
def test_required_check_enrichment_is_order_independent_under_bounded_budget(
    tmp_path,
    ordering,
):
    repo = "acme/example"

    def details(run_id, job_id):
        return f"https://github.com/{repo}/actions/runs/{run_id}/job/{job_id}"

    check_runs = []
    raw_checks = []
    for offset in range(9):
        run_id = str(100 + offset)
        url = details(run_id, f"{run_id}1")
        raw_checks.append(
            {
                "workflowName": "Basic Tests",
                "name": "basic",
                "detailsUrl": url,
                "app": {"slug": "github-actions"},
                "workflow": {"path": ".github/workflows/tests.yml"},
                "headSha": HEAD_SHA,
            }
        )
        check_runs.append(
            {
                "id": 100 + offset,
                "name": "basic",
                "head_sha": HEAD_SHA,
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
            {
                "workflowName": "Basic Tests",
                "name": "basic",
                "detailsUrl": url,
            }
        )
        check_runs.append(
            {
                "id": check_id,
                "name": "basic",
                "head_sha": HEAD_SHA,
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
            "head_sha": HEAD_SHA,
            "status": "COMPLETED",
            "conclusion": "SUCCESS",
            "created_at": "2026-07-18T00:08:00Z",
            "started_at": "2026-07-18T00:08:00Z",
            "completed_at": "2026-07-18T00:09:00Z",
            "details_url": pr_body_url,
            "app": {"slug": "github-actions"},
        }
    )
    raw_checks.insert(
        0,
        {
            "workflowName": "Unrelated",
            "name": "unrelated",
            "detailsUrl": details("999", "9991"),
        },
    )
    if ordering == "reversed":
        raw_checks.reverse()
        check_runs.reverse()
    elif ordering == "rotated":
        raw_checks[:] = raw_checks[5:] + raw_checks[:5]
        check_runs[:] = check_runs[7:] + check_runs[:7]

    queried_runs = []

    def run(args, **_kwargs):
        endpoint = args[2]
        if "/check-runs?" in endpoint:
            return _completed(args, stdout=json.dumps({"check_runs": check_runs}))
        run_id = endpoint.rsplit("/", 1)[-1]
        queried_runs.append(run_id)
        path = {
            "900": ".github/workflows/tests.yml",
            "901": ".github/workflows/pr-body-format.yml",
        }.get(run_id, f".github/workflows/spoof-{run_id}.yml")
        return _completed(
            args,
            stdout=json.dumps({"path": path, "head_sha": HEAD_SHA}),
        )

    payload = _pr_payload(checks=raw_checks)
    enriched = closeout.enrich_required_check_identities(
        payload,
        repo=repo,
        root=tmp_path,
        run=run,
    )
    summary = closeout.summarize_required_checks(
        enriched["statusCheckRollup"],
        head_sha=HEAD_SHA,
    )

    assert summary["status"] == "failed"
    assert summary["total"] == 2
    assert summary["failed"] == ["Basic Tests / basic"]
    assert queried_runs == ["900", "901"]
    assert "_required_check_identity_error" not in enriched


def test_shared_reconciliation_finds_failed_required_checks_on_second_page(
    monkeypatch,
    tmp_path,
):
    _patch_repo_boundary(monkeypatch)
    raw_basic_old, basic_run_old = _raw_required_check(
        "Basic Tests",
        "basic",
        "900",
        check_id=9000,
        created_at="2026-07-18T00:00:00Z",
        completed_at="2026-07-18T00:01:00Z",
    )
    raw_basic, basic_run = _raw_required_check(
        "Basic Tests",
        "basic",
        "900",
        check_id=9001,
        conclusion="FAILURE",
        created_at="2026-07-18T00:02:00Z",
        completed_at="2026-07-18T00:03:00Z",
    )
    raw_body, body_run = _raw_required_check(
        "PR Body Format",
        "pr body",
        "901",
        check_id=9011,
    )
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(
                args,
                stdout=json.dumps(
                    _pr_payload(checks=[raw_basic_old, raw_basic, raw_body])
                ),
            )
        if args[:2] == ["gh", "api"] and "/check-runs?" in args[2]:
            page = (
                [basic_run_old, basic_run, body_run]
                if "&page=2" in args[2]
                else _unrelated_check_runs()
            )
            return _completed(
                args,
                stdout=json.dumps({"total_count": 103, "check_runs": page}),
            )
        if args[:2] == ["gh", "api"] and "/actions/runs/" in args[2]:
            run_id = args[2].rsplit("/", 1)[-1]
            path = {
                "900": ".github/workflows/tests.yml",
                "901": ".github/workflows/pr-body-format.yml",
            }[run_id]
            return _completed(
                args,
                stdout=json.dumps({"path": path, "head_sha": HEAD_SHA}),
            )
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(
        _state(tmp_path),
        now=100,
        run=run,
    )

    assert transition.outcome == "repair_required"
    assert transition.state["ci"]["failed"] == ["Basic Tests / basic"]
    assert transition.state["errors"][-1]["code"] == "required_checks_failed"
    check_pages = [args[2] for args in calls if args[:2] == ["gh", "api"] and "/check-runs?" in args[2]]
    assert len(check_pages) == 2
    assert "&page=2" not in check_pages[0]
    assert check_pages[1].endswith("&page=2")
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


def test_check_run_page_bound_exhaustion_is_retryable_error(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    raw_basic, _basic_run = _raw_required_check("Basic Tests", "basic", "900")
    raw_body, _body_run = _raw_required_check("PR Body Format", "pr body", "901")
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(
                args,
                stdout=json.dumps(_pr_payload(checks=[raw_basic, raw_body])),
            )
        if args[:2] == ["gh", "api"] and "/check-runs?" in args[2]:
            page = (
                _unrelated_check_runs(start=101)
                if "&page=2" in args[2]
                else _unrelated_check_runs()
            )
            return _completed(
                args,
                stdout=json.dumps({"total_count": 201, "check_runs": page}),
            )
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(
        _state(tmp_path),
        now=100,
        run=run,
    )

    assert transition.outcome == "pending"
    assert transition.outcome != "waiting_for_ci"
    error = transition.state["errors"][-1]
    assert error["code"] == "required_check_identity_failed"
    assert "pagination budget exhausted" in error["message"]
    assert len(error["message"]) <= 600
    assert transition.state["ci"]["status"] == "not_checked"
    check_pages = [args for args in calls if args[:2] == ["gh", "api"] and "/check-runs?" in args[2]]
    assert len(check_pages) == 2
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


def test_malformed_check_run_pagination_fails_closed(tmp_path):
    raw_basic, _basic_run = _raw_required_check("Basic Tests", "basic", "900")
    raw_body, _body_run = _raw_required_check("PR Body Format", "pr body", "901")

    def run(args, **_kwargs):
        return _completed(
            args,
            stdout=json.dumps(
                {"total_count": 101, "check_runs": _unrelated_check_runs(count=99)}
            ),
        )

    enriched = closeout.enrich_required_check_identities(
        _pr_payload(checks=[raw_basic, raw_body]),
        repo="acme/example",
        root=tmp_path,
        run=run,
    )

    assert "pagination was inconsistent" in enriched[
        "_required_check_identity_error"
    ]
    summary = closeout.summarize_required_checks(
        enriched["statusCheckRollup"],
        head_sha=HEAD_SHA,
    )
    assert summary["status"] == "pending"
    assert summary["total"] == 0


def test_initial_identity_api_failure_is_retryable_with_durable_error(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    raw_basic, _basic_run = _raw_required_check("Basic Tests", "basic", "101")
    raw_body, _body_run = _raw_required_check("PR Body Format", "pr body", "202")
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(
                args,
                stdout=json.dumps(_pr_payload(checks=[raw_basic, raw_body])),
            )
        if args[:2] == ["gh", "api"] and "/check-runs?" in args[2]:
            return _completed(
                args,
                returncode=403,
                stderr=(
                    "token=should-not-persist denied at "
                    "https://api.github.com/protected"
                ),
            )
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(
        _state(tmp_path),
        now=100,
        run=run,
    )

    assert transition.outcome == "pending"
    assert transition.next_due_at == 130
    assert transition.terminal is False
    error = transition.state["errors"][-1]
    assert error["code"] == "required_check_identity_failed"
    assert "should-not-persist" not in error["message"]
    assert "api.github.com" not in error["message"]
    assert len(error["message"]) <= 600
    assert transition.state["ci"]["status"] == "not_checked"
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


def test_default_command_budget_reaches_publication_after_maximum_identity_lookups(
    monkeypatch,
    tmp_path,
):
    _patch_repo_boundary(monkeypatch)
    raw_checks = []
    check_runs = []
    workflow_paths = {}
    for workflow, name, run_ids, expected_path in (
        (
            "Basic Tests",
            "basic",
            (1200, 1100, 1000, 900),
            ".github/workflows/tests.yml",
        ),
        (
            "PR Body Format",
            "pr body",
            (2200, 2100, 2000, 1900),
            ".github/workflows/pr-body-format.yml",
        ),
    ):
        for run_id in run_ids:
            raw, check_run = _raw_required_check(workflow, name, str(run_id))
            raw_checks.append(raw)
            check_runs.append(check_run)
            workflow_paths[str(run_id)] = (
                expected_path
                if run_id == run_ids[-1]
                else f".github/workflows/spoof-{run_id}.yml"
            )
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(
                args,
                stdout=json.dumps(_pr_payload(checks=raw_checks)),
            )
        if args[:2] == ["gh", "api"] and "/check-runs?" in args[2]:
            page = check_runs if "&page=2" in args[2] else _unrelated_check_runs()
            return _completed(
                args,
                stdout=json.dumps({"total_count": 108, "check_runs": page}),
            )
        if args[:2] == ["gh", "api"] and "/actions/runs/" in args[2]:
            run_id = args[2].rsplit("/", 1)[-1]
            return _completed(
                args,
                stdout=json.dumps(
                    {"path": workflow_paths[run_id], "head_sha": HEAD_SHA}
                ),
            )
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(
        _state(tmp_path),
        now=100,
        run=run,
    )

    assert transition.outcome == "pr_published"
    assert transition.terminal is True
    assert transition.state["errors"] == []
    assert len(calls) == 12
    assert not any(args[:3] in (["gh", "pr", "ready"], ["gh", "pr", "merge"]) for args in calls)


def test_command_budget_exhaustion_is_retryable_error_not_ci_wait(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    raw_basic, _basic_run = _raw_required_check("Basic Tests", "basic", "101")
    raw_body, _body_run = _raw_required_check("PR Body Format", "pr body", "202")
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(
                args,
                stdout=json.dumps(_pr_payload(checks=[raw_basic, raw_body])),
            )
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(
        _state(tmp_path),
        now=100,
        run=run,
        max_commands=2,
    )

    assert transition.outcome == "pending"
    assert transition.outcome != "waiting_for_ci"
    assert transition.next_due_at == 130
    error = transition.state["errors"][-1]
    assert error["code"] == "required_check_identity_failed"
    assert "command budget exceeded" in error["message"]
    assert transition.state["ci"]["status"] == "not_checked"
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


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
    _patch_identity_passthrough(monkeypatch)
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


def test_existing_pr_pushes_only_explicit_pending_verified_head(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
    calls = []
    state = _state(tmp_path)
    pending_head = "4" * 40
    state["pr"]["head_sha"] = pending_head
    state["pr"]["pending_push_head_sha"] = pending_head
    state["local_verification"] = {"status": "passed", "head_sha": pending_head}

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload(head_sha=HEAD_SHA)))
        if args[:2] == ["git", "push"]:
            return _completed(args)
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    assert transition.outcome == "pr_pending"
    assert [args for args in calls if args[:2] == ["git", "push"]] == [
        [
            "git",
            "push",
            "-u",
            "origin",
            f"{pending_head}:refs/heads/feature/test",
        ]
    ]
    assert not any(args[:2] == ["git", "rev-parse"] for args in calls)


def test_existing_pr_clears_pending_push_after_exact_head_observation(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
    calls = []
    state = _state(tmp_path)
    state["pr"]["pending_push_head_sha"] = HEAD_SHA
    state["mutation_uncertainty"] = {
        "status": "uncertain",
        "operation": "git_push",
        "at": 99.0,
        "head_sha": HEAD_SHA,
    }

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload()))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    assert transition.outcome == "pr_published"
    assert transition.state["pr"]["pending_push_head_sha"] == ""
    assert transition.state["mutation_uncertainty"] == {"status": "none"}
    assert not any(args[:2] in (["git", "push"], ["git", "rev-parse"]) for args in calls)


def test_failed_current_head_ci_requires_repair_without_polling(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
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
    _patch_identity_passthrough(monkeypatch)
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
    _patch_identity_passthrough(monkeypatch)
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
    _patch_identity_passthrough(monkeypatch)
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

    assert draft.outcome == "pr_published"
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

    published = closeout.reconcile_trusted_closeout(
        _state(tmp_path, mode="shadow"),
        now=101,
        run=run_ready,
    )

    assert published.outcome == "pr_published"
    assert closeout.closeout_terminal_eligible(published.state) is False
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


def test_visual_qa_success_marks_draft_ready_after_current_head_gates(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
    calls = []

    draft = [True]

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload(draft=draft[0])))
        if args[:3] == ["gh", "pr", "ready"]:
            draft[0] = False
            return _completed(args)
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(_state(tmp_path), now=100, run=run)

    assert transition.outcome == "pr_published"
    assert transition.terminal is True
    assert transition.state["pr"]["is_draft"] is False
    assert transition.state["pr"]["ready_at"] == 100
    assert sum(args[:3] == ["gh", "pr", "ready"] for args in calls) == 1
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


def test_draft_stays_draft_when_visual_qa_is_not_required_or_proven(
    monkeypatch,
    tmp_path,
):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
    calls = []
    state = _state(tmp_path)
    state["policy"]["require_visual_qa"] = False
    state["visual_qa"] = {"status": "not_required"}

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload(draft=True)))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    assert transition.outcome == "pr_published"
    assert transition.state["pr"]["is_draft"] is True
    assert transition.state["pr"]["ready_at"] is None
    assert not any(args[:3] == ["gh", "pr", "ready"] for args in calls)


def test_uncertain_pr_ready_is_reobserved_before_retry(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
    state = _state(tmp_path)
    state["mutation_uncertainty"] = {
        "status": "uncertain",
        "operation": "github_pr_ready",
        "head_sha": HEAD_SHA,
    }
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload(draft=False)))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    assert transition.outcome == "pr_published"
    assert transition.state["mutation_uncertainty"] == {"status": "none"}
    assert transition.state["pr"]["is_draft"] is False
    assert transition.state["pr"]["ready_at"] == 100
    assert not any(args[:3] == ["gh", "pr", "ready"] for args in calls)


def test_pr_ready_head_race_restores_draft_before_reconciling_new_head(
    monkeypatch,
    tmp_path,
):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
    calls = []
    views = iter(
        (
            _pr_payload(draft=True),
            _pr_payload(head_sha=OLD_SHA, draft=False),
            _pr_payload(head_sha=OLD_SHA, draft=True),
        )
    )

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(next(views)))
        if args[:3] == ["gh", "pr", "ready"]:
            return _completed(args)
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(_state(tmp_path), now=100, run=run)

    assert transition.outcome == "pr_pending"
    assert transition.terminal is False
    assert transition.wake_immediately is True
    assert transition.state["pr"]["head_sha"] == OLD_SHA
    assert transition.state["pr"]["is_draft"] is True
    assert transition.state["pr"]["ready_at"] is None
    assert transition.state["visual_qa"] == {"status": "stale"}
    assert transition.state["mutation_uncertainty"] == {"status": "none"}
    ready_calls = [args for args in calls if args[:3] == ["gh", "pr", "ready"]]
    assert len(ready_calls) == 2
    assert "--undo" not in ready_calls[0]
    assert "--undo" in ready_calls[1]


def test_external_repo_without_hermes_workflows_does_not_wait_for_impossible_checks(
    monkeypatch,
    tmp_path,
):
    """Closeout must not require Hermes-only CI names in another project repo."""

    _patch_repo_boundary(monkeypatch)
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    (workflows / "scrape.yml").write_text("name: Scrape\n", encoding="utf-8")
    calls = []

    draft = [True]

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(
                args,
                stdout=json.dumps(_pr_payload(draft=draft[0], checks=[])),
            )
        if args[:3] == ["gh", "pr", "ready"]:
            draft[0] = False
            return _completed(args)
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(_state(tmp_path), now=100, run=run)

    assert transition.outcome == "pr_published"
    assert transition.state["ci"] == {
        "head_sha": HEAD_SHA,
        "status": "passed",
        "total": 0,
        "failed": [],
        "wait_state": "not_required",
        "required": [],
    }
    assert transition.state["pr"]["is_draft"] is False
    assert transition.state["pr"]["ready_at"] == 100
    assert sum(args[:3] == ["gh", "pr", "ready"] for args in calls) == 1


@pytest.mark.parametrize("visual_status", ["pending", "stale", "failed"])
def test_incomplete_visual_gate_never_readies_or_merges(
    monkeypatch,
    tmp_path,
    visual_status,
):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
    calls = []
    state = _state(tmp_path)
    state["visual_qa"] = {"status": visual_status, "head_sha": HEAD_SHA}

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload(draft=True)))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    assert transition.outcome == "waiting_for_gates"
    assert transition.terminal is False
    assert not any(args[:3] == ["gh", "pr", "ready"] for args in calls)
    assert not any(args[:3] == ["gh", "pr", "merge"] for args in calls)


def test_uncertain_pr_create_rejects_historical_branch_match(
    monkeypatch,
    tmp_path,
):
    _patch_repo_boundary(monkeypatch)
    calls = []
    state = _state(tmp_path)
    state["pr"] = {"title": "Test PR"}
    state["mutation_uncertainty"] = {
        "status": "uncertain",
        "operation": "github_pr_create",
        "at": 200.0,
        "head_sha": HEAD_SHA,
        "branch": "feature/test",
        "base_branch": "main",
        "repository": "acme/example",
    }

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "list"]:
            return _completed(
                args,
                stdout=json.dumps(
                    [
                        {
                            "number": 3,
                            "url": "https://github.com/acme/example/pull/3",
                            "state": "OPEN",
                            "headRefOid": HEAD_SHA,
                            "headRefName": "feature/test",
                            "baseRefName": "main",
                            "headRepository": {
                                "name": "example",
                                "nameWithOwner": "acme/example",
                            },
                            "headRepositoryOwner": {"login": "acme"},
                            "createdAt": "1970-01-01T00:01:00Z",
                        }
                    ]
                ),
            )
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=300, run=run)

    assert transition.outcome == "pending"
    assert transition.state["mutation_uncertainty"]["operation"] == "github_pr_create"
    assert transition.state["errors"][-1]["code"] == (
        "pr_create_reobservation_identity_mismatch"
    )
    assert not any(args[:3] == ["gh", "pr", "create"] for args in calls)
    assert not any(args[:2] == ["git", "push"] for args in calls)
    assert not any(args[:2] == ["git", "rev-parse"] for args in calls)


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        ("[]", "pr_create_reobservation_empty"),
        ("{malformed", "pr_create_reobservation_invalid_json"),
    ],
)
def test_uncertain_pr_create_keeps_fence_on_inconclusive_discovery(
    monkeypatch,
    tmp_path,
    payload,
    expected_code,
):
    _patch_repo_boundary(monkeypatch)
    calls = []
    state = _state(tmp_path)
    state["pr"] = {"title": "Test PR"}
    state["mutation_uncertainty"] = {
        "status": "uncertain",
        "operation": "github_pr_create",
        "at": 200.0,
        "head_sha": HEAD_SHA,
        "branch": "feature/test",
        "base_branch": "main",
        "repository": "acme/example",
    }

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "list"]:
            return _completed(args, stdout=payload)
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=300, run=run)

    assert transition.outcome == "pending"
    assert transition.state["mutation_uncertainty"]["operation"] == (
        "github_pr_create"
    )
    assert transition.state["errors"][-1]["code"] == expected_code
    assert not any(args[:3] == ["gh", "pr", "create"] for args in calls)
    assert not any(args[:2] == ["git", "push"] for args in calls)


def test_uncertain_pr_create_adopts_fenced_sha_after_local_branch_advances(
    monkeypatch,
    tmp_path,
):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
    calls = []
    state = _state(tmp_path)
    state["policy"]["merge"] = "manual"
    state["pr"] = {"title": "Test PR"}
    state["mutation_uncertainty"] = {
        "status": "uncertain",
        "operation": "github_pr_create",
        "at": 200.0,
        "head_sha": HEAD_SHA,
        "branch": "feature/test",
        "base_branch": "main",
        "repository": "acme/example",
    }

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "list"]:
            return _completed(
                args,
                stdout=json.dumps(
                    [
                        {
                            "number": 9,
                            "url": "https://github.com/acme/example/pull/9",
                            "state": "OPEN",
                            "headRefOid": HEAD_SHA,
                            "headRefName": "feature/test",
                            "baseRefName": "main",
                            "headRepository": {
                                "name": "example",
                                "nameWithOwner": "acme/example",
                            },
                            "headRepositoryOwner": {"login": "acme"},
                            "createdAt": "1970-01-01T00:03:21Z",
                        }
                    ]
                ),
            )
        if args[:3] == ["gh", "pr", "view"]:
            payload = _pr_payload()
            payload.update(
                {
                    "number": 9,
                    "url": "https://github.com/acme/example/pull/9",
                }
            )
            return _completed(args, stdout=json.dumps(payload))
        if args[:2] == ["git", "rev-parse"]:
            raise AssertionError("fenced create recovery must not read advanced branch")
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=300, run=run)

    assert transition.state["pr"]["number"] == "9"
    assert transition.state["mutation_uncertainty"] == {"status": "none"}
    assert not any(args[:3] == ["gh", "pr", "create"] for args in calls)
    assert not any(args[:2] == ["git", "push"] for args in calls)


def test_uncertain_push_reobserves_fenced_exact_head_not_moving_branch(
    monkeypatch,
    tmp_path,
):
    _patch_repo_boundary(monkeypatch)
    calls = []
    state = _state(tmp_path)
    state["pr"] = {"title": "Test PR"}
    state["mutation_uncertainty"] = {
        "status": "uncertain",
        "operation": "git_push",
        "at": 100.0,
        "head_sha": HEAD_SHA,
    }

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "list"]:
            return _completed(args, stdout="[]")
        if args[:2] == ["git", "ls-remote"]:
            return _completed(
                args,
                stdout=f"{HEAD_SHA}\trefs/heads/feature/test\n",
            )
        if args[:3] == ["gh", "pr", "create"]:
            return _completed(
                args,
                stdout="https://github.com/acme/example/pull/8\n",
            )
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=101, run=run)

    assert transition.outcome == "pr_pending"
    assert not any(args[:2] == ["git", "rev-parse"] for args in calls)
    assert not any(args[:2] == ["git", "push"] for args in calls)


@pytest.mark.parametrize("source", ["direct", "fable", "opus"])
def test_new_closeout_pushes_immutable_head_refspec_without_force(
    monkeypatch,
    tmp_path,
    source,
):
    _patch_repo_boundary(monkeypatch)
    calls = []
    state = _state(tmp_path)
    state["source"] = source
    state["pr"] = {"title": "Test PR", "head_sha": HEAD_SHA}

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "list"]:
            return _completed(args, stdout="[]")
        if args[:2] == ["git", "push"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "create"]:
            return _completed(
                args,
                stdout="https://github.com/acme/example/pull/12\n",
            )
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    assert transition.outcome == "pr_pending"
    push_calls = [args for args in calls if args[:2] == ["git", "push"]]
    assert push_calls == [
        [
            "git",
            "push",
            "-u",
            "origin",
            f"{HEAD_SHA}:refs/heads/feature/test",
        ]
    ]
    assert not any("--force" in arg or arg.startswith("+") for arg in push_calls[0])
    assert not any(args[:2] == ["git", "rev-parse"] for args in calls)


def test_required_visual_pending_forces_initial_draft_publication(
    monkeypatch,
    tmp_path,
):
    _patch_repo_boundary(monkeypatch)
    calls = []
    state = _state(tmp_path)
    state["policy"]["early_draft_pr"] = False
    state["visual_qa"] = {"status": "pending", "head_sha": HEAD_SHA}
    state["pr"] = {"title": "Visual PR", "head_sha": HEAD_SHA}

    def run(args, **_kwargs):
        calls.append(args)
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "list"]:
            return _completed(args, stdout="[]")
        if args[:2] == ["git", "push"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "create"]:
            return _completed(
                args,
                stdout="https://github.com/acme/example/pull/13\n",
            )
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    assert transition.outcome == "pr_pending"
    create = next(args for args in calls if args[:3] == ["gh", "pr", "create"])
    assert "--draft" in create


def test_legacy_merge_policy_normalizes_to_terminal_published_pr(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
    state = _state(tmp_path)
    state["policy"]["merge"] = "manual"

    def run(args, **_kwargs):
        if args[:3] == ["gh", "auth", "status"]:
            return _completed(args)
        if args[:3] == ["gh", "pr", "view"]:
            return _completed(args, stdout=json.dumps(_pr_payload()))
        raise AssertionError(args)

    transition = closeout.reconcile_trusted_closeout(state, now=100, run=run)

    assert transition.outcome == "pr_published"
    assert transition.terminal is True
    assert transition.state["policy"]["merge"] == "never"
    assert transition.state["telemetry"]["green_unmerged_since"] is None
    assert transition.state["telemetry"]["green_unmerged_overdue"] is False
    assert closeout.closeout_terminal_eligible(transition.state) is True


def test_draft_and_incomplete_gates_suppress_green_unmerged_telemetry(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)
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
    )

    assert transition.outcome == "waiting_for_gates"
    assert transition.state["telemetry"]["green_unmerged_since"] is None
    assert transition.state["telemetry"]["green_unmerged_overdue"] is False


def test_one_pass_never_sleeps_and_sanitizes_command_errors(monkeypatch, tmp_path):
    _patch_repo_boundary(monkeypatch)
    _patch_identity_passthrough(monkeypatch)

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
    _patch_identity_passthrough(monkeypatch)
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
    assert transition.state["telemetry"]["last_transition"] == "pr_published"
