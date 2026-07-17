import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest


MINIMAL_CONFIG = (
    'sandbox_mode = "workspace-write"\n'
    'approval_policy = "never"\n'
)


def _set_inheritance(monkeypatch, *, enabled: bool) -> None:
    from hermes_cli import config as hermes_config

    monkeypatch.setattr(
        hermes_config,
        "load_config",
        lambda: {"coding_worker": {"inherit_fast_mode": enabled}},
    )


def _write_parent_config(codex_home: Path, content: str) -> None:
    codex_home.mkdir(parents=True)
    (codex_home / "config.toml").write_text(content, encoding="utf-8")


def test_shared_worker_home_inherits_parent_preferences(tmp_path, monkeypatch):
    from agent import codex_worker_auth

    _set_inheritance(monkeypatch, enabled=True)
    parent_home = tmp_path / "parent-codex"
    _write_parent_config(
        parent_home,
        'service_tier = "fast"\n'
        'personality = "warm \\"guide\\""\n'
        'model_verbosity = "high"\n'
        'model_reasoning_summary = "concise"\n'
        "\n[features]\n"
        "fast_mode = true\n"
        "\n[memories]\n"
        "use_memories = true\n"
        "generate_memories = false\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    entry = SimpleNamespace(
        id="pool-1",
        access_token="access",
        refresh_token="refresh",
        id_token="id-token",
        last_status=None,
    )
    monkeypatch.setattr(
        codex_worker_auth,
        "select_codex_worker_credential",
        lambda parent_agent: (object(), entry),
    )

    worker_home = tmp_path / "worker-codex"
    credential_id = codex_worker_auth.prepare_codex_worker_home(
        worker_home,
        source_env={"CODEX_HOME": str(parent_home)},
    )

    assert credential_id == "pool-1"
    assert worker_home.is_symlink()
    worker_config = (worker_home / "config.toml").read_text(encoding="utf-8")
    assert worker_config == (
        MINIMAL_CONFIG
        + 'service_tier = "fast"\n'
        + 'personality = "warm \\"guide\\""\n'
        + 'model_verbosity = "high"\n'
        + 'model_reasoning_summary = "concise"\n'
        + "[features]\n"
        + "fast_mode = true\n"
        + "[memories]\n"
        + "use_memories = true\n"
        + "generate_memories = false\n"
    )
    assert tomllib.loads(worker_config) == {
        "sandbox_mode": "workspace-write",
        "approval_policy": "never",
        "service_tier": "fast",
        "personality": 'warm "guide"',
        "model_verbosity": "high",
        "model_reasoning_summary": "concise",
        "features": {"fast_mode": True},
        "memories": {
            "use_memories": True,
            "generate_memories": False,
        },
    }


@pytest.mark.parametrize(
    ("parent_config", "expected_config"),
    [
        (
            'personality = "pragmatic"\n',
            MINIMAL_CONFIG + 'personality = "pragmatic"\n',
        ),
        (
            "[features]\nfast_mode = false\n",
            MINIMAL_CONFIG + "[features]\nfast_mode = false\n",
        ),
        (
            "[memories]\ngenerate_memories = true\n",
            MINIMAL_CONFIG + "[memories]\ngenerate_memories = true\n",
        ),
    ],
)
def test_partial_parent_preferences_are_inherited(
    tmp_path,
    monkeypatch,
    parent_config,
    expected_config,
):
    from agent.codex_worker_auth import _write_minimal_config

    _set_inheritance(monkeypatch, enabled=True)
    parent_home = tmp_path / "parent-codex"
    _write_parent_config(parent_home, parent_config)
    worker_home = tmp_path / "worker-codex"
    worker_home.mkdir()

    _write_minimal_config(worker_home, source_env={"CODEX_HOME": str(parent_home)})

    worker_config = (worker_home / "config.toml").read_text(encoding="utf-8")
    assert worker_config == expected_config
    tomllib.loads(worker_config)


def test_parent_without_inherited_preferences_keeps_minimal_config(tmp_path, monkeypatch):
    from agent.codex_worker_auth import _write_minimal_config

    _set_inheritance(monkeypatch, enabled=True)
    parent_home = tmp_path / "parent-codex"
    _write_parent_config(parent_home, 'model = "gpt-5.6-sol"\n')
    worker_home = tmp_path / "worker-codex"
    worker_home.mkdir()

    _write_minimal_config(worker_home, source_env={"CODEX_HOME": str(parent_home)})

    assert (worker_home / "config.toml").read_bytes() == MINIMAL_CONFIG.encode()


def test_wrong_typed_parent_preferences_are_omitted(tmp_path, monkeypatch):
    from agent.codex_worker_auth import _write_minimal_config

    _set_inheritance(monkeypatch, enabled=True)
    parent_home = tmp_path / "parent-codex"
    _write_parent_config(
        parent_home,
        "service_tier = true\n"
        "personality = 42\n"
        'model_verbosity = ["high"]\n'
        'model_reasoning_summary = { value = "concise" }\n'
        "\n[features]\n"
        'fast_mode = "true"\n'
        "\n[memories]\n"
        'use_memories = "true"\n'
        "generate_memories = 1\n",
    )
    worker_home = tmp_path / "worker-codex"
    worker_home.mkdir()

    _write_minimal_config(worker_home, source_env={"CODEX_HOME": str(parent_home)})

    assert (worker_home / "config.toml").read_bytes() == MINIMAL_CONFIG.encode()


def test_malformed_parent_config_keeps_minimal_config(tmp_path, monkeypatch):
    from agent.codex_worker_auth import _write_minimal_config

    _set_inheritance(monkeypatch, enabled=True)
    parent_home = tmp_path / "parent-codex"
    _write_parent_config(parent_home, 'service_tier = "fast\n')
    worker_home = tmp_path / "worker-codex"
    worker_home.mkdir()

    _write_minimal_config(worker_home, source_env={"CODEX_HOME": str(parent_home)})

    assert (worker_home / "config.toml").read_bytes() == MINIMAL_CONFIG.encode()


def test_inherit_fast_mode_kill_switch_keeps_minimal_config(tmp_path, monkeypatch):
    from agent.codex_worker_auth import _write_minimal_config

    _set_inheritance(monkeypatch, enabled=False)
    parent_home = tmp_path / "parent-codex"
    _write_parent_config(
        parent_home,
        'service_tier = "fast"\n'
        'personality = "pragmatic"\n'
        'model_verbosity = "high"\n'
        'model_reasoning_summary = "concise"\n'
        "\n[features]\n"
        "fast_mode = true\n"
        "\n[memories]\n"
        "use_memories = true\n"
        "generate_memories = false\n",
    )
    worker_home = tmp_path / "worker-codex"
    worker_home.mkdir()

    _write_minimal_config(worker_home, source_env={"CODEX_HOME": str(parent_home)})

    assert (worker_home / "config.toml").read_bytes() == MINIMAL_CONFIG.encode()


def test_codex_home_env_takes_precedence_over_default_home(tmp_path, monkeypatch):
    from agent.codex_worker_auth import _write_minimal_config

    _set_inheritance(monkeypatch, enabled=True)
    default_home = tmp_path / "user-home"
    _write_parent_config(
        default_home / ".codex",
        'service_tier = "standard"\n\n[features]\nfast_mode = false\n',
    )
    env_home = tmp_path / "env-codex"
    _write_parent_config(
        env_home,
        'service_tier = "fast"\n\n[features]\nfast_mode = true\n',
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: default_home))
    worker_home = tmp_path / "worker-codex"
    worker_home.mkdir()

    _write_minimal_config(worker_home, source_env={"CODEX_HOME": str(env_home)})

    worker_config = (worker_home / "config.toml").read_text(encoding="utf-8")
    assert 'service_tier = "fast"' in worker_config
    assert "fast_mode = true" in worker_config
    assert "standard" not in worker_config
    assert "fast_mode = false" not in worker_config


def test_inherit_fast_mode_defaults_true():
    from hermes_cli.config import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["coding_worker"]["inherit_fast_mode"] is True
