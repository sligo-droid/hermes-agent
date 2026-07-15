from __future__ import annotations

from types import SimpleNamespace
from queue import SimpleQueue
from unittest.mock import patch


def test_cli_fable_prints_usage_for_empty_command():
    import cli as cli_mod

    stub = SimpleNamespace(session_id="s1")
    with patch.object(cli_mod, "_cprint") as mock_cprint:
        cli_mod.HermesCLI._handle_fable_command(stub, "/fable")

    printed = " ".join(str(call) for call in mock_cprint.call_args_list)
    assert "Usage: /fable <request>" in printed


def test_cli_fable_enqueues_plan_skill_invocation(monkeypatch):
    import cli as cli_mod

    def fake_build(request):
        assert request.prompt == "plan the work"
        assert request.platform == "cli"
        return "PLAN SKILL MESSAGE"

    monkeypatch.setattr("hermes_cli.fable_planner.build_fable_plan_invocation", fake_build)
    pending_input = SimpleQueue()
    stub = SimpleNamespace(session_id="s1", _pending_input=pending_input)

    cli_mod.HermesCLI._handle_fable_command(stub, "/fable plan the work")

    assert pending_input.get_nowait() == "PLAN SKILL MESSAGE"


def test_cli_bare_fable_remains_plan_only(monkeypatch):
    import cli as cli_mod

    def fake_build(request):
        assert request.prompt == "build the work"
        assert request.platform == "cli"
        return "PLAN SKILL MESSAGE"

    monkeypatch.setattr("hermes_cli.fable_planner.build_fable_plan_invocation", fake_build)
    pending_input = SimpleQueue()
    stub = SimpleNamespace(session_id="s1", _pending_input=pending_input)

    cli_mod.HermesCLI._handle_fable_command(stub, "/fable build the work")

    assert pending_input.get_nowait() == "PLAN SKILL MESSAGE"


def test_cli_fable_prints_error_when_plan_skill_missing(monkeypatch):
    import cli as cli_mod

    monkeypatch.setattr("hermes_cli.fable_planner.build_fable_plan_invocation", lambda _request: None)
    stub = SimpleNamespace(session_id="s1")
    with patch.object(cli_mod, "_cprint") as mock_cprint:
        cli_mod.HermesCLI._handle_fable_command(stub, "/fable plan the work")

    printed = " ".join(str(call) for call in mock_cprint.call_args_list)
    assert "requires the `plan` skill" in printed
