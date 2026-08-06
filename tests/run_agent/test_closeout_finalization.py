from __future__ import annotations

import json
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from run_agent import AIAgent


def _tool_defs(*names: str) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _tool_call(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"call_{uuid.uuid4().hex[:8]}",
        type="function",
        function=SimpleNamespace(name=name, arguments=json.dumps(args)),
    )


def _response(content: str = "", tool_calls=None, finish_reason: str = "stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)],
        model="test/model",
        usage=None,
    )


def _agent(*tools: str, max_iterations: int = 1) -> AIAgent:
    with (
        patch("run_agent.get_tool_definitions", return_value=_tool_defs(*tools)),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            max_iterations=max_iterations,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent._disable_streaming = True
    agent.compression_enabled = False
    agent.save_trajectories = False
    agent.tool_delay = 0
    return agent


def _receipt_result(sha: str = "a" * 40) -> str:
    return json.dumps({
        "output": json.dumps({"status": "passed", "head_sha": sha}),
        "exit_code": 0,
        "error": None,
        "classification": {
            "kind": "unknown",
            "semantic_failure": False,
            "dependency_installation_indicated": False,
            "summary": "Command completed without a recognized semantic failure.",
        },
        "closeout_receipt": {
            "schema_version": 1,
            "status": "passed",
            "head_sha": sha,
            "script": "scripts/closeout.sh",
        },
    })


def test_receipt_forces_one_bounded_no_tool_finalization_and_resets_next_turn():
    agent = _agent("terminal")
    terminal_turn = _response(
        tool_calls=[_tool_call("terminal", {"command": "./scripts/closeout.sh"})],
        finish_reason="tool_calls",
    )
    agent.client.chat.completions.create.side_effect = [
        terminal_turn,
        _response("Implemented and verified."),
    ]

    with (
        patch("run_agent.handle_function_call", return_value=_receipt_result()) as execute,
        patch(
            "agent.terminal_outcomes.inspect_repo_closeout_receipt",
            return_value={
                "status": "passed",
                "head_sha": "a" * 40,
                "script": "scripts/closeout.sh",
            },
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("hello")

    assert execute.call_count == 1
    assert result["final_response"] == "Implemented and verified."
    assert result["api_calls"] == 2
    assert result["closeout_receipt"]["head_sha"] == "a" * 40
    assert result["runtime_breakdown"]["closeout_receipt"] == result["closeout_receipt"]
    assert result["terminal_success"] is True
    initial_kwargs = agent.client.chat.completions.create.call_args_list[0].kwargs
    finalizer_kwargs = agent.client.chat.completions.create.call_args_list[1].kwargs
    assert finalizer_kwargs["tool_choice"] == "none"
    assert finalizer_kwargs["tools"] == initial_kwargs["tools"]
    assert finalizer_kwargs.get("parallel_tool_calls") == initial_kwargs.get(
        "parallel_tool_calls"
    )
    assert finalizer_kwargs["max_tokens"] <= 768

    agent.client.chat.completions.create.reset_mock()
    agent.client.chat.completions.create.side_effect = None
    agent.client.chat.completions.create.return_value = _response("Next turn is normal.")
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        next_result = agent.run_conversation("new request")

    assert "closeout_receipt" not in next_result
    assert "terminal_success" not in next_result
    normal_kwargs = agent.client.chat.completions.create.call_args.kwargs
    assert normal_kwargs.get("tool_choice") != "none"
    assert "tools" in normal_kwargs


def test_ignored_tool_choice_is_blocked_and_retried_at_most_once():
    agent = _agent("terminal", "web_search")
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[_tool_call("terminal", {"command": "./scripts/closeout.sh"})],
            finish_reason="tool_calls",
        ),
        _response(
            tool_calls=[_tool_call("web_search", {"query": "should not run"})],
            finish_reason="tool_calls",
        ),
        _response("Finalized without more tools."),
    ]

    with (
        patch("run_agent.handle_function_call", return_value=_receipt_result()) as execute,
        patch(
            "agent.terminal_outcomes.inspect_repo_closeout_receipt",
            return_value={
                "status": "passed",
                "head_sha": "a" * 40,
                "script": "scripts/closeout.sh",
            },
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("hello")

    assert execute.call_count == 1
    assert result["final_response"] == "Finalized without more tools."
    assert result["api_calls"] == 3
    assert agent._closeout_tool_choice_retries == 1
    finalization_calls = agent.client.chat.completions.create.call_args_list[1:]
    assert len(finalization_calls) == 2
    assert all(call.kwargs["tool_choice"] == "none" for call in finalization_calls)
    initial_tools = agent.client.chat.completions.create.call_args_list[0].kwargs["tools"]
    assert all(call.kwargs["tools"] == initial_tools for call in finalization_calls)
    skipped = [
        message for message in result["messages"]
        if message.get("role") == "tool" and "authoritative closeout receipt" in str(message.get("content"))
    ]
    assert len(skipped) == 1


def test_receipt_stops_later_tools_in_same_model_batch():
    agent = _agent("terminal", "web_search")
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[
                _tool_call("terminal", {"command": "./scripts/closeout.sh"}),
                _tool_call("web_search", {"query": "must not run"}),
            ],
            finish_reason="tool_calls",
        ),
        _response("Done."),
    ]

    with (
        patch("run_agent.handle_function_call", return_value=_receipt_result()) as execute,
        patch(
            "agent.terminal_outcomes.inspect_repo_closeout_receipt",
            return_value={
                "status": "passed",
                "head_sha": "a" * 40,
                "script": "scripts/closeout.sh",
            },
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("hello")

    assert execute.call_count == 1
    assert execute.call_args.args[0] == "terminal"
    skipped = [
        message for message in result["messages"]
        if message.get("role") == "tool" and "authoritative closeout receipt" in str(message.get("content"))
    ]
    assert len(skipped) == 1


def test_visual_qa_promotes_pending_receipt_and_skips_unrelated_closeout_tools():
    agent = _agent("terminal", "visual_qa", "web_search")
    agent._preview_readiness = None
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[
                _tool_call("terminal", {"command": "./scripts/closeout.sh"}),
                _tool_call("web_search", {"query": "duplicate verification"}),
                _tool_call("web_search", {"query": "duplicate status check"}),
                _tool_call("visual_qa", {"assertions": [{"kind": "visible"}]}),
                _tool_call("web_search", {"query": "post-closeout work"}),
            ],
            finish_reason="tool_calls",
        ),
        _response("Done after visual QA."),
    ]
    visual_passed = False
    saved_closeout_results = []
    recorded_results = []

    def execute(name, *_args, **_kwargs):
        if name == "terminal":
            return _receipt_result()
        if name == "visual_qa":
            return json.dumps({"status": "passed"})
        raise AssertionError(f"unexpected tool execution: {name}")

    def gate_reason(_agent):
        return "" if visual_passed else "visual_qa_pending"

    def record_evidence(_agent, function_name, _function_args, function_result, *_args, **_kwargs):
        nonlocal visual_passed
        recorded_results.append((function_name, function_result))
        if function_name == "visual_qa":
            visual_passed = True

    def persist_result(*, content, threshold=None, **_kwargs):
        if threshold == 0:
            saved_closeout_results.append(content)
            return "<persisted-output>closeout log saved</persisted-output>"
        return content

    with (
        patch("run_agent.handle_function_call", side_effect=execute) as execute_call,
        patch(
            "agent.terminal_outcomes.inspect_repo_closeout_receipt",
            return_value={
                "status": "passed",
                "head_sha": "a" * 40,
                "script": "scripts/closeout.sh",
            },
        ),
        patch("agent.tool_executor._closeout_receipt_gate_reason", side_effect=gate_reason),
        patch(
            "agent.terminal_outcomes.closeout_receipt_matches_repo_state",
            return_value=True,
        ),
        patch("agent.tool_executor._record_turn_verification_evidence", side_effect=record_evidence),
        patch("agent.tool_executor.maybe_persist_tool_result", side_effect=persist_result),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("ship the visual change")

    assert [call.args[0] for call in execute_call.call_args_list] == ["terminal", "visual_qa"]
    assert result["final_response"] == "Done after visual QA."
    assert result["closeout_receipt"]["head_sha"] == "a" * 40
    assert len(saved_closeout_results) == 1
    assert "closeout_receipt" in saved_closeout_results[0]
    assert "output" in json.loads(recorded_results[0][1])

    tool_contents = [
        str(message.get("content"))
        for message in result["messages"]
        if message.get("role") == "tool"
    ]
    terminal_payload = json.loads(tool_contents[0])
    assert terminal_payload["closeout_receipt_pending"]["required_tool"] == "visual_qa"
    assert terminal_payload["closeout_log"].startswith("<persisted-output>")
    assert "only visual_qa and browser preparation tools may run" in tool_contents[1]
    assert "only visual_qa and browser preparation tools may run" in tool_contents[2]
    visual_payload = json.loads(tool_contents[3])
    assert visual_payload["closeout_receipt"]["head_sha"] == "a" * 40
    assert "finalization_required" in visual_payload
    assert "authoritative closeout receipt" in tool_contents[4]


def test_pending_closeout_recovers_lost_browser_before_visual_qa_retry():
    agent = _agent(
        "terminal",
        "visual_qa",
        "browser_navigate",
        "browser_authenticate",
        "web_search",
        max_iterations=6,
    )
    agent._preview_readiness = None
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[_tool_call("terminal", {"command": "./scripts/closeout.sh"})],
            finish_reason="tool_calls",
        ),
        _response(
            tool_calls=[_tool_call("visual_qa", {"assertions": [{"kind": "visible"}]})],
            finish_reason="tool_calls",
        ),
        _response(
            tool_calls=[_tool_call("browser_navigate", {"url": "http://127.0.0.1:3000"})],
            finish_reason="tool_calls",
        ),
        _response(
            tool_calls=[
                _tool_call("web_search", {"query": "must remain blocked"}),
                _tool_call("browser_authenticate", {"profile": "local"}),
            ],
            finish_reason="tool_calls",
        ),
        _response(
            tool_calls=[_tool_call("visual_qa", {"assertions": [{"kind": "visible"}]})],
            finish_reason="tool_calls",
        ),
        _response("Done after browser recovery and visual QA."),
    ]
    visual_passed = False
    visual_calls = 0

    def execute(name, *_args, **_kwargs):
        nonlocal visual_calls
        if name == "terminal":
            return _receipt_result()
        if name == "visual_qa":
            visual_calls += 1
            if visual_calls == 1:
                return json.dumps(
                    {
                        "status": "blocked",
                        "code": "browser_supervisor_unavailable",
                        "correction": "Reinitialize the task browser, then retry visual_qa once.",
                    }
                )
            return json.dumps({"status": "passed"})
        if name in {"browser_navigate", "browser_authenticate"}:
            return json.dumps({"status": "ok"})
        raise AssertionError(f"unexpected tool execution: {name}")

    def gate_reason(_agent):
        return "" if visual_passed else "visual_qa_pending"

    def record_evidence(_agent, function_name, _function_args, function_result, *_args, **_kwargs):
        nonlocal visual_passed
        if function_name == "visual_qa":
            visual_passed = json.loads(function_result).get("status") == "passed"

    with (
        patch("run_agent.handle_function_call", side_effect=execute) as execute_call,
        patch(
            "agent.terminal_outcomes.inspect_repo_closeout_receipt",
            return_value={
                "status": "passed",
                "head_sha": "a" * 40,
                "script": "scripts/closeout.sh",
            },
        ),
        patch("agent.tool_executor._closeout_receipt_gate_reason", side_effect=gate_reason),
        patch(
            "agent.terminal_outcomes.closeout_receipt_matches_repo_state",
            return_value=True,
        ),
        patch("agent.tool_executor._record_turn_verification_evidence", side_effect=record_evidence),
        patch(
            "agent.tool_executor.maybe_persist_tool_result",
            side_effect=lambda *, content, **_kwargs: content,
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("ship the visual change")

    assert [call.args[0] for call in execute_call.call_args_list] == [
        "terminal",
        "visual_qa",
        "browser_navigate",
        "browser_authenticate",
        "visual_qa",
    ]
    assert result["final_response"] == "Done after browser recovery and visual QA."
    assert result["closeout_receipt"]["head_sha"] == "a" * 40
    skipped = [
        str(message.get("content"))
        for message in result["messages"]
        if message.get("role") == "tool"
        and "only visual_qa and browser preparation tools may run" in str(message.get("content"))
    ]
    assert len(skipped) == 1
    assert "web_search" in skipped[0]


def test_visual_qa_allows_only_one_correction_retry_per_turn():
    agent = _agent("visual_qa", max_iterations=4)
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[_tool_call("visual_qa", {"assertions": [{"kind": "visible"}]})],
            finish_reason="tool_calls",
        ),
        _response(
            tool_calls=[_tool_call("visual_qa", {"assertions": [{"kind": "visible"}]})],
            finish_reason="tool_calls",
        ),
        _response(
            tool_calls=[_tool_call("visual_qa", {"assertions": [{"kind": "visible"}]})],
            finish_reason="tool_calls",
        ),
        _response("Stopped after the bounded retry."),
    ]

    with (
        patch(
            "run_agent.handle_function_call",
            return_value=json.dumps(
                {
                    "status": "failed",
                    "code": "visual_assertions_complete",
                    "attempts": [{"attempt": 1}],
                }
            ),
        ) as execute_call,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("confirm visual QA")

    assert execute_call.call_count == 2
    assert result["final_response"] == "Stopped after the bounded retry."
    assert any(
        "two executable calls plus one malformed-contract repair" in str(
            message.get("content")
        )
        for message in result["messages"]
        if message.get("role") == "tool"
    )


def test_visual_qa_malformed_correction_does_not_consume_execution_slot():
    agent = _agent("visual_qa", max_iterations=5)

    def tool_call():
        return _tool_call(
            "visual_qa",
            {"assertions": [{"kind": "visible"}]},
        )

    agent.client.chat.completions.create.side_effect = [
        _response(tool_calls=[tool_call()], finish_reason="tool_calls"),
        _response(tool_calls=[tool_call()], finish_reason="tool_calls"),
        _response(tool_calls=[tool_call()], finish_reason="tool_calls"),
        _response(tool_calls=[tool_call()], finish_reason="tool_calls"),
        _response("Stopped after contract repair and bounded retry."),
    ]
    failed = json.dumps(
        {
            "status": "failed",
            "code": "visual_assertions_complete",
            "attempts": [{"attempt": 1}],
        }
    )
    invalid = json.dumps(
        {
            "status": "uncertain",
            "code": "invalid_visual_contract",
            "attempts": [],
        }
    )
    passed = json.dumps(
        {
            "status": "passed",
            "code": "visual_assertions_complete",
            "attempts": [{"attempt": 1}],
        }
    )

    with (
        patch(
            "run_agent.handle_function_call",
            side_effect=[failed, invalid, passed],
        ) as execute_call,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("confirm visual QA")

    assert execute_call.call_count == 3
    assert result["final_response"] == "Stopped after contract repair and bounded retry."
    assert any(
        "two executable calls plus one malformed-contract repair" in str(
            message.get("content")
        )
        for message in result["messages"]
        if message.get("role") == "tool"
    )


def test_closed_unmerged_pr_claim_gets_typed_main_verification_followup():
    agent = _agent("verify_main_parent", max_iterations=3)
    agent.verify_on_stop = True
    agent.platform = "discord"
    agent._runtime_mode = "action"
    final = (
        "PR [#1111](https://github.com/sligo-labs/PID/pull/1111) checks passed, "
        "was closed without merge, and main stayed unchanged."
    )
    agent.client.chat.completions.create.side_effect = [
        _response(final),
        _response(
            tool_calls=[
                _tool_call(
                    "verify_main_parent",
                    {"pr_number": 1111, "workdir": "/tmp/pid"},
                )
            ],
            finish_reason="tool_calls",
        ),
        _response(final),
    ]
    main_sha = "a" * 40
    head_sha = "b" * 40
    typed_result = json.dumps(
        {
            "success": True,
            "exit_code": 0,
            "error": None,
            "repository": "sligo-labs/pid",
            "repository_root": "/tmp/pid",
            "pr_number": 1111,
            "head_sha": head_sha,
            "pr_evidence": {
                "status": "success",
                "state": "closed",
                "merged": False,
                "base_ref": "main",
                "head_sha": head_sha,
            },
            "main_branch_evidence": {
                "status": "success",
                "remote_main": main_sha,
                "commit_parent": main_sha,
            },
        }
    )

    with (
        patch("run_agent.handle_function_call", return_value=typed_result) as execute_call,
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("close PR #1111 and leave main unchanged")

    assert execute_call.call_count == 1
    assert execute_call.call_args.args[0] == "verify_main_parent"
    assert result["api_calls"] == 3
    assert result["final_response"] == final
    assert "Verification downgrade" not in result["final_response"]


def test_pending_closeout_gets_visual_and_finalization_grace_calls():
    agent = _agent("terminal", "visual_qa")
    agent._preview_readiness = None
    agent.client.chat.completions.create.side_effect = [
        _response(
            tool_calls=[_tool_call("terminal", {"command": "./scripts/closeout.sh"})],
            finish_reason="tool_calls",
        ),
        _response(
            tool_calls=[_tool_call("visual_qa", {"assertions": [{"kind": "visible"}]})],
            finish_reason="tool_calls",
        ),
        _response("Done after the required visual check."),
    ]
    visual_passed = False

    def execute(name, *_args, **_kwargs):
        if name == "terminal":
            return _receipt_result()
        if name == "visual_qa":
            return json.dumps({"status": "passed"})
        raise AssertionError(f"unexpected tool execution: {name}")

    def gate_reason(_agent):
        return "" if visual_passed else "visual_qa_pending"

    def record_evidence(_agent, function_name, *_args, **_kwargs):
        nonlocal visual_passed
        if function_name == "visual_qa":
            visual_passed = True

    with (
        patch("run_agent.handle_function_call", side_effect=execute),
        patch(
            "agent.terminal_outcomes.inspect_repo_closeout_receipt",
            return_value={
                "status": "passed",
                "head_sha": "a" * 40,
                "script": "scripts/closeout.sh",
            },
        ),
        patch("agent.tool_executor._closeout_receipt_gate_reason", side_effect=gate_reason),
        patch(
            "agent.terminal_outcomes.closeout_receipt_matches_repo_state",
            return_value=True,
        ),
        patch("agent.tool_executor._record_turn_verification_evidence", side_effect=record_evidence),
        patch("agent.tool_executor.maybe_persist_tool_result", side_effect=lambda *, content, **_kwargs: content),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("ship the visual change")

    assert result["final_response"] == "Done after the required visual check."
    assert result["api_calls"] == 3
    assert result["closeout_receipt"]["head_sha"] == "a" * 40
