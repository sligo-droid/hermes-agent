import json
import subprocess

from gateway.direct_closeout_observer import observe_merged_direct_closeout
from gateway.work_ledger import GatewayWorkLedger


HEAD = "a" * 40
MERGE = "b" * 40


def _item(tmp_path):
    return {
        "closeout_authoritative": False,
        "closeout": {
            "source": "direct",
            "mode": "enforce",
            "status": "pending",
            "workspace": {
                "path": str(tmp_path),
                "canonical_path": "",
                "repository": "sligo-labs/PID",
                "branch": "discord-action/pid-test",
                "base_branch": "main",
            },
            "policy": {"merge": "auto", "post_merge_requirements": {}},
        },
    }


def test_observer_adopts_only_exact_merged_green_pr(tmp_path):
    payload = {
        "number": 1035,
        "url": "https://github.com/sligo-labs/PID/pull/1035",
        "state": "MERGED",
        "headRefOid": HEAD,
        "headRefName": "discord-action/pid-test",
        "baseRefName": "main",
        "mergeCommit": {"oid": MERGE},
        "mergeStateStatus": "CLEAN",
        "mergeable": "MERGEABLE",
        "isDraft": False,
        "reviewDecision": "",
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "workflowName": "CI",
                "name": "tests",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
            {
                "__typename": "StatusContext",
                "context": "Vercel",
                "state": "SUCCESS",
            },
        ],
    }

    def run(args, **_kwargs):
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        if args[1] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, HEAD + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    state = observe_merged_direct_closeout(
        _item(tmp_path),
        "Done: https://github.com/sligo-labs/PID/pull/1035",
        run=run,
    )

    assert state["status"] == "post_merge_complete"
    assert state["pr"]["merge_sha"] == MERGE
    assert state["ci"]["status"] == "passed"
    assert state["ci"]["total"] == 2


def test_observer_uses_latest_check_run_after_rerun(tmp_path):
    payload = {
        "number": 1035,
        "url": "https://github.com/sligo-labs/PID/pull/1035",
        "state": "MERGED",
        "headRefOid": HEAD,
        "headRefName": "discord-action/pid-test",
        "baseRefName": "main",
        "mergeCommit": {"oid": MERGE},
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "workflowName": "PR Body Format",
                "name": "pr body",
                "startedAt": "2026-07-20T22:13:10Z",
                "completedAt": "2026-07-20T22:13:32Z",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
            },
            {
                "__typename": "CheckRun",
                "workflowName": "PR Body Format",
                "name": "pr body",
                "startedAt": "2026-07-20T22:14:27Z",
                "completedAt": "2026-07-20T22:14:49Z",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
            },
        ],
    }

    def run(args, **_kwargs):
        if args[:3] == ["gh", "pr", "view"]:
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), "")
        if args[1] == "rev-parse":
            return subprocess.CompletedProcess(args, 0, HEAD + "\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    state = observe_merged_direct_closeout(
        _item(tmp_path),
        "Done: https://github.com/sligo-labs/PID/pull/1035",
        run=run,
    )

    assert state is not None
    assert state["ci"]["status"] == "passed"
    assert state["ci"]["total"] == 1


def test_observer_rejects_unrelated_or_non_green_pr(tmp_path):
    assert observe_merged_direct_closeout(
        _item(tmp_path),
        "https://github.com/other/repo/pull/12",
        run=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
    ) is None


def test_ledger_adopts_terminal_observation(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 50.0)
    data = {
        "version": 1,
        "items": {
            "work": {
                "id": "work",
                "status": "agent_running",
                "closeout_authoritative": False,
                "closeout": _item(tmp_path)["closeout"],
            }
        },
    }
    ledger.path.write_text(json.dumps(data), encoding="utf-8")
    observed = _item(tmp_path)["closeout"]
    observed.update(
        {
            "status": "post_merge_complete",
            "local_verification": {"status": "passed", "head_sha": HEAD},
            "pr": {
                "state": "MERGED",
                "head_sha": HEAD,
                "merge_sha": MERGE,
                "url": "https://github.com/sligo-labs/PID/pull/1035",
            },
            "ci": {"status": "passed", "head_sha": HEAD, "total": 1},
            "post_merge": {"target_sha": MERGE},
        }
    )

    stored = ledger.adopt_observed_direct_closeout(
        "work",
        observed,
        expected_run_state={"status": "agent_running", "active_run": None},
    )

    assert stored["closeout_authoritative"] is True
    assert stored["closeout"]["status"] == "post_merge_complete"
