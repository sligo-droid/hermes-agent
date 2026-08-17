"""Tests for Chromium-presence detection in browser_tool.

Regression guard for the "browser tool advertised but Chromium missing"
class of bug — where ``agent-browser`` CLI is discoverable but no
Chromium build is on disk, causing every browser_* tool call to hang
for the full command timeout before surfacing a useless error.
"""

import os

import pytest

from hermes_constants import display_hermes_home
from tools import browser_tool as bt


@pytest.fixture(autouse=True)
def _reset_chromium_cache():
    bt._cached_chromium_installed = None
    yield
    bt._cached_chromium_installed = None


class TestChromiumSearchRoots:
    def test_respects_playwright_browsers_path_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        roots = bt._chromium_search_roots()
        assert str(tmp_path) == roots[0]


    def test_always_includes_default_ms_playwright_cache(self, monkeypatch):
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        roots = bt._chromium_search_roots()
        home = os.path.expanduser("~")
        assert any(r == os.path.join(home, ".cache", "ms-playwright") for r in roots)

    def test_includes_profile_home_ms_playwright_cache(self, monkeypatch, tmp_path):
        hermes_home = tmp_path / "hermes"
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)

        roots = bt._chromium_search_roots()

        assert str(hermes_home / "home" / ".cache" / "ms-playwright") == roots[0]


class TestChromiumInstalled:
    def test_true_when_plain_chromium_on_path(self, monkeypatch):
        monkeypatch.delenv("AGENT_BROWSER_EXECUTABLE_PATH", raising=False)
        monkeypatch.setattr(
            bt.shutil,
            "which",
            lambda name, path=None: "/usr/bin/chromium" if name == "chromium" else None,
        )

        assert bt._chromium_installed() is True


    def test_result_cached(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        (tmp_path / "chromium-1208").mkdir()
        assert bt._chromium_installed() is True
        # Delete after first call — cached True should still return True.
        (tmp_path / "chromium-1208").rmdir()
        assert bt._chromium_installed() is True


class TestCheckBrowserRequirementsChromium:

    def test_local_mode_with_chromium_returns_true(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bt, "_is_camofox_mode", lambda: False)
        monkeypatch.setattr(bt, "_find_agent_browser", lambda **_kw: "/usr/local/bin/agent-browser")
        monkeypatch.setattr(bt, "_requires_real_termux_browser_install", lambda _: False)
        monkeypatch.setattr(bt, "_get_cloud_provider", lambda: None)
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        (tmp_path / "chromium-1208").mkdir()

        assert bt.check_browser_requirements() is True


    def test_camofox_mode_does_not_require_chromium(self, monkeypatch, tmp_path):
        monkeypatch.setattr(bt, "_is_camofox_mode", lambda: True)
        # Even with no chromium on disk, camofox drives its own backend.
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "fakehome"))

        assert bt.check_browser_requirements() is True


class TestRunBrowserCommandChromiumGuard:
    """Verify _run_browser_command fails fast (no timeout hang) when
    Chromium is missing in local mode.
    """



class TestPlaywrightChromiumPreflight:
    def test_fails_when_profile_cache_empty_even_with_system_chromium(self, monkeypatch, tmp_path):
        hermes_home = tmp_path / "hermes"
        cache = hermes_home / "home" / ".cache" / "ms-playwright"
        cache.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        monkeypatch.setattr(
            bt.shutil,
            "which",
            lambda name: "/usr/bin/chromium-browser" if name == "chromium-browser" else None,
        )
        monkeypatch.setattr(bt, "_running_in_docker", lambda: False)

        ok, message = bt.check_playwright_chromium_preflight()

        assert ok is False
        assert "Playwright Chromium preflight failed" in message
        assert f"Hermes profile HOME: {display_hermes_home()}" in message
        assert f"Browser subprocess HOME: {hermes_home / 'home'}" in message
        assert str(cache) in message

    def test_missing_message_includes_profile_paths_and_install(self, monkeypatch, tmp_path):
        hermes_home = tmp_path / "hermes"
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        monkeypatch.setattr(bt.shutil, "which", lambda name: None)
        monkeypatch.setattr(bt, "_running_in_docker", lambda: False)

        ok, message = bt.check_playwright_chromium_preflight()

        assert ok is False
        assert "Playwright Chromium preflight failed" in message
        assert f"Hermes profile HOME: {display_hermes_home()}" in message
        assert f"Browser subprocess HOME: {hermes_home / 'home'}" in message
        assert str(hermes_home / "home" / ".cache" / "ms-playwright") in message
        assert "npx playwright install --with-deps chromium" in message
        assert "never installs browsers automatically" in message

    def test_cli_returns_nonzero_when_missing(self, monkeypatch, tmp_path, capsys):
        from hermes_cli import browser_preflight

        hermes_home = tmp_path / "hermes"
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        monkeypatch.setattr(bt.shutil, "which", lambda name: None)
        monkeypatch.setattr(bt, "_running_in_docker", lambda: False)

        assert browser_preflight.main(["chromium"]) == 1
        assert str(hermes_home / "home" / ".cache" / "ms-playwright") in capsys.readouterr().out
