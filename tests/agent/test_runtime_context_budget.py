from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.agent_init import _finalize_extended_tool_schemas
from agent.conversation_loop import _apply_automatic_runtime_context_budget
from agent.model_metadata import estimate_request_tokens_rough
from agent.runtime_context_budget import (
    RuntimeContextPart,
    automatic_context_budget,
    render_runtime_context_parts,
)


def test_automatic_context_budget_clamps_and_respects_remaining_threshold():
    assert automatic_context_budget(context_length=64_000) == 3_200
    assert automatic_context_budget(context_length=1_000_000) == 8_000
    assert automatic_context_budget(context_length=10_000) == 2_000
    assert (
        automatic_context_budget(
            context_length=200_000,
            threshold_tokens=5_000,
            base_request_tokens=4_600,
        )
        == 400
    )
    assert (
        automatic_context_budget(
            context_length=200_000,
            threshold_tokens=5_000,
            base_request_tokens=5_500,
        )
        == 0
    )


def test_runtime_context_render_labels_and_truncates_head_tail():
    body = "HEAD_MARKER " + ("x" * 8_000) + " TAIL_MARKER"
    render = render_runtime_context_parts(
        [
            RuntimeContextPart(
                label="memory prefetch",
                text=body,
                fence="memory-context",
            )
        ],
        budget_tokens=500,
    )

    assert render.changed is True
    assert render.truncated == ("memory prefetch",)
    assert render.omitted == ()
    assert render.text.startswith("<memory-context>")
    assert "Automatic context from memory prefetch" in render.text
    assert "bounded head/tail excerpt" in render.text
    assert "HEAD_MARKER" in render.text
    assert "TAIL_MARKER" in render.text


def test_apply_runtime_context_budget_shrinks_api_copy_not_persisted_message():
    persisted = {"role": "user", "content": "base " + ("b" * 16_000)}
    api_messages = [persisted.copy()]
    agent = SimpleNamespace(
        tools=[],
        context_compressor=SimpleNamespace(
            context_length=100_000,
            threshold_tokens=4_800,
        ),
    )

    _apply_automatic_runtime_context_budget(
        agent,
        api_messages,
        [
            RuntimeContextPart(
                label="pre_llm_call hook output",
                text="PLUGIN_HEAD " + ("p" * 20_000) + " PLUGIN_TAIL",
            )
        ],
    )

    assert persisted["content"] == "base " + ("b" * 16_000)
    assert "pre_llm_call hook output" in api_messages[0]["content"]
    assert estimate_request_tokens_rough(api_messages, tools=[]) <= 4_800


def _mock_assistant_msg(content="Hello", tool_calls=None):
    return SimpleNamespace(content=content, tool_calls=tool_calls)


def _mock_response(content="Hello", finish_reason="stop", tool_calls=None):
    msg = _mock_assistant_msg(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


def _make_agent():
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            max_iterations=4,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent.compression_enabled = True
    agent.tools = []
    agent.valid_tool_names = set()
    agent.context_compressor.context_length = 100_000
    agent.context_compressor.threshold_tokens = 1_000
    return agent


def test_pre_dispatch_over_threshold_request_compresses_before_provider_call():
    agent = _make_agent()
    agent.client.chat.completions.create.return_value = _mock_response("ok")
    huge_user_message = "x" * 8_000

    with (
        patch.object(
            agent.context_compressor,
            "should_compress",
            side_effect=lambda tokens: tokens >= 1_000,
        ),
        patch.object(
            agent,
            "_compress_context",
            return_value=([{"role": "user", "content": "small"}], "You are helpful."),
        ) as compress,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(huge_user_message)

    compress.assert_called_once()
    agent.client.chat.completions.create.assert_called_once()
    sent = agent.client.chat.completions.create.call_args.kwargs["messages"]
    assert "x" * 1_000 not in str(sent)
    assert result["completed"] is True
    assert result["final_response"] == "ok"


def test_pre_dispatch_over_threshold_rejects_when_compression_makes_no_progress():
    agent = _make_agent()
    huge_user_message = "x" * 8_000

    def no_progress(messages, system_message, **kwargs):
        return messages, "You are helpful."

    with (
        patch.object(
            agent.context_compressor,
            "should_compress",
            side_effect=lambda tokens: tokens >= 1_000,
        ),
        patch.object(agent, "_compress_context", side_effect=no_progress) as compress,
        patch.object(
            agent.context_compressor,
            "emergency_shrink",
            side_effect=lambda messages, target_tokens=None: (messages, {}),
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(huge_user_message)

    assert compress.call_count == 3
    agent.client.chat.completions.create.assert_not_called()
    assert result["failed"] is True
    assert result["compression_exhausted"] is True
    assert "before provider dispatch" in result["error"]


def test_final_tool_schema_pass_runs_after_extension_schemas(monkeypatch):
    from tools.tool_search import AssemblyResult, ToolSearchConfig

    agent = SimpleNamespace(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "core_tool",
                    "description": "core",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "late_context_tool",
                    "description": "late",
                    "parameters": {"type": "object", "properties": {}},
                },
            },
        ],
        valid_tool_names={"core_tool", "late_context_tool"},
        _context_engine_tool_names={"late_context_tool"},
        context_compressor=SimpleNamespace(context_length=100_000),
        quiet_mode=True,
    )
    seen = {}

    def fake_assemble(
        tool_defs,
        *,
        context_length=None,
        config=None,
        always_visible_names=None,
    ):
        seen["names"] = [tool["function"]["name"] for tool in tool_defs]
        seen["context_length"] = context_length
        seen["always_visible_names"] = set(always_visible_names or ())
        return AssemblyResult(
            tool_defs=[tool for tool in tool_defs if tool["function"]["name"] in always_visible_names],
            activated=True,
            deferred_count=1,
            deferred_tokens=123,
            threshold_tokens=456,
        )

    monkeypatch.setattr(
        "tools.tool_search.load_config",
        lambda: ToolSearchConfig(
            enabled="on",
            threshold_pct=10.0,
            search_default_limit=5,
            max_search_limit=20,
        ),
    )
    monkeypatch.setattr("tools.tool_search.assemble_tool_defs", fake_assemble)

    _finalize_extended_tool_schemas(agent)

    assert seen["names"] == ["core_tool", "late_context_tool"]
    assert seen["context_length"] == 100_000
    assert seen["always_visible_names"] == {"late_context_tool"}
    assert agent.valid_tool_names == {"late_context_tool"}
    assert agent._tool_search_final_assembly == {
        "activated": True,
        "deferred_count": 1,
        "deferred_tokens": 123,
        "threshold_tokens": 456,
    }
