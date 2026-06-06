import os

from hermes_cli import gateway_child_isolation as iso


def test_gateway_child_systemd_env_filters_secrets():
    env = {
        "HERMES_HOME": "/home/droid/.hermes/profiles/discord",
        "HERMES_SESSION_KEY": "discord:123",
        "HERMES_PROJECT_PATH": "/repo",
        "CODEX_HOME": "/tmp/codex-home",
        "PATH": "/usr/bin",
        "RUST_LOG": "warn",
        "OPENAI_API_KEY": "secret",
        "ANTHROPIC_API_KEY": "secret",
        "SLACK_BOT_TOKEN": "secret",
    }

    out = iso.gateway_child_systemd_env(env)

    assert out["HERMES_HOME"] == "/home/droid/.hermes/profiles/discord"
    assert out["HERMES_SESSION_KEY"] == "discord:123"
    assert out["HERMES_PROJECT_PATH"] == "/repo"
    assert out["CODEX_HOME"] == "/tmp/codex-home"
    assert out["PATH"] == "/usr/bin"
    assert out["RUST_LOG"] == "warn"
    assert "OPENAI_API_KEY" not in out
    assert "ANTHROPIC_API_KEY" not in out
    assert "SLACK_BOT_TOKEN" not in out


def test_build_gateway_child_scope_argv_uses_transient_user_scope(monkeypatch, tmp_path):
    monkeypatch.setattr(iso.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(iso.time, "time", lambda: 123.456)

    argv, meta = iso.build_gateway_child_scope_argv(
        ["/bin/sh", "-lc", "sleep 60"],
        env={
            "HERMES_HOME": "/home/droid/.hermes/profiles/discord",
            "HERMES_SESSION_KEY": "discord:123",
            "OPENAI_API_KEY": "secret",
        },
        cwd=str(tmp_path),
        kind="terminal",
        purpose="background terminal process",
        command_label="sleep",
        session_key="discord:123",
    )

    assert meta.enabled is True
    assert meta.unit == "hermes-gateway-child-terminal-discord-123-sleep-123456.scope"
    assert argv[:5] == [
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--unit",
        "hermes-gateway-child-terminal-discord-123-sleep-123456",
    ]
    assert "--pipe" in argv
    assert f"WorkingDirectory={tmp_path}" in argv
    assert "--setenv=HERMES_HOME=/home/droid/.hermes/profiles/discord" in argv
    assert "--setenv=HERMES_SESSION_KEY=discord:123" in argv
    assert all("OPENAI_API_KEY" not in part for part in argv)
    assert argv[-4:] == ["--", "/bin/sh", "-lc", "sleep 60"]


def test_build_gateway_child_scope_argv_falls_back_without_systemd(monkeypatch):
    monkeypatch.setattr(iso.shutil, "which", lambda name: None)
    command = ["/bin/sh", "-lc", "sleep 60"]

    argv, meta = iso.build_gateway_child_scope_argv(
        command,
        env=os.environ,
        kind="terminal",
        purpose="background terminal process",
    )

    assert argv == command
    assert meta.enabled is False
    assert meta.unit == ""
