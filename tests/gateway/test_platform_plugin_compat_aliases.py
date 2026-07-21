"""Compatibility coverage for platform adapters migrated into plugins."""

from __future__ import annotations

import importlib

import pytest


@pytest.mark.parametrize(
    ("legacy_name", "plugin_name", "class_name"),
    (
        (
            "gateway.platforms.dingtalk",
            "plugins.platforms.dingtalk.adapter",
            "DingTalkAdapter",
        ),
        (
            "gateway.platforms.slack",
            "plugins.platforms.slack.adapter",
            "SlackAdapter",
        ),
        (
            "gateway.platforms.telegram",
            "plugins.platforms.telegram.adapter",
            "TelegramAdapter",
        ),
    ),
)
def test_legacy_platform_import_is_plugin_module(
    legacy_name: str,
    plugin_name: str,
    class_name: str,
) -> None:
    legacy = importlib.import_module(legacy_name)
    plugin = importlib.import_module(plugin_name)

    assert legacy is plugin
    assert getattr(legacy, class_name) is getattr(plugin, class_name)
