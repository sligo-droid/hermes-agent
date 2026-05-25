from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "notify_discord_main_commits.py"
    spec = importlib.util.spec_from_file_location("notify_discord_main_commits", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _commit(index: int, message: str | None = None) -> dict:
    sha = f"{index:040x}"
    return {
        "id": sha,
        "message": message or f"feat: add thing {index}",
        "url": f"https://github.com/NousResearch/hermes-agent/commit/{sha}",
        "timestamp": "2026-05-21T12:00:00Z",
        "author": {"name": f"Author {index}"},
    }


def _event(commits: list[dict], **overrides) -> dict:
    event = {
        "ref": "refs/heads/main",
        "deleted": False,
        "repository": {
            "full_name": "NousResearch/hermes-agent",
            "html_url": "https://github.com/NousResearch/hermes-agent",
        },
        "pusher": {"name": "sligo-droid"},
        "commits": commits,
    }
    event.update(overrides)
    return event


def test_single_commit_payload_links_embed_and_disables_mentions():
    mod = _load_module()

    payloads = mod.build_webhook_payloads(_event([_commit(1, "feat: notify Discord logs")]))

    assert len(payloads) == 1
    payload = payloads[0]
    assert payload["allowed_mentions"] == {"parse": []}
    assert "content" not in payload
    assert len(payload["embeds"]) == 1
    embed = payload["embeds"][0]
    assert embed["title"] == "0000000 pushed to main"
    assert embed["description"] == "feat: notify Discord logs"
    assert embed["url"] == "https://github.com/NousResearch/hermes-agent/commit/0000000000000000000000000000000000000001"
    assert {"name": "Pusher", "value": "sligo-droid", "inline": True} in embed["fields"]


def test_multi_commit_payloads_are_chunked_at_discord_embed_limit():
    mod = _load_module()

    payloads = mod.build_webhook_payloads(_event([_commit(i) for i in range(25)]))

    assert [len(payload["embeds"]) for payload in payloads] == [10, 10, 5]
    assert sum(len(payload["embeds"]) for payload in payloads) == 25


def test_merge_commit_description_prefers_meaningful_body_line():
    mod = _load_module()
    merge_message = "Merge pull request #123 from user/branch\n\nAdd main commit notifications\n\nMore detail"

    payload = mod.build_webhook_payloads(_event([_commit(2, merge_message)]))[0]

    assert payload["embeds"][0]["description"] == "Add main commit notifications"


def test_generic_merge_commit_uses_associated_pr_body_and_field():
    mod = _load_module()
    commit = _commit(4, "Merge remote-tracking branch 'origin/main'")
    pull_request = {
        "number": 81,
        "title": "Show worker run profile on tickets",
        "body": "Expose model, sandbox, and runtime settings on worker tickets.",
        "html_url": "https://github.com/NousResearch/hermes-agent/pull/81",
    }

    payload = mod.build_webhook_payloads(
        _event([commit]),
        pull_request_lookup=lambda _sha: pull_request,
    )[0]

    embed = payload["embeds"][0]
    assert embed["title"] == "Show worker run profile on tickets"
    assert embed["url"] == "https://github.com/NousResearch/hermes-agent/pull/81"
    assert embed["description"] == (
        "Expose model, sandbox, and runtime settings on worker tickets."
    )
    assert {
        "name": "Pull Request",
        "value": (
            "[#81 Show worker run profile on tickets]"
            "(https://github.com/NousResearch/hermes-agent/pull/81)"
        ),
        "inline": False,
    } in embed["fields"]


def test_non_generic_pr_associated_commit_uses_pr_body_and_field():
    mod = _load_module()
    commit = _commit(8, "feat: improve main log notifications")
    pull_request = {
        "number": 83,
        "title": "Improve main log notifications",
        "body": "Use PR details for squash and rebase merges.",
        "html_url": "https://github.com/NousResearch/hermes-agent/pull/83",
    }

    payload = mod.build_webhook_payloads(
        _event([commit]),
        pull_request_lookup=lambda _sha: pull_request,
    )[0]

    embed = payload["embeds"][0]
    assert embed["title"] == "Improve main log notifications"
    assert embed["url"] == "https://github.com/NousResearch/hermes-agent/pull/83"
    assert embed["description"] == "Use PR details for squash and rebase merges."
    assert {
        "name": "Pull Request",
        "value": (
            "[#83 Improve main log notifications]"
            "(https://github.com/NousResearch/hermes-agent/pull/83)"
        ),
        "inline": False,
    } in embed["fields"]


def test_squash_merge_message_falls_back_to_pr_number_lookup():
    mod = _load_module()
    commit = _commit(30, "fix: improve Discord logs (#130)")
    pull_request = {
        "number": 130,
        "title": "Improve Discord logs",
        "body": "Post merged PRs even when commit association has not indexed yet.",
        "html_url": "https://github.com/NousResearch/hermes-agent/pull/130",
    }
    seen_sha = []
    seen_number = []

    payload = mod.build_webhook_payloads(
        _event([commit]),
        pull_request_lookup=lambda sha: seen_sha.append(sha) or None,
        pull_request_number_lookup=lambda number: seen_number.append(number) or pull_request,
    )[0]

    assert seen_sha == [commit["id"]]
    assert seen_number == [130]
    embed = payload["embeds"][0]
    assert embed["title"] == "Improve Discord logs"
    assert embed["url"] == "https://github.com/NousResearch/hermes-agent/pull/130"
    assert embed["description"] == (
        "Post merged PRs even when commit association has not indexed yet."
    )


def test_merge_pull_request_message_falls_back_to_pr_number_lookup():
    mod = _load_module()
    commit = _commit(31, "Merge pull request #131 from sligo-droid/fix/logs")
    pull_request = {
        "number": 131,
        "title": "Catch merged PR logs",
        "body": "",
        "html_url": "https://github.com/NousResearch/hermes-agent/pull/131",
    }
    seen_number = []

    payload = mod.build_webhook_payloads(
        _event([commit]),
        pull_request_lookup=lambda _sha: None,
        pull_request_number_lookup=lambda number: seen_number.append(number) or pull_request,
    )[0]

    assert seen_number == [131]
    assert payload["embeds"][0]["title"] == "Catch merged PR logs"


def test_pr_description_strips_tests_section():
    mod = _load_module()
    commit = _commit(7, "Merge pull request #82 from sligo-droid/example")
    pull_request = {
        "number": 82,
        "title": "Link Discord feature summary branch field",
        "body": (
            "Summary\n"
            "render the Discord feature-summary Branch field as a GitHub branch link\n"
            "remove the separate Feature Branch URL embed field\n"
            "\n"
            "Tests\n"
            "./scripts/run_tests.sh tests/gateway/test_discord_summaries.py"
        ),
        "html_url": "https://github.com/NousResearch/hermes-agent/pull/82",
    }

    payload = mod.build_webhook_payloads(
        _event([commit]),
        pull_request_lookup=lambda _sha: pull_request,
    )[0]

    description = payload["embeds"][0]["description"]
    assert "render the Discord feature-summary Branch field" in description
    assert "Feature Branch URL" in description
    assert "Tests" not in description
    assert "run_tests.sh" not in description


def test_pr_merge_push_suppresses_intermediate_commits_from_same_payload():
    mod = _load_module()
    commits = [
        _commit(10, "fix: link Discord summary branch field"),
        _commit(11, "Merge branch 'main' into fix/discord-summary-branch-link"),
        _commit(
            12,
            "Merge pull request #82 from sligo-droid/fix/discord-summary-branch-link\n\n"
            "Link Discord feature summary branch field",
        ),
    ]
    pull_request = {
        "number": 82,
        "title": "Link Discord feature summary branch field",
        "body": "Summary\nUse the PR body once.",
        "html_url": "https://github.com/NousResearch/hermes-agent/pull/82",
    }

    payloads = mod.build_webhook_payloads(
        _event(commits),
        pull_request_lookup=lambda _sha: pull_request,
    )

    assert len(payloads) == 1
    assert len(payloads[0]["embeds"]) == 1
    embed = payloads[0]["embeds"][0]
    assert embed["title"] == "Link Discord feature summary branch field"
    assert embed["url"] == "https://github.com/NousResearch/hermes-agent/pull/82"
    assert embed["description"] == "Summary\nUse the PR body once."
    assert "content" not in payloads[0]


def test_direct_multi_commit_push_still_posts_each_commit():
    mod = _load_module()

    payloads = mod.build_webhook_payloads(
        _event([
            _commit(20, "feat: direct one"),
            _commit(21, "fix: direct two"),
        ])
    )

    assert len(payloads) == 1
    assert [embed["description"] for embed in payloads[0]["embeds"]] == [
        "feat: direct one",
        "fix: direct two",
    ]


def test_generic_merge_commit_falls_back_to_pr_title_when_body_empty():
    mod = _load_module()
    commit = _commit(5, "Merge pull request #82 from sligo-droid/example")
    pull_request = {
        "number": 82,
        "title": "Improve Discord commit messages",
        "body": "",
        "html_url": "https://github.com/NousResearch/hermes-agent/pull/82",
    }

    payload = mod.build_webhook_payloads(
        _event([commit]),
        pull_request_lookup=lambda _sha: pull_request,
    )[0]

    assert payload["embeds"][0]["description"] == "Improve Discord commit messages"


def test_pr_lookup_mode_skips_commit_when_no_pr_is_associated():
    mod = _load_module()
    commit = _commit(6, "feat: direct commit detail")
    seen = []

    def pull_request_lookup(sha):
        seen.append(sha)
        return None

    payloads = mod.build_webhook_payloads(
        _event([commit]),
        pull_request_lookup=pull_request_lookup,
    )

    assert seen == [commit["id"]]
    assert payloads == []


def test_deleted_branch_and_empty_commits_are_noops():
    mod = _load_module()

    assert mod.build_webhook_payloads(_event([_commit(1)], deleted=True)) == []
    assert mod.build_webhook_payloads(_event([])) == []


def test_commit_url_falls_back_to_repository_html_url():
    mod = _load_module()
    commit = _commit(3)
    commit.pop("url")

    payload = mod.build_webhook_payloads(_event([commit]))[0]

    assert payload["embeds"][0]["url"] == (
        "https://github.com/NousResearch/hermes-agent/commit/"
        "0000000000000000000000000000000000000003"
    )


def test_main_requires_webhook_url(tmp_path, monkeypatch, capsys):
    mod = _load_module()
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(_event([_commit(1)])), encoding="utf-8")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.delenv("DISCORD_LOGS_WEBHOOK_URL", raising=False)

    assert mod.main() == 2
    assert "DISCORD_LOGS_WEBHOOK_URL is required" in capsys.readouterr().err


def test_post_payload_raises_on_non_2xx_response(monkeypatch):
    mod = _load_module()

    class FakeResponse:
        status = 500

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"bad webhook"

    monkeypatch.setattr(mod.request, "urlopen", lambda *_args, **_kwargs: FakeResponse())

    with pytest.raises(RuntimeError, match="HTTP 500"):
        mod.post_payload("https://discord.example/webhook", {"embeds": []})


def test_fetch_pull_request_for_commit_returns_first_pr(monkeypatch):
    mod = _load_module()
    seen = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps([{"number": 84, "body": "PR detail"}]).encode()

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["headers"] = dict(req.header_items())
        seen["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(mod.request, "urlopen", fake_urlopen)

    pr = mod.fetch_pull_request_for_commit(
        "NousResearch/hermes-agent",
        "abc123",
        "token",
    )

    assert pr == {"number": 84, "body": "PR detail"}
    assert seen["url"].endswith("/repos/NousResearch/hermes-agent/commits/abc123/pulls")
    assert seen["headers"]["Authorization"] == "Bearer token"
    assert seen["headers"]["Accept"] == "application/vnd.github+json"
    assert seen["timeout"] == 15


def test_main_posts_all_payload_chunks(tmp_path, monkeypatch):
    mod = _load_module()
    event_path = tmp_path / "event.json"
    event_path.write_text(json.dumps(_event([_commit(i) for i in range(11)])), encoding="utf-8")
    posted = []

    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event_path))
    monkeypatch.setenv("DISCORD_LOGS_WEBHOOK_URL", "https://discord.example/webhook")
    monkeypatch.setattr(mod, "post_payload", lambda url, payload: posted.append((url, payload)))

    assert mod.main() == 0
    assert [len(payload["embeds"]) for _url, payload in posted] == [10, 1]
    assert {url for url, _payload in posted} == {"https://discord.example/webhook"}
