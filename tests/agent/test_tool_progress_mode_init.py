from unittest.mock import patch

from run_agent import AIAgent


def test_aiagent_accepts_and_stores_tool_progress_mode():
    with patch.object(AIAgent, "_get_transport"):
        agent = AIAgent(
            model="test/model",
            api_key="test-key",
            base_url="http://localhost:1234/v1",
            tool_progress_mode="off",
            skip_context_files=True,
            skip_memory=True,
        )

    assert agent.tool_progress_mode == "off"
