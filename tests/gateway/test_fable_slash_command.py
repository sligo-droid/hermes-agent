from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.runtime_capabilities import RuntimeMode
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, MetadataReply
from gateway.session import SessionEntry, SessionSource, build_session_key


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
    runner._session_model_overrides = {}
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._evict_cached_agent = MagicMock()
    runner._draining = False
    runner._busy_input_mode = "interrupt"
    runner._is_user_authorized = lambda _source: True
    runner._session_key_for_source = lambda source: build_session_key(source)
    runner._check_slash_access = lambda _source, _command: None
    runner._begin_session_run_generation = MagicMock(return_value=1)
    runner._is_session_run_current = MagicMock(return_value=True)
    runner._handle_message_with_agent = AsyncMock(return_value="agent response")
    return runner


def _session_entry_for_event(event):
    now = datetime.now()
    return SessionEntry(
        session_key=build_session_key(event.source),
        session_id="session-1",
        created_at=now,
        updated_at=now,
        origin=event.source,
        platform=event.source.platform,
        chat_type=event.source.chat_type,
    )


def _fable_override():
    return {
        "model": "claude-fable-5",
        "provider": "anthropic",
        "api_key": "sk-ant-oat01-test",
        "base_url": "https://api.anthropic.com",
        "api_mode": "anthropic_messages",
        "disable_fallback": "true",
    }


def _fable_proxy_override():
    return {
        **_fable_override(),
        "api_key": "cliproxy-key",
        "base_url": "http://127.0.0.1:8317",
    }


def _make_event(text="/fable build X"):
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread1",
        chat_type="thread",
        user_id="user1",
        user_name="User",
        thread_id="thread1",
        parent_chat_id="123",
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg1",
        feature_summary={"initial_request": "Build X", "kanban_board": None},
    )


@pytest.mark.asyncio
async def test_fable_plan_command_routes_through_normal_agent_with_fable_override(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()

    def fake_build(request, task_id=None):
        assert request.prompt == "build X"
        assert request.platform == "discord"
        assert task_id == build_session_key(_make_event("/fable plan build X").source)
        return "PLAN SKILL: build X"

    monkeypatch.setattr("hermes_cli.fable_planner.build_fable_plan_invocation", fake_build)
    monkeypatch.setattr(
        "hermes_cli.fable_planner.fable_session_model_override",
        lambda config=None: (_fable_override(), ""),
    )
    monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: {})

    result = await GatewayRunner._handle_message(runner, _make_event("/fable plan build X"))

    assert isinstance(result, MetadataReply)
    assert result == "agent response"
    assert result.metadata["command"] == "fable"
    assert result.metadata["plan_artifact_kind"] == "fable_plan"
    assert result.metadata["provider"] == "anthropic"
    assert result.metadata["model"] == "claude-fable-5"
    assert result.metadata["transport"] == "anthropic_oauth"
    assert result.metadata["source_message_id"] == "msg1"
    runner._handle_message_with_agent.assert_awaited_once()
    agent_event = runner._handle_message_with_agent.await_args.args[0]
    assert agent_event.text == "PLAN SKILL: build X"
    assert agent_event.invoked_skill_command == "fable"
    assert agent_event.fable_enabled_toolsets == ["file", "terminal", "web", "browser", "discord"]
    assert agent_event.fable_reasoning_config == {"enabled": True, "effort": "high"}
    assert agent_event.fable_transcript_user_message == "/fable plan build X"
    assert build_session_key(agent_event.source) not in runner._session_model_overrides


@pytest.mark.asyncio
async def test_bare_discord_fable_routes_to_implementation_with_normal_tool_surface(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()

    def fake_build(request):
        assert request.prompt == "build X"
        assert request.platform == "discord"
        return "FABLE IMPLEMENTATION: build X"

    monkeypatch.setattr("hermes_cli.fable_planner.build_fable_implementation_instruction", fake_build)
    monkeypatch.setattr(
        "hermes_cli.fable_planner.fable_session_model_override",
        lambda config=None: (_fable_override(), ""),
    )
    monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: {})

    result = await GatewayRunner._handle_message(runner, _make_event())

    assert isinstance(result, MetadataReply)
    assert result == "agent response"
    assert result.metadata["fable_mode"] == "implementation"
    assert result.metadata["kind"] == "fable_implementation"
    assert "plan_artifact_kind" not in result.metadata
    agent_event = runner._handle_message_with_agent.await_args.args[0]
    assert agent_event.text == "FABLE IMPLEMENTATION: build X"
    assert agent_event.fable_implementation is True
    assert not hasattr(agent_event, "fable_enabled_toolsets")


@pytest.mark.asyncio
async def test_fable_implementation_preprovisions_worktree_for_worker_prompt(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()
    runner._cache_session_source = MagicMock()
    event = _make_event()
    event.source.project_path = "/canonical/PID"
    event.discord_pr_generation = 2
    runner._accept_discord_work_item = MagicMock(return_value={"id": "fable-pr2"})

    def fake_worktree(
        source,
        feature_summary,
        config,
        session_key,
        runtime_mode,
        pr_generation,
    ):
        assert source.project_path == "/canonical/PID"
        assert feature_summary == event.feature_summary
        assert session_key == build_session_key(event.source)
        assert runtime_mode is RuntimeMode.ACTION
        assert pr_generation == 2
        return "/workspaces/fable-pid", None, "/workspaces/fable-pid"

    def fake_build(request):
        assert request.workdir == "/workspaces/fable-pid"
        assert not hasattr(request, "git_lifecycle")
        return "FABLE IMPLEMENTATION: build X"

    monkeypatch.setattr("gateway.run._resolve_gateway_turn_cwd", fake_worktree)
    monkeypatch.setattr("hermes_cli.fable_planner.build_fable_implementation_instruction", fake_build)
    monkeypatch.setattr(
        "hermes_cli.fable_planner.fable_session_model_override",
        lambda config=None: (_fable_override(), ""),
    )
    monkeypatch.setattr(
        "gateway.run._load_gateway_runtime_config",
        lambda: {"closeout": {"mode": "enforce", "surfaces": {"direct": True}}},
    )

    result = await GatewayRunner._handle_message(runner, event)

    assert isinstance(result, MetadataReply)
    assert "git_lifecycle" not in result.metadata
    assert event.source.project_path == "/workspaces/fable-pid"
    runner._cache_session_source.assert_called_once_with(
        build_session_key(event.source), event.source
    )


@pytest.mark.asyncio
async def test_natural_discord_fable_plan_is_plan_only_without_action_thread(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()
    event = _make_event("/fable help me plan to build X")
    event.source.chat_id = "123"
    event.source.chat_type = "group"
    event.source.thread_id = None
    event.source.parent_chat_id = None
    event.feature_summary = None

    def fake_build(request, task_id=None):
        assert request.prompt == "help me plan to build X"
        assert request.platform == "discord"
        assert task_id == build_session_key(event.source)
        return "PLAN SKILL: help me plan to build X"

    monkeypatch.setattr("hermes_cli.fable_planner.build_fable_plan_invocation", fake_build)
    monkeypatch.setattr(
        "hermes_cli.fable_planner.fable_session_model_override",
        lambda config=None: (_fable_override(), ""),
    )
    monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: {})

    result = await GatewayRunner._handle_message(runner, event)

    assert isinstance(result, MetadataReply)
    assert result.metadata["fable_mode"] == "plan"
    assert result.metadata["kind"] == "fable_plan"
    agent_event = runner._handle_message_with_agent.await_args.args[0]
    assert agent_event.text == "PLAN SKILL: help me plan to build X"
    assert agent_event.fable_implementation is False
    assert "action_worktree_task" not in runner._handle_message_with_agent.await_args.kwargs


@pytest.mark.asyncio
async def test_bare_non_discord_fable_remains_plan_only(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()
    event = _make_event("/fable build X")
    event.source.platform = Platform.TELEGRAM
    event.source.chat_type = "group"
    event.source.thread_id = None
    event.source.parent_chat_id = None
    event.feature_summary = None

    def fake_build(request, task_id=None):
        assert request.prompt == "build X"
        assert request.platform == "telegram"
        assert task_id
        return "PLAN SKILL: build X"

    monkeypatch.setattr("hermes_cli.fable_planner.build_fable_plan_invocation", fake_build)
    monkeypatch.setattr(
        "hermes_cli.fable_planner.fable_session_model_override",
        lambda config=None: (_fable_override(), ""),
    )
    monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: {})

    result = await GatewayRunner._handle_message(runner, event)

    assert isinstance(result, MetadataReply)
    assert result.metadata["fable_mode"] == "plan"
    agent_event = runner._handle_message_with_agent.await_args.args[0]
    assert agent_event.text == "PLAN SKILL: build X"


@pytest.mark.asyncio
async def test_fable_command_uses_configured_toolset_budget(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()

    monkeypatch.setattr("hermes_cli.fable_planner.build_fable_plan_invocation", lambda *_args, **_kwargs: "PLAN")
    monkeypatch.setattr(
        "hermes_cli.fable_planner.fable_session_model_override",
        lambda config=None: (_fable_override(), ""),
    )
    monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: {"fable": {"enabled_toolsets": ["file", "web"]}})

    result = await GatewayRunner._handle_message(runner, _make_event("/fable plan build X"))

    assert isinstance(result, MetadataReply)
    agent_event = runner._handle_message_with_agent.await_args.args[0]
    assert agent_event.fable_enabled_toolsets == ["file", "web"]


@pytest.mark.asyncio
async def test_fable_command_metadata_identifies_configured_proxy_route(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()
    config = {
        "fable": {
            "route": "anthropic_proxy",
            "key_env": "CLI_PROXY_API_KEY",
            "base_url": "http://127.0.0.1:8317",
        }
    }
    monkeypatch.setattr("hermes_cli.fable_planner.build_fable_plan_invocation", lambda *_args, **_kwargs: "PLAN")
    monkeypatch.setattr(
        "hermes_cli.fable_planner.fable_session_model_override",
        lambda config=None: (_fable_proxy_override(), ""),
    )
    monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: config)

    result = await GatewayRunner._handle_message(runner, _make_event("/fable plan build X"))

    assert isinstance(result, MetadataReply)
    assert result.metadata["route"] == "anthropic_proxy"
    assert result.metadata["transport"] == "anthropic_proxy"
    assert result.metadata["provider"] == "anthropic"
    assert result.metadata["model"] == "claude-fable-5"


@pytest.mark.asyncio
async def test_fable_command_usage_without_args(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()
    result = await GatewayRunner._handle_message(runner, _make_event("/fable"))

    assert result == "Usage: /fable <request> or /fable plan <request>"
    runner._handle_message_with_agent.assert_not_awaited()


@pytest.mark.asyncio
async def test_fable_command_reports_failed_route_without_artifact_metadata(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()
    runner._handle_message_with_agent = AsyncMock(
        return_value="⚠️ /fable is pinned to Claude Fable 5 via Hermes' Anthropic OAuth route and will not fall back to another model or provider. spend limit reached"
    )

    monkeypatch.setattr("hermes_cli.fable_planner.build_fable_plan_invocation", lambda *_args, **_kwargs: "PLAN")
    monkeypatch.setattr(
        "hermes_cli.fable_planner.fable_session_model_override",
        lambda config=None: (_fable_override(), ""),
    )
    monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: {})

    result = await GatewayRunner._handle_message(runner, _make_event("/fable plan build X"))

    assert isinstance(result, str)
    assert not isinstance(result, MetadataReply)
    assert "will not fall back to another model or provider" in result


def test_fable_run_agent_disables_fallback_and_restricts_provider(monkeypatch):
    import asyncio
    import sys
    import threading
    import types

    import gateway.run as gateway_run

    captured = {}

    class CapturingAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.tools = []

        def run_conversation(self, user_message, conversation_history=None, task_id=None):
            return {
                "final_response": "fable ok",
                "messages": [],
                "api_calls": 1,
                "model": "claude-fable-5",
                "provider": "anthropic",
            }

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "agent": {"reasoning_effort": "medium"},
            "discord": {"feature_request_reasoning_effort": "high"},
        },
    )
    GatewayRunner = gateway_run.GatewayRunner

    runner = _make_runner()
    runner._handle_message_with_agent = GatewayRunner._handle_message_with_agent.__get__(runner, GatewayRunner)
    runner._run_agent = GatewayRunner._run_agent.__get__(runner, GatewayRunner)
    runner._resolve_session_agent_runtime = lambda **_kwargs: ("claude-fable-5", _fable_override())
    runner._agent_cache_lock = threading.Lock()
    runner._fallback_model = {"provider": "openai-codex", "model": "gpt-5.5"}
    runner._provider_routing = {"only": ["openai-codex"]}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._voice_mode = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._service_tier = None
    runner._session_db = None
    runner._background_tasks = set()
    runner.adapters = {}
    event = _make_event()
    event.text = "PLAN"
    event.fable_plan_metadata = {"command": "fable"}
    session_entry = _session_entry_for_event(event)
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.update_session = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_any_sessions.return_value = True

    result = asyncio.run(
        runner._handle_message_with_agent(
            event,
            event.source,
            build_session_key(event.source),
            1,
        )
    )

    assert result == "fable ok"
    assert captured["model"] == "claude-fable-5"
    assert captured["provider"] == "anthropic"
    assert captured["enabled_toolsets"] == ["file", "terminal", "web", "browser", "discord"]
    assert captured["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert captured["fallback_model"] is None
    assert captured["providers_allowed"] == ["anthropic"]


def test_fable_run_agent_uses_event_toolset_override(monkeypatch):
    import asyncio
    import sys
    import threading
    import types

    import gateway.run as gateway_run

    captured = {}

    class CapturingAgent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.tools = []

        def run_conversation(self, user_message, conversation_history=None, task_id=None):
            return {
                "final_response": "fable ok",
                "messages": [],
                "api_calls": 1,
                "model": "claude-fable-5",
                "provider": "anthropic",
            }

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {"tools": {"discord": {"enabled": ["all"]}}})
    GatewayRunner = gateway_run.GatewayRunner

    runner = _make_runner()
    runner._handle_message_with_agent = GatewayRunner._handle_message_with_agent.__get__(runner, GatewayRunner)
    runner._run_agent = GatewayRunner._run_agent.__get__(runner, GatewayRunner)
    runner._resolve_session_agent_runtime = lambda **_kwargs: ("claude-fable-5", _fable_override())
    runner._agent_cache_lock = threading.Lock()
    runner._fallback_model = None
    runner._provider_routing = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._voice_mode = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._service_tier = None
    runner._session_db = None
    runner._background_tasks = set()
    runner.adapters = {}
    event = _make_event()
    event.text = "PLAN"
    event.fable_plan_metadata = {"command": "fable"}
    event.fable_enabled_toolsets = ["file", "web"]
    session_entry = _session_entry_for_event(event)
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.update_session = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_any_sessions.return_value = True

    result = asyncio.run(
        runner._handle_message_with_agent(
            event,
            event.source,
            build_session_key(event.source),
            1,
        )
    )

    assert result == "fable ok"
    assert captured["enabled_toolsets"] == ["file", "web"]


def test_fable_implementation_keeps_normal_discord_toolsets(monkeypatch):
    import asyncio
    import sys
    import threading
    import types

    import gateway.run as gateway_run

    captured = {}

    class CapturingAgent:
        instance = None

        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.tools = []
            type(self).instance = self

        def run_conversation(self, user_message, conversation_history=None, task_id=None):
            return {
                "final_response": "fable implementation complete",
                "messages": [],
                "api_calls": 1,
                "model": "claude-fable-5",
                "provider": "anthropic",
            }

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "tools": {"discord": {"enabled": ["all"]}},
        },
    )
    import hermes_cli.tools_config as tools_config

    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda _config, _platform: {"core", "terminal"})
    GatewayRunner = gateway_run.GatewayRunner
    runner = _make_runner()
    runner._handle_message_with_agent = GatewayRunner._handle_message_with_agent.__get__(runner, GatewayRunner)
    runner._run_agent = GatewayRunner._run_agent.__get__(runner, GatewayRunner)
    runner._resolve_session_agent_runtime = lambda **_kwargs: ("claude-fable-5", _fable_override())
    runner._agent_cache_lock = threading.Lock()
    runner._fallback_model = {"provider": "openai-codex", "model": "gpt-5.5"}
    runner._provider_routing = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._voice_mode = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._service_tier = None
    runner._session_db = None
    runner._background_tasks = set()
    runner.adapters = {}
    event = _make_event()
    event.text = "FABLE IMPLEMENTATION PAYLOAD"
    event.fable_plan_metadata = {
        "command": "fable",
        "fable_mode": "implementation",
        "route": "anthropic_proxy",
        "anthropic_oauth_tool_name_compat": True,
    }
    session_entry = _session_entry_for_event(event)
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.update_session = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_any_sessions.return_value = True

    result = asyncio.run(
        runner._handle_message_with_agent(
            event,
            event.source,
            build_session_key(event.source),
            1,
        )
    )

    assert result == "fable implementation complete"
    assert captured["model"] == "claude-fable-5"
    assert captured["enabled_toolsets"] == ["core", "terminal"]
    assert captured["fallback_model"] is None
    assert captured["providers_allowed"] == ["anthropic"]
    assert CapturingAgent.instance._fable_implementation_turn is True
    assert not hasattr(CapturingAgent.instance, "_fable_git_lifecycle")
    assert CapturingAgent.instance._anthropic_oauth_tool_name_compat is True


def test_fable_run_agent_refuses_non_fable_model_result(monkeypatch):
    import asyncio
    import sys
    import threading
    import types

    import gateway.run as gateway_run

    class FallbackAgent:
        def __init__(self, **_kwargs):
            self.tools = []
            self.model = "gpt-5.5"

        def run_conversation(self, user_message, conversation_history=None, task_id=None):
            return {
                "final_response": "fallback response",
                "messages": [],
                "api_calls": 1,
                "model": "gpt-5.5",
            }

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = FallbackAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    GatewayRunner = gateway_run.GatewayRunner

    runner = _make_runner()
    runner._handle_message_with_agent = GatewayRunner._handle_message_with_agent.__get__(runner, GatewayRunner)
    runner._run_agent = GatewayRunner._run_agent.__get__(runner, GatewayRunner)
    runner._resolve_session_agent_runtime = lambda **_kwargs: ("claude-fable-5", _fable_override())
    runner._agent_cache_lock = threading.Lock()
    runner._fallback_model = {"provider": "openai-codex", "model": "gpt-5.5"}
    runner._provider_routing = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._voice_mode = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._service_tier = None
    runner._session_db = None
    runner._background_tasks = set()
    runner.adapters = {}
    event = _make_event()
    event.text = "PLAN"
    event.fable_plan_metadata = {"command": "fable"}
    session_entry = _session_entry_for_event(event)
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.update_session = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_any_sessions.return_value = True

    result = asyncio.run(
        runner._handle_message_with_agent(
            event,
            event.source,
            build_session_key(event.source),
            1,
        )
    )

    assert "will not fall back to another model or provider" in result
    assert "gpt-5.5" in result


def test_fable_turn_persists_slash_command_not_plan_skill_payload(monkeypatch):
    import asyncio
    import sys
    import threading
    import types

    import gateway.run as gateway_run

    class CapturingAgent:
        last_conversation_kwargs = None

        def __init__(self, **_kwargs):
            self.tools = []

        def run_conversation(self, user_message, **kwargs):
            CapturingAgent.last_conversation_kwargs = kwargs
            persisted_user_message = kwargs.get("persist_user_message", user_message)
            return {
                "final_response": "fable ok",
                "messages": [
                    {"role": "user", "content": persisted_user_message},
                    {"role": "assistant", "content": "fable ok"},
                ],
                "history_offset": 0,
                "api_calls": 1,
                "model": "claude-fable-5",
                "provider": "anthropic",
            }

    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = CapturingAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    GatewayRunner = gateway_run.GatewayRunner

    runner = _make_runner()
    runner._handle_message_with_agent = GatewayRunner._handle_message_with_agent.__get__(runner, GatewayRunner)
    runner._run_agent = GatewayRunner._run_agent.__get__(runner, GatewayRunner)
    runner._resolve_session_agent_runtime = lambda **_kwargs: ("claude-fable-5", _fable_override())
    runner._agent_cache_lock = threading.Lock()
    runner._fallback_model = None
    runner._provider_routing = {}
    runner._session_reasoning_overrides = {}
    runner._pending_model_notes = {}
    runner._voice_mode = {}
    runner._ephemeral_system_prompt = ""
    runner._prefill_messages = []
    runner._reasoning_config = None
    runner._show_reasoning = False
    runner._service_tier = None
    runner._session_db = None
    runner._background_tasks = set()
    runner.adapters = {}
    event = _make_event()
    event.text = "PLAN SKILL PAYLOAD WITH FABLE RUNTIME NOTE"
    event.fable_plan_metadata = {"command": "fable"}
    event.fable_transcript_user_message = "/fable build X"
    session_entry = _session_entry_for_event(event)
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.update_session = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.has_any_sessions.return_value = True

    result = asyncio.run(
        runner._handle_message_with_agent(
            event,
            event.source,
            build_session_key(event.source),
            1,
        )
    )

    assert result == "fable ok"
    assert CapturingAgent.last_conversation_kwargs is not None
    assert CapturingAgent.last_conversation_kwargs["persist_user_message"] == "/fable build X"
    transcript_entries = [call.args[1] for call in runner.session_store.append_to_transcript.call_args_list]
    user_entries = [entry for entry in transcript_entries if entry.get("role") == "user"]
    assert user_entries
    assert user_entries[0]["content"] == "/fable build X"
    assert "FABLE RUNTIME NOTE" not in user_entries[0]["content"]


@pytest.mark.asyncio
async def test_fable_command_failure_is_not_plan_artifact_metadata(monkeypatch):
    from gateway.run import GatewayRunner

    runner = _make_runner()

    monkeypatch.setattr("hermes_cli.fable_planner.build_fable_plan_invocation", lambda *_args, **_kwargs: "PLAN")
    monkeypatch.setattr(
        "hermes_cli.fable_planner.fable_session_model_override",
        lambda config=None: (None, "route unavailable"),
    )
    monkeypatch.setattr("gateway.run._load_gateway_runtime_config", lambda: {})

    result = await GatewayRunner._handle_message(runner, _make_event("/fable plan build X"))

    assert isinstance(result, str)
    assert not isinstance(result, MetadataReply)
    assert "route unavailable" in result
    runner._handle_message_with_agent.assert_not_awaited()
