from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from tools.terminal_tool import terminal_tool


def _config(cwd: str) -> dict:
    return {
        "env_type": "local",
        "timeout": 180,
        "cwd": cwd,
        "host_cwd": None,
        "modal_mode": "auto",
        "docker_image": "",
        "singularity_image": "",
        "modal_image": "",
        "daytona_image": "",
        "local_persistent": False,
    }


def test_terminal_blocks_exact_lock_install_before_execute(tmp_path):
    env = MagicMock()
    block = {
        "code": "exact_lock_install_blocked",
        "package_root": str(tmp_path),
        "node_modules": str(tmp_path / "node_modules"),
        "shared_target": "/primary/node_modules",
        "remediation": "unlink node_modules before installing",
    }
    with (
        patch("tools.terminal_tool._get_env_config", return_value=_config(str(tmp_path))),
        patch("tools.terminal_tool._start_cleanup_thread"),
        patch("tools.terminal_tool._active_environments", {"default": env}),
        patch("tools.terminal_tool._last_activity", {"default": 0}),
        patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}),
        patch("agent.terminal_outcomes.exact_lock_pnpm_install_block", return_value=block),
    ):
        result = json.loads(terminal_tool("pnpm install"))

    env.execute.assert_not_called()
    assert result["status"] == "blocked"
    assert result["exit_code"] == 126
    assert result["install_guard"] == block


def test_terminal_noninstall_pnpm_command_reaches_execute(tmp_path):
    env = MagicMock()
    env.execute.return_value = {"output": "ok", "returncode": 0}
    with (
        patch("tools.terminal_tool._get_env_config", return_value=_config(str(tmp_path))),
        patch("tools.terminal_tool._start_cleanup_thread"),
        patch("tools.terminal_tool._active_environments", {"default": env}),
        patch("tools.terminal_tool._last_activity", {"default": 0}),
        patch("tools.terminal_tool._check_all_guards", return_value={"approved": True}),
        patch("agent.terminal_outcomes.exact_lock_pnpm_install_block", return_value=None),
    ):
        result = json.loads(terminal_tool("pnpm test"))

    env.execute.assert_called_once()
    assert result["exit_code"] == 0
    assert result["classification"]["semantic_failure"] is False
