"""Tests for Discord mainline action-request fast-path tuning."""

from types import SimpleNamespace
import sys
import threading
import types

from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType, ProcessingOutcome
from gateway.session import SessionSource


class _CapturingAgent:
    last_init = None
    last_run = None
    init_count = 0

    def __init__(self, *args, **kwargs):
        type(self).last_init = dict(kwargs)
        type(self).init_count += 1
        self.tools = []
        self.iteration_budget = SimpleNamespace(max_total=0)

    def run_conversation(self, user_message, conversation_history=None, task_id=None, **kwargs):
        type(self).last_run = {
            "user_message": user_message,
            "conversation_history": conversation_history,
            "task_id": task_id,
            **kwargs,
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
    runner._process_epoch = "1-test"
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
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda config=None: "gpt-5.6-terra")
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


async def _run_discord_agent(
    runner,
    feature_summary,
    *,
    intent=None,
    message="Build a deploy dashboard",
    channel_prompt=None,
    escalation_allowed=False,
    action_preflight_prompt=None,
    fable_transcript_user_message=None,
):
    return await runner._run_agent(
        message=message,
        context_prompt="Context prompt",
        history=[],
        source=_make_discord_source(),
        session_id="session-1",
        session_key="agent:main:discord:thread:thread-1",
        channel_prompt=channel_prompt,
        feature_summary=feature_summary,
        discord_runtime_mode="read_only" if intent is False else "action",
        discord_action_request_intent=intent,
        discord_action_escalation_allowed=escalation_allowed,
        action_preflight_prompt=action_preflight_prompt,
        fable_transcript_user_message=fable_transcript_user_message,
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
    assert result["reasoning_effort"] == "medium"
    init = _CapturingAgent.last_init
    assert init is not None
    assert init["tool_delay"] == 0.0
    assert init["verify_on_stop"] is True
    assert init["enabled_toolsets"] == ["core"]
    assert init["model"] == "gpt-5.6-sol"
    assert init["reasoning_config"] == {"enabled": True, "effort": "medium"}
    cached_agent = runner._agent_cache["agent:main:discord:thread:thread-1"][0]
    audit = cached_agent._runtime_audit_context
    assert audit["model_tier"] == "discord_action"
    assert audit["model_tier_source"] == "route"
    assert audit["runtime_route"] == "discord_action_request"
    assert audit["reasoning_source"] == "model_tier"
    assert init.get("skip_memory", False) is False
    assert "Discord action-request thread guidance" in init["ephemeral_system_prompt"]
    assert "latest user message as the authoritative task" in init["ephemeral_system_prompt"]
    assert "[Recent channel messages]" in init["ephemeral_system_prompt"]
    assert "context compaction summaries are background only" in init["ephemeral_system_prompt"]
    assert "do not treat 'ready for PR'" in init["ephemeral_system_prompt"]
    assert "durable trusted closeout" in init["ephemeral_system_prompt"]
    assert "Do not announce a draft PR alone" in init["ephemeral_system_prompt"]
    assert "include the preview and PR URLs" in init["ephemeral_system_prompt"]
    assert "omit commit hashes" in init["ephemeral_system_prompt"]
    assert "rerun the same package script" in init["ephemeral_system_prompt"]
    lifecycle_prompt = init["ephemeral_system_prompt"]
    assert "non-short-circuit" in lifecycle_prompt
    assert "records every exit status" in lifecycle_prompt
    assert "one bounded pr_workflow_preflight snapshot" in lifecycle_prompt
    assert "Stage only owned files" in lifecycle_prompt
    assert "first feature-branch push as one recoverable" in lifecycle_prompt
    assert lifecycle_prompt.index("first feature-branch push") < lifecycle_prompt.index("PR creation")
    assert "separate explicit user authorization" in lifecycle_prompt
    assert "exact-head protection" in lifecycle_prompt
    assert "actual merge as a separate mutation" in lifecycle_prompt
    assert "one bounded post-merge snapshot" in lifecycle_prompt
    assert "without inspecting recent PR history" in lifecycle_prompt
    assert "Do not request branch deletion as part of the merge mutation" in lifecycle_prompt
    assert "cleanup from the mutable action worktree" in lifecycle_prompt
    assert "Do not load github-pr-workflow for routine closeout" in init["ephemeral_system_prompt"]
    assert "diagnosis or recovery" in init["ephemeral_system_prompt"]
    assert "terminal(background=True, notify_on_complete=True)" in init["ephemeral_system_prompt"]
    assert "do not open extra cleanup" in init["ephemeral_system_prompt"]
    assert "Record non-critical follow-ups" in init["ephemeral_system_prompt"]
    assert "Choose delegate_coding_task model_tier deliberately" in init["ephemeral_system_prompt"]
    assert "Set reasoning_effort only for exceptional overrides" in init["ephemeral_system_prompt"]
    assert "worker_tier" not in init["ephemeral_system_prompt"]
    assert "Front-load what you learned into relevant_files" in init["ephemeral_system_prompt"]
    assert "several delegate_coding_task calls in one response" in init["ephemeral_system_prompt"]
    assert "non-overlapping scope_paths" in init["ephemeral_system_prompt"]
    assert "Never parallelize coupled edits" in init["ephemeral_system_prompt"]
    assert "review the merged result afterward" in init["ephemeral_system_prompt"]
    assert "batchable non-code work" in init["ephemeral_system_prompt"]
    assert "delegate_task batch mode" in init["ephemeral_system_prompt"]
    assert "deciding treatment and reviewing results" in init["ephemeral_system_prompt"]
    assert "Early review fanout" in init["ephemeral_system_prompt"]
    assert "two or more independent surfaces" in init["ephemeral_system_prompt"]
    assert "in the first tool turn after at most one" in init["ephemeral_system_prompt"]
    assert "do not open child-owned implementation files" in init["ephemeral_system_prompt"]
    assert "root still owns shared context" in init["ephemeral_system_prompt"]
    assert "Do not apply this fast path to coupled surfaces" in init["ephemeral_system_prompt"]
    assert "Do not foreground-watch long external runs" in init["ephemeral_system_prompt"]
    assert "bounded cron poller" in init["ephemeral_system_prompt"]
    assert "completion re-enters as a follow-up turn" in init["ephemeral_system_prompt"]
    assert "one focused coding-worker attempt" in init["ephemeral_system_prompt"]
    assert "repeatedly retrying the same backend" in init["ephemeral_system_prompt"]
    assert "local-only work" in init["ephemeral_system_prompt"]
    assert "at most one concise question" in init["ephemeral_system_prompt"]
    assert "recommended default" in init["ephemeral_system_prompt"]
    assert "state the assumption and continue" in init["ephemeral_system_prompt"]
    assert "small non-visual frontend-only edit" in init["ephemeral_system_prompt"]
    assert "do it inline in the current agent turn" in init["ephemeral_system_prompt"]
    assert "phase timing line" not in init["ephemeral_system_prompt"]
    assert "contradictory done/not-verified" in init["ephemeral_system_prompt"]
    assert init["ephemeral_system_prompt"].endswith("Global prompt")


@pytest.mark.asyncio
async def test_action_preflight_is_api_only_and_preserves_agent_cache(monkeypatch):
    _patch_agent_runtime(monkeypatch)
    runner = _make_runner()
    _CapturingAgent.init_count = 0
    feature = {"initial_request": "Build it", "message_id": "301", "kanban_board": None}

    await _run_discord_agent(
        runner,
        feature,
        message="first request",
        action_preflight_prompt="snapshot one",
    )
    await _run_discord_agent(
        runner,
        feature,
        message="second request",
        action_preflight_prompt="snapshot two",
    )

    assert _CapturingAgent.init_count == 1
    assert "snapshot one" not in _CapturingAgent.last_init["ephemeral_system_prompt"]
    assert "snapshot two" not in _CapturingAgent.last_init["ephemeral_system_prompt"]
    assert _CapturingAgent.last_run["user_message"] == "snapshot two\n\nsecond request"
    assert _CapturingAgent.last_run["persist_user_message"] == "second request"


@pytest.mark.asyncio
async def test_action_preflight_preserves_premium_transcript_override(monkeypatch):
    _patch_agent_runtime(monkeypatch)
    runner = _make_runner()

    await _run_discord_agent(
        runner,
        {"initial_request": "Build it", "message_id": "303", "kanban_board": None},
        message="synthetic premium instruction",
        action_preflight_prompt="fresh snapshot",
        fable_transcript_user_message="/fable build it",
    )

    assert _CapturingAgent.last_run["user_message"] == (
        "fresh snapshot\n\nsynthetic premium instruction"
    )
    assert _CapturingAgent.last_run["persist_user_message"] == "/fable build it"


@pytest.mark.asyncio
async def test_action_preflight_preserves_multimodal_payload(monkeypatch):
    _patch_agent_runtime(monkeypatch)
    runner = _make_runner()
    clean_message = [
        {"type": "text", "text": "inspect this"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]

    await _run_discord_agent(
        runner,
        {"initial_request": "Build it", "message_id": "302", "kanban_board": None},
        message=clean_message,
        action_preflight_prompt="fresh snapshot",
    )

    api_message = _CapturingAgent.last_run["user_message"]
    assert api_message[0]["text"] == "fresh snapshot\n\ninspect this"
    assert api_message[1] == clean_message[1]
    assert _CapturingAgent.last_run["persist_user_message"] == clean_message
    assert clean_message[0]["text"] == "inspect this"


@pytest.mark.parametrize(
    "initial_request",
    [
        "Fix a typo in the README",
        "Test out the new async stuff",
        "Migrate the production auth schema",
    ],
)
@pytest.mark.asyncio
async def test_discord_action_request_uses_routine_tier_regardless_of_keywords(
    monkeypatch, initial_request
):
    _patch_agent_runtime(monkeypatch)
    runner = _make_runner()

    result = await _run_discord_agent(
        runner,
        {"initial_request": initial_request, "message_id": "300", "kanban_board": None},
        intent=True,
    )

    assert result["reasoning_effort"] == "medium"
    assert _CapturingAgent.last_init["model"] == "gpt-5.6-sol"
    assert _CapturingAgent.last_init["reasoning_config"] == {
        "enabled": True,
        "effort": "medium",
    }
    audit = runner._agent_cache["agent:main:discord:thread:thread-1"][0]._runtime_audit_context
    assert audit["model_tier"] == "discord_action"
    assert audit["model_tier_source"] == "route"
    assert audit["runtime_route"] == "discord_action_request"


@pytest.mark.asyncio
async def test_invalid_routine_tier_preserves_legacy_action_fallback(monkeypatch):
    _patch_agent_runtime(monkeypatch)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "discord": {
                "action_request_model_tier": "missing-tier",
                "action_request_reasoning_effort": "xhigh",
            }
        },
    )
    runner = _make_runner()

    result = await _run_discord_agent(
        runner,
        {"initial_request": "Build it", "message_id": "300", "kanban_board": None},
        intent=True,
    )

    assert _CapturingAgent.last_init["model"] == "gpt-5.6-terra"
    assert result["reasoning_effort"] == "xhigh"
    assert _CapturingAgent.last_init["reasoning_config"] == {
        "enabled": True,
        "effort": "xhigh",
    }
    audit = runner._agent_cache["agent:main:discord:thread:thread-1"][0]._runtime_audit_context
    assert audit["model_tier"] == ""
    assert audit["runtime_route"] == "discord_action_request"
    assert audit["reasoning_source"] == "discord_config"


@pytest.mark.asyncio
async def test_existing_action_thread_direct_question_uses_read_only_tier(monkeypatch):
    _patch_agent_runtime(monkeypatch)
    runner = _make_runner()
    feature_summary = {
        "initial_request": "Build bounded Federal Register evidence ingestion",
        "message_id": "300",
        "kanban_board": None,
    }

    result = await _run_discord_agent(
        runner,
        feature_summary,
        intent=False,
        message=(
            "Without building anything, can you give me some concrete examples of "
            'the sort of items in the "include only" list?'
        ),
        channel_prompt="Answer this current direct question in place.",
    )

    assert result["reasoning_effort"] == "low"
    init = _CapturingAgent.last_init
    assert init["model"] == "gpt-5.6-sol"
    assert init["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert "tool_delay" not in init
    assert "verify_on_stop" not in init
    assert init["runtime_mode"] == "read_only"
    assert init["memory_read_only"] is True
    assert "Discord action-request thread guidance" not in init["ephemeral_system_prompt"]
    assert "Answer this current direct question in place." in init["ephemeral_system_prompt"]
    audit = runner._agent_cache["agent:main:discord:thread:thread-1"][0]._runtime_audit_context
    assert audit["model_tier"] == "discord_read_only"
    assert audit["model_tier_source"] == "route"
    assert audit["runtime_route"] == "discord_read_only"
    assert audit["reasoning_source"] == "model_tier"
    assert feature_summary["initial_request"] == "Build bounded Federal Register evidence ingestion"


@pytest.mark.asyncio
async def test_bare_discord_read_only_turn_uses_discord_read_only_tier(monkeypatch):
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
        return "captured-read-only-signature"

    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_agent_config_signature",
        staticmethod(capture_signature),
    )

    result = await _run_discord_agent(
        runner,
        None,
        intent=False,
        message="What is the current deployment status?",
        escalation_allowed=False,
    )

    assert result["reasoning_effort"] == "low"
    init = _CapturingAgent.last_init
    assert init["model"] == "gpt-5.6-sol"
    assert init["reasoning_config"] == {"enabled": True, "effort": "low"}
    assert init["runtime_mode"] == "read_only"
    assert "tool_delay" not in init
    assert "verify_on_stop" not in init
    audit = runner._agent_cache["agent:main:discord:thread:thread-1"][0]._runtime_audit_context
    assert audit["model_tier"] == "discord_read_only"
    assert audit["model_tier_source"] == "route"
    assert audit["runtime_route"] == "discord_read_only"
    assert captured_cache_keys[0]["gateway.discord_model_route"] is True
    assert captured_cache_keys[0]["gateway.discord_model_tier"] == "discord_read_only"
    assert captured_cache_keys[0]["gateway.discord_action_request_fast_path"] is False
    assert captured_cache_keys[0]["gateway.discord_feature_request_fast_path"] is False


@pytest.mark.parametrize("intent", [False, True])
@pytest.mark.asyncio
async def test_explicit_higher_reasoning_uses_deep_review_tier(monkeypatch, intent):
    _patch_agent_runtime(monkeypatch)
    runner = _make_runner()

    result = await _run_discord_agent(
        runner,
        {"initial_request": "Review this", "message_id": "300", "kanban_board": None}
        if intent
        else None,
        intent=intent,
        message="Use high reasoning for this turn.",
    )

    assert result["reasoning_effort"] == "high"
    assert _CapturingAgent.last_init["model"] == "gpt-5.6-sol"
    assert _CapturingAgent.last_init["reasoning_config"] == {
        "enabled": True,
        "effort": "high",
    }
    audit = runner._agent_cache["agent:main:discord:thread:thread-1"][0]._runtime_audit_context
    assert audit["model_tier"] == "deep_review"
    assert audit["runtime_route"] == (
        "discord_action_request" if intent else "discord_read_only"
    )


@pytest.mark.asyncio
async def test_read_only_turn_exposes_escalation_schema_despite_legacy_flag(monkeypatch):
    _patch_agent_runtime(monkeypatch)
    runner = _make_runner()

    result = await _run_discord_agent(
        runner,
        None,
        intent=False,
        message="Do not implement; plan only.",
        escalation_allowed=False,
    )

    assert result["final_response"] == "ok"
    assert "discord-action-escalation" in _CapturingAgent.last_init["enabled_toolsets"]
    cached_agent = runner._agent_cache["agent:main:discord:thread:thread-1"][0]
    assert cached_agent._discord_action_escalation_allowed is True


@pytest.mark.asyncio
async def test_ambiguous_read_only_turn_exposes_escalation_schema(monkeypatch):
    _patch_agent_runtime(monkeypatch)
    runner = _make_runner()

    await _run_discord_agent(
        runner,
        None,
        intent=False,
        message="Could this parser be improved?",
        escalation_allowed=True,
    )

    assert "discord-action-escalation" in _CapturingAgent.last_init["enabled_toolsets"]


@pytest.mark.asyncio
async def test_explicit_session_model_and_provider_override_action_tier(monkeypatch):
    _patch_agent_runtime(monkeypatch)
    runner = _make_runner()
    session_key = "agent:main:discord:thread:thread-1"
    runner._session_model_overrides[session_key] = {
        "model": "custom/explicit-model",
        "provider": "custom-provider",
        "api_key": "test-key",
        "base_url": "https://provider.example/v1",
        "api_mode": "chat_completions",
    }

    await _run_discord_agent(
        runner,
        {"initial_request": "Build it", "message_id": "300", "kanban_board": None},
        intent=True,
    )

    init = _CapturingAgent.last_init
    assert init["model"] == "custom/explicit-model"
    assert init["provider"] == "custom-provider"
    audit = runner._agent_cache[session_key][0]._runtime_audit_context
    assert audit["model_tier"] == ""
    assert audit["model_tier_source"] == "session_override"
    assert audit["runtime_route"] == "discord_action_request"


@pytest.mark.asyncio
async def test_persisted_auto_thread_summary_reaches_action_request_route(monkeypatch):
    _patch_agent_runtime(monkeypatch)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {"discord": {"action_request_model_tier": "advanced"}},
    )
    runner = _make_runner()
    source = _make_discord_source()
    feature_summary = {
        "initial_request": "no-op change end-to-end",
        "message_id": "300",
        "thread_id": "thread-1",
        "kanban_board": None,
    }
    load_handle = MagicMock(return_value=feature_summary)
    runner.adapters = {
        Platform.DISCORD: SimpleNamespace(
            _load_feature_summary_handle_by_thread_id=load_handle,
        )
    }
    event = MessageEvent(
        text="no-op change end-to-end",
        message_type=MessageType.TEXT,
        source=source,
        message_id="thread-1",
    )

    assert runner._hydrate_discord_feature_summary_from_adapter(event) is feature_summary
    load_handle.assert_called_once_with("thread-1")
    runner.adapters = {}

    result = await runner._run_agent(
        message=event.text,
        context_prompt="Context prompt",
        history=[],
        source=source,
        session_id="session-1",
        session_key="agent:main:discord:thread:thread-1",
        feature_summary=event.feature_summary,
        discord_runtime_mode="action",
    )

    assert result["final_response"] == "ok"
    init = _CapturingAgent.last_init
    assert init["model"] == "gpt-5.6-sol"
    audit = runner._agent_cache["agent:main:discord:thread:thread-1"][0]._runtime_audit_context
    assert audit["model_tier"] == "advanced"
    assert audit["model_tier_source"] == "route"
    assert audit["runtime_route"] == "discord_action_request"


@pytest.mark.asyncio
async def test_zero_background_workers_finalize_summary_and_reaction():
    runner = object.__new__(gateway_run.GatewayRunner)
    callbacks = []
    original_start = AsyncMock()
    original_complete = AsyncMock()

    def register_callback(session_key, callback, generation=None):
        callbacks.append(callback)

    adapter = SimpleNamespace(
        platform=Platform.DISCORD,
        on_processing_start=original_start,
        on_processing_complete=original_complete,
        register_post_delivery_callback=register_callback,
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._session_has_pending_background_workers = MagicMock(return_value=False)
    runner._discord_work_item_id_for_event = lambda *args, **kwargs: None
    runner._update_discord_summaries = AsyncMock(return_value=True)
    source = _make_discord_source()
    event = MessageEvent(
        text="no-op change end-to-end",
        source=source,
        feature_summary={
            "initial_request": "no-op change end-to-end",
            "message_id": "300",
            "kanban_board": None,
        },
        discord_runtime_mode="action",
    )

    runner._install_background_worker_reaction_gate(adapter)
    await adapter.on_processing_complete(event, ProcessingOutcome.SUCCESS)

    original_start.assert_not_awaited()
    original_complete.assert_awaited_once_with(event, ProcessingOutcome.SUCCESS)

    runner._register_discord_summary_post_delivery(
        event=event,
        source=source,
        session_key="agent:main:discord:thread:thread-1",
        run_generation=1,
        session_id="session-1",
        final_response="Completed the no-op change.",
        agent_result={"completed": True},
    )
    assert len(callbacks) == 1
    assert await callbacks[0]() is True
    update_kwargs = runner._update_discord_summaries.await_args.kwargs
    assert update_kwargs["feature_summary"] is event.feature_summary
    assert update_kwargs["status"] == "Complete"
    assert update_kwargs["final_response"] == "Completed the no-op change."


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
    assert "verify_on_stop" not in init
    assert init["reasoning_config"] == {"enabled": True, "effort": "high"}
    assert "Discord action-request thread guidance" not in str(init.get("ephemeral_system_prompt") or "")


@pytest.mark.asyncio
async def test_discord_action_request_reasoning_override_is_configurable(monkeypatch):
    _install_fake_agent(monkeypatch)
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "discord": {"action_request_model_tier": "feature"},
            "model_tiers": {
                "feature": {
                    "model": "custom/feature-model",
                    "opencode_model": "custom/feature-worker",
                    "reasoning_effort": "low",
                }
            },
        },
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

    assert _CapturingAgent.last_init["model"] == "custom/feature-model"
    assert _CapturingAgent.last_init["reasoning_config"] == {"enabled": True, "effort": "low"}


@pytest.mark.asyncio
async def test_discord_feature_request_reasoning_override_remains_legacy_alias(monkeypatch):
    _install_fake_agent(monkeypatch)
    monkeypatch.setattr(gateway_run, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        gateway_run,
        "_load_gateway_config",
        lambda: {
            "discord": {
                "action_request_model_tier": "",
                "feature_request_reasoning_effort": "medium",
            }
        },
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
    assert captured_cache_keys[0]["gateway.runtime_mode"] == "action"
    assert captured_cache_keys[0]["gateway.discord_action_request_fast_path"] is True
    assert captured_cache_keys[0]["gateway.discord_feature_request_fast_path"] is True
    assert captured_cache_keys[0]["gateway.tool_delay"] == 0.0
    assert captured_cache_keys[0]["gateway.verify_on_stop"] is True
