from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


def test_cli_fable_prints_usage_for_empty_command():
    import cli as cli_mod

    stub = SimpleNamespace(session_id="s1")
    with patch.object(cli_mod, "_cprint") as mock_cprint:
        cli_mod.HermesCLI._handle_fable_command(stub, "/fable")

    printed = " ".join(str(call) for call in mock_cprint.call_args_list)
    assert "Usage: /fable <request>" in printed


def test_cli_fable_prints_plan(monkeypatch, capsys):
    import cli as cli_mod
    from hermes_cli.fable_planner import FablePlanResult

    def fake_generate(request):
        assert request.prompt == "plan the work"
        assert request.platform == "cli"
        return FablePlanResult(True, "# Implementation Plan", "anthropic_oauth", "claude-fable-5")

    monkeypatch.setattr("hermes_cli.fable_planner.generate_fable_plan", fake_generate)
    stub = SimpleNamespace(session_id="s1")

    cli_mod.HermesCLI._handle_fable_command(stub, "/fable plan the work")

    assert "# Implementation Plan" in capsys.readouterr().out


def test_cli_fable_prints_error(monkeypatch):
    import cli as cli_mod
    from hermes_cli.fable_planner import FablePlanResult

    monkeypatch.setattr(
        "hermes_cli.fable_planner.generate_fable_plan",
        lambda _request: FablePlanResult(False, "", "anthropic_oauth", "claude-fable-5", error="route unavailable"),
    )
    stub = SimpleNamespace(session_id="s1")
    with patch.object(cli_mod, "_cprint") as mock_cprint:
        cli_mod.HermesCLI._handle_fable_command(stub, "/fable plan the work")

    printed = " ".join(str(call) for call in mock_cprint.call_args_list)
    assert "route unavailable" in printed
