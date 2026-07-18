"""Tests for plugin-provided session artifact links."""

import logging
from unittest.mock import patch

import pytest

from hermes_cli.plugins import (
    PluginContext,
    PluginManager,
    PluginManifest,
    collect_session_artifacts,
)


def _context(manager: PluginManager, name: str = "artifact-plugin") -> PluginContext:
    return PluginContext(PluginManifest(name=name, source="user"), manager)


def _collect(manager: PluginManager, session_id: str = "session-123", **kwargs):
    manager._discovered = True
    with patch("hermes_cli.plugins._plugin_manager", manager):
        return collect_session_artifacts(session_id, **kwargs)


def test_collects_single_mapping_and_iterable_in_registration_order():
    manager = PluginManager()
    calls = []

    def first(session_id, surface):
        calls.append(("first", session_id, surface))
        return {
            "kind": " report ",
            "label": " First report ",
            "url": "https://example.com/reports/first",
            "ignored": "host strips extra fields",
        }

    def second(session_id, surface):
        calls.append(("second", session_id, surface))
        return iter(
            [
                {
                    "kind": "dashboard",
                    "label": "Second report",
                    "url": "https://example.com/reports/second",
                }
            ]
        )

    _context(manager, "first-plugin").register_session_artifact_provider(first)
    _context(manager, "second-plugin").register_session_artifact_provider(second)

    assert _collect(manager) == [
        {
            "kind": "report",
            "label": "First report",
            "url": "https://example.com/reports/first",
        },
        {
            "kind": "dashboard",
            "label": "Second report",
            "url": "https://example.com/reports/second",
        },
    ]
    assert calls == [
        ("first", "session-123", "discord_feature_summary"),
        ("second", "session-123", "discord_feature_summary"),
    ]


def test_forwards_explicit_surface():
    manager = PluginManager()
    calls = []
    _context(manager).register_session_artifact_provider(
        lambda session_id, surface: calls.append((session_id, surface)) or []
    )

    assert _collect(manager, surface="web_session_details") == []
    assert calls == [("session-123", "web_session_details")]


def test_provider_exceptions_and_broken_iterators_are_isolated(caplog):
    manager = PluginManager()

    def raises(**_):
        raise RuntimeError("provider failed")

    def broken_iterator(**_):
        yield {
            "kind": "report",
            "label": "Recovered before iterator error",
            "url": "https://example.com/recovered",
        }
        raise RuntimeError("iterator failed")

    _context(manager, "raises").register_session_artifact_provider(raises)
    _context(manager, "broken").register_session_artifact_provider(broken_iterator)
    _context(manager, "healthy").register_session_artifact_provider(
        lambda session_id, surface: {
            "kind": "report",
            "label": "Healthy provider",
            "url": "https://example.com/healthy",
        }
    )

    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        artifacts = _collect(manager)

    assert [artifact["label"] for artifact in artifacts] == [
        "Recovered before iterator error",
        "Healthy provider",
    ]
    assert "provider failed" in caplog.text
    assert "iterator failed" in caplog.text


@pytest.mark.parametrize(
    "candidate",
    [
        None,
        "https://example.com/not-a-mapping",
        {"kind": "report", "label": "Missing URL"},
        {"kind": 1, "label": "Wrong kind type", "url": "https://example.com"},
        {"kind": "report", "label": "", "url": "https://example.com"},
        {"kind": "report\n", "label": "Control", "url": "https://example.com"},
        {"kind": "report", "label": "Control\nlabel", "url": "https://example.com"},
        {"kind": "report", "label": "Relative", "url": "/reports/1"},
        {"kind": "report", "label": "Other scheme", "url": "ftp://example.com/a"},
        {"kind": "report", "label": "Credentials", "url": "https://u:p@example.com/a"},
        {"kind": "report", "label": "Whitespace", "url": "https://example.com/a b"},
        {"kind": "report", "label": "Control", "url": "https://example.com/a\n"},
        {"kind": "k" * 65, "label": "Long kind", "url": "https://example.com"},
        {"kind": "report", "label": "l" * 101, "url": "https://example.com"},
        {"kind": "report", "label": "Long URL", "url": "https://example.com/" + "a" * 2030},
    ],
)
def test_rejects_invalid_or_unsafe_artifacts(candidate):
    manager = PluginManager()
    _context(manager).register_session_artifact_provider(
        lambda session_id, surface: candidate
    )
    assert _collect(manager) == []


def test_deduplicates_exact_artifacts_and_caps_output_count():
    manager = PluginManager()
    duplicate = {
        "kind": "report",
        "label": "Same report",
        "url": "https://example.com/same",
    }
    artifacts = [duplicate, dict(duplicate)] + [
        {
            "kind": "report",
            "label": f"Report {index}",
            "url": f"https://example.com/reports/{index}",
        }
        for index in range(30)
    ]
    _context(manager).register_session_artifact_provider(
        lambda session_id, surface: artifacts
    )

    collected = _collect(manager)
    assert len(collected) == 20
    assert collected.count(duplicate) == 1
    assert collected[-1]["label"] == "Report 18"


def test_rejects_non_callable_and_async_providers():
    manager = PluginManager()
    context = _context(manager)

    async def async_provider(*_):
        return []

    with pytest.raises(ValueError, match="non-callable"):
        context.register_session_artifact_provider(None)
    with pytest.raises(ValueError, match="must be synchronous"):
        context.register_session_artifact_provider(async_provider)


def test_sync_provider_returning_awaitable_is_ignored(caplog):
    manager = PluginManager()

    async def result():
        return []

    _context(manager).register_session_artifact_provider(
        lambda session_id, surface: result()
    )

    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        assert _collect(manager) == []
    assert "returned an awaitable" in caplog.text


def test_force_rediscovery_clears_session_artifact_providers(monkeypatch):
    manager = PluginManager()
    _context(manager).register_session_artifact_provider(
        lambda session_id, surface: []
    )
    manager._discovered = True
    monkeypatch.setattr(manager, "_discover_and_load_inner", lambda: None)

    manager.discover_and_load(force=True)

    assert manager._session_artifact_providers == []
