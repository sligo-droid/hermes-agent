from __future__ import annotations

from hermes_cli import coding_worker_switch as cws


def test_parse_args():
    assert cws.parse_args("") == (None, [])
    assert cws.parse_args("status") == (None, [])
    assert cws.parse_args("on") == (True, [])
    assert cws.parse_args("off") == (False, [])

    value, errors = cws.parse_args("maybe")
    assert value is None
    assert errors and "Unknown coding-worker" in errors[0]


def test_default_enabled_when_unset():
    assert cws.get_enabled({}) is True
    assert cws.get_enabled({"coding_worker": {}}) is True
    assert cws.get_enabled(None) is True  # type: ignore[arg-type]


def test_apply_persists_toggle():
    cfg = {}
    persisted = {}

    def persist(config):
        persisted.update(config)

    result = cws.apply(cfg, False, persist_callback=persist)

    assert result.success is True
    assert result.enabled is False
    assert result.old_enabled is True
    assert cfg["coding_worker"]["enabled"] is False
    assert persisted["coding_worker"]["enabled"] is False


def test_coding_request_detection_is_conservative():
    assert cws.looks_like_coding_request("implement the parser fix in src/parser.py")
    assert cws.looks_like_coding_request("use coding worker to debug failing tests")
    assert cws.looks_like_coding_request("review this diff before merge")
    assert cws.looks_like_coding_request("add a dashboard component for sessions")
    assert cws.looks_like_coding_request("wire up the CLI command config loader")
    assert cws.looks_like_coding_request("integrate the auth provider")
    assert cws.looks_like_coding_request("write tests for the gateway route")
    assert cws.looks_like_coding_request("add regression test coverage")
    assert not cws.looks_like_coding_request("what is the weather today?")


def test_coding_worker_meta_questions_do_not_trigger_guidance():
    assert not cws.looks_like_coding_request(
        'we just set "openai_runtime: auto" with coding-worker; why are responses slower?'
    )
    assert not cws.looks_like_coding_request(
        "tighten the heuristic for invoking codex/opencode later"
    )
    assert not cws.looks_like_coding_request(
        "when does the handoff to codex app server happen?"
    )
    assert not cws.looks_like_coding_request("improve the routing criteria")


def test_echoed_coding_worker_guidance_is_ignored_before_classification():
    prefix = cws.build_worker_guidance(
        "fix tests in tests/test_parser.py",
        enabled=True,
        tool_available=True,
        api_mode="chat_completions",
    )

    assert not cws.looks_like_coding_request(prefix)
    assert not cws.looks_like_coding_request(
        prefix + "what is responsible for the performance difference?"
    )
    assert cws.looks_like_coding_request(prefix + "implement the fix in src/parser.py")


def test_worker_guidance_requires_enabled_tool_and_normal_runtime():
    msg = "debug the failing tests in tests/test_parser.py"

    assert cws.build_worker_guidance(
        msg,
        enabled=True,
        tool_available=True,
        api_mode="chat_completions",
    )
    assert cws.build_worker_guidance(
        msg,
        enabled=False,
        tool_available=True,
        api_mode="chat_completions",
    ) == ""
    assert cws.build_worker_guidance(
        msg,
        enabled=True,
        tool_available=False,
        api_mode="chat_completions",
    ) == ""
    assert cws.build_worker_guidance(
        msg,
        enabled=True,
        tool_available=True,
        api_mode="codex_app_server",
    ) == ""


def test_simple_non_hermes_ui_edits_do_not_delegate():
    decision = cws.assess_worker_routing(
        'remove the three header pills from the dashboard page',
        enabled=True,
        tool_available=True,
        api_mode="chat_completions",
        cwd="/tmp/client-app",
    )

    assert decision.coding_request is True
    assert decision.should_delegate is False
    assert decision.guidance == ""


def test_explicit_worker_request_still_delegates_simple_edit():
    decision = cws.assess_worker_routing(
        'use coding worker to remove the three header pills from the dashboard page',
        enabled=True,
        tool_available=True,
        api_mode="chat_completions",
        cwd="/tmp/client-app",
    )

    assert decision.should_delegate is True
    assert "Small localized edits" in decision.guidance


def test_parent_guidance_keeps_post_worker_checks_minimal(monkeypatch, tmp_path):
    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir()
    monkeypatch.setattr(cws, "_known_hermes_roots", lambda: (hermes_root,))
    monkeypatch.setattr(cws, "_git_common_dir", lambda cwd: None)

    guidance = cws.build_worker_guidance(
        "add a gateway regression test",
        enabled=True,
        tool_available=True,
        api_mode="chat_completions",
        cwd=str(hermes_root),
    )

    assert "minimal sanity check" in guidance
    assert "comprehensive testing belongs to the worker" in guidance


def test_parent_guidance_names_pr_boundary_workflow(monkeypatch, tmp_path):
    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir()
    monkeypatch.setattr(cws, "_known_hermes_roots", lambda: (hermes_root,))
    monkeypatch.setattr(cws, "_git_common_dir", lambda cwd: None)

    guidance = cws.build_worker_guidance(
        "fix the Hermes gateway regression",
        enabled=True,
        tool_available=True,
        api_mode="chat_completions",
        cwd=str(hermes_root),
    )

    assert "github-pr-workflow" in guidance
    assert "PR boundary" in guidance
    assert "worker returned code changes or a committed repo fix" in guidance
    assert "complete PR->CI->merge->pull" in guidance
    assert "review-only or blocked" in guidance


def test_hermes_context_detects_prompt_references_and_cwd(monkeypatch, tmp_path):
    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir()
    monkeypatch.setattr(cws, "_known_hermes_roots", lambda: (hermes_root,))
    monkeypatch.setattr(cws, "_git_common_dir", lambda cwd: None)

    assert cws.message_references_hermes_repo("update the Hermes repo CLI command")
    assert cws.message_references_hermes_repo(f"fix {hermes_root} run_agent.py")
    assert cws.message_references_hermes_repo("add tests in ~/hermes")
    assert cws.cwd_is_hermes_repo(str(hermes_root / "agent"))


def test_hermes_context_detects_git_worktree_common_dir(monkeypatch, tmp_path):
    hermes_root = tmp_path / "hermes"
    worktree = tmp_path / "worktrees" / "feature"
    worktree.mkdir(parents=True)
    monkeypatch.setattr(cws, "_known_hermes_roots", lambda: (hermes_root,))
    monkeypatch.setattr(cws, "_git_common_dir", lambda cwd: hermes_root / ".git")

    assert cws.cwd_is_hermes_repo(str(worktree))


def test_hermes_coding_request_mandates_coding_worker(monkeypatch, tmp_path):
    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir()
    monkeypatch.setattr(cws, "_known_hermes_roots", lambda: (hermes_root,))
    monkeypatch.setattr(cws, "_git_common_dir", lambda cwd: None)

    decision = cws.assess_worker_routing(
        "add a gateway regression test",
        enabled=True,
        tool_available=True,
        api_mode="chat_completions",
        cwd=str(hermes_root),
    )

    assert decision.required is True
    assert decision.should_delegate is True
    assert "must call `delegate_coding_task`" in decision.guidance


def test_qmd_service_setup_in_hermes_cwd_stays_operational(monkeypatch, tmp_path):
    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir()
    monkeypatch.setattr(cws, "_known_hermes_roots", lambda: (hermes_root,))
    monkeypatch.setattr(cws, "_git_common_dir", lambda cwd: None)

    decision = cws.assess_worker_routing(
        "fix QMD service persistence by creating qmd-pid.service",
        enabled=True,
        tool_available=True,
        api_mode="chat_completions",
        cwd=str(hermes_root),
    )

    assert decision.required is False
    assert decision.should_delegate is False
    assert decision.guidance == ""


def test_qmd_repo_docs_request_still_mandates_worker(monkeypatch, tmp_path):
    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir()
    monkeypatch.setattr(cws, "_known_hermes_roots", lambda: (hermes_root,))
    monkeypatch.setattr(cws, "_git_common_dir", lambda cwd: None)

    decision = cws.assess_worker_routing(
        "update optional-skills/research/qmd/SKILL.md for the systemd service section",
        enabled=True,
        tool_available=True,
        api_mode="chat_completions",
        cwd=str(hermes_root),
    )

    assert decision.required is True
    assert decision.should_delegate is True


def test_hermes_coding_request_ignores_disabled_toggle(monkeypatch, tmp_path):
    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir()
    monkeypatch.setattr(cws, "_known_hermes_roots", lambda: (hermes_root,))
    monkeypatch.setattr(cws, "_git_common_dir", lambda cwd: None)

    decision = cws.assess_worker_routing(
        "add a gateway regression test",
        enabled=False,
        tool_available=True,
        api_mode="chat_completions",
        cwd=str(hermes_root),
    )

    assert decision.required is True
    assert decision.should_delegate is True


def test_hermes_context_enables_improvement_classifier(monkeypatch, tmp_path):
    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir()
    monkeypatch.setattr(cws, "_known_hermes_roots", lambda: (hermes_root,))
    monkeypatch.setattr(cws, "_git_common_dir", lambda cwd: None)

    for message in (
        "tighten the heuristic",
        "improve the routing criteria",
    ):
        decision = cws.assess_worker_routing(
            message,
            enabled=False,
            tool_available=True,
            api_mode="chat_completions",
            cwd=str(hermes_root),
        )
        assert decision.required is True
        assert decision.coding_request is True


def test_improvement_classifier_requires_hermes_context():
    decision = cws.assess_worker_routing(
        "tighten the heuristic for invoking codex/opencode later",
        enabled=True,
        tool_available=True,
        api_mode="chat_completions",
        cwd="/tmp/not-hermes",
    )

    assert decision == cws.CodingWorkerRoutingDecision()


def test_hermes_coding_request_fails_loud_when_tool_unavailable(monkeypatch, tmp_path):
    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir()
    monkeypatch.setattr(cws, "_known_hermes_roots", lambda: (hermes_root,))
    monkeypatch.setattr(cws, "_git_common_dir", lambda cwd: None)

    decision = cws.assess_worker_routing(
        "update the TUI session state",
        enabled=True,
        tool_available=False,
        api_mode="chat_completions",
        cwd=str(hermes_root),
    )

    assert decision.required is True
    assert decision.fail_loud is True
    assert "tool is unavailable" in decision.guidance


def test_full_codex_app_server_runtime_remains_opt_in(monkeypatch, tmp_path):
    hermes_root = tmp_path / "hermes"
    hermes_root.mkdir()
    monkeypatch.setattr(cws, "_known_hermes_roots", lambda: (hermes_root,))

    assert cws.assess_worker_routing(
        "update the Hermes repo CLI command",
        enabled=True,
        tool_available=True,
        api_mode="codex_app_server",
        cwd=str(hermes_root),
    ) == cws.CodingWorkerRoutingDecision()
