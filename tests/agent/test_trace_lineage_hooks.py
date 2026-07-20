"""Focused observer-lineage tests for delegated and coding workers."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_cli import plugins
from tools import coding_worker_tool as coding
from tools import delegate_tool as delegate


def test_live_conversation_end_hook_carries_full_turn_lineage(monkeypatch):
    from run_agent import AIAgent

    manager = plugins.PluginManager()
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    captured = []
    manager._hooks["on_session_end"] = [lambda **kwargs: captured.append(kwargs)]
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="test-model",
            provider="test-provider",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
            enabled_toolsets=[],
        )
    agent.client = MagicMock()
    agent.client.chat.completions.create.return_value = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content="done", tool_calls=None),
                finish_reason="stop",
            )
        ],
        model="test-model",
        usage=None,
    )
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.tool_delay = 0
    agent._fallback_chain = []
    agent.session_id = "child-session"
    agent._parent_session_id = "parent-session"
    agent._delegate_root_agent = SimpleNamespace(session_id="root-session")

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("finish", task_id="task-live")

    assert result["final_response"] == "done"
    assert len(captured) == 1
    assert captured[0]["session_id"] == "child-session"
    assert captured[0]["parent_session_id"] == "parent-session"
    assert captured[0]["root_session_id"] == "root-session"
    assert captured[0]["task_id"] == "task-live"
    assert captured[0]["turn_id"] == agent._current_turn_id
    assert captured[0]["turn_id"].startswith("child-session:task-live:")


def test_subagent_start_carries_root_parent_and_child_ids(monkeypatch):
    manager = plugins.PluginManager()
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    captured = []
    manager._hooks["subagent_start"] = [lambda **kwargs: captured.append(kwargs)]
    root = SimpleNamespace(session_id="root-session")
    parent = SimpleNamespace(
        session_id="parent-session",
        platform="discord",
        _current_turn_id="turn-1",
    )
    child = SimpleNamespace(session_id="child-session")

    delegate._emit_subagent_start(
        child=child,
        parent_agent=parent,
        root_agent=root,
        parent_subagent_id="sa-parent",
        subagent_id="sa-child",
        role="leaf",
        goal="inspect parser",
    )

    assert captured == [
        {
            "root_session_id": "root-session",
            "parent_session_id": "parent-session",
            "parent_turn_id": "turn-1",
            "parent_subagent_id": "sa-parent",
            "child_session_id": "child-session",
            "child_subagent_id": "sa-child",
            "child_role": "leaf",
            "child_goal": "inspect parser",
            "platform": "discord",
            "telemetry_schema_version": "hermes.observer.v1",
        }
    ]


def test_coding_worker_hooks_share_one_stable_root_and_redact_messages(monkeypatch):
    manager = plugins.PluginManager()
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    starts = []
    stops = []
    manager._hooks["coding_worker_start"] = [lambda **kwargs: starts.append(kwargs)]
    manager._hooks["coding_worker_stop"] = [lambda **kwargs: stops.append(kwargs)]
    parent = SimpleNamespace(
        session_id="parent-session",
        platform="discord",
        _current_turn_id="turn-1",
        turn_worker_runs=[],
    )
    secret = "sk-secretsecretsecret"

    record = coding._start_worker_run(
        parent,
        backend="codex",
        model="gpt-test",
        reasoning="high",
        model_tier="advanced",
        task=f"Authorization: Bearer {secret}\n" + ("x" * 100_100),
        cwd=f"/workspace?access_token={secret}",
    )
    coding._finish_worker_run(
        record,
        failed=False,
        status="completed",
        summary=f"done with {secret}",
        error=f"diagnostic {secret}",
        duration_seconds=1.25,
        thread_id="thread-1",
        turn_id="worker-turn-1",
        worker_messages=[
            {
                "role": "assistant",
                "content": f"Authorization: Bearer {secret}",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "terminal",
                            "arguments": json.dumps({"cmd": f"echo {secret}"}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-1",
                "content": "tests passed",
            },
        ],
        worker_events=[
            {"type": "log", "message": f"Authorization: Bearer {secret}"}
        ],
    )

    assert len(starts) == len(stops) == 1
    assert starts[0]["worker_session_id"] == stops[0]["worker_session_id"]
    assert starts[0]["root_session_id"] == "parent-session"
    assert stops[0]["parent_session_id"] == "parent-session"
    assert stops[0]["parent_turn_id"] == "turn-1"
    assert stops[0]["thread_id"] == "thread-1"
    assert stops[0]["duration_ms"] == 1250
    assert len(starts[0]["task"]) == 100_000
    assert secret not in starts[0]["task"]
    assert secret not in starts[0]["cwd"]
    assert secret not in stops[0]["summary"]
    assert secret not in stops[0]["error"]
    assert secret not in stops[0]["worker_messages"][0]["content"]
    assert stops[0]["worker_messages"][1]["tool_name"] == "terminal"
    assert secret not in json.dumps(stops[0]["worker_events"])
    assert id(record) not in coding._WORKER_OBSERVER_CONTEXTS
    assert parent.turn_worker_runs == [
        {
            "backend": "codex",
            "model": "gpt-test",
            "reasoning": "high",
            "model_tier": "advanced",
        }
    ]


def test_coding_worker_stop_hook_failure_is_fail_open(monkeypatch):
    manager = plugins.PluginManager()
    monkeypatch.setattr(plugins, "_plugin_manager", manager)

    def fail_hook(**_kwargs):
        raise RuntimeError("observer unavailable")

    manager._hooks["coding_worker_stop"] = [fail_hook]
    parent = SimpleNamespace(session_id="parent-session", turn_worker_runs=[])
    record = coding._start_worker_run(
        parent,
        backend="codex",
        model="gpt-test",
        reasoning="medium",
        model_tier=None,
    )

    coding._finish_worker_run(record, failed=False, summary="done")

    assert id(record) not in coding._WORKER_OBSERVER_CONTEXTS
    assert parent.turn_worker_runs[0].get("failed") is None


def test_missing_coding_worker_observers_is_a_noop(monkeypatch):
    manager = plugins.PluginManager()
    monkeypatch.setattr(plugins, "_plugin_manager", manager)
    parent = SimpleNamespace(
        session_id="parent-session",
        platform="discord",
        turn_worker_runs=[],
    )
    monkeypatch.setattr(
        coding,
        "_observer_safe_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("observer payload should not be built")
        ),
    )

    record = coding._start_worker_run(
        parent,
        backend="codex",
        model="gpt-test",
        reasoning="medium",
        model_tier=None,
    )

    assert id(record) not in coding._WORKER_OBSERVER_CONTEXTS
    coding._finish_worker_run(record, failed=False)
    assert parent.turn_worker_runs == [
        {
            "backend": "codex",
            "model": "gpt-test",
            "reasoning": "medium",
            "model_tier": None,
        }
    ]
