import json

import tools.terminal_tool as terminal_tool


class _FakeEnv:
    env = {}

    def __init__(self):
        self.commands = []

    def execute(self, command, **kwargs):
        self.commands.append(command)
        return {"output": "ok", "returncode": 0}


def _patch_terminal_env(monkeypatch, fake_env):
    monkeypatch.setattr(terminal_tool, "_active_environments", {"default": fake_env})
    monkeypatch.setattr(terminal_tool, "_last_activity", {"default": 0.0})
    monkeypatch.setattr(terminal_tool, "_task_env_overrides", {})
    monkeypatch.setattr(
        terminal_tool,
        "_get_env_config",
        lambda: {
            "env_type": "local",
            "cwd": "/tmp",
            "timeout": 60,
            "lifetime_seconds": 3600,
        },
    )
    monkeypatch.setattr(
        terminal_tool,
        "_check_all_guards",
        lambda command, env_type, **kwargs: {"approved": True},
    )


def test_gateway_lifecycle_guard_blocks_before_execution(monkeypatch):
    fake_env = _FakeEnv()
    _patch_terminal_env(monkeypatch, fake_env)
    monkeypatch.setenv("_HERMES_GATEWAY", "1")

    result = json.loads(terminal_tool.terminal_tool("systemctl restart hermes-gateway"))

    assert result["exit_code"] == 1
    assert result["status"] == "error"
    assert "Blocked" in result["error"]
    assert fake_env.commands == []


def test_gateway_lifecycle_guard_allows_safe_commands(monkeypatch):
    fake_env = _FakeEnv()
    _patch_terminal_env(monkeypatch, fake_env)
    monkeypatch.setenv("_HERMES_GATEWAY", "1")

    result = json.loads(terminal_tool.terminal_tool("printf ok"))

    assert result["exit_code"] == 0
    assert result["output"] == "ok"
    assert fake_env.commands == ["printf ok"]


def test_gateway_lifecycle_guard_inactive_outside_gateway(monkeypatch):
    fake_env = _FakeEnv()
    _patch_terminal_env(monkeypatch, fake_env)
    monkeypatch.delenv("_HERMES_GATEWAY", raising=False)

    result = json.loads(terminal_tool.terminal_tool("systemctl restart hermes-gateway"))

    assert result["exit_code"] == 0
    assert fake_env.commands == ["systemctl restart hermes-gateway"]
