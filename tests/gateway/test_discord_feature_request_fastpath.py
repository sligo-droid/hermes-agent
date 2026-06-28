"""Tests for Discord mainline action-request fast-path tuning."""

from types import SimpleNamespace
import sys
import threading
import types

from unittest.mock import AsyncMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.session import SessionSource


class _CapturingAgent:
    last_init = None
    last_run = None

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        self.tools = []

    def run_conversation(self, user_message, conversation_history=None, task_id=None):
        type(self).last_run = {
            "user_message": user_message,
            "conversation_history": conversation_history,
            "task_id": task_id,
        }
        return {
            "final_response": "ok",
            "messages": [],
            "api_calls": 1,
            "completed": True,
        }


def _install_fake_agent(monkeypatch):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)


def _make_runner():
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {}
    runner._ephemeral_system_prompt = "Global prompt"
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._service_tier = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._pending_model_notes = {}
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._session_model_overrides = {}
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.config = SimpleNamespace(streaming=None)
    runner.session_store = SimpleNamespace(
        get_or_create_session=lambda source: SimpleNamespace(session_id="session-1"),
        load_transcript=lambda session_id: [],
    )
    runner._enrich_message_with_vision = AsyncMock(return_value="ENRICHED")
    return runner


def _make_discord_source() -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        user_id="user-1",
        thread_id="thread-1",
    )


def _patch_agent_runtime(monkeypatch):
    _install_fake_agent(monkeypatch)
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_load_reasoning_config",
        staticmethod(lambda: {"enabled": True, "effort": "high"}),
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})


async def _run_discord_agent(runner, feature_summary):
    return await runner._run_agent(
        message="Build a deploy dashboard",
        context_prompt="Context prompt",
        history=[],
        source=_make_discord_source(),
        session_id="session-1",
        session_key="agent:main:discord:thread:thread-1",
        feature_summary=feature_summary,
    )


def test_standard_discord_action_request_helper_excludes_goal():
    source = _make_discord_source()

    assert gateway_run._is_standard_discord_action_request(
        source,
        {"initial_request": "Build it", "kanban_board": None},
    )
    assert not gateway_run._is_standard_discord_action_request(
        source,
        {"initial_request": "/goal Build it", "kanban_board": {"slug": "discord-1"}},
    )
    assert not gateway_run._is_standard_discord_action_request(
        SessionSource(platform=Platform.TELEGRAM, chat_id="1", chat_type="dm"),
        {"initial_request": "Build it", "kanban_board": None},
    )


@pytest.mark.asyncio
async def test_discord_action_request_keeps_full_platform_tool_surface(monkeypatch):
    _patch_agent_runtime(monkeypatch)
    runner = _make_runner()
    _CapturingAgent.last_init = None

    result = await _run_discord_agent(
        runner,
        {"initial_request": "Build it", "message_id": "300", "kanban_board": None},
    )

    assert result["final_response"] == "ok"
    init = _CapturingAgent.last_init
    assert init is not None
    assert init["tool_delay"] == 0.0
    assert init["enabled_toolsets"] == ["core"]
    assert init["reasoning_config"] == {"enabled": True, "effort": "xhigh"}
    assert init.get("skip_memory", False) is False
    assert "Discord action-request thread guidance" in init["ephemeral_system_prompt"]
    assert "latest user message as the authoritative task" in init["ephemeral_system_prompt"]
    assert "[Recent channel messages]" in init["ephemeral_system_prompt"]
    assert "context compaction summaries are background only" in init["ephemeral_system_prompt"]
    assert "do not treat 'ready for PR'" in init["ephemeral_system_prompt"]
    assert "github-pr-workflow" in init["ephemeral_system_prompt"]
    assert "full lifecycle means code merged, canonical/runtime checkout synced" in init["ephemeral_system_prompt"]
    assert "do not hold the final Discord response open" in init["ephemeral_system_prompt"]
    assert "terminal(background=True, notify_on_complete=True)" in init["ephemeral_system_prompt"]
    assert "local-only work" in init["ephemeral_system_prompt"]
    assert "at most one concise question" in init["ephemeral_system_prompt"]
    assert "recommended default" in init["ephemeral_system_prompt"]
    assert "state the assumption and continue" in init["ephemeral_system_prompt"]
    assert "Simple UI fast lane" in init["ephemeral_system_prompt"]
    assert "instead of spawning a coding worker" in init["ephemeral_system_prompt"]
    assert "phase timing line" in init["ephemeral_system_prompt"]
    assert "contradictory done/not-verified" in init["ephemeral_system_prompt"]
    assert init["ephemeral_system_prompt"].endswith("Global prompt")


@pytest.mark.asyncio
async def test_discord_goal_feature_summary_does_not_use_fast_path(monkeypatch):
    _patch_agent_runtime(monkeypatch)
    runner = _make_runner()
    _CapturingAgent.last_init = None

    result = await _run_discord_agent(
        runner,
        {
            "initial_request": "/goal Build it",
            "message_id": "300",
            "kanban_board": {"slug": "discord-thread-1"},
        },
    )

    assert result["final_response"] == "ok"
    init = _CapturingAgent.last_init
    assert init is not None
    assert "tool_delay" not in init
    assert init["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert "Discord action-request thread guidance" not in str(init.get("ephemeral_system_prompt") or "")


@pytest.mark.asyncio
async def test_discord_action_request_reasoning_override_is_configurable(monkeypatch):
    _install_fake_agent(monkeypatch)
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"discord": {"action_request_reasoning_effort": "low"}},
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_load_reasoning_config",
        staticmethod(lambda: {"enabled": True, "effort": "high"}),
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})
    runner = _make_runner()
    _CapturingAgent.last_init = None

    await _run_discord_agent(
        runner,
        {"initial_request": "Build it", "message_id": "300", "kanban_board": None},
    )

    assert _CapturingAgent.last_init["reasoning_config"] == {"enabled": True, "effort": "low"}


@pytest.mark.asyncio
async def test_discord_feature_request_reasoning_override_remains_legacy_alias(monkeypatch):
    _install_fake_agent(monkeypatch)
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"discord": {"feature_request_reasoning_effort": "medium"}},
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_load_reasoning_config",
        staticmethod(lambda: {"enabled": True, "effort": "high"}),
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.4")
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {
            "provider": "openrouter",
            "api_mode": "chat_completions",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "***",
        },
    )

    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda user_config, platform_key: {"core"})
    runner = _make_runner()
    _CapturingAgent.last_init = None

    await _run_discord_agent(
        runner,
        {"initial_request": "Build it", "message_id": "300", "kanban_board": None},
    )

    assert _CapturingAgent.last_init["reasoning_config"] == {"enabled": True, "effort": "medium"}


@pytest.mark.asyncio
async def test_discord_action_request_cache_signature_records_fast_path(monkeypatch):
    _patch_agent_runtime(monkeypatch)
    runner = _make_runner()
    captured_cache_keys = []

    def capture_signature(
        model,
        runtime,
        enabled_toolsets,
        ephemeral_prompt,
        cache_keys=None,
        user_id=None,
        user_id_alt=None,
    ):
        captured_cache_keys.append(dict(cache_keys or {}))
        return "captured-signature"

    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_agent_config_signature",
        staticmethod(capture_signature),
    )

    await _run_discord_agent(
        runner,
        {"initial_request": "Build it", "message_id": "300", "kanban_board": None},
    )

    assert captured_cache_keys
    assert captured_cache_keys[0]["gateway.discord_action_request_fast_path"] is True
    assert captured_cache_keys[0]["gateway.discord_feature_request_fast_path"] is True
    assert captured_cache_keys[0]["gateway.tool_delay"] == 0.0
