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


def test_compression_gpt_outage_uses_proxy_chat_fallback_and_continues(
    monkeypatch, tmp_path
):
    """Real compression should recover through proxy Chat Completions."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CLI_PROXY_API_KEY", "test-proxy-key")

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

    proxy_chat = MagicMock()
    proxy_chat.base_url = "http://127.0.0.1:8317/v1"
    proxy_chat.chat.completions.create.return_value = _chat_response(
        "## Active Task\nContinue the current conversation safely."
    )

    config = {
        "model": {
            "provider": "custom",
            "model": "main-model",
        },
        "providers": {
            "cli-proxy-api": {
                "name": "CLIProxyAPI",
                "base_url": "http://127.0.0.1:8317/v1",
                "key_env": "CLI_PROXY_API_KEY",
                "api_mode": "codex_responses",
                "default_model": "gpt-5.6-luna",
            }
        },
        "auxiliary": {
            "compression": {
                "provider": "openai-codex",
                "model": "gpt-5.4",
                "timeout": 30,
                "fallback_chain": [
                    {
                        "provider": "cli-proxy-api",
                        "model": "claude-sonnet-4-6",
                        "api_mode": "chat_completions",
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
        "hermes_cli.runtime_provider.load_config", return_value=config
    ), patch(
        "agent.auxiliary_client._build_codex_client",
        return_value=(gpt_auxiliary, "gpt-5.4"),
    ), patch(
        "agent.auxiliary_client.OpenAI", return_value=proxy_chat
    ) as build_proxy_chat:
        result = agent.run_conversation(
            "Please continue from the compacted context.",
            conversation_history=history,
        )

    assert result["completed"] is True
    assert result["final_response"] == "Conversation continued after compression."
    assert agent.context_compressor.compression_count == 1
    assert gpt_auxiliary.chat.completions.create.call_count == 1
    build_proxy_chat.assert_called_once()
    proxy_kwargs = build_proxy_chat.call_args.kwargs
    assert proxy_kwargs["api_key"] == "test-proxy-key"
    assert proxy_kwargs["base_url"] == "http://127.0.0.1:8317/v1"
    assert proxy_kwargs["max_retries"] == 0
    assert proxy_chat.chat.completions.create.call_count == 1
    proxy_kwargs = proxy_chat.chat.completions.create.call_args.kwargs
    assert proxy_kwargs["model"] == "claude-sonnet-4-6"
    assert "extra_body" not in proxy_kwargs or (
        "reasoning" not in proxy_kwargs["extra_body"]
    )
    assert any(
        SUMMARY_PREFIX in str(message.get("content", ""))
        and "User asked (deterministic, from compacted turns)" in str(
            message.get("content", "")
        )
        and "Historical request" in str(message.get("content", ""))
        for message in result["messages"]
    )
    main_client.chat.completions.create.assert_called_once()


def test_compression_proxy_chat_outage_uses_native_codex_fallback_and_continues(
    monkeypatch, tmp_path
):
    """Real compression should recover from proxy Chat Completions through Codex."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("CLI_PROXY_API_KEY", "test-proxy-key")

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

    agent.context_compressor.context_length = 4_000
    agent.context_compressor.threshold_tokens = 2_000
    agent.context_compressor.tail_token_budget = 300
    agent.context_compressor.protect_last_n = 2
    agent.context_compressor.protect_first_n = 1

    main_client = MagicMock()
    main_client.base_url = "https://main.example/v1"
    main_client.chat.completions.create.return_value = _chat_response(
        "Conversation continued after inverse compression fallback."
    )
    agent.client = main_client

    outage = Exception("Service Unavailable")
    outage.status_code = 503
    proxy_chat = MagicMock()
    proxy_chat.base_url = "http://127.0.0.1:8317/v1"
    proxy_chat.chat.completions.create.side_effect = outage

    native_codex = MagicMock()
    native_codex.base_url = "https://chatgpt.com/backend-api/codex"
    native_codex.chat.completions.create.return_value = _chat_response(
        "## Active Task\nContinue through native Codex compression fallback."
    )

    config = {
        "model": {
            "provider": "custom",
            "model": "main-model",
        },
        "providers": {
            "cli-proxy-api": {
                "name": "CLIProxyAPI",
                "base_url": "http://127.0.0.1:8317/v1",
                "key_env": "CLI_PROXY_API_KEY",
                "api_mode": "codex_responses",
                "default_model": "gpt-5.6-luna",
            }
        },
        "auxiliary": {
            "compression": {
                "provider": "cli-proxy-api",
                "model": "claude-sonnet-4-6",
                "api_mode": "chat_completions",
                "timeout": 30,
                "fallback_chain": [
                    {
                        "provider": "openai-codex",
                        "model": "gpt-5.4",
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
        "hermes_cli.runtime_provider.load_config", return_value=config
    ), patch(
        "agent.auxiliary_client.OpenAI", return_value=proxy_chat
    ) as build_proxy_chat, patch(
        "agent.auxiliary_client._build_codex_client",
        return_value=(native_codex, "gpt-5.4"),
    ):
        result = agent.run_conversation(
            "Please continue from the compacted context.",
            conversation_history=history,
        )

    assert result["completed"] is True
    assert result["final_response"] == (
        "Conversation continued after inverse compression fallback."
    )
    assert agent.context_compressor.compression_count == 1
    # Startup feasibility ran before this test-scoped provider config was
    # installed, so the actual compression call builds the proxy client once.
    # It must preserve the explicit Chat Completions route rather than the
    # named provider's codex_responses default.
    assert build_proxy_chat.call_count == 1
    for proxy_client_call in build_proxy_chat.call_args_list:
        proxy_client_kwargs = proxy_client_call.kwargs
        assert proxy_client_kwargs["api_key"] == "test-proxy-key"
        assert proxy_client_kwargs["base_url"] == "http://127.0.0.1:8317/v1"
        assert proxy_client_kwargs["max_retries"] == 0
    assert proxy_chat.chat.completions.create.call_count == 1
    proxy_kwargs = proxy_chat.chat.completions.create.call_args.kwargs
    assert proxy_kwargs["model"] == "claude-sonnet-4-6"
    assert "extra_body" not in proxy_kwargs or (
        "reasoning" not in proxy_kwargs["extra_body"]
    )
    assert native_codex.chat.completions.create.call_count == 1
    native_kwargs = native_codex.chat.completions.create.call_args.kwargs
    assert native_kwargs["model"] == "gpt-5.4"
    assert native_kwargs["extra_body"]["reasoning"] == {"enabled": False}
    assert any(
        SUMMARY_PREFIX in str(message.get("content", ""))
        and "User asked (deterministic, from compacted turns)" in str(
            message.get("content", "")
        )
        and "Historical request" in str(message.get("content", ""))
        for message in result["messages"]
    )
    main_client.chat.completions.create.assert_called_once()
