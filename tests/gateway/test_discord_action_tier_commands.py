"""Command-level contracts for Discord action-request tier stepping."""

from __future__ import annotations

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _source(*, chat_type: str = "thread", platform: Platform = Platform.DISCORD) -> SessionSource:
    return SessionSource(
        platform=platform,
        chat_id="thread-1",
        chat_type=chat_type,
        thread_id="thread-1" if chat_type == "thread" else None,
        user_id="user-1",
    )


def _event(
    text: str,
    *,
    source: SessionSource | None = None,
    feature_summary: dict | None = None,
) -> MessageEvent:
    return MessageEvent(
        text=text,
        source=source or _source(),
        feature_summary=feature_summary
        if feature_summary is not None
        else {"initial_request": "Build the dashboard", "kanban_board": None},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "direction", "expected_tier"),
    [
        ("dumb", -1, "trivial"),
        ("smart", 2, "advanced"),
    ],
)
async def test_tier_command_rewrites_only_the_current_action_turn(
    monkeypatch,
    command: str,
    direction: int,
    expected_tier: str,
) -> None:
    runner = object.__new__(gateway_run.GatewayRunner)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_runtime_config",
        lambda: {"discord": {"action_request_model_tier": "basic"}},
    )
    event = _event(f"/{command} implement the dashboard")

    result = await runner._handle_action_request_tier_command(
        event,
        command=command,
        direction=direction,
    )

    assert result is None
    assert event.text == "implement the dashboard"
    assert event.action_request_model_tier_override == expected_tier
    assert event.action_request_transcript_user_message == f"/{command} implement the dashboard"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "config", "command", "direction", "expected"),
    [
        (
            _event("/dumb"),
            {"discord": {"action_request_model_tier": "basic"}},
            "dumb",
            -1,
            "Usage: /dumb <request>",
        ),
        (
            _event("/smart build", source=_source(chat_type="group")),
            {"discord": {"action_request_model_tier": "basic"}},
            "smart",
            2,
            "normal non-Kanban Discord action-request thread",
        ),
        (
            _event("/smart build", feature_summary={"initial_request": "/goal build", "kanban_board": {}}),
            {"discord": {"action_request_model_tier": "basic"}},
            "smart",
            2,
            "normal non-Kanban Discord action-request thread",
        ),
        (
            _event("/smart build"),
            {"discord": {"action_request_model_tier": "custom"}},
            "smart",
            2,
            "requires `discord.action_request_model_tier`",
        ),
        (
            _event("/dumb build"),
            {"discord": {"action_request_model_tier": "trivial"}},
            "dumb",
            -1,
            "below `trivial`",
        ),
        (
            _event("/smart build"),
            {"discord": {"action_request_model_tier": "advanced"}},
            "smart",
            2,
            "above `advanced`",
        ),
    ],
)
async def test_tier_command_rejects_invalid_request_context_or_boundary(
    monkeypatch,
    event: MessageEvent,
    config: dict,
    command: str,
    direction: int,
    expected: str,
) -> None:
    runner = object.__new__(gateway_run.GatewayRunner)
    monkeypatch.setattr(gateway_run, "_load_gateway_runtime_config", lambda: config)

    result = await runner._handle_action_request_tier_command(
        event,
        command=command,
        direction=direction,
    )

    assert result is not None
    assert expected in result


def test_turn_scoped_tier_beats_global_and_session_models_without_mutating_session(
    monkeypatch,
) -> None:
    runner = object.__new__(gateway_run.GatewayRunner)
    session_key = "agent:main:discord:thread:thread-1"
    session_override = {
        "model": "session/model",
        "provider": "openrouter",
        "api_key": "test-key",
        "base_url": "https://example.test/v1",
        "api_mode": "chat_completions",
    }
    runner._session_model_overrides = {session_key: dict(session_override)}
    runner._last_resolved_model = {}
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "model": "runtime/model",
            "provider": "openrouter",
            "api_key": "runtime-key",
            "base_url": "https://runtime.example/v1",
            "api_mode": "chat_completions",
        },
    )

    forced_model, _forced_runtime = runner._resolve_session_agent_runtime(
        source=_source(),
        session_key=session_key,
        user_config={},
        turn_model_override="gpt-5.6-terra",
    )
    next_model, _next_runtime = runner._resolve_session_agent_runtime(
        source=_source(),
        session_key=session_key,
        user_config={},
    )

    assert forced_model == "gpt-5.6-terra"
    assert next_model == "session/model"
    assert runner._session_model_overrides[session_key] == session_override
