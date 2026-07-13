from __future__ import annotations

import copy
import json
from types import SimpleNamespace

from hermes_cli.config import DEFAULT_CONFIG
from hermes_cli.ui_work_routing import resolve_ui_specialist_runtime, resolve_ui_work_route
from tools import coding_worker_tool


def test_specialist_route_is_independent_from_coding_worker_backend():
    config = copy.deepcopy(DEFAULT_CONFIG)
    config["coding_worker"]["backend"] = "opencode"

    decision = resolve_ui_work_route(
        config,
        task="Polish the responsive dashboard layout",
        backend="opencode",
        route_decision={"route": "ui_visual_specialist"},
    )
    runtime = resolve_ui_specialist_runtime(config)

    assert decision.selected_route == "ui_visual_specialist"
    assert decision.selected_provider == "anthropic"
    assert decision.selected_model == "claude-fable-5"
    assert runtime == {
        "backend": "claude_code",
        "binary": "claude",
        "provider": "anthropic",
        "model": "claude-fable-5",
        "reasoning_effort": "medium",
    }


def test_specialist_runner_invokes_claude_code_fable_medium(monkeypatch, tmp_path):
    seen = {}
    claude_config = tmp_path / "account" / ".claude"
    claude_config.mkdir(parents=True)
    (claude_config / ".credentials.json").write_text("{}", encoding="utf-8")

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="Implemented UI and ran checks.\n", stderr="")

    monkeypatch.setattr(coding_worker_tool.shutil, "which", lambda binary: "/usr/bin/claude")
    monkeypatch.setattr(coding_worker_tool.subprocess, "run", fake_run)
    monkeypatch.setattr("hermes_constants._process_user_home", lambda: tmp_path / "account")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "raw-api-key-must-not-leak")
    monkeypatch.setenv("ANTHROPIC_TOKEN", "raw-token-must-not-leak")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "env-oauth-must-not-take-precedence")
    monkeypatch.setenv("PATH", "/safe/runtime/bin")

    result = json.loads(
        coding_worker_tool._run_ui_specialist(
            prompt="Implement the UI",
            workdir=str(tmp_path),
            timeout=120,
            parent_agent=SimpleNamespace(),
            route_metadata={"selected_route": "ui_visual_specialist"},
        )
    )

    assert result["success"] is True
    assert result["backend"] == "claude_code"
    assert seen["args"] == [
        "/usr/bin/claude",
        "--print",
        "--output-format",
        "text",
        "--model",
        "claude-fable-5",
        "--effort",
        "medium",
        "--permission-mode",
        "acceptEdits",
        "--no-session-persistence",
        "Implement the UI",
    ]
    assert seen["cwd"] == str(tmp_path)
    assert seen["env"]["CLAUDE_CONFIG_DIR"] == str(claude_config)
    assert seen["env"]["PATH"] == "/safe/runtime/bin"
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert "ANTHROPIC_TOKEN" not in seen["env"]
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in seen["env"]


def test_specialist_runner_fails_closed_without_claude_oauth(monkeypatch, tmp_path):
    monkeypatch.setattr(coding_worker_tool.shutil, "which", lambda binary: "/usr/bin/claude")
    monkeypatch.setattr("hermes_constants._process_user_home", lambda: tmp_path / "account")

    result = json.loads(
        coding_worker_tool._run_ui_specialist(
            prompt="Implement the UI",
            workdir=str(tmp_path),
            timeout=120,
            parent_agent=SimpleNamespace(),
            route_metadata={"selected_route": "ui_visual_specialist"},
        )
    )

    assert result["success"] is False
    assert "Claude Code OAuth credentials are unavailable for the Fable route" in result["error"]
