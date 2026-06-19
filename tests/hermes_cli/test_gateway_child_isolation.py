import os

from hermes_cli import gateway_child_isolation as iso


def test_gateway_child_systemd_env_filters_secrets():
    env = {
        "HERMES_HOME": "/home/droid/.hermes/profiles/discord",
        "HERMES_SESSION_KEY": "discord:123",
        "HERMES_PROJECT_PATH": "/repo",
        "HERMES_CODEX_WORKER_NETWORK_ACCESS": "1",
        "CODEX_HOME": "/tmp/codex-home",
        "GH_CONFIG_DIR": "/home/droid/.config/gh",
        "GIT_CONFIG_GLOBAL": "/home/droid/.gitconfig",
        "GIT_SSH_COMMAND": "ssh -F /tmp/ssh_config",
        "HTTPS_PROXY": "http://proxy.example.invalid:8080",
        "REQUESTS_CA_BUNDLE": "/etc/ssl/certs/ca-certificates.crt",
        "PATH": "/usr/bin",
        "RUST_LOG": "warn",
        "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent.sock",
        "XDG_CONFIG_HOME": "/home/droid/.config",
        "GITHUB_TOKEN": "ghp_secret",
        "GH_TOKEN": "gho_secret",
        "OPENAI_API_KEY": "secret",
        "ANTHROPIC_API_KEY": "secret",
        "SLACK_BOT_TOKEN": "secret",
    }

    out = iso.gateway_child_systemd_env(env)

    assert out["HERMES_HOME"] == "/home/droid/.hermes/profiles/discord"
    assert out["HERMES_SESSION_KEY"] == "discord:123"
    assert out["HERMES_PROJECT_PATH"] == "/repo"
    assert out["HERMES_CODEX_WORKER_NETWORK_ACCESS"] == "1"
    assert out["CODEX_HOME"] == "/tmp/codex-home"
    assert out["GH_CONFIG_DIR"] == "/home/droid/.config/gh"
    assert out["GIT_CONFIG_GLOBAL"] == "/home/droid/.gitconfig"
    assert out["GIT_SSH_COMMAND"] == "ssh -F /tmp/ssh_config"
    assert out["HTTPS_PROXY"] == "http://proxy.example.invalid:8080"
    assert out["REQUESTS_CA_BUNDLE"] == "/etc/ssl/certs/ca-certificates.crt"
    assert out["PATH"] == "/usr/bin"
    assert out["RUST_LOG"] == "warn"
    assert out["SSH_AUTH_SOCK"] == "/run/user/1000/ssh-agent.sock"
    assert out["XDG_CONFIG_HOME"] == "/home/droid/.config"
    assert "GITHUB_TOKEN" not in out
    assert "GH_TOKEN" not in out
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
            "HERMES_CODEX_WORKER_NETWORK_ACCESS": "1",
            "GH_CONFIG_DIR": "/home/droid/.config/gh",
            "GIT_CONFIG_GLOBAL": "/home/droid/.gitconfig",
            "HTTPS_PROXY": "http://proxy.example.invalid:8080",
            "SSH_AUTH_SOCK": "/run/user/1000/ssh-agent.sock",
            "GITHUB_TOKEN": "ghp_secret",
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
    assert "--pipe" not in argv
    assert "--working-directory" in argv
    assert str(tmp_path) in argv
    assert "--setenv=HERMES_HOME=/home/droid/.hermes/profiles/discord" in argv
    assert "--setenv=HERMES_SESSION_KEY=discord:123" in argv
    assert "--setenv=HERMES_CODEX_WORKER_NETWORK_ACCESS=1" in argv
    assert "--setenv=GH_CONFIG_DIR=/home/droid/.config/gh" in argv
    assert "--setenv=GIT_CONFIG_GLOBAL=/home/droid/.gitconfig" in argv
    assert "--setenv=HTTPS_PROXY=http://proxy.example.invalid:8080" in argv
    assert "--setenv=SSH_AUTH_SOCK=/run/user/1000/ssh-agent.sock" in argv
    assert all("GITHUB_TOKEN" not in part for part in argv)
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
