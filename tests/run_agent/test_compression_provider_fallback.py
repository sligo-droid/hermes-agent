"""Runtime regression for compression provider-outage fallback."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.context_compressor import SUMMARY_PREFIX
from run_agent import AIAgent


def _chat_response(text: str):
    message = SimpleNamespace(
        content=text,
        tool_calls=None,
        reasoning=None,
        reasoning_content=None,
    )
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="main-model",
        usage=None,
    )


def _anthropic_response(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=None,
    )


def test_compression_gpt_outage_falls_back_to_native_anthropic_and_continues(
    monkeypatch, tmp_path
):
    """Real compression should recover through configured native Anthropic."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.delenv("ANTHROPIC_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    with patch("run_agent.get_tool_definitions", return_value=[]), patch(
        "run_agent.check_toolset_requirements", return_value={}
    ), patch("run_agent.OpenAI"):
        agent = AIAgent(
            model="main-model",
            provider="custom",
            api_mode="chat_completions",
            base_url="https://main.example/v1",
            api_key="main-key",
            quiet_mode=True,
            max_iterations=4,
            skip_context_files=True,
            skip_memory=True,
        )

    agent._cleanup_task_resources = lambda task_id: None
    agent._persist_session = lambda messages, history=None: None
    agent._save_trajectory = lambda messages, user_message, completed: None
    agent._save_session_log = lambda messages: None
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.save_trajectories = False
    agent.compression_enabled = True

    # Make the real preflight compressor trigger on the synthetic history and
    # retain only a small recent tail after summarization.
    agent.context_compressor.context_length = 4_000
    agent.context_compressor.threshold_tokens = 2_000
    agent.context_compressor.tail_token_budget = 300
    agent.context_compressor.protect_last_n = 2
    agent.context_compressor.protect_first_n = 1

    main_client = MagicMock()
    main_client.base_url = "https://main.example/v1"
    main_client.chat.completions.create.return_value = _chat_response(
        "Conversation continued after compression."
    )
    agent.client = main_client

    outage = Exception("Service Unavailable")
    outage.status_code = 503
    gpt_auxiliary = MagicMock()
    gpt_auxiliary.base_url = "https://chatgpt.com/backend-api/codex"
    gpt_auxiliary.chat.completions.create.side_effect = outage

    native_anthropic = MagicMock()
    native_anthropic.messages.create.return_value = _anthropic_response(
        "## Active Task\nContinue the current conversation safely."
    )

    config = {
        "model": {
            "provider": "custom",
            "model": "main-model",
        },
        "auxiliary": {
            "compression": {
                "provider": "openai-codex",
                "model": "gpt-5.4",
                "timeout": 30,
                "fallback_chain": [
                    {
                        "provider": "anthropic",
                        "model": "claude-sonnet-4-6",
                    }
                ],
            }
        },
    }

    history = []
    for index in range(24):
        history.extend(
            [
                {
                    "role": "user",
                    "content": f"Historical request {index}: " + ("x" * 600),
                },
                {
                    "role": "assistant",
                    "content": f"Historical response {index}: " + ("y" * 600),
                },
            ]
        )

    with patch("hermes_cli.config.load_config", return_value=config), patch(
        "agent.auxiliary_client._build_codex_client",
        return_value=(gpt_auxiliary, "gpt-5.4"),
    ), patch(
        "agent.auxiliary_client._select_explicit_anthropic_pool_entry",
        return_value=(False, None),
    ), patch(
        "agent.anthropic_adapter.build_anthropic_client",
        return_value=native_anthropic,
    ):
        result = agent.run_conversation(
            "Please continue from the compacted context.",
            conversation_history=history,
        )

    assert result["completed"] is True
    assert result["final_response"] == "Conversation continued after compression."
    assert agent.context_compressor.compression_count == 1
    assert gpt_auxiliary.chat.completions.create.call_count == 1
    assert native_anthropic.messages.create.call_count == 1
    assert native_anthropic.messages.create.call_args.kwargs["model"] == "claude-sonnet-4-6"
    assert any(
        SUMMARY_PREFIX in str(message.get("content", ""))
        and "Continue the current conversation safely" in str(message.get("content", ""))
        for message in result["messages"]
    )
    main_client.chat.completions.create.assert_called_once()
