from pathlib import Path

import pytest

from plugins.traces.hermes_traces_plugin.config import (
    DEFAULT_EXECUTABLE,
    PUBLIC_BASE_URL,
    Config,
)


def test_config_uses_profile_scoped_state_path(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    config = Config.from_env()

    assert config.hermes_home == tmp_path
    assert config.index_path == tmp_path / "state" / "plugins" / "traces" / "index.json"
    assert config.executable == DEFAULT_EXECUTABLE
    assert config.sligo_url("opaque") == PUBLIC_BASE_URL + "opaque"


def test_config_accepts_executable_and_timeout_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_TRACES_EXECUTABLE", "/opt/traces")
    monkeypatch.setenv("HERMES_TRACES_TIMEOUT", "12.5")

    config = Config.from_env()

    assert config.executable == "/opt/traces"
    assert config.timeout == 12.5


@pytest.mark.parametrize("timeout", [0, -1, 301, True, "30"])
def test_config_rejects_invalid_timeout(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        Config(tmp_path, timeout=timeout)


def test_config_rejects_invalid_timeout_environment(monkeypatch):
    monkeypatch.setenv("HERMES_TRACES_TIMEOUT", "not-a-number")

    with pytest.raises(ValueError, match="HERMES_TRACES_TIMEOUT"):
        Config.from_env()


@pytest.mark.parametrize(
    "base_url",
    [
        "http://sligo.sligolabs.com/traces/",
        "https://sligo.sligolabs.com/traces",
        "https://evil.example/traces/",
        "https://sligo.sligolabs.com/traces/?target=evil",
    ],
)
def test_config_rejects_noncanonical_public_base(tmp_path, base_url):
    with pytest.raises(ValueError, match="exact HTTPS Sligo traces path"):
        Config(tmp_path, base_url=base_url)


def test_config_expands_home_directory():
    config = Config(Path("~/profile"))

    assert config.hermes_home == Path("~/profile").expanduser()
