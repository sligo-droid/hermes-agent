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
    build_pr_amend_intake_artifact,
    evaluate_request,
    extract_request,
    fetch_pr_related_context,
    policy_from_route,
    preflight_request,
    write_pr_amend_intake_artifact,
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
    "user": {"login": "sligo-droid"},
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
    "pull_request": {"number": 182, "user": {"login": "sligo-droid"}},
    "review": {
        "id": 4900001,
        "html_url": "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#pullrequestreview-4900001",
        "state": "changes_requested",
        "body": "@sligo-droid please address the requested changes.",
    },
}

PR_RELATED_CONTEXT = {
    "reviews": [{"id": 4900001, "state": "CHANGES_REQUESTED", "body": "Use the existing helper."}],
    "review_comments": [{"id": 4800001, "path": "src/example.ts", "body": "Inline fix."}],
    "issue_comments": [{"id": 4700001, "body": "@sligo-droid please update the tests."}],
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

    def test_accepts_missing_mention_review_on_sligo_droid_authored_pr(self):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["review"]["body"] = "please address the requested changes."
        request = extract_request("pull_request_review", payload)
        decision = evaluate_request(request, PR_INFO, GitHubPrAmendPolicy())
        assert decision.accepted is True

    def test_defers_missing_mention_review_without_payload_author_to_pr_metadata(self):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["pull_request"].pop("user")
        payload["review"]["body"] = "please address the requested changes."
        request = extract_request("pull_request_review", payload)
        assert preflight_request(request, GitHubPrAmendPolicy()) is None
        decision = evaluate_request(request, PR_INFO, GitHubPrAmendPolicy())
        assert decision.accepted is True

    def test_rejects_missing_mention_review_when_pr_metadata_author_is_not_sligo_droid(self):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["pull_request"].pop("user")
        payload["review"]["body"] = "please address the requested changes."
        pr_info = json.loads(json.dumps(PR_INFO))
        pr_info["user"]["login"] = "someone-else"
        request = extract_request("pull_request_review", payload)
        assert preflight_request(request, GitHubPrAmendPolicy()) is None
        decision = evaluate_request(request, pr_info, GitHubPrAmendPolicy())
        assert decision.accepted is False
        assert decision.reason == (
            "missing mention @sligo-droid; PR author 'someone-else' is not sligo-droid"
        )

    def test_rejects_missing_mention_review_on_non_sligo_droid_authored_pr(self):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["pull_request"]["user"]["login"] = "someone-else"
        payload["review"]["body"] = "please address the requested changes."
        request = extract_request("pull_request_review", payload)
        assert preflight_request(request, GitHubPrAmendPolicy()) == (
            "missing mention @sligo-droid; PR author 'someone-else' is not sligo-droid"
        )

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

    def test_intake_artifact_contains_required_operational_contract(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        request = extract_request("issue_comment", ISSUE_COMMENT_PAYLOAD, delivery_id="delivery-artifact")
        policy = policy_from_route(ROUTE)
        decision = evaluate_request(request, PR_INFO, policy)

        artifact = build_pr_amend_intake_artifact(
            request,
            decision,
            policy,
            PR_INFO,
            ISSUE_COMMENT_PAYLOAD,
            PR_RELATED_CONTEXT,
        )
        path = write_pr_amend_intake_artifact(artifact)
        saved = json.loads(path.read_text(encoding="utf-8"))

        assert saved["artifact_version"] == 1
        assert saved["delivery_id"] == "delivery-artifact"
        assert saved["event"] == {"type": "issue_comment", "action": "created"}
        assert saved["sender"]["login"] == "tbrent"
        assert saved["repository"]["full_name"] == "reserve-protocol/reserve-index-dtf"
        assert saved["pull_request"]["number"] == 182
        assert saved["pull_request"]["head"]["repo"] == "sligo-droid/reserve-index-dtf"
        assert saved["pull_request"]["base"]["repo"] == "reserve-protocol/reserve-index-dtf"
        assert saved["source"]["body"] == ISSUE_COMMENT_PAYLOAD["comment"]["body"]
        assert saved["fetched_context"]["pull_request"]["title"] == PR_INFO["title"]
        assert saved["fetched_context"]["reviews"] == PR_RELATED_CONTEXT["reviews"]
        assert saved["fetched_context"]["review_comments"] == PR_RELATED_CONTEXT["review_comments"]
        assert saved["fetched_context"]["issue_comments"] == PR_RELATED_CONTEXT["issue_comments"]
        assert saved["policy_decision"]["accepted"] is True
        instructions = saved["operational_instructions"].lower()
        assert "do not post github text comments" in instructions
        assert "command center/discord worker-board path" in instructions
        assert "worker-board embed/thread" in instructions
        assert "open and merge a pr in the `sligo-droid` fork" in instructions
        assert "final public github output is pushed commits/prs plus reactions only" in instructions

    def test_fetch_pr_related_context_fetches_paginated_lists(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "reviews" in cmd[-1]:
                stdout = '[{"id": 1}]\n[{"id": 2}]'
            elif "pulls/182/comments" in cmd[-1]:
                stdout = '[{"id": 3}]'
            else:
                stdout = '[{"id": 4}]'
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        monkeypatch.setattr("gateway.github_pr_amend.subprocess.run", fake_run)

        context = fetch_pr_related_context("reserve-protocol/reserve-index-dtf", 182)

        assert context == {
            "reviews": [{"id": 1}, {"id": 2}],
            "review_comments": [{"id": 3}],
            "issue_comments": [{"id": 4}],
        }
        assert all("--paginate" in cmd and "--slurp" not in cmd for cmd in calls)
        assert calls == [
            [
                "gh",
                "api",
                "--paginate",
                "repos/reserve-protocol/reserve-index-dtf/pulls/182/reviews?per_page=100",
            ],
            [
                "gh",
                "api",
                "--paginate",
                "repos/reserve-protocol/reserve-index-dtf/pulls/182/comments?per_page=100",
            ],
            [
                "gh",
                "api",
                "--paginate",
                "repos/reserve-protocol/reserve-index-dtf/issues/182/comments?per_page=100",
            ],
        ]


class TestGitHubPrAmendWebhookRoute:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("event_type", "payload", "endpoint"),
        [
            (
                "issue_comment",
                ISSUE_COMMENT_PAYLOAD,
                "repos/reserve-protocol/reserve-index-dtf/issues/comments/4700001/reactions",
            ),
            (
                "pull_request_review_comment",
                REVIEW_COMMENT_PAYLOAD,
                "repos/reserve-protocol/reserve-index-dtf/pulls/comments/4800001/reactions",
            ),
            (
                "pull_request_review",
                REVIEW_PAYLOAD,
                "repos/reserve-protocol/reserve-index-dtf/issues/182/reactions",
            ),
        ],
    )
    async def test_github_pr_amend_reaction_endpoint_mapping(
        self, event_type, payload, endpoint
    ):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        request = extract_request(event_type, payload)
        completed = subprocess.CompletedProcess(["gh"], 0, stdout="{}", stderr="")

        with patch("gateway.platforms.webhook.subprocess.run", return_value=completed) as run:
            assert await adapter._add_github_pr_amend_reaction(request, "eyes") is True

        argv = run.call_args.args[0]
        assert endpoint in argv
        assert "content=eyes" in argv
        assert "Accept: application/vnd.github+json" in argv
        assert "X-GitHub-Api-Version: 2022-11-28" in argv

    @pytest.mark.asyncio
    async def test_signed_issue_comment_routes_to_discord_worker_board(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        secret = "route-secret"
        route = json.loads(json.dumps(ROUTE))
        route["secret"] = secret
        route["github_pr_amend"]["discord_channel_id"] = "channel-123"
        adapter = _make_adapter({"github-pr-amend": route})
        adapter._add_github_pr_amend_reaction = AsyncMock(return_value=True)

        body = json.dumps(ISSUE_COMMENT_PAYLOAD).encode()
        sig = _github_signature(body, secret)

        board_metadata = {
            "discord_channel_id": "channel-123",
            "discord_top_level_message_id": "msg-123",
            "discord_thread_id": "thread-123",
            "discord_thread_url": "https://discord.test/thread-123",
            "discord_board": "discord-thread-123",
            "discord_board_public_url": "https://workers.test/thread-123",
            "discord_guild_id": "guild-123",
        }

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO), patch(
            "gateway.github_pr_amend.fetch_pr_related_context", return_value=PR_RELATED_CONTEXT
        ), patch(
            "gateway.github_pr_amend.publish_and_activate_pr_amend_intake",
            return_value=board_metadata,
        ) as publish:
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
        assert data["status"] == "queued"
        assert data["lock_key"] == "sligo-droid/reserve-index-dtf:feat/irrevocable-fee-recipients"
        assert data["artifact_path"].endswith("delivery-accepted.json")
        assert data["discord_board"] == "discord-thread-123"
        assert Path(data["artifact_path"]).is_file()
        artifact = json.loads(Path(data["artifact_path"]).read_text(encoding="utf-8"))
        assert artifact["delivery_id"] == "delivery-accepted"
        assert artifact["fetched_context"]["reviews"] == PR_RELATED_CONTEXT["reviews"]
        assert "sligo-droid" in artifact["operational_instructions"]
        publish.assert_called_once()
        assert publish.call_args.kwargs["channel_id"] == "channel-123"
        card = publish.call_args.args[0]
        assert card["kind"] == "github_pr_amend"
        assert "Open and merge a PR" in card["body"]
        assert card["github_pr_amend"]["head_ref"] == "feat/irrevocable-fee-recipients"
        assert card["github_pr_amend"]["base_repo"] == "reserve-protocol/reserve-index-dtf"
        assert card["project_context"]["github_pr_target_repo"] == "sligo-droid/reserve-index-dtf"
        assert card["project_context"]["base_branch"] == "feat/irrevocable-fee-recipients"
        assert card["project_context"]["github_pr_amend"]["head_sha"] == "19a1d0b"
        assert any(
            "Discord worker-board embed/thread" in criterion
            for criterion in card["acceptance_criteria"]
        )
        assert [call.args[1] for call in adapter._add_github_pr_amend_reaction.await_args_list] == [
            "eyes",
            "rocket",
        ]
        assert data["lock_key"] in adapter._github_pr_amend_locks
        assert adapter._github_pr_amend_lock_boards[data["lock_key"]] == "discord-thread-123"

    @pytest.mark.asyncio
    async def test_non_tbrent_mention_is_ignored(self):
        payload = json.loads(json.dumps(ISSUE_COMMENT_PAYLOAD))
        payload["sender"]["login"] = "stranger"
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock()

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
        adapter._add_github_pr_amend_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_issue_comment_missing_mention_is_ignored_before_pr_lookup(self):
        payload = json.loads(json.dumps(ISSUE_COMMENT_PAYLOAD))
        payload["comment"]["body"] = "please update the tests"
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock()

        with patch("gateway.github_pr_amend.fetch_pr_info") as fetch_pr_info:
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=payload,
                    headers={
                        "X-GitHub-Event": "issue_comment",
                        "X-GitHub-Delivery": "delivery-missing-mention",
                    },
                )
                data = await resp.json()

        assert resp.status == 200
        assert data["status"] == "ignored"
        assert "missing mention" in data["reason"]
        fetch_pr_info.assert_not_called()
        adapter._add_github_pr_amend_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_seen_reaction_failure_does_not_prevent_job_start(self, tmp_path):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock(side_effect=RuntimeError("gh down"))

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO), patch(
            "gateway.github_pr_amend.fetch_pr_related_context", return_value=PR_RELATED_CONTEXT
        ), patch(
            "gateway.github_pr_amend.resolve_pr_amend_discord_channel", return_value="channel-123"
        ), patch(
            "gateway.github_pr_amend.publish_and_activate_pr_amend_intake",
            return_value={"discord_board": "board", "discord_thread_id": "thread"},
        ):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=ISSUE_COMMENT_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "issue_comment",
                        "X-GitHub-Delivery": "delivery-reaction-failed",
                    },
                )
                data = await resp.json()

        assert resp.status == 202
        assert data["status"] == "queued"

    @pytest.mark.asyncio
    async def test_unresolved_discord_channel_fails_clearly_before_worker_board_publish(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock(return_value=True)

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO), patch(
            "gateway.github_pr_amend.fetch_pr_related_context", return_value=PR_RELATED_CONTEXT
        ), patch(
            "gateway.github_pr_amend.resolve_pr_amend_discord_channel", return_value=""
        ):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=ISSUE_COMMENT_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "issue_comment",
                        "X-GitHub-Delivery": "delivery-no-channel",
                    },
                )
                data = await resp.json()

        assert resp.status == 500
        assert data["status"] == "error"
        assert data["error"] == "Failed to queue PR amendment worker board"
        assert Path(data["artifact_path"]).is_file()
        assert adapter._add_github_pr_amend_reaction.await_args.args[1] == "-1"

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

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO), patch(
            "gateway.github_pr_amend.fetch_pr_related_context", return_value=PR_RELATED_CONTEXT
        ), patch(
            "gateway.github_pr_amend.resolve_pr_amend_discord_channel", return_value="channel-123"
        ), patch(
            "gateway.github_pr_amend.publish_and_activate_pr_amend_intake",
            return_value={"discord_board": "board", "discord_thread_id": "thread"},
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
        assert data["status"] == "queued"

    @pytest.mark.asyncio
    async def test_review_submitted_missing_mention_on_sligo_droid_authored_pr_is_accepted(self, tmp_path):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["pull_request"].pop("user")
        payload["review"]["body"] = "please address the requested changes."
        adapter = _make_adapter({"github-pr-amend": ROUTE})

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO), patch(
            "gateway.github_pr_amend.fetch_pr_related_context", return_value=PR_RELATED_CONTEXT
        ), patch(
            "gateway.github_pr_amend.resolve_pr_amend_discord_channel", return_value="channel-123"
        ), patch(
            "gateway.github_pr_amend.publish_and_activate_pr_amend_intake",
            return_value={"discord_board": "board", "discord_thread_id": "thread"},
        ):
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=payload,
                    headers={
                        "X-GitHub-Event": "pull_request_review",
                        "X-GitHub-Delivery": "delivery-review-implicit",
                    },
                )
                data = await resp.json()

        assert resp.status == 202
        assert data["status"] == "queued"

    @pytest.mark.asyncio
    async def test_review_submitted_missing_mention_on_non_sligo_droid_authored_pr_is_ignored(self):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["pull_request"]["user"]["login"] = "someone-else"
        payload["review"]["body"] = "please address the requested changes."
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock()

        with patch("gateway.github_pr_amend.fetch_pr_info") as fetch_pr_info:
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=payload,
                    headers={
                        "X-GitHub-Event": "pull_request_review",
                        "X-GitHub-Delivery": "delivery-review-other-author",
                    },
                )
                data = await resp.json()

        assert resp.status == 200
        assert data["status"] == "ignored"
        assert "PR author 'someone-else' is not sligo-droid" in data["reason"]
        fetch_pr_info.assert_not_called()
        adapter._add_github_pr_amend_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_pr_fetch_failure_allows_github_retry_same_delivery(self, tmp_path):
        adapter = _make_adapter({"github-pr-amend": ROUTE})

        with patch(
            "gateway.github_pr_amend.fetch_pr_info",
            side_effect=[RuntimeError("api down"), PR_INFO],
        ), patch(
            "gateway.github_pr_amend.fetch_pr_related_context", return_value=PR_RELATED_CONTEXT
        ), patch(
            "gateway.github_pr_amend.resolve_pr_amend_discord_channel", return_value="channel-123"
        ), patch(
            "gateway.github_pr_amend.publish_and_activate_pr_amend_intake",
            return_value={"discord_board": "board", "discord_thread_id": "thread"},
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
        assert second_data["status"] == "queued"
