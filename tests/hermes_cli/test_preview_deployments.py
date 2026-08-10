from __future__ import annotations

import base64
import json
import subprocess

import pytest

from hermes_cli.preview_deployments import collect_vercel_preview


HEAD_SHA = "a" * 40
BRANCH = "discord/thread-1"
DEPLOYMENT_URL = "https://pid-a1b2c3-sligo-labs.vercel.app"
BRANCH_URL = "https://pid-git-discord-thread-1-sligo-labs.vercel.app"


def _completed(args, payload, returncode=0):
    return subprocess.CompletedProcess(
        args,
        returncode,
        json.dumps(payload) if not isinstance(payload, str) else payload,
        "",
    )


def _vercel_actor(*, login="vercel[bot]", actor_type="Bot"):
    return {"login": login, "type": actor_type}


def _deployment(*, sha=HEAD_SHA, ref=BRANCH):
    return {
        "id": 42,
        "sha": sha,
        "ref": ref,
        "environment": "Preview",
        "creator": _vercel_actor(),
        "created_at": "2026-08-09T12:00:00Z",
        "updated_at": "2026-08-09T12:00:01Z",
    }


def _status(*, url=DEPLOYMENT_URL):
    return {
        "state": "success",
        "environment_url": url,
        "creator": _vercel_actor(),
        "created_at": "2026-08-09T12:00:01Z",
        "updated_at": "2026-08-09T12:00:01Z",
    }


def _vercel_comment(
    *,
    login="vercel[bot]",
    actor_type="Bot",
    app_id=8329,
    app_slug="vercel",
    team="sligo-labs",
    project="pid",
    branch_url=BRANCH_URL,
    repository="PID",
    pr_number=17,
    updated_at="2026-08-09T12:00:02Z",
):
    inspector_url = f"https://vercel.com/{team}/{project}/deployment-1"
    payload = {
        "projects": [
            {
                "name": project,
                "inspectorUrl": inspector_url,
                "previewUrl": branch_url.removeprefix("https://"),
                "nextCommitStatus": "DEPLOYED",
            }
        ],
        "requestReviewUrl": (
            "https://vercel.com/vercel-agent/request-review"
            f"?owner={team}&repo={repository}&pr={pr_number}"
        ),
    }
    encoded = base64.b64encode(json.dumps(payload).encode()).decode()
    body = (
        f"[vc]: #signature:{encoded}\n"
        f"[{project}](https://vercel.com/{team}/{project}) "
        f"[Ready]({inspector_url}) [Preview]({branch_url})"
    )
    return {
        "user": _vercel_actor(login=login, actor_type=actor_type),
        "performed_via_github_app": {"id": app_id, "slug": app_slug},
        "updated_at": updated_at,
        "body": body,
    }


def _ready_run(comment):
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if "/deployments?" in args[2]:
            return _completed(args, [_deployment()])
        if "/statuses?" in args[2]:
            return _completed(args, [_status()])
        return _completed(args, [comment] if comment else [])

    return calls, run


def _collect(tmp_path, run):
    return collect_vercel_preview(
        repository="sligo-labs/PID",
        head_sha=HEAD_SHA,
        branch=BRANCH,
        pr_number=17,
        root=tmp_path,
        run=run,
    )


def test_collect_vercel_preview_returns_branch_alias_after_exact_head_ready(tmp_path):
    calls, run = _ready_run(_vercel_comment())

    result = _collect(tmp_path, run)

    assert result.as_dict() == {
        "provider": "vercel",
        "status": "ready",
        "observed_sha": HEAD_SHA,
        "url": BRANCH_URL,
        "deployment_id": "42",
    }
    assert len(calls) == 3


def test_collect_vercel_preview_rejects_wrong_head_and_non_preview_alias(tmp_path):
    result = _collect(
        tmp_path,
        lambda args, **_kwargs: _completed(args, [_deployment(sha="b" * 40)]),
    )

    assert result.status == "pending"
    assert result.url == ""


def test_collect_vercel_preview_requires_vercel_app_url(tmp_path):
    def run(args, **_kwargs):
        if "/deployments?" in args[2]:
            return _completed(args, [_deployment()])
        return _completed(args, [_status(url="https://vercel.com/acme/project")])

    result = _collect(tmp_path, run)

    assert result.status == "pending"
    assert result.diagnostic_code == "preview_url_missing"


def test_collect_vercel_preview_waits_for_branch_alias_comment(tmp_path):
    _calls, run = _ready_run(None)

    result = _collect(tmp_path, run)

    assert result.status == "pending"
    assert result.url == ""
    assert result.diagnostic_code == "preview_branch_url_missing"


@pytest.mark.parametrize(
    "comment",
    [
        _vercel_comment(login="evil-vercel-user"),
        _vercel_comment(actor_type="User"),
        _vercel_comment(app_id=9999),
        _vercel_comment(app_slug="fake-vercel"),
        _vercel_comment(project="other"),
        _vercel_comment(team="other-team"),
        _vercel_comment(repository="OTHER"),
        _vercel_comment(pr_number=99),
        _vercel_comment(
            branch_url="https://pid-git-unrelated-branch-sligo-labs.vercel.app"
        ),
        _vercel_comment(updated_at="2026-08-09T11:59:59Z"),
    ],
)
def test_collect_vercel_preview_rejects_unbound_or_stale_vercel_comments(
    tmp_path, comment
):
    _calls, run = _ready_run(comment)

    result = _collect(tmp_path, run)

    assert result.status == "pending"
    assert result.url == ""
    assert result.diagnostic_code == "preview_branch_url_missing"
