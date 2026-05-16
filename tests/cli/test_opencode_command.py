"""Tests for CLI OpenCode worker-mode controls."""

from queue import Queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import cli as cli_mod
from cli import HermesCLI
from hermes_cli.commands import COMMAND_REGISTRY


def _bare_cli():
    cli = object.__new__(HermesCLI)
    cli._opencode_mode = False
    cli._opencode_env_cache = None
    cli._opencode_env_cache_at = 0.0
    cli.model = "gpt-5.4"
    cli.provider = "openai"
    cli.requested_provider = "openai"
    cli.preloaded_skills = []
    return cli


def _ready_status():
    return {
        "available": True,
        "binary_path": "/bin/opencode",
        "version": "1.14.49",
        "credentials_configured": True,
        "credentials_summary": "1 credentials",
        "error": "",
    }


def _blocked_status(error="OpenCode CLI not found in PATH."):
    return {
        "available": False,
        "binary_path": "",
        "version": "",
        "credentials_configured": False,
        "credentials_summary": "",
        "error": error,
    }


def test_opencode_exists_in_command_registry_as_cli_only():
    opencode = next(cmd for cmd in COMMAND_REGISTRY if cmd.name == "opencode")

    assert opencode.cli_only is True
    assert opencode.args_hint == "[on|off|status]"
    assert opencode.subcommands == ("on", "off", "status")


def test_process_command_routes_opencode_status():
    cli = _bare_cli()
    cli._handle_opencode_command = MagicMock()

    cli.process_command("/opencode status")

    cli._handle_opencode_command.assert_called_once_with("/opencode status")


def test_builtin_opencode_wins_over_same_named_skill(monkeypatch):
    cli = _bare_cli()
    cli._handle_opencode_command = MagicMock()
    cli._pending_input = Queue()
    monkeypatch.setattr(
        cli_mod,
        "_skill_commands",
        {"/opencode": {"name": "opencode", "description": "Skill command"}},
    )

    cli.process_command("/opencode do this with the skill")

    cli._handle_opencode_command.assert_called_once()
    assert cli._pending_input.empty()


def test_inspect_opencode_environment_extracts_version_and_credentials(monkeypatch):
    import subprocess

    cli = _bare_cli()
    monkeypatch.setattr(cli_mod.shutil, "which", lambda name: "/bin/opencode")

    def _run(args, **_kwargs):
        if args == ["/bin/opencode", "--version"]:
            return SimpleNamespace(returncode=0, stdout="1.14.49\n", stderr="")
        if args == ["/bin/opencode", "providers", "list"]:
            return SimpleNamespace(
                returncode=0,
                stdout="\x1b[0m\n┌ Credentials\n│\n└  1 credentials\n",
                stderr="",
            )
        raise AssertionError(args)

    monkeypatch.setattr(subprocess, "run", _run)

    status = cli._inspect_opencode_environment(refresh=True)

    assert status["available"] is True
    assert status["binary_path"] == "/bin/opencode"
    assert status["version"] == "1.14.49"
    assert status["credentials_configured"] is True
    assert status["credentials_summary"] == "1 credentials"
    assert status["error"] == ""


def test_opencode_status_reports_integration_disabled(monkeypatch):
    cli = _bare_cli()
    output = []
    monkeypatch.setattr(cli_mod, "_cprint", lambda text: output.append(text))
    monkeypatch.setattr(cli, "_inspect_opencode_environment", lambda refresh=False: _ready_status())

    cli._handle_opencode_command("/opencode status")

    text = "\n".join(output)
    assert "OpenCode worker mode: disabled" in text
    assert "temporarily disabled" in text


def test_opencode_on_is_disabled_even_when_environment_ready(monkeypatch):
    cli = _bare_cli()
    output = []
    monkeypatch.setattr(cli_mod, "_cprint", lambda text: output.append(text))
    monkeypatch.setattr(cli, "_inspect_opencode_environment", lambda refresh=False: _ready_status())

    cli._handle_opencode_command("/opencode on")

    assert cli._opencode_mode is False
    text = "\n".join(output)
    assert "OpenCode worker mode: disabled" in text


def test_opencode_on_does_not_save_config_while_disabled(monkeypatch):
    cli = _bare_cli()
    monkeypatch.setattr(cli_mod, "_cprint", lambda *_a, **_kw: None)
    monkeypatch.setattr(cli, "_inspect_opencode_environment", lambda refresh=False: _ready_status())
    save_config = MagicMock()
    monkeypatch.setattr(cli_mod, "save_config_value", save_config)

    cli._handle_opencode_command("/opencode on")

    assert cli._opencode_mode is False
    save_config.assert_not_called()


def test_opencode_off_disables_current_session_only(monkeypatch):
    cli = _bare_cli()
    monkeypatch.setattr(cli_mod, "_cprint", lambda *_a, **_kw: None)

    cli._handle_opencode_command("/opencode off")

    assert cli._opencode_mode is False


def test_build_opencode_prefix_skips_while_integration_disabled():
    cli = _bare_cli()
    cli.preloaded_skills = ["hermes-agent"]

    prefix = cli._build_opencode_prefix("implement the parser fix in src/parser.py")

    assert prefix == ""


def test_build_opencode_prefix_for_coding_request_when_reenabled(monkeypatch):
    monkeypatch.setattr(cli_mod, "_OPENCODE_WORKER_INTEGRATION_ENABLED", True)
    cli = _bare_cli()
    cli._opencode_mode = True
    cli.preloaded_skills = ["hermes-agent"]

    prefix = cli._build_opencode_prefix("implement the parser fix in src/parser.py")

    assert prefix.startswith("[OpenCode worker mode is enabled for this session.")
    assert "Hermes remains the orchestrator" in prefix
    assert "interactive OpenCode background PTY" in prefix
    assert "opencode --model openai/gpt-5.4-fast" in prefix
    assert "opencode run --model openai/gpt-5.4-fast" in prefix
    assert "FAST mode" in prefix
    assert "--variant high" in prefix
    assert "hermes-agent" in prefix


def test_build_opencode_prefix_skips_non_coding_request():
    cli = _bare_cli()

    assert cli._build_opencode_prefix("what is the weather today?") == ""


def test_build_opencode_prefix_skips_when_disabled():
    cli = _bare_cli()
    cli._opencode_mode = False

    assert cli._build_opencode_prefix("fix tests in test_parser.py") == ""


def test_coding_request_heuristic_negative_and_positive_cases():
    cli = _bare_cli()

    assert cli._looks_like_coding_request("what is opencode?") is False
    assert cli._looks_like_coding_request("what is the README?") is False
    assert cli._looks_like_coding_request("use opencode to refactor cli.py") is True
    assert cli._looks_like_coding_request("please review this diff") is True
    assert cli._looks_like_coding_request("look at src/parser.py") is True


def test_opencode_fast_gpt_model_mapping():
    assert HermesCLI._opencode_fast_gpt_model_id("openai/gpt-5.5") == (
        "openai/gpt-5.5-fast",
        True,
    )
    assert HermesCLI._opencode_fast_gpt_model_id("openai/gpt-5.5-fast") == (
        "openai/gpt-5.5-fast",
        False,
    )
    assert HermesCLI._opencode_fast_gpt_model_id("anthropic/claude-opus-4.6") == (
        "anthropic/claude-opus-4.6",
        False,
    )
    assert HermesCLI._opencode_fast_gpt_model_id("gpt-5.4") == ("gpt-5.4-fast", True)


def test_opencode_model_argument_maps_provider_and_fast_mode():
    cli = _bare_cli()

    model, reason = cli._opencode_model_argument()

    assert model == "openai/gpt-5.4-fast"
    assert "FAST" in reason


def test_opencode_model_argument_preserves_provider_qualified_non_gpt():
    cli = _bare_cli()
    cli.model = "anthropic/claude-opus-4.6"
    cli.provider = "anthropic"

    model, reason = cli._opencode_model_argument()

    assert model == "anthropic/claude-opus-4.6"
    assert "provider-qualified" in reason


def test_opencode_model_argument_surfaces_missing_model():
    cli = _bare_cli()
    cli.model = ""

    model, reason = cli._opencode_model_argument()

    assert model == ""
    assert "not resolved" in reason


def test_opencode_variant_does_not_inherit_hermes_reasoning():
    cli = _bare_cli()
    cli.reasoning_config = {"enabled": True, "effort": "xhigh"}

    variant, reason = cli._choose_opencode_variant("fix a typo in README")

    assert variant == "minimal"
    assert "typo" in reason


def test_risky_audit_escalates_to_max_even_when_hermes_reasoning_is_minimal():
    cli = _bare_cli()
    cli.reasoning_config = {"enabled": True, "effort": "minimal"}

    variant, reason = cli._choose_opencode_variant("use opencode to audit auth credential handling")

    assert variant == "max"
    assert "auth" in reason


def test_unclear_worker_task_uses_default_variant_guidance(monkeypatch):
    monkeypatch.setattr(cli_mod, "_OPENCODE_WORKER_INTEGRATION_ENABLED", True)
    cli = _bare_cli()
    cli._opencode_mode = True

    variant, reason = cli._choose_opencode_variant("use opencode for this worker task")
    prefix = cli._build_opencode_prefix("use opencode for this worker task")

    assert variant == "default"
    assert "no strong" in reason
    assert "configured default variant" in prefix
    assert "--variant default" not in prefix


def test_explicit_worker_variant_request_is_honored():
    cli = _bare_cli()

    variant, reason = cli._choose_opencode_variant(
        "use opencode worker reasoning max to review this diff"
    )

    assert variant == "max"
    assert "explicitly requested" in reason


def _chat_ready_cli():
    cli = _bare_cli()
    cli.agent = SimpleNamespace(
        session_id="sid",
        max_iterations=90,
        run_conversation=MagicMock(
            return_value={"final_response": "", "messages": [], "completed": True, "api_calls": 1}
        ),
    )
    cli.session_id = "sid"
    cli.conversation_history = []
    cli._session_db = None
    cli._active_agent_route_signature = "same"
    cli._ensure_runtime_credentials = lambda: True
    cli._resolve_turn_agent_config = lambda _message: {
        "signature": "same",
        "model": None,
        "runtime": None,
        "request_overrides": None,
    }
    cli._reset_stream_state = lambda: None
    cli._flush_stream = lambda: None
    cli._voice_tts = False
    cli._voice_mode = False
    cli._voice_continuous = False
    cli._clarify_state = None
    cli._clarify_freetext = None
    cli._pending_model_switch_note = None
    cli._pending_skills_reload_note = None
    cli._prompt_start_time = None
    cli._prompt_duration = 0.0
    cli._stream_started = False
    cli._stream_box_opened = False
    cli.show_reasoning = False
    cli.bell_on_complete = False
    cli.final_response_markdown = "strip"
    cli.base_url = ""
    cli.api_key = ""
    cli.api_mode = ""
    return cli


def test_coding_chat_does_not_inject_opencode_prefix_while_disabled():
    cli = _chat_ready_cli()

    cli.chat("implement a small refactor in cli.py")

    kwargs = cli.agent.run_conversation.call_args.kwargs
    assert kwargs["user_message"] == "implement a small refactor in cli.py"
    assert kwargs["persist_user_message"] is None


def test_non_coding_chat_does_not_inject_opencode_prefix():
    cli = _chat_ready_cli()

    cli.chat("what is the weather today?")

    kwargs = cli.agent.run_conversation.call_args.kwargs
    assert kwargs["user_message"] == "what is the weather today?"
    assert kwargs["persist_user_message"] is None


def test_new_session_resets_opencode_mode_to_default_off(monkeypatch):
    cli = _bare_cli()
    cli._opencode_mode = True
    cli.agent = None
    cli.conversation_history = []
    cli._session_db = None
    cli.session_id = "old"
    cli._pending_title = None
    cli._resumed = False
    monkeypatch.setattr(cli_mod, "_cprint", lambda *_a, **_kw: None)

    cli.new_session(silent=True)

    assert cli._opencode_mode is False


def test_resume_resets_opencode_mode_to_default_off(monkeypatch):
    import hermes_cli.main as main_mod

    class FakeSessionDB:
        def get_session(self, session_id):
            return {"id": session_id, "title": "Target"}

        def resolve_resume_session_id(self, session_id):
            return session_id

        def end_session(self, *_args):
            pass

        def get_messages_as_conversation(self, _session_id):
            return []

        def reopen_session(self, _session_id):
            pass

    cli = _bare_cli()
    cli._opencode_mode = False
    cli._session_db = FakeSessionDB()
    cli.session_id = "old"
    cli.agent = None
    cli._pending_title = None
    cli.conversation_history = [{"role": "user", "content": "old"}]
    monkeypatch.setattr(main_mod, "_resolve_session_by_name_or_id", lambda _target: "target")
    monkeypatch.setattr(cli_mod, "_cprint", lambda *_a, **_kw: None)

    cli._handle_resume_command("/resume target")

    assert cli.session_id == "target"
    assert cli._opencode_mode is False
