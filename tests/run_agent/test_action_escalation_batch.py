from types import SimpleNamespace
from unittest.mock import MagicMock

from run_agent import AIAgent
from agent.tool_executor import _discord_intake_mutation_block


def _call(call_id: str, name: str):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments="{}"),
    )


def test_action_escalation_skips_every_sibling_tool_call():
    agent = object.__new__(AIAgent)
    escalation = _call("esc-1", "escalate_to_action")
    mutation = _call("mut-1", "terminal")
    assistant = SimpleNamespace(tool_calls=[mutation, escalation])
    messages = []

    def execute_only_escalation(current, result_messages, *_args):
        assert current.tool_calls == [escalation]
        result_messages.append(
            {
                "role": "tool",
                "name": "escalate_to_action",
                "tool_call_id": "esc-1",
                "content": '{"success": true, "action_escalation_requested": true}',
            }
        )

    agent._execute_tool_calls_sequential = MagicMock(side_effect=execute_only_escalation)
    agent._execute_tool_calls(assistant, messages, "task-1")

    assert assistant.tool_calls == [mutation, escalation]
    assert [message["tool_call_id"] for message in messages] == ["esc-1", "mut-1"]
    assert "skipped" in messages[1]["content"]


def test_read_only_runtime_allows_observation_and_blocks_mutation():
    agent = SimpleNamespace(_discord_intake_read_only=True)

    assert _discord_intake_mutation_block(
        agent, "write_file", {"path": "app.py"}
    )
    assert _discord_intake_mutation_block(
        agent, "terminal", {"command": "git status"}
    )
    assert _discord_intake_mutation_block(
        agent,
        "delegate_task",
        {"goal": "investigate the repository", "read_only": True},
    ) is None
    assert _discord_intake_mutation_block(
        agent, "browser_navigate", {"url": "https://example.com"}
    ) is None
    assert _discord_intake_mutation_block(agent, "browser_snapshot", {}) is None
    assert _discord_intake_mutation_block(agent, "browser_click", {"ref": "@e1"})
    assert _discord_intake_mutation_block(
        agent, "discord_send_message", {"channel_id": "1", "content": "hi"}
    )
    assert _discord_intake_mutation_block(
        agent, "delegate_task", {"goal": "inspect it"}
    )
    assert _discord_intake_mutation_block(agent, "unknown_plugin_tool", {})
    assert _discord_intake_mutation_block(agent, "read_file", {}) is None
    assert _discord_intake_mutation_block(agent, "web_search", {}) is None
    assert _discord_intake_mutation_block(agent, "escalate_to_action", {}) is None


def test_deferred_read_only_agent_can_recall_without_opening_a_new_store():
    agent = object.__new__(AIAgent)
    provided_db = MagicMock()
    agent._session_db = provided_db
    agent._persist_disabled = True

    assert agent._get_session_db_for_recall() is provided_db

    agent._session_db = None
    assert agent._get_session_db_for_recall() is None
