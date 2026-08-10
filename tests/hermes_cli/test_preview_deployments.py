from __future__ import annotations

import json
import subprocess

from hermes_cli.preview_deployments import collect_vercel_preview


HEAD_SHA = "a" * 40


def _completed(args, payload, returncode=0):
    return subprocess.CompletedProcess(
        args,
        returncode,
        json.dumps(payload) if not isinstance(payload, str) else payload,
        "",
    )


def test_collect_vercel_preview_returns_exact_head_environment_url(tmp_path):
    calls = []

    def run(args, **_kwargs):
        calls.append(args)
        if "/deployments?" in args[2]:
            return _completed(
                args,
                [
                    {
                        "id": 42,
                        "sha": HEAD_SHA,
                        "ref": "discord/thread-1",
                        "environment": "Preview",
                        "creator": {"login": "vercel[bot]"},
                        "created_at": "2026-08-09T12:00:00Z",
                    }
                ],
            )
        return _completed(
            args,
            [
                {
                    "state": "success",
                    "environment_url": "https://pid-git-discord-thread-1.vercel.app",
                }
            ],
        )

    result = collect_vercel_preview(
        repository="sligo-labs/PID",
        head_sha=HEAD_SHA,
        branch="discord/thread-1",
        root=tmp_path,
        run=run,
    )

    assert result.as_dict() == {
        "provider": "vercel",
        "status": "ready",
        "observed_sha": HEAD_SHA,
        "url": "https://pid-git-discord-thread-1.vercel.app",
        "deployment_id": "42",
    }
    assert len(calls) == 2


def test_collect_vercel_preview_rejects_wrong_head_and_non_preview_alias(tmp_path):
    responses = iter(
        [
            [
                {
                    "id": 41,
                    "sha": "b" * 40,
                    "ref": "discord/thread-1",
                    "environment": "Preview",
                    "creator": {"login": "vercel[bot]"},
                }
            ],
        ]
    )

    result = collect_vercel_preview(
        repository="sligo-labs/PID",
        head_sha=HEAD_SHA,
        branch="discord/thread-1",
        root=tmp_path,
        run=lambda args, **_kwargs: _completed(args, next(responses)),
    )

    assert result.status == "pending"
    assert result.url == ""


def test_collect_vercel_preview_requires_vercel_app_url(tmp_path):
    def run(args, **_kwargs):
        if "/deployments?" in args[2]:
            return _completed(
                args,
                [
                    {
                        "id": 42,
                        "sha": HEAD_SHA,
                        "ref": "discord/thread-1",
                        "environment": "Preview",
                        "creator": {"login": "vercel[bot]"},
                    }
                ],
            )
        return _completed(
            args,
            [{"state": "success", "environment_url": "https://vercel.com/acme/project"}],
        )

    result = collect_vercel_preview(
        repository="sligo-labs/PID",
        head_sha=HEAD_SHA,
        branch="discord/thread-1",
        root=tmp_path,
        run=run,
    )

    assert result.status == "pending"
    assert result.diagnostic_code == "preview_url_missing"
