"""Finite deterministic web_extract truncation tests (no LLM, no storage)."""

from __future__ import annotations

import asyncio
import json
import math
from unittest.mock import MagicMock

import pytest

import tools.web_tools as wt


class FakeExtractProvider:
    name = "fake"
    display_name = "Fake Extract"

    def __init__(self, results):
        self.results = results

    def supports_extract(self):
        return True

    async def extract(self, urls, **kwargs):
        return self.results


@pytest.fixture
def run_extract(monkeypatch):
    async def _safe(_url):
        return True

    monkeypatch.setattr(wt, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(wt, "_get_extract_backend", lambda: "fake")
    monkeypatch.setattr(wt, "async_is_safe_url", _safe)
    monkeypatch.setattr(wt, "check_website_access", lambda _url: None)
    monkeypatch.setattr("tools.interrupt.is_interrupted", lambda: False)

    async def _run(results, urls=None, **kwargs):
        provider = FakeExtractProvider(results)
        monkeypatch.setattr(
            "agent.web_search_registry.get_provider", lambda _name: provider
        )
        payload = await wt.web_extract_tool(
            urls or [result.get("url", "https://example.com") for result in results],
            **kwargs,
        )
        return payload, json.loads(payload)

    return _run


def test_short_page_unchanged_after_base64_cleanup(run_extract):
    async def scenario():
        content = (
            "before ![chart](data:image/png;base64,QUJDRA==) after\n"
            '<img alt="diagram" src="data:image/svg+xml;base64,PHN2Zz4=" />\n'
            "![remote](https://example.com/remote.png)"
        )
        payload, parsed = await run_extract(
            [{"url": "https://example.com/a", "title": "A", "content": content}]
        )
        cleaned = parsed["results"][0]["content"]
        assert cleaned == (
            "before [IMAGE: chart] after\n"
            "[IMAGE: diagram]\n"
            "![remote](https://example.com/remote.png)"
        )
        assert "base64" not in payload
        assert "[TRUNCATED" not in cleaned

    asyncio.run(scenario())


def test_long_page_is_deterministic_head_tail_and_never_stored(
    run_extract, tmp_path, monkeypatch
):
    async def scenario():
        hermes_home = tmp_path / "hermes"
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        content = (
            "HEAD_UNIQUE\n"
            + "H" * 20_000
            + "MIDDLE_UNIQUE"
            + "M" * 20_000
            + "TAIL_UNIQUE"
        )
        results = [
            {"url": "https://example.com/long", "title": "Long", "content": content}
        ]
        first_payload, first = await run_extract(results, char_limit=4_000)
        second_payload, second = await run_extract(results, char_limit=4_000)

        assert first_payload == second_payload
        bounded = first["results"][0]["content"]
        assert len(bounded) <= 4_000
        assert bounded.count("[TRUNCATED") == 1
        assert "HEAD_UNIQUE" in bounded
        assert "TAIL_UNIQUE" in bounded
        assert "MIDDLE_UNIQUE" not in bounded
        assert "omitted and not stored" in bounded
        assert "browser_navigate" in bounded
        assert not (hermes_home / "cache" / "web").exists()
        assert second["results"][0]["content"] == bounded

    asyncio.run(scenario())


class TestCharLimitConfig:
    def test_default_config_and_per_call_precedence(self, monkeypatch):
        monkeypatch.setattr(wt, "_load_web_config", lambda: {})
        assert wt._get_extract_char_limit() == 15_000

        monkeypatch.setattr(
            wt, "_load_web_config", lambda: {"extract_char_limit": 40_000}
        )
        assert wt._get_extract_char_limit() == 40_000
        assert wt._get_extract_char_limit(5_000) == 5_000

    @pytest.mark.parametrize(
        "value",
        ["invalid", math.inf, -math.inf, math.nan, True, False, None],
    )
    def test_invalid_nonfinite_and_bool_fall_back(self, value):
        assert wt._clamp_extract_char_limit(value) == 15_000

    def test_public_floor_and_ceiling(self):
        assert wt._clamp_extract_char_limit(1) == 2_000
        assert wt._clamp_extract_char_limit(999_999) == 90_000
        assert wt.MAX_EXTRACT_CHAR_LIMIT == 90_000
        assert wt.WEB_EXTRACT_SCHEMA["parameters"]["properties"]["char_limit"] == {
            "type": "integer",
            "description": (
                "Maximum inline characters requested per page before aggregate "
                "allocation. Defaults to web.extract_char_limit (15,000); "
                "allowed range 2,000–90,000."
            ),
            "minimum": 2_000,
            "maximum": 90_000,
        }


def test_federal_register_610k_returns_promptly_without_auxiliary_or_storage(
    run_extract, tmp_path, monkeypatch
):
    async def scenario():
        hermes_home = tmp_path / "hermes"
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        head = "FEDERAL_REGISTER_HEAD_UNIQUE\nAgency: Example\n"
        middle = "FEDERAL_REGISTER_MIDDLE_UNIQUE"
        tail = "\nFEDERAL_REGISTER_TAIL_UNIQUE\nBilling Code 0000-00-P"
        content = head + "A" * 305_000 + middle + "B" * 305_000 + tail
        assert len(content) > 610_000

        auxiliary_call = MagicMock(side_effect=AssertionError("auxiliary call"))
        monkeypatch.setattr("agent.auxiliary_client.async_call_llm", auxiliary_call)
        payload, parsed = await asyncio.wait_for(
            run_extract(
                [
                    {
                        "url": "https://www.federalregister.gov/documents/example",
                        "title": "Federal Register",
                        "content": content,
                    }
                ]
            ),
            timeout=2,
        )

        bounded = parsed["results"][0]["content"]
        assert len(payload) < 100_000
        assert len(bounded) <= 15_000
        assert bounded.count("[TRUNCATED") == 1
        assert "FEDERAL_REGISTER_HEAD_UNIQUE" in bounded
        assert "FEDERAL_REGISTER_TAIL_UNIQUE" in bounded
        assert "FEDERAL_REGISTER_MIDDLE_UNIQUE" not in bounded
        auxiliary_call.assert_not_called()
        assert not (hermes_home / "cache" / "web").exists()

    asyncio.run(scenario())


def test_five_url_aggregate_stays_below_registered_result_budget(run_extract):
    async def scenario():
        results = []
        urls = []
        for index in range(5):
            url = f"https://example.com/{index}"
            urls.append(url)
            results.append(
                {
                    "url": url,
                    "title": f"Page {index}",
                    "content": (
                        f"HEAD_{index}\n" + "x" * 120_000 + f"\nTAIL_{index}"
                    ),
                }
            )

        payload, parsed = await run_extract(
            results, urls=urls, char_limit=wt.MAX_EXTRACT_CHAR_LIMIT
        )
        contents = [result["content"] for result in parsed["results"]]
        assert len(payload) < 100_000
        assert sum(map(len, contents)) <= wt.AGGREGATE_EXTRACT_CONTENT_BUDGET
        assert len(contents) == 5
        for index, content in enumerate(contents):
            assert f"HEAD_{index}" in content
            assert f"TAIL_{index}" in content
            assert content.count("[TRUNCATED") == 1

    asyncio.run(scenario())


def test_json_escaping_cannot_push_aggregate_over_result_budget(run_extract):
    async def scenario():
        results = []
        for index in range(5):
            results.append(
                {
                    "url": f"https://example.com/escaped-{index}",
                    "title": f"Escaped {index}",
                    "content": (
                        f"ESCAPED_HEAD_{index}\n"
                        + ('"\\' * 70_000)
                        + f"\nESCAPED_TAIL_{index}"
                    ),
                }
            )

        payload, parsed = await run_extract(
            results, char_limit=wt.MAX_EXTRACT_CHAR_LIMIT
        )
        assert len(payload) < wt.MAX_EXTRACT_RESULT_SIZE_CHARS
        for index, result in enumerate(parsed["results"]):
            assert f"ESCAPED_HEAD_{index}" in result["content"]
            assert f"ESCAPED_TAIL_{index}" in result["content"]
            assert result["content"].count("[TRUNCATED") == 1

    asyncio.run(scenario())


def test_oversized_provider_metadata_is_bounded_json_safe_and_deterministic(
    run_extract,
):
    async def scenario():
        requested_urls = [f"https://example.com/request-{index}" for index in range(5)]
        results = []
        for index in range(5):
            results.append(
                {
                    "url": f"https://example.com/result-{index}/" + "u" * 120_000,
                    "title": f"TITLE_{index}_" + "t" * 120_000,
                    "content": "",
                    "error": f"ERROR_{index}_" + "e" * 120_000,
                    "blocked_by_policy": {
                        "host": f"HOST_{index}_" + "h" * 120_000,
                        "rule": f"RULE_{index}_" + "r" * 120_000,
                        "source": f"SOURCE_{index}_" + "s" * 120_000,
                        "ignored": "not exposed",
                    },
                }
            )

        first_payload, first = await run_extract(results, urls=requested_urls)
        second_payload, second = await run_extract(results, urls=requested_urls)

        assert first_payload == second_payload
        assert first == second
        assert len(first_payload) < wt.MAX_EXTRACT_RESULT_SIZE_CHARS
        assert len(first["results"]) == 5
        for result in first["results"]:
            assert isinstance(result["url"], str)
            assert isinstance(result["title"], str)
            assert isinstance(result["error"], str)
            assert len(result["url"]) <= wt.MAX_EXTRACT_URL_CHARS
            assert len(result["title"]) <= wt.MAX_EXTRACT_TITLE_CHARS
            assert len(result["error"]) <= wt.MAX_EXTRACT_ERROR_CHARS
            assert result["url"].endswith(wt.METADATA_TRUNCATION_MARKER)
            assert result["title"].endswith(wt.METADATA_TRUNCATION_MARKER)
            assert result["error"].endswith(wt.METADATA_TRUNCATION_MARKER)
            assert set(result["blocked_by_policy"]) == {"host", "rule", "source"}
            for value in result["blocked_by_policy"].values():
                assert isinstance(value, str)
                assert len(value) <= wt.MAX_EXTRACT_POLICY_VALUE_CHARS
                assert value.endswith(wt.METADATA_TRUNCATION_MARKER)

    asyncio.run(scenario())


def test_final_serialized_guard_preserves_order_count_and_valid_json():
    def rows():
        return [
            {
                "url": f"URL_{index}_" + "u" * 150_000,
                "title": f"TITLE_{index}_" + "t" * 150_000,
                "content": f"CONTENT_{index}_" + "c" * 150_000,
                "error": f"ERROR_{index}_" + "e" * 150_000,
                "blocked_by_policy": {
                    "host": f"HOST_{index}_" + "h" * 150_000,
                    "rule": f"RULE_{index}_" + "r" * 150_000,
                    "source": f"SOURCE_{index}_" + "s" * 150_000,
                },
            }
            for index in range(5)
        ]

    first_payload = wt._serialize_extract_results(rows())
    second_payload = wt._serialize_extract_results(rows())
    parsed = json.loads(first_payload)

    assert first_payload == second_payload
    assert len(first_payload) < wt.MAX_EXTRACT_RESULT_SIZE_CHARS
    assert len(parsed["results"]) == 5
    assert [result["url"].startswith(f"URL_{index}_") for index, result in enumerate(parsed["results"])] == [True] * 5
    assert all(result["content"] == "" for result in parsed["results"])
    assert all(
        wt.METADATA_TRUNCATION_MARKER in result["url"]
        for result in parsed["results"]
    )


@pytest.mark.parametrize("field", ["actual_url", "finalURL", "redirected_url"])
def test_post_fetch_private_actual_final_or_redirect_is_blocked(
    run_extract, monkeypatch, field
):
    async def scenario():
        private = "http://127.0.0.1/private"

        async def safety(url):
            return url != private

        monkeypatch.setattr(wt, "async_is_safe_url", safety)
        result = {
            "url": "https://example.com/requested",
            "title": "Unsafe redirect",
            "content": "must not escape",
            field: private,
            "canonical_url": "http://127.0.0.1/canonical-hint",
        }
        _, parsed = await run_extract(
            [result], urls=["https://example.com/requested"]
        )
        blocked = parsed["results"][0]
        assert blocked["url"] == private
        assert blocked["content"] == ""
        assert "private or internal" in blocked["error"]

    asyncio.run(scenario())


def test_post_fetch_policy_blocked_metadata_redirect_is_blocked(
    run_extract, monkeypatch
):
    async def scenario():
        blocked_url = "https://blocked.example/final"

        def policy(url):
            if url == blocked_url:
                return {
                    "host": "blocked.example",
                    "rule": "blocked.example",
                    "source": "config",
                    "message": "Blocked by website policy",
                }
            return None

        monkeypatch.setattr(wt, "check_website_access", policy)
        _, parsed = await run_extract(
            [
                {
                    "url": "https://example.com/requested",
                    "title": "Blocked redirect",
                    "content": "must not escape",
                    "metadata": {"redirectURL": blocked_url},
                }
            ],
            urls=["https://example.com/requested"],
        )
        blocked = parsed["results"][0]
        assert blocked["url"] == blocked_url
        assert blocked["content"] == ""
        assert blocked["blocked_by_policy"]["rule"] == "blocked.example"

    asyncio.run(scenario())


def test_private_canonical_hint_is_not_treated_as_fetched_target(
    run_extract, monkeypatch
):
    async def scenario():
        async def safety(url):
            return url != "http://127.0.0.1/hint-only"

        monkeypatch.setattr(wt, "async_is_safe_url", safety)
        _, parsed = await run_extract(
            [
                {
                    "url": "https://example.com/actual",
                    "canonical_url": "http://127.0.0.1/hint-only",
                    "title": "Safe actual",
                    "content": "visible",
                }
            ]
        )
        assert parsed["results"][0]["content"] == "visible"

    asyncio.run(scenario())


def test_schema_stub_and_direct_docs_describe_bounded_no_storage_contract():
    from pathlib import Path

    from hermes_cli.config import DEFAULT_CONFIG
    from tools.code_execution_tool import _TOOL_DOC_LINES, _TOOL_STUBS

    assert DEFAULT_CONFIG["web"]["extract_char_limit"] == 15_000
    urls = wt.WEB_EXTRACT_SCHEMA["parameters"]["properties"]["urls"]
    assert urls["items"] == {"type": "string"}
    assert urls["maxItems"] == 5

    contract = "\n".join(
        [wt.WEB_EXTRACT_SCHEMA["description"], _TOOL_STUBS["web_extract"][2]]
        + [text for name, text in _TOOL_DOC_LINES if name == "web_extract"]
    ).lower()
    assert "90000" in contract or "90,000" in contract
    assert "not stored" in contract
    assert "cache/web" not in contract
    assert "read_file" not in contract
    assert "stored on disk" not in contract

    repo = Path(__file__).resolve().parents[2]
    for relative in (
        "website/docs/user-guide/features/web-search.md",
        "website/docs/reference/tools-reference.md",
        "website/docs/user-guide/configuration.md",
    ):
        text = (repo / relative).read_text(encoding="utf-8").lower()
        assert "full text saved" not in text
        assert "cache/web" not in text
        assert "summarization timed out" not in text
