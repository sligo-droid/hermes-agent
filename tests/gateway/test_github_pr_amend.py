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

from hermes_cli import kanban_db
from hermes_cli.discord_worker_boards import _planner_instructions
from gateway.github_pr_amend import (
    GitHubPrAmendPolicy,
    build_pr_amend_intake_artifact,
    build_pr_amend_discord_card,
    evaluate_request,
    extract_request,
    fetch_pr_info,
    fetch_pr_related_context,
    policy_from_route,
    preflight_request,
    request_with_parent_review_state,
    resolve_pr_amend_existing_discord_route,
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
        "node_id": "IC_kwDOReviewIssueComment",
        "html_url": "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#issuecomment-4700001",
        "body": "@sligo-droid please update the tests for this change.",
    },
}


REVIEW_COMMENT_PAYLOAD = {
    "action": "created",
    "repository": {"full_name": "reserve-protocol/reserve-index-dtf"},
    "sender": {"login": "tbrent"},
    "pull_request": {"number": 182, "user": {"login": "sligo-droid"}},
    "comment": {
        "id": 4800001,
        "node_id": "PRRC_kwDOReviewComment",
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
        "node_id": "PRR_kwDOReviewSummary",
        "url": "https://api.github.com/repos/reserve-protocol/reserve-index-dtf/pulls/182/reviews/4900001",
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


def _changes_requested_review_comment_request(payload=None, *, delivery_id=""):
    raw = json.loads(json.dumps(payload or REVIEW_COMMENT_PAYLOAD))
    request = extract_request("pull_request_review_comment", raw, delivery_id=delivery_id)
    return request_with_parent_review_state(request, {"state": "CHANGES_REQUESTED"})


def _review_payload(body=None, *, state="changes_requested"):
    payload = json.loads(json.dumps(REVIEW_PAYLOAD))
    payload["review"]["state"] = state
    if body is not None:
        payload["review"]["body"] = body
    return payload


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
        assert request.pr_author == "sligo-droid"

    def test_extracts_changes_requested_review_context(self):
        request = extract_request("pull_request_review", REVIEW_PAYLOAD)
        assert request.source_kind == "review"
        assert request.review_state == "changes_requested"
        assert request.review_id == "4900001"

    def test_preflight_rejects_non_tbrent_review_before_pr_lookup(self):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["sender"]["login"] = "stranger"
        request = extract_request("pull_request_review", payload)
        assert preflight_request(request, GitHubPrAmendPolicy()) == "sender 'stranger' is not allowlisted"

    def test_preflight_rejects_non_tbrent_missing_mention_review_before_pr_lookup(self):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["sender"]["login"] = "stranger"
        payload["pull_request"].pop("user")
        payload["review"]["body"] = "please address the requested changes."
        request = extract_request("pull_request_review", payload)
        assert preflight_request(request, GitHubPrAmendPolicy()) == "sender 'stranger' is not allowlisted"

    def test_accepts_changes_requested_review_on_canary_pr(self):
        policy = policy_from_route(ROUTE)
        request = extract_request("pull_request_review", REVIEW_PAYLOAD)
        decision = evaluate_request(request, PR_INFO, policy)
        assert decision.accepted is True
        assert decision.lock_key == "sligo-droid/reserve-index-dtf:feat/irrevocable-fee-recipients"

    def test_exact_repo_allowlists_still_pass_without_wildcards(self):
        route = json.loads(json.dumps(ROUTE))
        route["github_pr_amend"].pop("canary_prs")
        policy = policy_from_route(route)
        request = extract_request("pull_request_review", REVIEW_PAYLOAD)
        decision = evaluate_request(request, PR_INFO, policy)
        assert decision.accepted is True

    def test_wildcard_base_org_accepts_other_reserve_repo(self):
        route = json.loads(json.dumps(ROUTE))
        route["github_pr_amend"].pop("canary_prs")
        route["github_pr_amend"]["allowed_base_repos"] = ["reserve-protocol/*"]
        route["github_pr_amend"]["allowed_head_repos"] = ["sligo-droid/*", "reserve-protocol/*"]
        payload = _review_payload()
        payload["repository"]["full_name"] = "reserve-protocol/other-dtf"
        payload["pull_request"]["number"] = 17
        pr_info = json.loads(json.dumps(PR_INFO))
        pr_info["base"]["repo"]["full_name"] = "reserve-protocol/other-dtf"
        pr_info["head"]["repo"]["full_name"] = "reserve-protocol/other-dtf"
        request = extract_request("pull_request_review", payload)
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
        request = extract_request("pull_request_review", REVIEW_PAYLOAD)
        decision = evaluate_request(request, pr_info, policy_from_route(route))
        assert decision.accepted is False
        assert "head repo" in decision.reason

    def test_canary_prs_only_narrows_when_configured(self):
        route = json.loads(json.dumps(ROUTE))
        route["github_pr_amend"].pop("canary_prs")
        payload = _review_payload()
        payload["pull_request"]["number"] = 999
        request = extract_request("pull_request_review", payload)
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

    def test_rejects_issue_comments_even_with_mention(self):
        request = extract_request("issue_comment", ISSUE_COMMENT_PAYLOAD)
        decision = evaluate_request(request, PR_INFO, GitHubPrAmendPolicy())
        assert decision.accepted is False
        assert decision.reason == "issue_comment is not a changes-requested review signal"

    def test_accepts_missing_mention_review_on_sligo_droid_authored_pr(self):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["review"]["body"] = "please address the requested changes."
        request = extract_request("pull_request_review", payload)
        decision = evaluate_request(request, PR_INFO, GitHubPrAmendPolicy())
        assert decision.accepted is True

    def test_accepts_missing_mention_review_comment_on_sligo_droid_authored_pr(self):
        payload = json.loads(json.dumps(REVIEW_COMMENT_PAYLOAD))
        payload["comment"]["body"] = "use the existing helper here."
        request = _changes_requested_review_comment_request(payload)
        assert preflight_request(request, GitHubPrAmendPolicy()) is None

        decision = evaluate_request(request, PR_INFO, GitHubPrAmendPolicy())

        assert decision.accepted is True

    def test_rejects_review_comment_without_parent_changes_requested_state(self):
        payload = json.loads(json.dumps(REVIEW_COMMENT_PAYLOAD))
        payload["comment"]["body"] = "use the existing helper here."
        request = extract_request("pull_request_review_comment", payload)

        decision = evaluate_request(request, PR_INFO, GitHubPrAmendPolicy())

        assert decision.accepted is False
        assert decision.reason == "parent review state 'unknown' is not CHANGES_REQUESTED"

    def test_rejects_review_comment_on_commented_parent_review(self):
        request = request_with_parent_review_state(
            extract_request("pull_request_review_comment", REVIEW_COMMENT_PAYLOAD),
            {"state": "COMMENTED"},
        )

        decision = evaluate_request(request, PR_INFO, GitHubPrAmendPolicy())

        assert decision.accepted is False
        assert decision.reason == "parent review state 'COMMENTED' is not CHANGES_REQUESTED"

    def test_rejects_review_op_that_is_not_changes_requested(self):
        payload = _review_payload(state="commented")
        request = extract_request("pull_request_review", payload)

        assert preflight_request(request, GitHubPrAmendPolicy()) == (
            "review state 'COMMENTED' is not CHANGES_REQUESTED"
        )

    def test_rejects_missing_mention_review_comment_on_non_sligo_droid_authored_pr(self):
        payload = json.loads(json.dumps(REVIEW_COMMENT_PAYLOAD))
        payload["pull_request"]["user"]["login"] = "someone-else"
        payload["comment"]["body"] = "use the existing helper here."
        request = extract_request("pull_request_review_comment", payload)
        assert preflight_request(request, GitHubPrAmendPolicy()) == (
            "missing mention @sligo-droid; PR author 'someone-else' is not sligo-droid"
        )

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
        request = extract_request("pull_request_review", REVIEW_PAYLOAD)
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
        request = extract_request("pull_request_review", REVIEW_PAYLOAD, delivery_id="delivery-artifact")
        policy = policy_from_route(ROUTE)
        decision = evaluate_request(request, PR_INFO, policy)

        artifact = build_pr_amend_intake_artifact(
            request,
            decision,
            policy,
            PR_INFO,
            REVIEW_PAYLOAD,
            PR_RELATED_CONTEXT,
        )
        path = write_pr_amend_intake_artifact(artifact)
        saved = json.loads(path.read_text(encoding="utf-8"))

        assert saved["artifact_version"] == 1
        assert saved["delivery_id"] == "delivery-artifact"
        assert saved["event"] == {"type": "pull_request_review", "action": "submitted"}
        assert saved["sender"]["login"] == "tbrent"
        assert saved["repository"]["full_name"] == "reserve-protocol/reserve-index-dtf"
        assert saved["pull_request"]["number"] == 182
        assert saved["pull_request"]["head"]["repo"] == "sligo-droid/reserve-index-dtf"
        assert saved["pull_request"]["base"]["repo"] == "reserve-protocol/reserve-index-dtf"
        assert saved["source"]["body"] == REVIEW_PAYLOAD["review"]["body"]
        assert saved["fetched_context"]["pull_request"]["title"] == PR_INFO["title"]
        assert saved["fetched_context"]["reviews"] == PR_RELATED_CONTEXT["reviews"]
        assert saved["fetched_context"]["review_comments"] == PR_RELATED_CONTEXT["review_comments"]
        assert saved["fetched_context"]["issue_comments"] == PR_RELATED_CONTEXT["issue_comments"]
        assert saved["policy_decision"]["accepted"] is True
        instructions = saved["operational_instructions"].lower()
        assert "do not post github text comments" in instructions
        assert "command center/discord worker-board path" in instructions
        assert "worker-board embed/thread" in instructions
        assert "target checkout/repo" in instructions
        assert "`sligo-droid/reserve-index-dtf`" in instructions
        assert "base `feat/irrevocable-fee-recipients`" in instructions
        assert "review context only" in instructions
        assert "final public github output is pushed commits/prs plus reactions only" in instructions

    def test_reserve_protocol_pr_amend_card_includes_solidity_skill_hint(self, tmp_path):
        request = extract_request("pull_request_review", REVIEW_PAYLOAD, delivery_id="delivery-card")
        policy = policy_from_route(ROUTE)
        decision = evaluate_request(request, PR_INFO, policy)
        artifact = build_pr_amend_intake_artifact(request, decision, policy, PR_INFO, REVIEW_PAYLOAD)

        card = build_pr_amend_discord_card(artifact, artifact_path=tmp_path / "intake.json")

        assert card["project_context"]["worker_skill_hints"] == ["reserve-solidity-style"]
        assert "reserve-solidity-style" in card["project_context"]["worker_context_hints"][0]
        worker = {"project_context": card["project_context"]}
        assert any("reserve-solidity-style" in item for item in _planner_instructions(worker))

    def test_review_card_includes_inline_review_comments(self, tmp_path):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["review"]["id"] = 4518030260
        payload["review"]["html_url"] = (
            "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#pullrequestreview-4518030260"
        )
        payload["review"]["body"] = "@sligo-droid please address the inline comments."
        request = extract_request("pull_request_review", payload, delivery_id="delivery-review")
        policy = policy_from_route(ROUTE)
        decision = evaluate_request(request, PR_INFO, policy)
        context = {
            **PR_RELATED_CONTEXT,
            "reviews": [
                {
                    "id": 4518030260,
                    "node_id": "PRR_kwDOTopLevelReview",
                    "html_url": "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#pullrequestreview-4518030260",
                    "state": "CHANGES_REQUESTED",
                    "body": "@sligo-droid please address the inline comments.",
                }
            ],
            "review_comments": [
                {
                    "id": 3430163991,
                    "node_id": "PRRC_kwDOInlineOne",
                    "pull_request_review_id": 4518030260,
                    "html_url": "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#discussion_r3430163991",
                    "path": "contracts/interfaces/IFolio.sol",
                    "line": 235,
                    "body": "remove",
                    "diff_hunk": "@@ line 235 @@",
                },
                {
                    "id": 3430163992,
                    "node_id": "PRRC_kwDOInlineTwo",
                    "pull_request_review_id": 4518030260,
                    "html_url": "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#discussion_r3430163992",
                    "path": "contracts/utils/FolioLib.sol",
                    "line": 59,
                    "body": "remove since `_validateFeeRecipients()` will catch if the total is above the max",
                    "diff_hunk": "@@ line 59 @@",
                }
            ],
        }
        artifact = build_pr_amend_intake_artifact(request, decision, policy, PR_INFO, payload, context)

        card = build_pr_amend_discord_card(artifact, artifact_path=tmp_path / "intake.json")

        body = card["body"]
        assert "GitHub review context:" in body
        assert "review_id=4518030260" in body
        assert "contracts/interfaces/IFolio.sol" in body
        assert "line 235" in body
        assert "Body: remove" in body
        assert "contracts/utils/FolioLib.sol" in body
        assert "line 59" in body
        assert "comment 3430163992" in body
        assert "discussion_r3430163992" in body
        assert "remove since `_validateFeeRecipients()` will catch if the total is above the max" in body
        assert card["github_pr_amend"]["source_kind"] == "review"
        assert card["github_pr_amend"]["source_id"] == "4518030260"
        assert card["github_pr_amend"]["source_node_id"] == payload["review"]["node_id"]
        assert card["project_context"]["github_pr_amend"]["source_kind"] == "review"
        assert card["project_context"]["github_pr_amend"]["source_id"] == "4518030260"
        assert card["project_context"]["github_pr_amend"]["source_node_id"] == payload["review"]["node_id"]
        assert card["project_context"]["github_pr_amend"]["source_key"] == "github-pr-amend:review:4518030260"
        assert card["project_context"]["github_pr_amend"]["reaction_targets"] == [
            {
                "repo": "reserve-protocol/reserve-index-dtf",
                "pr_number": "182",
                "source_kind": "review",
                "source_id": "4518030260",
                "source_node_id": "PRR_kwDOTopLevelReview",
                "source_url": "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#pullrequestreview-4518030260",
            },
            {
                "repo": "reserve-protocol/reserve-index-dtf",
                "pr_number": "182",
                "source_kind": "review_comment",
                "source_id": "3430163991",
                "source_node_id": "PRRC_kwDOInlineOne",
                "source_url": "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#discussion_r3430163991",
            },
            {
                "repo": "reserve-protocol/reserve-index-dtf",
                "pr_number": "182",
                "source_kind": "review_comment",
                "source_id": "3430163992",
                "source_node_id": "PRRC_kwDOInlineTwo",
                "source_url": "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#discussion_r3430163992",
            },
        ]
        assert card["project_context"]["github_pr_amend"]["requires_head_sha_advance"] is True
        assert any(
            "Address the triggering review request verbatim: @sligo-droid please address the inline comments."
            == criterion
            for criterion in card["acceptance_criteria"]
        )
        assert artifact["fetched_context"]["review_comments"] == context["review_comments"]

    def test_pr_amend_card_targets_head_repo_and_marks_upstream_context_only(self, tmp_path):
        request = extract_request("pull_request_review", REVIEW_PAYLOAD, delivery_id="delivery-target")
        policy = policy_from_route(ROUTE)
        decision = evaluate_request(request, PR_INFO, policy)
        artifact = build_pr_amend_intake_artifact(request, decision, policy, PR_INFO, REVIEW_PAYLOAD, PR_RELATED_CONTEXT)

        card = build_pr_amend_discord_card(artifact, artifact_path=tmp_path / "intake.json")

        body = card["body"]
        criteria = "\n".join(card["acceptance_criteria"])
        assert "Target checkout/repo" in body
        assert "`sligo-droid/reserve-index-dtf`" in body
        assert "Target base branch" in body
        assert "`feat/irrevocable-fee-recipients`" in body
        assert "review context only" in body
        assert "do not open, close, review, or merge that upstream PR" in body
        assert "Target repo for checkout/PR lifecycle is `sligo-droid/reserve-index-dtf`" in criteria
        assert "upstream `reserve-protocol/reserve-index-dtf` is review context only" in criteria
        assert "base `feat/irrevocable-fee-recipients`" in criteria
        assert card["project_context"]["github_pr_target_repo"] == "sligo-droid/reserve-index-dtf"
        assert card["project_context"]["github_pr_target_url"] == "https://github.com/sligo-droid/reserve-index-dtf.git"
        assert card["project_context"]["base_branch"] == "feat/irrevocable-fee-recipients"
        amend = card["project_context"]["github_pr_amend"]
        assert amend["upstream_repo"] == "reserve-protocol/reserve-index-dtf"
        assert amend["upstream_pr_number"] == "182"
        assert amend["head_repo"] == "sligo-droid/reserve-index-dtf"
        assert amend["head_ref"] == "feat/irrevocable-fee-recipients"
        assert amend["requires_head_sha_advance"] is True

    def test_review_card_includes_all_matching_inline_review_comments(self, tmp_path):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["review"]["id"] = 4518030260
        payload["review"]["body"] = "@sligo-droid please address every inline comment."
        request = extract_request("pull_request_review", payload, delivery_id="delivery-review-many")
        policy = policy_from_route(ROUTE)
        decision = evaluate_request(request, PR_INFO, policy)
        comments = [
            {
                "id": 3430163900 + index,
                "pull_request_review_id": 4518030260,
                "path": f"contracts/File{index}.sol",
                "line": index,
                "body": f"inline detail {index}",
            }
            for index in range(1, 31)
        ]
        artifact = build_pr_amend_intake_artifact(
            request,
            decision,
            policy,
            PR_INFO,
            payload,
            {**PR_RELATED_CONTEXT, "review_comments": comments},
        )

        card = build_pr_amend_discord_card(artifact, artifact_path=tmp_path / "intake.json")

        body = card["body"]
        assert "Inline review comments (30):" in body
        assert "contracts/File1.sol" in body
        assert "inline detail 1" in body
        assert "contracts/File30.sol" in body
        assert "inline detail 30" in body
        assert "additional inline review comments omitted" not in body

    def test_review_card_without_matching_inline_comments_omits_stale_pr_comments(self, tmp_path):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["review"]["id"] = 4534458851
        payload["review"]["html_url"] = (
            "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#pullrequestreview-4534458851"
        )
        payload["review"]["body"] = "New direction: use an array of FeeRecipient."
        request = extract_request("pull_request_review", payload, delivery_id="delivery-review-no-comments")
        policy = policy_from_route(ROUTE)
        decision = evaluate_request(request, PR_INFO, policy)
        context = {
            **PR_RELATED_CONTEXT,
            "reviews": [
                {
                    "id": 4534458851,
                    "state": "CHANGES_REQUESTED",
                    "body": "New direction: use an array of FeeRecipient.",
                }
            ],
            "review_comments": [
                {
                    "id": 3430163992,
                    "pull_request_review_id": 4518030260,
                    "html_url": "https://github.com/reserve-protocol/reserve-index-dtf/pull/182#discussion_r3430163992",
                    "path": "contracts/utils/FolioLib.sol",
                    "line": 59,
                    "body": "stale older review comment",
                }
            ],
        }
        artifact = build_pr_amend_intake_artifact(request, decision, policy, PR_INFO, payload, context)

        card = build_pr_amend_discord_card(artifact, artifact_path=tmp_path / "intake.json")

        body = card["body"]
        assert "Review 4534458851 (CHANGES_REQUESTED):" in body
        assert "Inline review comments: none found in fetched context" in body
        assert "stale older review comment" not in body
        assert "discussion_r3430163992" not in body

    def test_non_reserve_pr_amend_card_omits_solidity_skill_hint(self, tmp_path):
        payload = json.loads(json.dumps(ISSUE_COMMENT_PAYLOAD))
        payload["repository"]["full_name"] = "acme/webapp"
        pr_info = json.loads(json.dumps(PR_INFO))
        pr_info["base"]["repo"]["full_name"] = "acme/webapp"
        pr_info["head"]["repo"]["full_name"] = "sligo-droid/webapp"
        policy = GitHubPrAmendPolicy(allowed_base_repos=("acme/*",), canary_prs={})
        request = extract_request("issue_comment", payload, delivery_id="delivery-card")
        decision = evaluate_request(request, pr_info, policy)
        artifact = build_pr_amend_intake_artifact(request, decision, policy, pr_info, payload)

        card = build_pr_amend_discord_card(artifact, artifact_path=tmp_path / "intake.json")

        assert "worker_skill_hints" not in card["project_context"]
        worker = {"project_context": card["project_context"]}
        assert not any("reserve-solidity-style" in item for item in _planner_instructions(worker))

    def test_resolves_existing_discord_route_from_worker_board_metadata(self, monkeypatch):
        request = extract_request("pull_request_review", REVIEW_PAYLOAD, delivery_id="delivery-route")
        policy = policy_from_route(ROUTE)
        decision = evaluate_request(request, PR_INFO, policy)
        artifact = build_pr_amend_intake_artifact(request, decision, policy, PR_INFO, REVIEW_PAYLOAD)

        monkeypatch.setattr(
            kanban_db,
            "list_boards",
            lambda include_archived=False: [{"slug": "discord-thread-123"}],
        )
        monkeypatch.setattr(
            kanban_db,
            "read_board_metadata",
            lambda board: {
                "discord_worker": {
                    "thread_id": "thread-123",
                    "source_message_id": "user-request-msg",
                    "summary_message_id": "bot-summary-msg",
                    "parent_channel_id": "channel-123",
                    "guild_id": "guild-123",
                    "public_url": "https://workers.test/thread-123",
                    "project_context": {
                        "github_pr_target_repo": "sligo-droid/reserve-index-dtf",
                        "base_branch": "feat/irrevocable-fee-recipients",
                    },
                }
            },
        )

        route = resolve_pr_amend_existing_discord_route(artifact)

        assert route == {
            "discord_channel_id": "channel-123",
            "discord_top_level_message_id": "user-request-msg",
            "discord_thread_id": "thread-123",
            "discord_thread_url": "",
            "discord_board": "discord-thread-123",
            "discord_board_public_url": "https://workers.test/thread-123",
            "discord_guild_id": "guild-123",
            "discord_summary_message_id": "bot-summary-msg",
        }

    @pytest.mark.parametrize(
        ("terminal_metadata", "expected"),
        [
            ({"thread_state": "done"}, {}),
            ({"goal_status": "done"}, {}),
            ({"phase": "complete"}, {}),
            ({"cancelled": True}, {}),
            ({"thread_state": "active", "goal_status": "running", "phase": "active"}, {
                "discord_channel_id": "channel-123",
                "discord_top_level_message_id": "user-request-msg",
                "discord_thread_id": "done-thread",
                "discord_thread_url": "",
                "discord_board": "discord-done-thread",
                "discord_board_public_url": "",
                "discord_guild_id": "",
                "discord_summary_message_id": "",
            }),
        ],
    )
    def test_does_not_reuse_terminal_existing_discord_route(self, monkeypatch, terminal_metadata, expected):
        request = extract_request("pull_request_review", REVIEW_PAYLOAD, delivery_id="delivery-route")
        policy = policy_from_route(ROUTE)
        decision = evaluate_request(request, PR_INFO, policy)
        artifact = build_pr_amend_intake_artifact(request, decision, policy, PR_INFO, REVIEW_PAYLOAD)

        monkeypatch.setattr(
            kanban_db,
            "list_boards",
            lambda include_archived=False: [{"slug": "discord-done-thread"}],
        )
        worker_metadata = {
            **terminal_metadata,
            "thread_id": "done-thread",
            "source_message_id": "user-request-msg",
            "parent_channel_id": "channel-123",
            "project_context": {
                "github_pr_target_repo": "sligo-droid/reserve-index-dtf",
                "base_branch": "feat/irrevocable-fee-recipients",
            },
        }
        monkeypatch.setattr(
            kanban_db,
            "read_board_metadata",
            lambda board: {"discord_worker": worker_metadata},
        )

        assert resolve_pr_amend_existing_discord_route(artifact) == expected

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

    def test_fetch_pr_info_bridges_gh_config_without_token_env(self, monkeypatch, tmp_path):
        from hermes_cli import github_remote

        isolated_home = tmp_path / "hermes-home" / "home"
        real_gh_config = tmp_path / "real-home" / ".config" / "gh"
        real_gh_config.mkdir(parents=True)
        captured = {}
        monkeypatch.setenv("HOME", str(isolated_home))
        monkeypatch.setenv("GH_TOKEN", "gho_secret")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
        monkeypatch.delenv("GH_CONFIG_DIR", raising=False)
        monkeypatch.setattr(
            github_remote,
            "get_github_cli_config_dir",
            lambda env: str(real_gh_config)
            if env.get("HOME") == str(isolated_home)
            else "",
        )

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(PR_INFO),
                stderr="",
            )

        monkeypatch.setattr("gateway.github_pr_amend.subprocess.run", fake_run)

        assert fetch_pr_info("reserve-protocol/reserve-index-dtf", 182) == PR_INFO

        assert captured["cmd"] == [
            "gh",
            "api",
            "repos/reserve-protocol/reserve-index-dtf/pulls/182",
        ]
        env = captured["env"]
        assert env["GH_CONFIG_DIR"] == str(real_gh_config)
        assert "GH_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env


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

        argv = run.call_args_list[-1].args[0]
        assert endpoint in argv
        assert "content=eyes" in argv
        assert "Accept: application/vnd.github+json" in argv
        assert "X-GitHub-Api-Version: 2022-11-28" in argv

    @pytest.mark.asyncio
    async def test_github_pr_amend_review_reaction_uses_graphql_review_node(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        request = extract_request("pull_request_review", REVIEW_PAYLOAD)
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            joined = "\n".join(argv)
            if "reactionGroups" in joined:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "data": {
                                "node": {
                                    "reactionGroups": [
                                        {"content": "EYES", "viewerHasReacted": False},
                                        {"content": "ROCKET", "viewerHasReacted": False},
                                    ]
                                }
                            }
                        }
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"data": {}}), stderr="")

        with patch("gateway.platforms.webhook.subprocess.run", side_effect=fake_run):
            assert await adapter._add_github_pr_amend_reaction(request, "eyes") is True

        assert all("pulls/182/reviews/4900001/reactions" not in "\n".join(call) for call in calls)
        assert calls[0][:3] == ["gh", "api", "graphql"]
        assert f"id={REVIEW_PAYLOAD['review']['node_id']}" in calls[0]
        assert calls[1][:3] == ["gh", "api", "graphql"]
        assert f"subjectId={REVIEW_PAYLOAD['review']['node_id']}" in calls[1]
        assert "content=EYES" in calls[1]

    @pytest.mark.asyncio
    async def test_github_pr_amend_reaction_transition_deletes_prior_bot_status(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        request = extract_request("issue_comment", ISSUE_COMMENT_PAYLOAD)
        endpoint = "repos/reserve-protocol/reserve-index-dtf/issues/comments/4700001/reactions"
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["gh", "api"] and "-X" not in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        [
                            {"id": 1, "content": "eyes", "user": {"type": "Bot"}},
                            {"id": 2, "content": "eyes", "user": {"type": "User"}},
                            {"id": 3, "content": "heart", "user": {"type": "Bot"}},
                        ]
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

        with patch("gateway.platforms.webhook.subprocess.run", side_effect=fake_run):
            assert await adapter._add_github_pr_amend_reaction(request, "rocket") is True

        assert calls[0] == [
            "gh",
            "api",
            endpoint,
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
        ]
        assert calls[1][:5] == ["gh", "api", "-X", "DELETE", f"{endpoint}/1"]
        assert calls[2][0:4] == ["gh", "api", "-X", "POST"]
        assert f"{endpoint}/2" not in calls[1]
        assert all(f"{endpoint}/3" not in call for call in calls)

    @pytest.mark.asyncio
    async def test_github_pr_amend_terminal_success_deletes_active_status_and_adds_thumbs_up(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        request = extract_request("issue_comment", ISSUE_COMMENT_PAYLOAD)
        endpoint = "repos/reserve-protocol/reserve-index-dtf/issues/comments/4700001/reactions"
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["gh", "api"] and "-X" not in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        [
                            {"id": 1, "content": "eyes", "user": {"type": "Bot"}},
                            {"id": 2, "content": "rocket", "user": {"type": "Bot"}},
                            {"id": 3, "content": "-1", "user": {"type": "Bot"}},
                        ]
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

        with patch("gateway.platforms.webhook.subprocess.run", side_effect=fake_run):
            assert await adapter._add_github_pr_amend_reaction(request, "+1") is True

        assert calls[1][:5] == ["gh", "api", "-X", "DELETE", f"{endpoint}/1"]
        assert calls[2][:5] == ["gh", "api", "-X", "DELETE", f"{endpoint}/2"]
        assert calls[3][:5] == ["gh", "api", "-X", "DELETE", f"{endpoint}/3"]
        assert calls[4][0:4] == ["gh", "api", "-X", "POST"]
        assert "content=+1" in calls[4]

    @pytest.mark.asyncio
    async def test_github_pr_amend_terminal_failure_deletes_done_status_and_adds_thumbs_down(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        request = extract_request("pull_request_review_comment", REVIEW_COMMENT_PAYLOAD)
        endpoint = "repos/reserve-protocol/reserve-index-dtf/pulls/comments/4800001/reactions"
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["gh", "api"] and "-X" not in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        [
                            {"id": 7, "content": "+1", "user": {"type": "Bot"}},
                            {"id": 8, "content": "rocket", "user": {"type": "Bot"}},
                        ]
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

        with patch("gateway.platforms.webhook.subprocess.run", side_effect=fake_run):
            assert await adapter._add_github_pr_amend_reaction(request, "-1") is True

        assert calls[1][:5] == ["gh", "api", "-X", "DELETE", f"{endpoint}/7"]
        assert calls[2][:5] == ["gh", "api", "-X", "DELETE", f"{endpoint}/8"]
        assert calls[3][0:4] == ["gh", "api", "-X", "POST"]
        assert "content=-1" in calls[3]

    @pytest.mark.asyncio
    async def test_github_pr_amend_review_reaction_transition_uses_graphql_viewer_reactions(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        request = extract_request("pull_request_review", REVIEW_PAYLOAD)
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            joined = "\n".join(argv)
            if "reactionGroups" in joined:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "data": {
                                "node": {
                                    "reactionGroups": [
                                        {"content": "EYES", "viewerHasReacted": True},
                                        {"content": "ROCKET", "viewerHasReacted": False},
                                        {"content": "THUMBS_DOWN", "viewerHasReacted": False},
                                    ]
                                }
                            }
                        }
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"data": {}}), stderr="")

        with patch("gateway.platforms.webhook.subprocess.run", side_effect=fake_run):
            assert await adapter._add_github_pr_amend_reaction(request, "rocket") is True

        assert len(calls) == 3
        assert "reactionGroups" in "\n".join(calls[0])
        assert "removeReaction" in "\n".join(calls[1])
        assert f"subjectId={REVIEW_PAYLOAD['review']['node_id']}" in calls[1]
        assert "content=EYES" in calls[1]
        assert "addReaction" in "\n".join(calls[2])
        assert "content=ROCKET" in calls[2]

    @pytest.mark.asyncio
    async def test_github_pr_amend_review_terminal_reaction_uses_graphql_node(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        request = extract_request("pull_request_review", REVIEW_PAYLOAD)
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            joined = "\n".join(argv)
            if "reactionGroups" in joined:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "data": {
                                "node": {
                                    "reactionGroups": [
                                        {"content": "ROCKET", "viewerHasReacted": True},
                                        {"content": "THUMBS_DOWN", "viewerHasReacted": True},
                                        {"content": "THUMBS_UP", "viewerHasReacted": False},
                                    ]
                                }
                            }
                        }
                    ),
                    stderr="",
                )
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"data": {}}), stderr="")

        with patch("gateway.platforms.webhook.subprocess.run", side_effect=fake_run):
            assert await adapter._add_github_pr_amend_reaction(request, "+1") is True

        assert len(calls) == 4
        assert "reactionGroups" in "\n".join(calls[0])
        assert "removeReaction" in "\n".join(calls[1])
        assert "content=ROCKET" in calls[1]
        assert "removeReaction" in "\n".join(calls[2])
        assert "content=THUMBS_DOWN" in calls[2]
        assert "addReaction" in "\n".join(calls[3])
        assert f"subjectId={REVIEW_PAYLOAD['review']['node_id']}" in calls[3]
        assert "content=THUMBS_UP" in calls[3]

    def test_github_pr_amend_missing_existing_thread_can_route_when_channel_resolves(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})

        assert (
            adapter._github_pr_amend_degraded_reason(
                source_kind="review_comment",
                channel_id="channel-123",
                existing_route={},
            )
            == ""
        )
        assert (
            adapter._github_pr_amend_degraded_reason(
                source_kind="review",
                channel_id="channel-123",
                existing_route={},
            )
            == ""
        )

    @pytest.mark.asyncio
    async def test_github_pr_amend_terminal_reaction_uses_review_and_comment_targets(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock(return_value=True)
        metadata = {
            "repo": "reserve-protocol/reserve-index-dtf",
            "pr_number": "181",
            "source_kind": "review",
            "source_id": "4534464625",
            "source_node_id": "PRR_kwDOReviewSummary",
            "reaction_targets": [
                {
                    "source_kind": "review",
                    "source_id": "4534464625",
                    "source_node_id": "PRR_kwDOReviewSummary",
                },
                {
                    "source_kind": "review_comment",
                    "source_id": "3443862311",
                    "source_node_id": "PRRC_kwDOInlineComment",
                }
            ],
        }

        assert await adapter.sync_github_pr_amend_terminal_reaction(metadata, "done") is True

        assert adapter._add_github_pr_amend_reaction.await_count == 2
        first_request, first_content = adapter._add_github_pr_amend_reaction.await_args_list[0].args
        second_request, second_content = adapter._add_github_pr_amend_reaction.await_args_list[1].args
        assert first_content == "+1"
        assert first_request.source_kind == "review"
        assert first_request.source_id == "4534464625"
        assert first_request.source_node_id == "PRR_kwDOReviewSummary"
        assert second_content == "+1"
        assert second_request.source_kind == "review_comment"
        assert second_request.source_id == "3443862311"

    @pytest.mark.asyncio
    async def test_github_pr_amend_terminal_blocked_does_not_add_thumbs_down(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock(return_value=True)
        metadata = {
            "repo": "reserve-protocol/reserve-index-dtf",
            "pr_number": "182",
            "source_kind": "review_comment",
            "source_id": "3459980220",
            "source_node_id": "PRRC_kwDOChildInlineComment",
            "reaction_targets": [
                {
                    "source_kind": "review_comment",
                    "source_id": "3459980220",
                    "source_node_id": "PRRC_kwDOChildInlineComment",
                },
            ],
        }

        assert await adapter.sync_github_pr_amend_terminal_reaction(metadata, "blocked") is True

        adapter._add_github_pr_amend_reaction.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_github_pr_amend_terminal_error_targets_trigger_only_not_child_comments(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock(return_value=True)
        metadata = {
            "repo": "reserve-protocol/reserve-index-dtf",
            "pr_number": "182",
            "source_kind": "review",
            "source_id": "4553549454",
            "source_node_id": "PRR_kwDOTopLevelReview",
            "reaction_targets": [
                {
                    "source_kind": "review",
                    "source_id": "4553549454",
                    "source_node_id": "PRR_kwDOTopLevelReview",
                },
                {
                    "source_kind": "review_comment",
                    "source_id": "3459970539",
                    "source_node_id": "PRRC_kwDOParentInlineComment",
                },
                {
                    "source_kind": "review_comment",
                    "source_id": "3459980220",
                    "source_node_id": "PRRC_kwDOChildInlineComment",
                },
            ],
        }

        assert await adapter.sync_github_pr_amend_terminal_reaction(metadata, "errored") is True

        adapter._add_github_pr_amend_reaction.assert_awaited_once()
        request, content = adapter._add_github_pr_amend_reaction.await_args.args
        assert content == "-1"
        assert request.source_kind == "review"
        assert request.source_id == "4553549454"
        assert request.source_node_id == "PRR_kwDOTopLevelReview"

    @pytest.mark.asyncio
    async def test_github_pr_amend_enqueue_failure_targets_trigger_only_not_child_comments(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock(return_value=True)
        fallback = extract_request("pull_request_review_comment", REVIEW_COMMENT_PAYLOAD)
        metadata = {
            "repo": "reserve-protocol/reserve-index-dtf",
            "pr_number": "182",
            "source_kind": "review_comment",
            "source_id": "4800001",
            "source_node_id": "PRRC_kwDOReviewComment",
            "reaction_targets": [
                {
                    "source_kind": "review_comment",
                    "source_id": "4800001",
                    "source_node_id": "PRRC_kwDOReviewComment",
                },
                {
                    "source_kind": "review_comment",
                    "source_id": "4800002",
                    "source_node_id": "PRRC_kwDOFetchedInlineComment",
                },
            ],
        }

        await adapter._safe_github_pr_amend_reactions(metadata, fallback, "-1")

        adapter._add_github_pr_amend_reaction.assert_awaited_once()
        request, content = adapter._add_github_pr_amend_reaction.await_args.args
        assert content == "-1"
        assert request.source_kind == "review_comment"
        assert request.source_id == "4800001"
        assert request.source_node_id == "PRRC_kwDOReviewComment"

    @pytest.mark.asyncio
    async def test_github_pr_amend_terminal_success_removes_stale_child_thumbs_down(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        parent_endpoint = "repos/reserve-protocol/reserve-index-dtf/pulls/comments/3459970539/reactions"
        child_endpoint = "repos/reserve-protocol/reserve-index-dtf/pulls/comments/3459980220/reactions"
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["gh", "api"] and "-X" not in argv:
                if parent_endpoint in argv:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            [{"id": 405396274, "content": "-1", "user": {"login": "sligo-droid"}}]
                        ),
                        stderr="",
                    )
                if child_endpoint in argv:
                    return subprocess.CompletedProcess(
                        argv,
                        0,
                        stdout=json.dumps(
                            [{"id": 405396275, "content": "-1", "user": {"login": "sligo-droid"}}]
                        ),
                        stderr="",
                    )
            if argv == ["gh", "api", "user"]:
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"login": "sligo-droid"}), stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

        metadata = {
            "repo": "reserve-protocol/reserve-index-dtf",
            "pr_number": "182",
            "source_kind": "review_comment",
            "source_id": "3459970539",
            "source_node_id": "PRRC_kwDOParentInlineComment",
            "reaction_targets": [
                {
                    "source_kind": "review_comment",
                    "source_id": "3459970539",
                    "source_node_id": "PRRC_kwDOParentInlineComment",
                },
                {
                    "source_kind": "review_comment",
                    "source_id": "3459980220",
                    "source_node_id": "PRRC_kwDOChildInlineComment",
                },
            ],
        }

        with patch("gateway.platforms.webhook.subprocess.run", side_effect=fake_run):
            assert await adapter.sync_github_pr_amend_terminal_reaction(metadata, "done") is True

        delete_calls = [call for call in calls if call[:4] == ["gh", "api", "-X", "DELETE"]]
        post_calls = [call for call in calls if call[:4] == ["gh", "api", "-X", "POST"]]
        assert [call[:5] for call in delete_calls] == [
            ["gh", "api", "-X", "DELETE", f"{parent_endpoint}/405396274"],
            ["gh", "api", "-X", "DELETE", f"{child_endpoint}/405396275"],
        ]
        assert [call[4] for call in post_calls] == [parent_endpoint, child_endpoint]
        assert all("content=+1" in call for call in post_calls)

    @pytest.mark.asyncio
    async def test_github_pr_amend_reaction_transition_deletes_authenticated_user_status(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        request = extract_request("issue_comment", ISSUE_COMMENT_PAYLOAD)
        endpoint = "repos/reserve-protocol/reserve-index-dtf/issues/comments/4700001/reactions"
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv == ["gh", "api", endpoint, "-H", "Accept: application/vnd.github+json", "-H", "X-GitHub-Api-Version: 2022-11-28"]:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        [
                            {"id": 10, "content": "eyes", "user": {"type": "User", "login": "sligo-droid"}},
                            {"id": 11, "content": "eyes", "user": {"type": "User", "login": "tbrent"}},
                            {"id": 12, "content": "rocket", "user": {"type": "Bot", "login": "third-party-bot"}},
                        ]
                    ),
                    stderr="",
                )
            if argv == ["gh", "api", "user"]:
                return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"login": "sligo-droid"}), stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

        with patch("gateway.platforms.webhook.subprocess.run", side_effect=fake_run):
            assert await adapter._add_github_pr_amend_reaction(request, "rocket") is True

        assert [call[:5] for call in calls if len(call) >= 5 and call[0:4] == ["gh", "api", "-X", "DELETE"]] == [
            ["gh", "api", "-X", "DELETE", f"{endpoint}/10"]
        ]
        assert calls[-1][0:4] == ["gh", "api", "-X", "POST"]
        assert "content=rocket" in calls[-1]

    @pytest.mark.asyncio
    async def test_github_pr_amend_reaction_list_failure_still_posts(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        request = extract_request("issue_comment", ISSUE_COMMENT_PAYLOAD)
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[:2] == ["gh", "api"] and "-X" not in argv:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="nope")
            return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

        with patch("gateway.platforms.webhook.subprocess.run", side_effect=fake_run):
            assert await adapter._add_github_pr_amend_reaction(request, "rocket") is True

        assert len(calls) == 2
        assert calls[-1][0:4] == ["gh", "api", "-X", "POST"]

    @pytest.mark.asyncio
    async def test_signed_changes_requested_review_routes_to_discord_worker_board(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        secret = "route-secret"
        route = json.loads(json.dumps(ROUTE))
        route["secret"] = secret
        route["github_pr_amend"]["discord_channel_id"] = "channel-123"
        adapter = _make_adapter({"github-pr-amend": route})
        adapter._add_github_pr_amend_reaction = AsyncMock(return_value=True)

        body = json.dumps(REVIEW_PAYLOAD).encode()
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
                        "X-GitHub-Event": "pull_request_review",
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
        assert publish.call_args.kwargs["existing"] is None
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
    async def test_changes_requested_review_comment_routes_to_discord_worker_board(self, tmp_path):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock(return_value=True)

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO), patch(
            "gateway.github_pr_amend.fetch_review_info", return_value={"state": "CHANGES_REQUESTED"}
        ) as fetch_review, patch(
            "gateway.github_pr_amend.fetch_pr_related_context", return_value=PR_RELATED_CONTEXT
        ), patch(
            "gateway.github_pr_amend.resolve_pr_amend_discord_channel", return_value="channel-123"
        ), patch(
            "gateway.github_pr_amend.publish_and_activate_pr_amend_intake",
            return_value={"discord_board": "board", "discord_thread_id": "thread"},
        ) as publish:
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=REVIEW_COMMENT_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "pull_request_review_comment",
                        "X-GitHub-Delivery": "delivery-review-comment",
                    },
                )
                data = await resp.json()

        assert resp.status == 202
        assert data["status"] == "queued"
        fetch_review.assert_called_once_with(
            "reserve-protocol/reserve-index-dtf",
            182,
            "123",
        )
        publish.assert_called_once()
        card = publish.call_args.args[0]
        assert card["project_context"]["github_pr_amend"]["review_state"] == "CHANGES_REQUESTED"
        assert [call.args[1] for call in adapter._add_github_pr_amend_reaction.await_args_list] == [
            "eyes",
            "rocket",
        ]

    @pytest.mark.asyncio
    async def test_commented_review_comment_is_ignored_after_parent_review_lookup(self):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock()

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO), patch(
            "gateway.github_pr_amend.fetch_review_info", return_value={"state": "COMMENTED"}
        ), patch("gateway.github_pr_amend.fetch_pr_related_context") as fetch_related:
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=REVIEW_COMMENT_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "pull_request_review_comment",
                        "X-GitHub-Delivery": "delivery-commented-review-comment",
                    },
                )
                data = await resp.json()

        assert resp.status == 200
        assert data["status"] == "ignored"
        assert data["reason"] == "parent review state 'COMMENTED' is not CHANGES_REQUESTED"
        fetch_related.assert_not_called()
        adapter._add_github_pr_amend_reaction.assert_not_called()

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
    async def test_non_tbrent_review_missing_mention_is_ignored_before_pr_lookup(self):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["sender"]["login"] = "stranger"
        payload["pull_request"].pop("user")
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
                        "X-GitHub-Delivery": "delivery-review-stranger-implicit",
                    },
                )
                data = await resp.json()

        assert resp.status == 200
        assert data["status"] == "ignored"
        assert data["reason"] == "sender 'stranger' is not allowlisted"
        fetch_pr_info.assert_not_called()
        adapter._add_github_pr_amend_reaction.assert_not_called()

    @pytest.mark.asyncio
    async def test_issue_comment_is_ignored_before_pr_lookup(self):
        payload = json.loads(json.dumps(ISSUE_COMMENT_PAYLOAD))
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock()

        with patch("gateway.github_pr_amend.fetch_pr_info") as fetch_pr_info:
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=payload,
                    headers={
                        "X-GitHub-Event": "issue_comment",
                        "X-GitHub-Delivery": "delivery-issue-comment",
                    },
                )
                data = await resp.json()

        assert resp.status == 200
        assert data["status"] == "ignored"
        assert data["reason"] == "issue_comment is not a changes-requested review signal"
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
                    json=REVIEW_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "pull_request_review",
                        "X-GitHub-Delivery": "delivery-reaction-failed",
                    },
                )
                data = await resp.json()

        assert resp.status == 202
        assert data["status"] == "queued"

    @pytest.mark.asyncio
    async def test_unresolved_discord_channel_degrades_before_worker_board_publish(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock(return_value=True)

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO), patch(
            "gateway.github_pr_amend.fetch_pr_related_context", return_value=PR_RELATED_CONTEXT
        ), patch(
            "gateway.github_pr_amend.resolve_pr_amend_discord_channel", return_value=""
        ), patch("gateway.github_pr_amend.publish_and_activate_pr_amend_intake") as publish:
            async with TestClient(TestServer(_create_app(adapter))) as cli:
                resp = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=REVIEW_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "pull_request_review",
                        "X-GitHub-Delivery": "delivery-no-channel",
                    },
                )
                data = await resp.json()

        assert resp.status == 202
        assert data["status"] == "degraded"
        assert data["reason"] == "missing_discord_route"
        assert data["discord_dispatch"] == "skipped"
        assert Path(data["artifact_path"]).is_file()
        assert adapter._add_github_pr_amend_reaction.await_args.args[1] == "-1"
        assert data["lock_key"] not in adapter._github_pr_amend_locks
        publish.assert_not_called()

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
                    json=REVIEW_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "pull_request_review",
                        "X-GitHub-Delivery": "delivery-locked",
                    },
                )
                data = await resp.json()

        assert resp.status == 409
        assert data["status"] == "locked"

    @pytest.mark.asyncio
    async def test_review_submitted_changes_requested_is_accepted(self, tmp_path):
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        existing = {
            "discord_channel_id": "channel-123",
            "discord_top_level_message_id": "msg-123",
            "discord_thread_id": "thread-123",
            "discord_board": "board",
        }

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO), patch(
            "gateway.github_pr_amend.fetch_pr_related_context", return_value=PR_RELATED_CONTEXT
        ), patch(
            "gateway.github_pr_amend.resolve_pr_amend_discord_channel", return_value="channel-123"
        ), patch(
            "gateway.github_pr_amend.resolve_pr_amend_existing_discord_route", return_value=existing
        ), patch(
            "gateway.github_pr_amend.publish_and_activate_pr_amend_intake",
            return_value={"discord_board": "board", "discord_thread_id": "thread"},
        ) as publish:
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
        assert publish.call_args.kwargs["existing"] == existing

    @pytest.mark.asyncio
    async def test_review_without_original_discord_thread_routes_to_project_channel(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
        adapter = _make_adapter({"github-pr-amend": ROUTE})
        adapter._add_github_pr_amend_reaction = AsyncMock(return_value=True)
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["review"]["body"] = ""

        async def immediate_to_thread(func, /, *args, **kwargs):
            return func(*args, **kwargs)

        monkeypatch.setattr("gateway.platforms.webhook.asyncio.to_thread", immediate_to_thread)

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO), patch(
            "gateway.github_pr_amend.fetch_pr_related_context", return_value=PR_RELATED_CONTEXT
        ), patch(
            "gateway.github_pr_amend.resolve_pr_amend_discord_channel", return_value="channel-123"
        ), patch(
            "gateway.github_pr_amend.resolve_pr_amend_existing_discord_route", return_value={}
        ), patch(
            "gateway.github_pr_amend.publish_and_activate_pr_amend_intake",
            return_value={"discord_board": "board", "discord_thread_id": "thread"},
        ) as publish:
            resp = await adapter._handle_github_pr_amend(
                route_name="github-pr-amend",
                route_config=ROUTE,
                payload=payload,
                event_type="pull_request_review",
                delivery_id="delivery-review-no-thread",
            )
            data = json.loads(resp.text)

        assert resp.status == 202
        assert data["status"] == "queued"
        assert Path(data["artifact_path"]).is_file()
        publish.assert_called_once()
        assert publish.call_args.kwargs["channel_id"] == "channel-123"
        assert publish.call_args.kwargs["existing"] is None
        assert [call.args[1] for call in adapter._add_github_pr_amend_reaction.await_args_list] == [
            "eyes",
            "rocket",
        ]

    @pytest.mark.asyncio
    async def test_review_submitted_missing_mention_on_sligo_droid_authored_pr_is_accepted(self, tmp_path):
        payload = json.loads(json.dumps(REVIEW_PAYLOAD))
        payload["pull_request"].pop("user")
        payload["review"]["body"] = "please address the requested changes."
        adapter = _make_adapter({"github-pr-amend": ROUTE})

        existing = {
            "discord_channel_id": "channel-123",
            "discord_top_level_message_id": "msg-123",
            "discord_thread_id": "thread-123",
            "discord_board": "board",
        }

        with patch("gateway.github_pr_amend.fetch_pr_info", return_value=PR_INFO), patch(
            "gateway.github_pr_amend.fetch_pr_related_context", return_value=PR_RELATED_CONTEXT
        ), patch(
            "gateway.github_pr_amend.resolve_pr_amend_discord_channel", return_value="channel-123"
        ), patch(
            "gateway.github_pr_amend.resolve_pr_amend_existing_discord_route", return_value=existing
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
                    json=REVIEW_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "pull_request_review",
                        "X-GitHub-Delivery": "delivery-retryable",
                    },
                )
                first_data = await first.json()
                second = await cli.post(
                    "/webhooks/github-pr-amend",
                    json=REVIEW_PAYLOAD,
                    headers={
                        "X-GitHub-Event": "pull_request_review",
                        "X-GitHub-Delivery": "delivery-retryable",
                    },
                )
                second_data = await second.json()

        assert first.status == 502
        assert first_data["status"] == "error"
        assert second.status == 202
        assert second_data["status"] == "queued"
