"""Tests for GitHub mention-triggered PR amendment webhooks."""

import hashlib
import hmac
import json
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.github_pr_amend import (
    GitHubPrAmendPolicy,
    build_hermes_command,
    evaluate_request,
    extract_request,
    policy_from_route,
    preflight_request,
)
from gateway.config import PlatformConfig
from gateway.platforms.webhook import WebhookAdapter, _INSECURE_NO_AUTH


def _make_adapter(routes) -> WebhookAdapter:
    return WebhookAdapter(
        PlatformConfig(
            enabled=True,
            extra={"host": "0.0.0.0", "port": 0, "routes": routes},
        )
    )


def _create_app(adapter: WebhookAdapter) -> web.Application:
    app = web.Application()
    app.router.add_get("/health", adapter._handle_health)
    app.router.add_post("/webhooks/{route_name}", adapter._handle_webhook)
    return app


def _github_signature(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


PR_INFO = {
    "state": "open",
    "html_url": "https://github.com/reserve-protocol/reserve-index-dtf/pull/182",
    "title": "Add irrevocable fee recipients",
    "body": "PR body",
    "head": {
        "ref": "feat/irrevocable-fee-recipients",
        "sha": "19a1d0b",
        "repo": {"full_name": "sligo-droid/reserve-index-dtf"},
    },
    "base": {
        "ref": "main",
        "repo": {"full_name": "reserve-protocol/reserve-index-dtf"},
    },
}


ISSUE_COMMENT_PAYLOAD = {
    "action": "created",
    "repository": {"full_name": "reserve-protocol/reserve-index-dtf"},
    "sender": {"login": "tbrent"},
    "issue": {
        "number": 182,
        "pull_request": {
            "url": "https://api.github.com/repos/reserve-protocol/reserve-index-dtf/pulls/182"
        },
    },
    "comment": {
        "id": 4700001,
        "html_url": "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#issuecomment-4700001",
        "body": "@sligo-droid please update the tests for this change.",
    },
}


REVIEW_COMMENT_PAYLOAD = {
    "action": "created",
    "repository": {"full_name": "reserve-protocol/reserve-index-dtf"},
    "sender": {"login": "tbrent"},
    "pull_request": {"number": 182},
    "comment": {
        "id": 4800001,
        "html_url": "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#discussion_r4800001",
        "body": "@sligo-droid use the existing helper here.",
        "path": "src/example.ts",
        "line": 42,
        "diff_hunk": "@@ ...",
        "pull_request_review_id": 123,
    },
}


REVIEW_PAYLOAD = {
    "action": "submitted",
    "repository": {"full_name": "reserve-protocol/reserve-index-dtf"},
    "sender": {"login": "tbrent"},
    "pull_request": {"number": 182},
    "review": {
        "id": 4900001,
        "html_url": "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#pullrequestreview-4900001",
        "state": "changes_requested",
        "body": "@sligo-droid please address the requested changes.",
    },
}


ROUTE = {
    "secret": _INSECURE_NO_AUTH,
    "events": ["issue_comment", "pull_request_review_comment", "pull_request_review"],
    "mode": "github_pr_amend",
    "github_pr_amend": {
        "mention": "@sligo-droid",
        "allowed_senders": ["tbrent"],
        "allowed_base_repos": ["reserve-protocol/reserve-index-dtf"],
        "allowed_head_repos": ["sligo-droid/reserve-index-dtf"],
        "canary_prs": {"reserve-protocol/reserve-index-dtf": [182]},
        "job": {"hermes_command": "hermes", "workspace_root": "/tmp/github-pr-amend-test"},
    },
}


class TestGitHubPrAmendPolicy:
    def test_extracts_issue_comment_request(self):
        request = extract_request("issue_comment", ISSUE_COMMENT_PAYLOAD, delivery_id="d1")
        assert request.repo == "reserve-protocol/reserve-index-dtf"
        assert request.pr_number == 182
        assert request.sender == "tbrent"
        assert request.source_kind == "issue_comment"
        assert "@sligo-droid" in request.body

    def test_extracts_review_comment_line_context(self):
        request = extract_request("pull_request_review_comment", REVIEW_COMMENT_PAYLOAD)
        assert request.source_kind == "review_comment"
        assert request.path == "src/example.ts"
        assert request.line == 42
        assert request.review_id == "123"

    def test_extracts_changes_requested_review_context(self):
        request = extract_request("pull_request_review", REVIEW_PAYLOAD)
        assert request.source_kind == "review"
        assert request.review_state == "changes_requested"
        assert request.review_id == "4900001"

    def test_accepts_tbrent_mention_on_canary_pr(self):
        policy = policy_from_route(ROUTE)
        request = extract_request("issue_comment", ISSUE_COMMENT_PAYLOAD)
        decision = evaluate_request(request, PR_INFO, policy)
        assert decision.accepted is True
        assert decision.lock_key == "sligo-droid/reserve-index-dtf:feat/irrevocable-fee-recipients"

    def test_exact_repo_allowlists_still_pass_without_wildcards(self):
        route = json.loads(json.dumps(ROUTE))
        route["github_pr_amend"].pop("canary_prs")
        policy = policy_from_route(route)
        request = extract_request("issue_comment", ISSUE_COMMENT_PAYLOAD)
        decision = evaluate_request(request, PR_INFO, policy)
        assert decision.accepted is True

    def test_wildcard_base_org_accepts_other_reserve_repo(self):
        route = json.loads(json.dumps(ROUTE))
        route["github_pr_amend"].pop("canary_prs")
        route["github_pr_amend"]["allowed_base_repos"] = ["reserve-protocol/*"]
        route["github_pr_amend"]["allowed_head_repos"] = ["sligo-droid/*", "reserve-protocol/*"]
        payload = json.loads(json.dumps(ISSUE_COMMENT_PAYLOAD))
        payload["repository"]["full_name"] = "reserve-protocol/other-dtf"
        payload["issue"]["number"] = 17
        pr_info = json.loads(json.dumps(PR_INFO))
        pr_info["base"]["repo"]["full_name"] = "reserve-protocol/other-dtf"
        pr_info["head"]["repo"]["full_name"] = "reserve-protocol/other-dtf"
        request = extract_request("issue_comment", payload)
        decision = evaluate_request(request, pr_info, policy_from_route(route))
        assert decision.accepted is True
        assert decision.base_repo == "reserve-protocol/other-dtf"
        assert decision.head_repo == "reserve-protocol/other-dtf"

    def test_wildcard_base_org_rejects_other_org_before_pr_lookup(self):
        route = json.loads(json.dumps(ROUTE))
        route["github_pr_amend"].pop("canary_prs")
        route["github_pr_amend"]["allowed_base_repos"] = ["reserve-protocol/*"]
        payload = json.loads(json.dumps(ISSUE_COMMENT_PAYLOAD))
        payload["repository"]["full_name"] = "someone-else/reserve-index-dtf"
        request = extract_request("issue_comment", payload)
        reason = preflight_request(request, policy_from_route(route))
        assert reason == "base repo 'someone-else/reserve-index-dtf' is not allowlisted"

    def test_wildcard_head_allowlist_rejects_third_party_fork(self):
        route = json.loads(json.dumps(ROUTE))
        route["github_pr_amend"].pop("canary_prs")
        route["github_pr_amend"]["allowed_base_repos"] = ["reserve-protocol/*"]
        route["github_pr_amend"]["allowed_head_repos"] = ["sligo-droid/*", "reserve-protocol/*"]
        pr_info = json.loads(json.dumps(PR_INFO))
        pr_info["head"]["repo"]["full_name"] = "someone-else/reserve-index-dtf"
        request = extract_request("issue_comment", ISSUE_COMMENT_PAYLOAD)
        decision = evaluate_request(request, pr_info, policy_from_route(route))
        assert decision.accepted is False
        assert "head repo" in decision.reason

    def test_canary_prs_only_narrows_when_configured(self):
        route = json.loads(json.dumps(ROUTE))
        route["github_pr_amend"].pop("canary_prs")
        payload = json.loads(json.dumps(ISSUE_COMMENT_PAYLOAD))
        payload["issue"]["number"] = 999
        request = extract_request("issue_comment", payload)
        assert preflight_request(request, policy_from_route(route)) is None

        route["github_pr_amend"]["canary_prs"] = {"reserve-protocol/reserve-index-dtf": [182]}
        assert preflight_request(request, policy_from_route(route)) == "PR #999 is outside canary allowlist"

    def test_rejects_non_tbrent_sender(self):
        payload = json.loads(json.dumps(ISSUE_COMMENT_PAYLOAD))
        payload["sender"]["login"] = "stranger"
        request = extract_request("issue_comment", payload)
        decision = evaluate_request(request, PR_INFO, GitHubPrAmendPolicy())
        assert decision.accepted is False
        assert "not allowlisted" in decision.reason

    def test_rejects_missing_mention(self):
        payload = json.loads(json.dumps(ISSUE_COMMENT_PAYLOAD))
        payload["comment"]["body"] = "please update the tests"
        request = extract_request("issue_comment", payload)
        decision = evaluate_request(request, PR_INFO, GitHubPrAmendPolicy())
        assert decision.accepted is False
        assert "missing mention" in decision.reason

    def test_rejects_unallowlisted_head_repo(self):
        pr_info = json.loads(json.dumps(PR_INFO))
        pr_info["head"]["repo"]["full_name"] = "someone-else/reserve-index-dtf"
        request = extract_request("issue_comment", ISSUE_COMMENT_PAYLOAD)
        decision = evaluate_request(request, pr_info, GitHubPrAmendPolicy())
        assert decision.accepted is False
        assert "head repo" in decision.reason

    def test_preflight_rejects_non_tbrent_before_pr_lookup(self):
        payload = json.loads(json.dumps(ISSUE_COMMENT_PAYLOAD))
        payload["sender"]["login"] = "stranger"
        request = extract_request("issue_comment", payload)
        assert preflight_request(request, GitHubPrAmendPolicy()) == "sender 'stranger' is not allowlisted"

    def test_worker_command_is_noninteractive_and_bounded(self):
        policy = policy_from_route(ROUTE)
        argv = build_hermes_command("do the work", policy)
        assert argv[:4] == ["hermes", "chat", "--yolo", "--quiet"]
        assert "--max-turns" in argv
        assert argv[argv.index("--max-turns") + 1] == "120"
        assert argv[argv.index("--toolsets") + 1] == "terminal,file,web,session_search"
        assert argv[-2:] == ["--query", "do the work"]


    def test_worker_command_respects_string_false_for_booleans(self):
        route = json.loads(json.dumps(ROUTE))
        route["github_pr_amend"]["job"]["quiet"] = "false"
        route["github_pr_amend"]["job"]["yolo"] = "false"
        policy = policy_from_route(route)
        argv = build_hermes_command("do the work", policy)
        assert "--quiet" not in argv
        assert "--yolo" not in argv


class TestGitHubPrAmendWebhookRoute:
    @pytest.mark.asyncio
    async def test_signed_issue_comment_starts_amend_job(self, tmp_path):
        secret = "route-secret"
        route = json.loads(json.dumps(ROUTE))
        route["secret"] = secret
        adapter = _make_adapter({"github-pr-amend": route})
        adapter._run_github_pr_amend_job = AsyncMock()

        body = json.dumps(ISSUE_COMMENT_PAYLOAD).encode()
        sig = _github_signature(body, secret)

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO), patch(
            "gateway.github_pr_amend.write_job_brief", return_value=Path(tmp_path / "brief.json")
        ):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post(
                    "/webhooks/github-pr-amend",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "X-GitHub-Event": "issue_comment",
                        "X-Hub-Signature-256": sig,
                        "X-GitHub-Delivery": "delivery-accepted",
                    },
                )
                data = await resp.json()

        assert resp.status == 202
        assert data["status"] == "accepted"
        assert data["lock_key"] == "sligo-droid/reserve-index-dtf:feat/irrevocable-fee-recipients"
        assert adapter._run_github_pr_amend_job.await_count == 1

    @pytest.mark.asyncio
    async def test_non_tbrent_mention_is_ignored(self):
        payload = json.loads(json.dumps(ISSUE_COMMENT_PAYLOAD))
        payload["sender"]["login"] = "stranger"
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._run_github_pr_amend_job = AsyncMock()

        with patch("gateway.github_pr_amend.fetch_pr_info") as fetch_pr_info:
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=payload,
                    headers={
                        "X-GitHub-Event": "issue_comment",
                        "X-GitHub-Delivery": "delivery-ignored",
                    },
                )
                data = await resp.json()

        assert resp.status == 200
        assert data["status"] == "ignored"
        assert "not allowlisted" in data["reason"]
        fetch_pr_info.assert_not_called()
        adapter._run_github_pr_amend_job.assert_not_called()

    @pytest.mark.asyncio
    async def test_branch_lock_rejects_concurrent_job(self, tmp_path):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._github_pr_amend_locks.add(
            "sligo-droid/reserve-index-dtf:feat/irrevocable-fee-recipients"
        )

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=ISSUE_COMMENT_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "issue_comment",
                        "X-GitHub-Delivery": "delivery-locked",
                    },
                )
                data = await resp.json()

        assert resp.status == 409
        assert data["status"] == "locked"

    @pytest.mark.asyncio
    async def test_review_submitted_changes_requested_is_accepted(self, tmp_path):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._run_github_pr_amend_job = AsyncMock()

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO), patch(
            "gateway.github_pr_amend.write_job_brief", return_value=Path(tmp_path / "brief.json")
        ):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=REVIEW_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "pull_request_review",
                        "X-GitHub-Delivery": "delivery-review",
                    },
                )
                data = await resp.json()

        assert resp.status == 202
        assert data["status"] == "accepted"
        assert adapter._run_github_pr_amend_job.await_count == 1

    @pytest.mark.asyncio
    async def test_pr_fetch_failure_allows_github_retry_same_delivery(self, tmp_path):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._run_github_pr_amend_job = AsyncMock()

        with patch(
            "gateway.github_pr_amend.fetch_pr_info",
            side_effect=[RuntimeError("api down"), PR_INFO],
        ), patch(
            "gateway.github_pr_amend.write_job_brief", return_value=Path(tmp_path / "brief.json")
        ):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                first = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=ISSUE_COMMENT_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "issue_comment",
                        "X-GitHub-Delivery": "delivery-retryable",
                    },
                )
                first_data = await first.json()
                second = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=ISSUE_COMMENT_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "issue_comment",
                        "X-GitHub-Delivery": "delivery-retryable",
                    },
                )
                second_data = await second.json()

        assert first.status == 502
        assert first_data["status"] == "error"
        assert second.status == 202
        assert second_data["status"] == "accepted"
        assert adapter._run_github_pr_amend_job.await_count == 1

    @pytest.mark.asyncio
    async def test_worker_failure_comment_does_not_leak_output(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        request = extract_request("issue_comment", ISSUE_COMMENT_PAYLOAD, delivery_id="delivery-failed")
        policy = policy_from_route(ROUTE)
        decision = evaluate_request(request, PR_INFO, policy)
        adapter._run_github_pr_amend_subprocess = AsyncMock(
            return_value=subprocess.CompletedProcess(
                ["hermes"],
                7,
                stdout="SECRET_STDOUT",
                stderr="SECRET_STDERR with ``` fence",
            )
        )
        adapter._deliver_github_comment = AsyncMock()

        await adapter._run_github_pr_amend_job(
            route_name="github-pr-amend",
            request=request,
            decision=decision,
            policy=policy,
            prompt="prompt",
        )

        assert adapter._deliver_github_comment.await_count == 1
        comment = adapter._deliver_github_comment.await_args_list[0].args[0]
        assert "SECRET_STDOUT" not in comment
        assert "SECRET_STDERR" not in comment
        assert "Tail output" not in comment
        assert "Delivery ID: `delivery-failed`" in comment
