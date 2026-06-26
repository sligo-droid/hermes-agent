from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agent.transports.codex_app_server_session import TurnResult
from hermes_cli.config import DEFAULT_CONFIG
from tools import coding_worker_tool as cwt


class FakeSession:
    instances = []
    results = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.closed = False
        self.run_calls = []
        self.auth_payload = None
        if kwargs.get("codex_home"):
            auth_path = Path(kwargs["codex_home"]) / "auth.json"
            if auth_path.exists():
                self.auth_payload = json.loads(auth_path.read_text(encoding="utf-8"))
        FakeSession.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def run_turn(self, **kwargs):
        self.run_calls.append(kwargs)
        if FakeSession.results:
            return FakeSession.results.pop(0)
        return TurnResult(
            final_text="Changed src/app.py and ran pytest.",
            thread_id="thread-1",
            turn_id="turn-1",
            tool_iterations=2,
        )


@pytest.fixture(autouse=True)
def _default_codex_backend(monkeypatch):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: "codex")


def _parent(tmp_path, api_mode="chat_completions"):
    return SimpleNamespace(
        api_mode=api_mode,
        session_cwd=str(tmp_path),
        session_key="discord:123",
        _touch_activity=lambda message: None,
    )


def _skill_block(name, body, directory=None, description=None):
    lines = [
        f'[IMPORTANT: The user launched this CLI session with the "{name}" skill preloaded. Treat its instructions as active guidance for the duration of this session unless the user overrides them.]'
    ]
    if description:
        lines.extend(["---", f"description: {description}", "---"])
    lines.append(body)
    if directory:
        lines.append(f"[Skill directory: {directory}]")
    return "\n".join(lines)


def _stub_general_coding(monkeypatch, content="General coding full body."):
    monkeypatch.setattr(
        cwt,
        "_load_general_coding_skill",
        lambda: cwt._SkillBlock(
            name="general-coding",
            body=content,
            summary="General coding rules.",
            directory="/tmp/general-coding",
        ),
    )


def test_requires_parent_agent():
    result = json.loads(cwt.delegate_coding_task(task="fix bug"))
    assert "requires a parent agent" in result["error"]


def test_unavailable_inside_codex_app_server(tmp_path):
    result = json.loads(
        cwt.delegate_coding_task(
            task="fix bug",
            parent_agent=_parent(tmp_path, api_mode="codex_app_server"),
        )
    )
    assert "unavailable" in result["error"]


def test_default_turn_timeout_is_1800(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: {"coding_worker": {}})

    assert cwt._load_coding_worker_timeout() == 1800.0


def test_runs_codex_app_server_session(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    (tmp_path / "AGENTS.md").write_text(
        "Always use the repo test wrapper. Open PRs and merge them yourself."
    )
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(cwt, "_load_coding_worker_timeout", lambda: 123.0)

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            context="focus on src/parser.py",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["summary"] == "Changed src/app.py and ran pytest."
    assert result["cwd"] == str(tmp_path)
    assert FakeSession.instances[0].kwargs["cwd"] == str(tmp_path)
    assert FakeSession.instances[0].kwargs["env"]["HERMES_SESSION_KEY"] == "discord:123"
    assert "HERMES_CODEX_WORKER_NETWORK_ACCESS" not in FakeSession.instances[0].kwargs["env"]
    assert FakeSession.instances[0].kwargs["scope_kind"] == "coding-worker"
    assert FakeSession.instances[0].kwargs["scope_purpose"] == "Codex coding worker build pass"
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model_reasoning_effort="medium"',
    ]
    assert FakeSession.instances[0].run_calls[0]["turn_timeout"] == 123.0
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "fix the parser" in prompt
    assert "focus on src/parser.py" in prompt
    assert "Repository context loaded by Hermes" in prompt
    assert "## AGENTS.md" in prompt
    assert "Always use the repo test wrapper." in prompt
    assert "Worker boundary" in prompt
    assert "parent Hermes owns all git and PR lifecycle steps" in prompt
    assert "workspace-local autoreview helper" in prompt
    assert ".agents/skills/autoreview/scripts/autoreview --mode local" in prompt
    assert "after non-trivial code edits and focused checks" in prompt
    helper = tmp_path / ".agents" / "skills" / "autoreview" / "scripts" / "autoreview"
    assert helper.exists()
    assert os.access(helper, os.X_OK)
    assert prompt.index("Open PRs and merge them yourself") < prompt.index("Worker boundary")
    assert result["agents"] == ["build"]
    assert result["plan_used"] is False
    assert result["ui_work_route"]["matched"] is False


def test_delegate_repairs_missing_task_from_worker_context(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            context=(
                "User request: fix the parser startup regression.\n\n"
                "Task:\n"
                "Fix the parser startup regression and run focused tests.\n\n"
                "Verification:\n"
                "Report changed files and checks."
            ),
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["task_inferred_from_context"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Hermes tool-call repair: delegate_coding_task was invoked without" in prompt
    assert "Task:\nFix the parser startup regression and run focused tests." in prompt
    task_section = prompt.split("Task:\n", 1)[1].split("\n\nContext from Hermes:", 1)[0]
    assert "Verification:" not in task_section


def test_delegate_falls_back_to_workspace_parent_for_missing_cwd(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    requested = workspace / "reserve-index-dtf"
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Fix the missed PR diff cleanup.",
            context="Locate the checkout or clone it if absent.",
            cwd=str(requested),
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["cwd"] == str(workspace.resolve())
    assert result["cwd_fallback"] == {
        "requested_cwd": str(requested),
        "fallback_cwd": str(workspace.resolve()),
        "reason": "requested cwd did not exist",
    }
    assert FakeSession.instances[0].kwargs["cwd"] == str(workspace.resolve())
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Hermes cwd repair: the requested worker cwd did not exist" in prompt
    assert "First locate an existing checkout or clone/create the intended repository path" in prompt
    assert "Autoreview helper materialization was deferred" in prompt
    assert "Repository context loaded by Hermes" not in prompt
    assert not (workspace / ".agents" / "skills" / "autoreview").exists()


def test_delegate_preserves_json_route_decision_with_missing_cwd_fallback(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    requested = workspace / "missing-command-center-checkout"
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Implement Command Center card footer visual polish.",
            context="Requested checkout is absent; start from workspace parent.",
            cwd=str(requested),
            route_decision=json.dumps(
                {
                    "route": "ui_visual_specialist",
                    "source": "orchestrator",
                    "confidence": 0.97,
                    "rationale": "Command Center UI task",
                }
            ),
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["cwd"] == str(workspace.resolve())
    assert result["cwd_fallback"] == {
        "requested_cwd": str(requested),
        "fallback_cwd": str(workspace.resolve()),
        "reason": "requested cwd did not exist",
    }
    route = result["ui_work_route"]
    assert route["matched"] is True
    assert route["reason"] == "orchestrator route selected ui visual specialist"
    assert route["route_decision"] == "ui_visual_specialist"
    assert route["route_decision_source"] == "orchestrator"
    assert route["route_decision_confidence"] == 0.97
    assert route["route_decision_rationale"] == "Command Center UI task"
    assert route["selected_route"] == "ui_visual_specialist"
    assert route["selected_provider"] == "openrouter"
    assert route["selected_model"] == "z-ai/glm-5.2"
    assert FakeSession.instances[0].kwargs["cwd"] == str(workspace.resolve())
    assert FakeSession.instances[0].kwargs["extra_args"][:4] == [
        "-c",
        'model_provider="openrouter"',
        "-c",
        'model="z-ai/glm-5.2"',
    ]


def test_ui_codex_route_forces_openrouter_key_into_worker_env(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-secret")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Polish the Command Center card spacing.",
            route_decision={"route": "ui_visual_specialist"},
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    env = FakeSession.instances[0].kwargs["env"]
    assert "OPENROUTER_API_KEY" not in env
    assert env["_HERMES_FORCE_OPENROUTER_API_KEY"] == "sk-or-test-secret"


def test_ui_opencode_route_uses_configured_backend_and_model(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "opencode"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        ow,
        "load_coding_worker_backend",
        lambda config=None, worker_config=None: ow.BACKEND_OPENCODE,
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-secret")
    seen = {}

    def fake_run(prompt, workspace, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            final_text="Changed src/app.py and ran npm test.",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(
        cwt.delegate_coding_task(
            task="Polish the Command Center card spacing.",
            route_decision={"route": "ui_visual_specialist"},
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["backend"] == "opencode"
    route = result["ui_work_route"]
    assert route["matched"] is True
    assert route["backend"] == "opencode"
    assert route["selected_provider"] == "openrouter"
    assert route["selected_model"] == "z-ai/glm-5.2"
    assert seen["worker_config"] == {"opencode": {"model": "openrouter/z-ai/glm-5.2"}}
    env = seen["env"]
    assert "OPENROUTER_API_KEY" not in env
    assert env["_HERMES_FORCE_OPENROUTER_API_KEY"] == "sk-or-test-secret"


def test_default_opencode_route_keeps_openrouter_key_scrubbed(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "opencode"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        ow,
        "load_coding_worker_backend",
        lambda config=None, worker_config=None: ow.BACKEND_OPENCODE,
    )
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-secret")
    seen = {}

    def fake_run(prompt, workspace, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            final_text="Changed src/parser.py and ran pytest.",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(
        cwt.delegate_coding_task(
            task="Fix the parser bug.",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["backend"] == "opencode"
    assert "worker_config" not in seen
    assert "env" not in seen


def test_default_codex_route_keeps_openrouter_key_scrubbed(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-secret")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Fix the parser bug.",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    env = FakeSession.instances[0].kwargs["env"]
    assert "OPENROUTER_API_KEY" not in env
    assert "_HERMES_FORCE_OPENROUTER_API_KEY" not in env


def test_authorized_git_pr_lifecycle_updates_prompt_and_codex_env(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    gh_config = tmp_path / "gh"
    gh_config.mkdir()
    git_config = tmp_path / ".gitconfig"
    git_config.write_text("[user]\n\tname = Test\n\temail = test@example.invalid\n")
    monkeypatch.setenv("GH_CONFIG_DIR", str(gh_config))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(git_config))
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix and open a PR",
            parent_agent=_parent(tmp_path),
            allow_git_pr_lifecycle=True,
            trusted_allow_git_pr_lifecycle=True,
        )
    )

    assert result["success"] is True
    kwargs = FakeSession.instances[0].kwargs
    assert kwargs["replace_env"] is False
    env = kwargs["env"]
    assert env["HERMES_SESSION_KEY"] == "discord:123"
    assert env["HERMES_CODEX_WORKER_NETWORK_ACCESS"] == "1"
    assert env["HERMES_CODEX_WORKER_WORKSPACE"] == str(tmp_path)
    assert env["GH_CONFIG_DIR"] == str(gh_config)
    assert env["GIT_CONFIG_GLOBAL"] == str(git_config)
    assert env["SSH_AUTH_SOCK"] == "/tmp/ssh-agent.sock"
    assert "GIT_SSH_COMMAND" not in env
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Git/PR lifecycle is explicitly authorized" in prompt
    assert "open a non-draft PR" in prompt
    assert "Do not create commits or pull requests." not in prompt
    assert "Do not merge PRs" in prompt


def test_authorized_git_pr_lifecycle_preserves_explicit_git_ssh_command(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -F /custom/config")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix and open a PR",
            parent_agent=_parent(tmp_path),
            allow_git_pr_lifecycle=True,
            trusted_allow_git_pr_lifecycle=True,
        )
    )

    assert result["success"] is True
    env = FakeSession.instances[0].kwargs["env"]
    assert env["GIT_SSH_COMMAND"] == "ssh -F /custom/config"
    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env


def test_authorized_git_pr_lifecycle_bypasses_system_ssh_config_for_ssh_remotes(
    monkeypatch, tmp_path
):
    FakeSession.instances = []
    FakeSession.results = []
    cwt.subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=cwt.subprocess.PIPE)
    cwt.subprocess.run(
        ["git", "remote", "add", "origin", "git@github.com:sligo-labs/hermes.git"],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix and open a PR",
            parent_agent=_parent(tmp_path),
            allow_git_pr_lifecycle=True,
            trusted_allow_git_pr_lifecycle=True,
        )
    )

    assert result["success"] is True
    env = FakeSession.instances[0].kwargs["env"]
    assert env["GIT_SSH_COMMAND"] == "ssh -F /dev/null"


def test_authorized_git_pr_lifecycle_does_not_set_git_ssh_command_for_https_remotes(
    monkeypatch, tmp_path
):
    FakeSession.instances = []
    FakeSession.results = []
    cwt.subprocess.run(["git", "init"], cwd=tmp_path, check=True, stdout=cwt.subprocess.PIPE)
    cwt.subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/sligo-labs/hermes.git"],
        cwd=tmp_path,
        check=True,
    )
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix and open a PR",
            parent_agent=_parent(tmp_path),
            allow_git_pr_lifecycle=True,
            trusted_allow_git_pr_lifecycle=True,
        )
    )

    assert result["success"] is True
    env = FakeSession.instances[0].kwargs["env"]
    assert "GIT_SSH_COMMAND" not in env


def test_untrusted_git_pr_lifecycle_request_stays_local_only(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix and open a PR",
            parent_agent=_parent(tmp_path),
            allow_git_pr_lifecycle=True,
        )
    )

    assert result["success"] is True
    env = FakeSession.instances[0].kwargs["env"]
    assert "HERMES_CODEX_WORKER_NETWORK_ACCESS" not in env
    assert "HERMES_CODEX_WORKER_WORKSPACE" not in env
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Do not create commits or pull requests." in prompt
    assert "Git/PR lifecycle is explicitly authorized" not in prompt


def test_registry_ignores_model_supplied_git_pr_lifecycle_authorization(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    entry = cwt.registry.get_entry("delegate_coding_task")
    assert entry is not None
    result = json.loads(
        entry.handler(
            {
                "task": "fix and open a PR",
                "allow_git_pr_lifecycle": True,
            },
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    env = FakeSession.instances[0].kwargs["env"]
    assert "HERMES_CODEX_WORKER_NETWORK_ACCESS" not in env
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Do not create commits or pull requests." in prompt


def test_registry_forwards_route_decision(monkeypatch, tmp_path):
    captured = {}

    def fake_delegate_coding_task(**kwargs):
        captured.update(kwargs)
        return json.dumps({"success": True})

    monkeypatch.setattr(cwt, "delegate_coding_task", fake_delegate_coding_task)
    entry = cwt.registry.get_entry("delegate_coding_task")
    assert entry is not None
    route_decision = {
        "route": "ui_visual_specialist",
        "confidence": 0.91,
        "rationale": "visual implementation",
    }

    result = json.loads(
        entry.handler(
            {
                "task": "polish the AI budget dashboard",
                "route_decision": route_decision,
            },
            parent_agent=_parent(tmp_path),
        )
    )

    assert result == {"success": True}
    assert captured["route_decision"] is route_decision


def test_default_coding_worker_keeps_local_only_sanitized_codex_env(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.invalid:8080")
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -F /custom/config")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix without PR lifecycle",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    kwargs = FakeSession.instances[0].kwargs
    assert kwargs["replace_env"] is False
    assert kwargs["env"]["HERMES_SESSION_KEY"] == "discord:123"
    assert kwargs["env"]["HTTPS_PROXY"] == "http://proxy.example.invalid:8080"
    assert kwargs["env"]["GIT_SSH_COMMAND"] == "ssh -F /custom/config"
    assert kwargs["env"]["SSH_AUTH_SOCK"] == "/tmp/ssh-agent.sock"
    assert "HERMES_CODEX_WORKER_NETWORK_ACCESS" not in kwargs["env"]
    assert "GH_TOKEN" not in kwargs["env"]
    assert "GITHUB_TOKEN" not in kwargs["env"]


def test_worker_env_fallback_does_not_leak_secrets(monkeypatch):
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("GH_CONFIG_DIR", "/home/droid/.config/gh")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/home/droid/.gitconfig")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/ssh-agent.sock")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("GH_TOKEN", "gho_secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")

    env = cwt._coding_worker_fallback_env({"HERMES_SESSION_KEY": "discord:123"})

    assert env["PATH"] == "/usr/bin"
    assert env["GH_CONFIG_DIR"] == "/home/droid/.config/gh"
    assert env["GIT_CONFIG_GLOBAL"] == "/home/droid/.gitconfig"
    assert env["SSH_AUTH_SOCK"] == "/tmp/ssh-agent.sock"
    assert env["HERMES_SESSION_KEY"] == "discord:123"
    assert "OPENAI_API_KEY" not in env
    assert "GH_TOKEN" not in env
    assert "GITHUB_TOKEN" not in env


def test_ui_work_uses_codex_model_overlay(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Review feedback: implement the frontend dashboard layout polish",
            context="Keep the Command Center responsive.",
            route_decision={
                "route": "ui_visual_specialist",
                "confidence": 0.86,
                "rationale": "review feedback requires visual implementation",
            },
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    route = result["ui_work_route"]
    assert route["matched"] is True
    assert route["enabled"] is True
    assert route["reason"] == "orchestrator route selected ui visual specialist"
    assert route["provider"] == "openrouter"
    assert route["model"] == "z-ai/glm-5.2"
    assert route["backend"] == "codex"
    assert route["fallback_allowed"] is True
    assert route["error"] == ""
    assert route["route_decision"] == "ui_visual_specialist"
    assert route["route_decision_source"] == "orchestrator"
    assert route["route_decision_confidence"] == 0.86
    assert route["route_decision_rationale"] == "review feedback requires visual implementation"
    assert route["selected_route"] == "ui_visual_specialist"
    assert route["selected_provider"] == "openrouter"
    assert route["selected_model"] == "z-ai/glm-5.2"
    assert route["fallback_used"] is False
    assert route["fallback_reason"] == ""
    assert route["advisory_matched"] is False
    assert "negative keyword: review" in route["advisory_reason"]
    assert route["recommended_skills"] == [
        "taste-skill",
        "claude-design",
        "popular-web-designs",
    ]
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "UI specialist skill loading" in prompt
    assert "`taste-skill`" in prompt
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model_provider="openrouter"',
        "-c",
        'model="z-ai/glm-5.2"',
        "-c",
        'model_providers.openrouter.name="openrouter"',
        "-c",
        'model_providers.openrouter.base_url="https://openrouter.ai/api/v1"',
        "-c",
        'model_providers.openrouter.env_key="OPENROUTER_API_KEY"',
        "-c",
        'model_reasoning_effort="medium"',
    ]


def test_ui_work_smoke_title_uses_codex_model_overlay(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Smoke-test UI specialist route on Command Center polish.",
            context="This is Command Center visual polish work; verify the UI specialist route.",
            route_decision={"route": "ui_visual_specialist"},
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    route = result["ui_work_route"]
    assert route["matched"] is True
    assert route["selected_route"] == "ui_visual_specialist"
    assert route["selected_provider"] == "openrouter"
    assert route["selected_model"] == "z-ai/glm-5.2"
    assert route["advisory_matched"] is True
    assert "visual ui work" in route["advisory_reason"]
    assert FakeSession.instances[0].kwargs["extra_args"][:4] == [
        "-c",
        'model_provider="openrouter"',
        "-c",
        'model="z-ai/glm-5.2"',
    ]


def test_explicit_default_route_keeps_default_codex_despite_visual_keywords(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Implement the frontend dashboard layout polish",
            context="Keep the Command Center responsive.",
            route_decision={
                "route": "default_coding_worker",
                "confidence": 0.74,
                "rationale": "mostly data plumbing despite visual context",
            },
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    route = result["ui_work_route"]
    assert route["matched"] is False
    assert route["selected_route"] == "default_coding_worker"
    assert route["selected_provider"] == ""
    assert route["selected_model"] == ""
    assert route["route_decision_source"] == "orchestrator"
    assert route["route_decision_confidence"] == 0.74
    assert route["route_decision_rationale"] == "mostly data plumbing despite visual context"
    assert route["advisory_matched"] is True
    assert "visual ui work" in route["advisory_reason"]
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model_reasoning_effort="medium"',
    ]


def test_unknown_route_decision_errors_before_worker_launch(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Implement the frontend dashboard layout polish",
            route_decision={
                "route": "glm_visual",
                "confidence": 0.7,
                "rationale": "bad route",
            },
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is False
    assert result["status"] == "error"
    assert "unknown route_decision route" in result["error"]
    route = result["ui_work_route"]
    assert route["route_decision"] == "glm_visual"
    assert route["route_decision_source"] == "orchestrator"
    assert route["route_decision_confidence"] == 0.7
    assert route["route_decision_rationale"] == "bad route"
    assert route["selected_route"] == "default_coding_worker"
    assert FakeSession.instances == []


def test_ui_work_provider_failure_falls_back_to_default_codex_model(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = [
        TurnResult(
            error="codex app-server startup failed: Model provider `openrouter` not found",
            should_retire=True,
        ),
        TurnResult(
            final_text="Changed src/app.py and ran pytest.",
            thread_id="thread-default",
            turn_id="turn-default",
            tool_iterations=1,
        ),
    ]
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    cfg["ui_work"]["fallback"]["allow_default_worker"] = True
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Implement the frontend dashboard layout polish",
            context="Keep the Command Center responsive.",
            route_decision={"route": "ui_visual_specialist"},
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["thread_id"] == "thread-default"
    assert result["ui_work_route"]["fallback_used"] is True
    assert "openrouter" in result["ui_work_route"]["fallback_reason"].lower()
    assert result["ui_work_route"]["selected_route"] == "default_coding_worker"
    assert result["ui_work_route"]["selected_provider"] == ""
    assert result["ui_work_route"]["selected_model"] == ""
    assert len(FakeSession.instances) == 2
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model_provider="openrouter"',
        "-c",
        'model="z-ai/glm-5.2"',
        "-c",
        'model_providers.openrouter.name="openrouter"',
        "-c",
        'model_providers.openrouter.base_url="https://openrouter.ai/api/v1"',
        "-c",
        'model_providers.openrouter.env_key="OPENROUTER_API_KEY"',
        "-c",
        'model_reasoning_effort="medium"',
    ]
    assert FakeSession.instances[1].kwargs["extra_args"] == [
        "-c",
        'model_reasoning_effort="medium"',
    ]


def test_ui_work_matched_route_fails_closed_when_codex_overlay_unavailable(monkeypatch, tmp_path):
    FakeSession.instances = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    cfg["ui_work"]["fallback"]["allow_default_worker"] = False
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(cwt, "codex_ui_work_extra_args", None, raising=False)

    import builtins

    real_import = builtins.__import__

    def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "hermes_cli.ui_work_routing" and "codex_ui_work_extra_args" in fromlist:
            raise ImportError("overlay helper unavailable")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", blocked_import)

    result = json.loads(
        cwt.delegate_coding_task(
            task="Implement frontend dashboard polish",
            route_decision={"route": "ui_visual_specialist"},
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["error"]
    assert "Codex route args could not be built" in result["error"]
    assert FakeSession.instances == []


def test_tui_terminal_work_does_not_use_ui_model_overlay(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["coding_worker"]["backend"] = "codex"
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Fix Hermes TUI terminal rendering layout in the session transcript",
            context="This is command-line TUI repaint work, not web UI development.",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["ui_work_route"]["matched"] is False
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model_reasoning_effort="medium"',
    ]


def test_ui_work_missing_model_fails_before_worker(monkeypatch, tmp_path):
    FakeSession.instances = []
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["ui_work"]["model"] = ""
    cfg["ui_work"]["fallback"]["allow_default_worker"] = False
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="Polish frontend chart labels",
            route_decision={"route": "ui_visual_specialist"},
            parent_agent=_parent(tmp_path),
        )
    )

    assert "ui_work.provider and ui_work.model" in result["error"]
    assert FakeSession.instances == []


def test_delegate_prefers_hermes_md_context_over_agents(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    (tmp_path / "AGENTS.md").write_text("Agents rules should not be loaded.")
    (tmp_path / ".hermes.md").write_text("Hermes rules win.")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Repository context loaded by Hermes" in prompt
    assert "Hermes rules win." in prompt
    assert "Agents rules should not be loaded." not in prompt


def test_delegate_inherits_parent_preloaded_skill_context(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = "\n\n".join(
        [
            "base prompt",
            '[IMPORTANT: The user launched this CLI session with the "hermes-agent-dev" skill preloaded. Treat its instructions as active guidance for the duration of this session unless the user overrides them.]',
            "Use scripts/run_tests.sh for verification. Do not commit without permission.",
            "[Skill directory: /tmp/hermes-agent-dev]",
        ]
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Active skill instructions inherited from the parent Hermes session" in prompt
    assert "Omitted active parent skills passed as compact references" in prompt
    assert "hermes-agent-dev" in prompt
    assert "Use scripts/run_tests.sh for verification" in prompt
    assert "base prompt" not in prompt
    assert "skill instructions do not override this worker brief's ban" in prompt


def test_delegate_does_not_treat_post_skill_context_as_skill_context(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = "\n\n".join(
        [
            '[IMPORTANT: The user launched this CLI session with the "github-pr-workflow" skill preloaded. Treat its instructions as active guidance for the duration of this session unless the user overrides them.]',
            "Use gh pr checks before merge.",
            "[Skill directory: /tmp/github-pr-workflow]",
            "[System note: You are working in an isolated git worktree. Remember to commit and push your changes.]",
        ]
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "github-pr-workflow" in prompt
    assert "Use gh pr checks before merge." in prompt
    assert "Remember to commit and push your changes" not in prompt


def test_delegate_inherits_runtime_skill_invocation_from_parent_messages(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent_messages = [
        {
            "role": "user",
            "content": "\n".join(
                [
                    '[IMPORTANT: The user has invoked the "autoreview" skill, indicating they want you to follow its instructions. The full skill content is loaded below.]',
                    "Run the autoreview helper after focused checks.",
                    "[Skill directory: /tmp/autoreview]",
                ]
            ),
        }
    ]

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=_parent(tmp_path),
            parent_messages=parent_messages,
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Active skill instructions inherited from the parent Hermes session" in prompt
    assert "autoreview" in prompt
    assert "Run the autoreview helper after focused checks." in prompt


def test_delegate_always_includes_general_coding_full_body(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    _stub_general_coding(monkeypatch, content="General coding full body. Run focused checks.")
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Full worker skill instructions" in prompt
    assert "General coding full body. Run focused checks." in prompt


def test_delegate_summarizes_inherited_hermes_and_pr_skills_by_default(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(cwt, "_load_general_coding_skill", lambda: None)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = "\n\n".join(
        [
            _skill_block(
                "hermes-agent",
                "Hermes summary line.\nFull Hermes body must stay omitted.",
                directory="/tmp/hermes-agent",
            ),
            _skill_block(
                "github-pr-workflow",
                "PR summary line.\nFull PR body must stay omitted.",
                directory="/tmp/github-pr-workflow",
            ),
        ]
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Omitted active parent skills passed as compact references" in prompt
    assert "hermes-agent: Hermes summary line" in prompt
    assert "Skill directory: /tmp/hermes-agent" in prompt
    assert "github-pr-workflow: PR summary line" in prompt
    assert "Skill directory: /tmp/github-pr-workflow" in prompt
    assert "Full Hermes body must stay omitted" not in prompt
    assert "Full PR body must stay omitted" not in prompt


def test_delegate_passes_full_body_for_explicit_worker_relevant_skill(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(cwt, "_load_general_coding_skill", lambda: None)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = _skill_block(
        "autoreview",
        "Autoreview summary line. Full autoreview worker instructions.",
        directory="/tmp/autoreview",
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser\nworker-relevant skill: autoreview",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Full explicitly worker-relevant inherited skill instructions" in prompt
    assert "Full autoreview worker instructions." in prompt
    assert "Omitted active parent skills" not in prompt


def test_general_coding_does_not_consume_inherited_skill_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(cwt, "_INHERITED_SKILL_CONTEXT_BUDGET_CHARS", 500)
    _stub_general_coding(monkeypatch, content="General coding full body. " + "g" * 2000)
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = _skill_block(
        "hermes-agent",
        "Hermes compact summary.\nFull Hermes body must stay omitted.",
        directory="/tmp/hermes-agent",
    )

    context = cwt._parent_skill_context(parent)

    assert "General coding full body" in context
    assert "hermes-agent: Hermes compact summary" in context
    assert "Skill directory: /tmp/hermes-agent" in context
    assert "Full Hermes body must stay omitted" not in context


def test_parent_skill_context_budget_omits_oversized_relevant_skill(monkeypatch, tmp_path):
    monkeypatch.setattr(cwt, "_load_general_coding_skill", lambda: None)
    monkeypatch.setattr(cwt, "_INHERITED_SKILL_CONTEXT_BUDGET_CHARS", 250)
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = _skill_block(
        "big-skill",
        "x" * 500,
        directory="/tmp/big-skill",
    )

    context = cwt._parent_skill_context(
        parent,
        task="fix bug\npass full skill: big-skill",
    )

    assert "Inherited skill context budget note" in context
    assert "250-character budget" in context
    assert "big-skill" not in context.split("Inherited skill context budget note", 1)[0]


def test_registry_parent_messages_dispatch_keeps_relevant_skill_full(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(cwt, "_load_general_coding_skill", lambda: None)
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    parent_messages = [
        {
            "role": "user",
            "content": _skill_block(
                "autoreview",
                "Autoreview summary line. Registry dispatch full body.",
                directory="/tmp/autoreview",
            ),
        }
    ]

    result = json.loads(
        cwt.registry.dispatch(
            "delegate_coding_task",
            {
                "task": "fix the parser\nworker skill: autoreview",
                "_parent_messages": parent_messages,
            },
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Registry dispatch full body." in prompt


def test_delegate_does_not_add_skill_context_when_parent_has_no_loaded_skills(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(cwt, "_load_general_coding_skill", lambda: None)
    parent = _parent(tmp_path)
    parent.ephemeral_system_prompt = "General non-skill instruction."

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Active skill instructions inherited" not in prompt
    assert "General non-skill instruction" not in prompt


def test_delegate_reports_autoreview_materialization_failure(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda _workdir: (_ for _ in ()).throw(RuntimeError("readonly workspace")),
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    prompt = FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Autoreview helper materialization failed before worker start: readonly workspace" in prompt


def test_codex_backend_runs_plan_then_build_for_complex_task(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = [
        TurnResult(
            final_text="Plan: inspect auth boundary, patch parser, run tests.",
            thread_id="thread-plan",
            turn_id="turn-plan",
            tool_iterations=1,
        ),
        TurnResult(
            final_text="Implemented the auth fix and ran pytest.",
            thread_id="thread-build",
            turn_id="turn-build",
            tool_iterations=3,
        ),
    ]
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix production auth race",
            context="focus on src/auth.py",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert result["backend"] == "codex"
    assert result["agents"] == ["plan", "build"]
    assert result["plan_used"] is True
    assert result["summary"] == "Implemented the auth fix and ran pytest."
    assert result["thread_id"] == "thread-build"
    assert result["tool_iterations"] == 4
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model_reasoning_effort="xhigh"',
    ]
    assert FakeSession.instances[1].kwargs["extra_args"] == [
        "-c",
        'model_reasoning_effort="medium"',
    ]
    assert "Do not edit repository files" in FakeSession.instances[0].run_calls[0]["user_input"]
    assert "Codex plan to follow:" in FakeSession.instances[1].run_calls[0]["user_input"]


def test_codex_backend_uses_configured_reasoning_levels(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    FakeSession.instances = []
    FakeSession.results = [
        TurnResult(final_text="Plan", thread_id="thread-plan", turn_id="turn-plan"),
        TurnResult(final_text="Built", thread_id="thread-build", turn_id="turn-build"),
    ]
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )
    monkeypatch.setattr(
        ow,
        "load_coding_worker_pass_config",
        lambda: {
            "simple_build_reasoning_level": "low",
            "complex_plan_reasoning_level": "max",
            "complex_build_reasoning_level": "high",
        },
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix production auth race",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert FakeSession.instances[0].kwargs["extra_args"] == [
        "-c",
        'model_reasoning_effort="max"',
    ]
    assert FakeSession.instances[1].kwargs["extra_args"] == [
        "-c",
        'model_reasoning_effort="high"',
    ]


def test_delegate_uses_opencode_backend_when_configured(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    (tmp_path / "AGENTS.md").write_text("OpenCode should see repo rules.")
    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    activity_messages = []
    parent = SimpleNamespace(
        api_mode="chat_completions",
        session_cwd=str(tmp_path),
        _touch_activity=activity_messages.append,
    )

    def fake_run(prompt, workspace, **kwargs):
        assert "fix the parser" in prompt
        assert "OpenCode should see repo rules." in prompt
        assert "workspace-local autoreview helper" in prompt
        assert workspace == str(tmp_path)
        assert kwargs["context_for_classification"]
        assert callable(kwargs["on_event"])
        kwargs["on_event"]({"type": "message", "agent": "build"})
        kwargs["on_event"](["unexpected event shape"])
        return SimpleNamespace(
            final_text="Changed src/parser.py and ran pytest.",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            context="focus on src/parser.py",
            parent_agent=parent,
        )
    )

    assert result["success"] is True
    assert result["backend"] == "opencode"
    assert result["agents"] == ["build"]
    assert result["summary"] == "Changed src/parser.py and ran pytest."
    assert activity_messages == ["OpenCode coding worker event: message: build"]


def test_delegate_opencode_preserves_parent_scope_when_backend_supports_it(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    parent = _parent(tmp_path)
    seen = {}

    def fake_run(prompt, workspace, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            final_text="done",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(cwt.delegate_coding_task(task="fix parser", parent_agent=parent))

    assert result["success"] is True
    assert seen["scope_session_key"] == "discord:123"


def test_preflight_repairs_canonical_cwd_to_existing_worktree(monkeypatch, tmp_path):
    canonical = tmp_path / "canonical" / "hermes"
    workspace_root = tmp_path / "workspaces"
    worktree = workspace_root / "hermes"
    canonical.mkdir(parents=True)
    worktree.mkdir(parents=True)
    parent = _parent(canonical)
    parent._coding_worker_required_this_turn = True

    monkeypatch.setattr(cwt, "_workspaces_path", lambda: workspace_root)
    monkeypatch.setattr(
        "tools.canonical_repo_guard.canonical_main_worker_violation",
        lambda workdir: "BLOCKED: delegate_coding_task was pointed at a protected canonical checkout",
    )
    monkeypatch.setattr(
        cwt,
        "_mutable_worktree_for_canonical_cwd",
        lambda workdir: str(worktree),
    )

    preflight = cwt.preflight_delegate_coding_task(
        {"task": "fix startup", "cwd": str(canonical), "context": "details"},
        parent,
    )

    assert preflight.suppressed_result is None
    assert preflight.args["cwd"] == str(worktree)
    assert "protected canonical cwd" in preflight.args["context"]


def test_preflight_suppresses_missing_worktree_for_required_canonical_cwd(monkeypatch, tmp_path):
    canonical = tmp_path / "canonical" / "hermes"
    canonical.mkdir(parents=True)
    parent = _parent(canonical)
    parent._coding_worker_required_this_turn = True

    monkeypatch.setattr(
        "tools.canonical_repo_guard.canonical_main_worker_violation",
        lambda workdir: "BLOCKED: delegate_coding_task was pointed at a protected canonical checkout",
    )
    monkeypatch.setattr(cwt, "_mutable_worktree_for_canonical_cwd", lambda workdir: None)

    preflight = cwt.preflight_delegate_coding_task(
        {"task": "fix startup", "cwd": str(canonical)},
        parent,
    )

    result = json.loads(preflight.suppressed_result)
    assert "could not find a mutable" in result["error"]
    assert "BLOCKED:" not in result["error"]


def test_delegate_opencode_omits_parent_scope_for_legacy_backend(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    seen = {}

    def fake_legacy_run(prompt, workspace, *, timeout, context_for_classification, title, on_event):
        seen.update(
            timeout=timeout,
            context_for_classification=context_for_classification,
            title=title,
            on_event=on_event,
        )
        return SimpleNamespace(
            final_text="done",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_legacy_run)

    result = json.loads(
        cwt.delegate_coding_task(
            task="system-doctor delegated worker after compaction",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert seen["title"] == "Hermes delegated coding task"


def test_delegate_opencode_retries_when_passthrough_signature_rejects_scope(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    calls = []

    def fake_wrapper(prompt, workspace, **kwargs):
        calls.append(dict(kwargs))
        if "scope_session_key" in kwargs:
            raise TypeError("run_opencode_task() got an unexpected keyword argument 'scope_session_key'")
        return SimpleNamespace(
            final_text="done",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_wrapper)

    result = json.loads(
        cwt.delegate_coding_task(
            task="system-doctor delegated worker after compaction",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    assert len(calls) == 2
    assert calls[0]["scope_session_key"] == "discord:123"
    assert "scope_session_key" not in calls[1]


def test_delegate_opencode_no_final_metadata_is_additive_and_degraded(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    metadata = {
        "classification": "no_final_text",
        "evidence_status": "degraded",
        "failure_class": "no_final_text",
        "backend": "opencode",
        "thread_id": "ses-empty",
        "turn_id": "ses-empty",
        "cwd": str(tmp_path),
        "branch": "feature",
        "commit": "abc123",
        "export_status": {"status": "empty", "session_id": "ses-empty"},
        "stderr_snippet": "",
        "error_snippet": "OpenCode completed without producing final text.",
        "local_file_changes": False,
        "local_commit_detected": False,
        "clean_committed_branch": False,
    }

    def fake_run(prompt, workspace, **kwargs):
        return SimpleNamespace(
            final_text="",
            error="OpenCode completed without producing final text.",
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-empty",
            turn_id="ses-empty",
            tool_iterations=1,
            no_final_metadata=metadata,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(cwt.delegate_coding_task(task="fix parser", parent_agent=_parent(tmp_path)))

    assert result["success"] is False
    assert result["status"] == "partial"
    assert result["backend"] == "opencode"
    assert result["summary"] == ""
    assert result["error"] == "OpenCode completed without producing final text."
    assert result["evidence_status"] == "degraded"
    assert result["failure_class"] == "no_final_text"
    assert result["no_final_metadata"] == metadata


def test_delegate_opencode_no_final_clean_commit_is_recoverable(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    metadata = {
        "classification": "no_final_text",
        "evidence_status": "recoverable_degraded",
        "failure_class": "no_final_text",
        "backend": "opencode",
        "thread_id": "ses-committed",
        "turn_id": "ses-committed",
        "cwd": str(tmp_path),
        "branch": "feature",
        "commit": "def456",
        "export_status": {"status": "empty", "session_id": "ses-committed"},
        "stderr_snippet": "",
        "error_snippet": "OpenCode completed without producing final text.",
        "local_file_changes": False,
        "local_commit_detected": True,
        "clean_committed_branch": True,
    }

    def fake_run(prompt, workspace, **kwargs):
        return SimpleNamespace(
            final_text="",
            error="OpenCode completed without producing final text.",
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-committed",
            turn_id="ses-committed",
            tool_iterations=1,
            no_final_metadata=metadata,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(cwt.delegate_coding_task(task="fix parser", parent_agent=_parent(tmp_path)))

    assert result["success"] is False
    assert result["evidence_status"] == "recoverable_degraded"
    assert result["failure_class"] == "no_final_text"
    assert result["no_final_metadata"]["local_commit_detected"] is True
    assert result["no_final_metadata"]["clean_committed_branch"] is True


def test_delegate_includes_repo_state_preflight(monkeypatch, tmp_path):
    from agent import opencode_worker as ow

    monkeypatch.setattr(ow, "load_coding_worker_backend", lambda: ow.BACKEND_OPENCODE)
    monkeypatch.setattr(
        cwt,
        "_repo_state_guard_notes",
        lambda workdir: "Repository state preflight:\n- concerns: dirty worktree",
    )

    def fake_run(prompt, workspace, **kwargs):
        assert "Repository state preflight:" in prompt
        assert "dirty worktree" in prompt
        assert "fix the parser" in prompt
        return SimpleNamespace(
            final_text="done",
            error=None,
            interrupted=False,
            agents=["build"],
            plan_text="",
            thread_id="ses-build",
            turn_id="ses-build",
            tool_iterations=1,
        )

    monkeypatch.setattr(ow, "run_opencode_task", fake_run)

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True


def test_prepare_pnpm_dependency_links_reuses_matching_worktree(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    package = repo / "dashboard"
    source_package = worktree / "dashboard"
    package.mkdir(parents=True)
    source_package.mkdir(parents=True)
    for root in (package, source_package):
        (root / "package.json").write_text('{"name":"dashboard"}')
        (root / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (source_package / "node_modules").mkdir()

    monkeypatch.setattr(cwt, "_repo_root_for_path", lambda path: repo)
    monkeypatch.setattr(cwt, "_git_worktree_paths", lambda root: [repo, worktree])

    notes = cwt._prepare_pnpm_dependency_links(str(repo))

    assert notes == [f"linked {package / 'node_modules'} -> {source_package / 'node_modules'}"]
    assert (package / "node_modules").is_symlink()
    assert (package / "node_modules").resolve() == (source_package / "node_modules").resolve()


def test_prepare_pnpm_dependency_links_requires_matching_lock(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    worktree = tmp_path / "worktree"
    package = repo / "dashboard"
    source_package = worktree / "dashboard"
    package.mkdir(parents=True)
    source_package.mkdir(parents=True)
    (package / "package.json").write_text('{"name":"dashboard"}')
    (package / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
    (source_package / "package.json").write_text('{"name":"dashboard"}')
    (source_package / "pnpm-lock.yaml").write_text("lockfileVersion: '8.0'\n")
    (source_package / "node_modules").mkdir()

    monkeypatch.setattr(cwt, "_repo_root_for_path", lambda path: repo)
    monkeypatch.setattr(cwt, "_git_worktree_paths", lambda root: [repo, worktree])

    assert cwt._prepare_pnpm_dependency_links(str(repo)) == []
    assert not (package / "node_modules").exists()


def test_runs_with_available_codex_pool_credential(monkeypatch, tmp_path):
    FakeSession.instances = []
    FakeSession.results = []
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {
                    "openai-codex": [
                        {
                            "id": "cred-1",
                            "label": "primary",
                            "auth_type": "oauth",
                            "priority": 0,
                            "source": "manual:device_code",
                            "access_token": "access-1",
                            "refresh_token": "refresh-1",
                            "id_token": "id-1",
                            "last_status": "exhausted",
                            "last_status_at": time.time(),
                            "last_error_code": 429,
                            "last_error_reset_at": time.time() + 5 * 3600,
                        },
                        {
                            "id": "cred-2",
                            "label": "secondary",
                            "auth_type": "oauth",
                            "priority": 1,
                            "source": "manual:device_code",
                            "access_token": "access-2",
                            "refresh_token": "refresh-2",
                            "id_token": "id-2",
                        },
                    ]
                },
            }
        )
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        FakeSession,
    )

    result = json.loads(
        cwt.delegate_coding_task(
            task="fix the parser",
            parent_agent=_parent(tmp_path),
        )
    )

    assert result["success"] is True
    codex_home = FakeSession.instances[0].kwargs["codex_home"]
    assert codex_home
    payload = FakeSession.instances[0].auth_payload
    assert payload is not None
    assert payload["tokens"]["access_token"] == "access-2"
    assert payload["tokens"]["refresh_token"] == "refresh-2"
    assert not Path(codex_home).exists()


def test_codex_worker_home_prefers_parent_current_credential(tmp_path):
    from agent.codex_worker_auth import prepare_codex_worker_home

    current_entry = SimpleNamespace(
        id="cred-current",
        access_token="access-current",
        refresh_token="refresh-current",
        id_token="id-current",
        last_status=None,
    )
    pool = SimpleNamespace(
        current=lambda: current_entry,
        select=MagicMock(),
    )
    parent = SimpleNamespace(provider="openai-codex", _credential_pool=pool)

    credential_id = prepare_codex_worker_home(tmp_path / "codex-home", parent_agent=parent)

    payload = json.loads((tmp_path / "codex-home" / "auth.json").read_text())
    assert credential_id == "cred-current"
    assert payload["tokens"]["access_token"] == "access-current"
    assert payload["tokens"]["refresh_token"] == "refresh-current"
    pool.select.assert_not_called()
