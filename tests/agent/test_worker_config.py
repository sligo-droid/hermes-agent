from __future__ import annotations

import os
import yaml

from agent.worker_config import WorkerRuntimeEnvelope, redacted_worker_config


def test_worker_config_and_environment_omit_client_knowledge_paths(monkeypatch, tmp_path):
    canaries = [
        str(tmp_path / "gbrain-home"), str(tmp_path / "source"),
        str(tmp_path / "gbrain-checkout"), str(tmp_path / "intake.db"),
    ]
    config = {
        "client_knowledge": {
            "gbrain": {"home": canaries[0], "source_checkout": canaries[1], "checkout": canaries[2]},
            "intake": {"db_path": canaries[3]},
        },
        "projects": {"pid": {"project_path": canaries[1], "honcho_peer_id": "secret-project"}},
        "coding_worker": {"backend": "opencode"},
        "model": {"provider": "test", "api_key": "secret-value"},
        "terminal": {"cwd": canaries[1], "security_mode": "auto"},
    }
    monkeypatch.setenv("GBRAIN_HOME", canaries[0])
    monkeypatch.setenv("HERMES_CLIENT_KNOWLEDGE_SOURCE", canaries[1])
    monkeypatch.setenv("HTTPS_PROXY", f"http://user:secret@proxy.invalid/{canaries[1]}")
    redacted = redacted_worker_config(config)
    blob = yaml.safe_dump(redacted)
    assert "client_knowledge" not in redacted
    assert "projects" not in redacted
    assert all(value not in blob for value in canaries)
    assert "secret-value" not in blob
    envelope = WorkerRuntimeEnvelope.create(config)
    try:
        env = envelope.environment(os.environ)
        assert env["HOME"] == str(envelope.home)
        assert env["HERMES_HOME"] == str(envelope.home)
        assert "GBRAIN_HOME" not in env
        assert "HERMES_CLIENT_KNOWLEDGE_SOURCE" not in env
        assert "HTTPS_PROXY" not in env
        assert all(value not in "\n".join(env.values()) for value in canaries)
        envelope.assert_paths_absent(canaries)
        loaded = yaml.safe_load((envelope.home / "config.yaml").read_text())
        assert "client_knowledge" not in loaded
        assert "projects" not in loaded
        with envelope.bind():
            from tools.environments.local import _sanitize_subprocess_env

            child_env = _sanitize_subprocess_env(os.environ)
            assert child_env["HOME"] == str(envelope.home)
            assert child_env["HERMES_HOME"] == str(envelope.home)
            assert "GBRAIN_HOME" not in child_env
            assert "HERMES_CLIENT_KNOWLEDGE_SOURCE" not in child_env
            assert all(value not in "\n".join(child_env.values()) for value in canaries)
    finally:
        envelope.cleanup()
