from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from agent.transports.codex_app_server_session import TurnResult
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


def _parent(tmp_path, api_mode="chat_completions"):
    return SimpleNamespace(
        api_mode=api_mode,
        session_cwd=str(tmp_path),
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
    assert "OpenClaw autoreview skill" in prompt
    assert "after non-trivial code edits and focused checks" in prompt
    assert prompt.index("Open PRs and merge them yourself") < prompt.index("Worker boundary")
    assert result["agents"] == ["build"]
    assert result["plan_used"] is False


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
        assert "OpenClaw autoreview skill" in prompt
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
