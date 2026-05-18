"""Tests for gateway /usage Codex subscription/model usage."""

from unittest.mock import MagicMock

import pytest


@pytest.mark.asyncio
async def test_usage_command_reports_codex_subscription_usage(monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    event = MagicMock()
    calls = []

    async def fake_to_thread(fn, *args, **kwargs):
        calls.append((fn, args, kwargs))
        return fn(*args, **kwargs)

    monkeypatch.setattr("gateway.run.asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr(
        "hermes_cli.codex_status.build_codex_usage_report",
        lambda: "Codex Status\n\nUsage:\n- GPT-5.3-Codex-Spark\n  - weekly: 14% used",
    )

    result = await runner._handle_usage_command(event)

    assert "Codex Status" in result
    assert "weekly: 14% used" in result
    assert "Session Token Usage" not in result
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_usage_command_does_not_require_session_agent_or_history(monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    # If the new command accidentally reaches old session-token paths these
    # missing attributes would raise AttributeError.
    event = MagicMock()
    monkeypatch.setattr(
        "hermes_cli.codex_status.build_codex_usage_report",
        lambda: "Codex Status\n\nNo Codex rate-limit usage returned.",
    )

    result = await runner._handle_usage_command(event)

    assert result == "Codex Status\n\nNo Codex rate-limit usage returned."


@pytest.mark.asyncio
async def test_usage_command_does_not_call_agent_inference(monkeypatch):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._run_agent = MagicMock(side_effect=AssertionError("/usage must not run inference"))
    event = MagicMock()
    monkeypatch.setattr(
        "hermes_cli.codex_status.build_codex_usage_report",
        lambda: "Codex Status\n\nUsage:\n- Codex\n  - weekly: 23% used",
    )

    result = await runner._handle_usage_command(event)

    assert "weekly: 23% used" in result
    runner._run_agent.assert_not_called()
