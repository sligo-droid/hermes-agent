"""Focused post-fetch and truncation robustness regressions."""

from __future__ import annotations

import asyncio
import json

from tools import web_tools


class _Provider:
    name = "fake"
    display_name = "Fake"

    def __init__(self, result):
        self.result = result

    def supports_extract(self):
        return True

    async def extract(self, urls, **kwargs):
        return [self.result]


def _configure(monkeypatch, result, safety, policy=lambda _url: None):
    provider = _Provider(result)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(web_tools, "_get_extract_backend", lambda: "fake")
    monkeypatch.setattr("agent.web_search_registry.get_provider", lambda _name: provider)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)
    monkeypatch.setattr(web_tools, "async_is_safe_url", safety)
    monkeypatch.setattr(web_tools, "check_website_access", policy)


def test_safe_metadata_source_cannot_mask_private_result_url(monkeypatch):
    requested = "https://example.com/start"
    private = "http://127.0.0.1/final"

    async def safety(url):
        return url != private

    _configure(
        monkeypatch,
        {
            "url": private,
            "title": "Unsafe actual target",
            "content": "must not escape",
            "metadata": {"sourceURL": requested},
        },
        safety,
    )
    parsed = json.loads(asyncio.run(web_tools.web_extract_tool([requested])))

    assert parsed["results"][0]["url"] == private
    assert parsed["results"][0]["content"] == ""
    assert "private or internal" in parsed["results"][0]["error"]


def test_every_distinct_returned_target_must_pass_policy(monkeypatch):
    requested = "https://example.com/start"
    blocked = "https://blocked.example/redirect"

    async def safety(_url):
        return True

    def policy(url):
        if url == blocked:
            return {
                "host": "blocked.example",
                "rule": "blocked.example",
                "source": "config",
                "message": "Blocked by website policy",
            }
        return None

    _configure(
        monkeypatch,
        {
            "url": requested,
            "actual_url": "https://example.com/actual",
            "final_url": "https://example.com/final",
            "redirectURL": blocked,
            "canonical_url": "https://canonical.example/hint",
            "title": "Multiple targets",
            "content": "must not escape",
        },
        safety,
        policy,
    )
    parsed = json.loads(asyncio.run(web_tools.web_extract_tool([requested])))

    result = parsed["results"][0]
    assert result["url"] == blocked
    assert result["content"] == ""
    assert result["blocked_by_policy"]["rule"] == "blocked.example"


def test_line_boundary_snapping_keeps_one_truthful_marker():
    content = "\n".join(f"record {index}: " + "x" * 70 for index in range(2_000))
    bounded, truncated = web_tools._truncate_with_footer(
        content, "https://example.com/records", 5_000
    )

    assert truncated is True
    assert len(bounded) <= 5_000
    assert bounded.count("[TRUNCATED") == 1
    assert "record 0:" in bounded
    assert "record 1999:" in bounded
    assert "record 1000:" not in bounded
    assert "omitted and not stored" in bounded
