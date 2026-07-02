from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, MetadataReply
from gateway.session import SessionSource, build_session_key


def _make_runner():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(platforms={Platform.DISCORD: PlatformConfig(enabled=True, token="***")})
    runner.adapters = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._queued_events = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner._busy_input_mode = "interrupt"
    runner._is_user_authorized = lambda _source: True
    runner._session_key_for_source = lambda source: build_session_key(source)
    runner._check_slash_access = lambda _source, _command: None
    runner._begin_session_run_generation = MagicMock(return_value=1)
    runner._is_session_run_current = MagicMock(return_value=True)
    runner._handle_message_with_agent = AsyncMock(return_value="agent response")
    return runner


def _make_event(text="/fable build X"):
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="123",
        chat_type="group",
        user_id="user1",
        user_name="User",
        thread_id="thread1",
    )
    return MessageEvent(text=text, message_type=MessageType.TEXT, source=source, message_id="msg1")


@pytest.mark.asyncio
async def test_fable_command_returns_runner_plan_without_normal_agent(monkeypatch):
    from gateway.run import GatewayRunner
    from hermes_cli.fable_planner import FablePlanResult

    runner = _make_runner()

    def fake_generate(request, config=None):
        assert request.prompt == "build X"
        assert request.platform == "discord"
        return FablePlanResult(True, "# Implementation Plan\n\nPlan only.", "anthropic_oauth", "claude-fable-5")

    monkeypatch.setattr("hermes_cli.fable_planner.generate_fable_plan", fake_generate)
    monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: {})

    result = await GatewayRunner._handle_message(runner, _make_event())

    assert isinstance(result, MetadataReply)
    assert "Implementation Plan" in result
    assert result.metadata["command"] == "fable"
    assert result.metadata["plan_artifact_kind"] == "fable_plan"
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_fable_command_usage_without_args(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()
    result = await GatewayRunner._handle_message(runner, _make_event("/fable"))

    assert result == "Usage: /fable <request>"
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_fable_command_failure_is_not_plan_artifact_metadata(monkeypatch):
    from gateway.run import GatewayRunner
    from hermes_cli.fable_planner import FablePlanResult

    runner = _make_runner()

    def fake_generate(request, config=None):
        return FablePlanResult(False, "", "anthropic_oauth", "claude-fable-5", error="route unavailable")

    monkeypatch.setattr("hermes_cli.fable_planner.generate_fable_plan", fake_generate)
    monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: {})

    result = await GatewayRunner._handle_message(runner, _make_event())

    assert isinstance(result, str)
    assert not isinstance(result, MetadataReply)
    assert "route unavailable" in result
    runner._handle_message_with_agent.assert_not_awaited()
