"""End-to-end regression coverage for verification budget exhaustion (#61631, #65919 §7)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest

from run_agent import AIAgent
from agent.visual_qa import (
    classify_visual_requirement,
    normalize_visual_requirement,
    visual_requirement_id,
)
from agent.transports.types import NormalizedResponse


def _response(content="composed report"):
    message = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        model="test/model",
        usage=None,
    )


def _tool_response(name="terminal"):
    call = SimpleNamespace(
        id="call_repair",
        type="function",
        function=SimpleNamespace(name=name, arguments="{}"),
    )
    message = SimpleNamespace(content="", tool_calls=[call])
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="tool_calls")],
        model="test/model",
        usage=None,
    )


def _content_filter_response():
    message = SimpleNamespace(content="", tool_calls=None, refusal="blocked")
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="content_filter")],
        model="test/model",
        usage=None,
    )


def _visual_receipt(requirement, *, order=2):
    normalized = normalize_visual_requirement(requirement)
    coverage_ids = [item["id"] for item in normalized["assertions"]]
    return {
        "requirement_id": visual_requirement_id(normalized),
        "contract_id": "vac_" + ("a" * 24),
        "assertion_ids": ["vassert_" + ("c" * 24)],
        "coverage_ids": coverage_ids,
        "status": "passed",
        "attempts": 1,
        "vision_calls": 2,
        "duration_ms": 25,
        "diagnostic_codes": ["appearance_satisfied"],
        "order": order,
    }


@pytest.fixture
def agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        instance = AIAgent(
            session_id="verify-budget-test",
            api_key="test-key",
            base_url="https://example.invalid/v1",
            provider="openai-compat",
            model="test/model",
            max_iterations=1,
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    instance._cached_system_prompt = "stable test prompt"
    instance._session_db = None
    instance._session_json_enabled = False
    instance.save_trajectories = False
    instance.compression_enabled = False
    instance._cleanup_task_resources = lambda *_a, **_kw: None
    instance._save_trajectory = lambda *_a, **_kw: None
    return instance


def _assert_pending_response_survives(agent, result):
    assert result["final_response"] == "composed report"
    assert result["turn_exit_reason"] == "max_iterations_reached(1/1)"
    assert result["completed"] is False
    assert agent._handle_max_iterations.call_count == 0
    # The private candidate+nudge pair is stripped, then the pending fallback
    # is appended once as the terminal assistant response.
    assert [message["role"] for message in result["messages"]] == [
        "user",
        "assistant",
    ]
    assert [message.get("content") for message in result["messages"]].count(
        "composed report"
    ) == 1


def test_verify_on_stop_preserves_composed_report_at_budget_limit(agent, monkeypatch):
    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"changed.py"}
        return _response()

    agent._interruptible_api_call = model_call
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    agent._emit_status = MagicMock()
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    with (
        patch("agent.verification_stop.build_verify_on_stop_nudge", return_value="verify it"),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    _assert_pending_response_survives(agent, result)
    # Only the exactly-once terminal fallback remains; private scaffolding was
    # removed before persistence/return.
    assert not result["messages"][1].get("_verification_stop_synthetic")
    agent._emit_status.assert_not_called()


def test_pre_verify_preserves_composed_report_at_budget_limit(agent, monkeypatch):
    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"changed.py"}
        return _response()

    agent._interruptible_api_call = model_call
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    callback_calls = []
    agent.interim_assistant_callback = lambda text, **kw: callback_calls.append(text)

    with (
        patch("hermes_cli.plugins.has_hook", side_effect=lambda name: name == "pre_verify"),
        patch(
            "hermes_cli.plugins.invoke_hook",
            return_value=[
                {"action": "continue", "message": "run project tests"},
            ],
        ),
        patch("agent.verify_hooks.max_verify_nudges", return_value=2),
    ):
        result = agent.run_conversation("edit changed.py")

    _assert_pending_response_survives(agent, result)
    assert callback_calls == []
    # Only the exactly-once terminal fallback remains; private scaffolding was
    # removed before persistence/return.
    assert not result["messages"][1].get("_pre_verify_synthetic")


def test_intermediate_ack_uses_summary_instead_of_premature_text(agent, monkeypatch):
    agent.valid_tool_names = ["web_search"]
    agent._intent_ack_continuation = True
    agent._looks_like_codex_intermediate_ack = MagicMock(return_value=True)
    agent._interruptible_api_call = lambda _kwargs: _response("I'll inspect the files now")
    agent._handle_max_iterations = MagicMock(return_value="verified summary.")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    emitted = []
    agent.interim_assistant_callback = lambda text, **kw: emitted.append(text)

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("inspect /tmp/project")

    assert result["final_response"] == "verified summary."
    assert result["turn_exit_reason"] == "max_iterations_reached(1/1)"
    assert result["response_previewed"] is False
    assert emitted == ["I'll inspect the files now"]
    assert agent._looks_like_codex_intermediate_ack.call_args.kwargs[
        "require_workspace"
    ] is False
    agent._handle_max_iterations.assert_called_once()


def test_intent_ack_continuation_requires_available_tools(agent, monkeypatch):
    agent.valid_tool_names = []
    agent._intent_ack_continuation = True
    agent._looks_like_codex_intermediate_ack = MagicMock(return_value=True)
    agent._interruptible_api_call = lambda _kwargs: _response("I'll inspect the files now")
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("inspect /tmp/project")

    assert result["final_response"] == "I'll inspect the files now"
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    agent._looks_like_codex_intermediate_ack.assert_not_called()
    agent._handle_max_iterations.assert_not_called()


def test_intent_ack_continuation_remains_capped_at_two_nudges(agent, monkeypatch):
    agent.max_iterations = 3
    agent.iteration_budget.max_total = 3
    agent.valid_tool_names = ["web_search"]
    agent._intent_ack_continuation = True
    agent._looks_like_codex_intermediate_ack = MagicMock(return_value=True)
    answers = iter([
        _response("ack one"),
        _response("ack two"),
        _response("third response becomes final"),
    ])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    emitted = []
    agent.interim_assistant_callback = lambda text, **kw: emitted.append(text)

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("inspect /tmp/project")

    assert emitted == ["ack one", "ack two"]
    assert result["final_response"] == "third response becomes final"
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    assert result["response_previewed"] is False
    assert agent._looks_like_codex_intermediate_ack.call_count == 2
    agent._handle_max_iterations.assert_not_called()


def test_later_verified_response_supersedes_pending_report(agent, monkeypatch):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    answers = iter([_response("premature report"), _response("verified final report")])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=["verify it", None],
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    assert result["final_response"] == "verified final report"
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    assert result["completed"] is True
    assert [message.get("content") for message in result["messages"]] == [
        "edit changed.py",
        "verified final report",
    ]
    agent._handle_max_iterations.assert_not_called()


def test_verified_discord_action_gets_one_private_complete_closeout(agent, monkeypatch):
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.verify_on_stop = True
    answers = iter([
        _response("Fresh verification passed."),
        _response(
            "Updated the dashboard query handling and added the regression test. "
            "`scripts/run_tests.sh tests/dashboard/test_query.py` passed. "
            "PR #42 is merged."
        ),
    ])
    calls = []

    def model_call(api_kwargs):
        calls.append(api_kwargs)
        agent._turn_file_mutation_paths = {"dashboard/query.py"}
        return next(answers)

    agent._interruptible_api_call = model_call
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            return_value=None,
        ),
        patch(
            "agent.verification_stop.should_synthesize_verification_response",
            return_value=True,
        ),
        patch(
            "agent.verification_stop.build_verification_response_nudge",
            return_value="return the complete evidenced closeout",
        ),
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("Fix the dashboard query and ship it.")

    assert result["final_response"].startswith("Updated the dashboard query handling")
    assert result["api_calls"] == 2
    assert result["completed"] is True
    assert agent._completion_response_synthesis_attempts == 1
    assert len(calls) == 2
    assert calls[1].get("tool_choice") == "none"
    assert "tools" not in calls[1]
    assert [message.get("content") for message in result["messages"]] == [
        "Fix the dashboard query and ship it.",
        result["final_response"],
    ]


def test_visual_qa_only_response_gets_one_private_closeout_retry(agent, monkeypatch):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.visual_qa_requirement = requirement
    agent.visual_qa_config = {"mode": "enforce_explicit"}
    answers = iter([
        _response("Visual QA passed. The Confidence view is shipped and live."),
        _response(
            "Split the financial and public charts, added July component breakdowns, "
            "and removed the old month-over-month chart. Visual QA passed."
        ),
    ])

    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"dashboard/src/routes/confidence/+page.svelte"}
        agent._visual_qa_last_edit_order = 1
        agent._turn_runtime_stats["visual_qa_receipts"] = [
            _visual_receipt(requirement, order=2)
        ]
        return next(answers)

    agent._interruptible_api_call = model_call
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation(
            "Split the confidence charts and add component breakdowns."
        )

    assert result["final_response"].startswith("Split the financial and public charts")
    assert [message.get("content") for message in result["messages"]] == [
        "Split the confidence charts and add component breakdowns.",
        result["final_response"],
    ]
    assert agent._completion_response_synthesis_attempts == 1
    agent._handle_max_iterations.assert_not_called()


def test_visual_qa_only_response_survives_when_retry_budget_is_exhausted(
    agent,
    monkeypatch,
):
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.visual_qa_requirement = requirement
    agent.visual_qa_config = {"mode": "enforce_explicit"}

    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"dashboard/src/routes/confidence/+page.svelte"}
        agent._visual_qa_last_edit_order = 1
        agent._turn_runtime_stats["visual_qa_receipts"] = [
            _visual_receipt(requirement, order=2)
        ]
        return _response("Visual QA passed. The Confidence view is shipped and live.")

    agent._interruptible_api_call = model_call
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation(
            "Split the confidence charts and add component breakdowns."
        )

    assert result["final_response"] == (
        "Visual QA passed. The Confidence view is shipped and live."
    )
    assert result["completed"] is True
    assert result["failed"] is False
    assert [message.get("content") for message in result["messages"]].count(
        result["final_response"]
    ) == 1
    agent._handle_max_iterations.assert_not_called()


def test_visual_qa_response_retry_fails_open_on_provider_error(agent, monkeypatch):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    agent._api_max_retries = 1
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.visual_qa_requirement = requirement
    agent.visual_qa_config = {"mode": "enforce_explicit"}
    answers = iter([
        _response("Visual QA passed. The Confidence view is shipped and live."),
        httpx.ConnectError("connection reset"),
    ])

    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"dashboard/src/routes/confidence/+page.svelte"}
        agent._visual_qa_last_edit_order = 1
        agent._turn_runtime_stats["visual_qa_receipts"] = [
            _visual_receipt(requirement, order=2)
        ]
        result = next(answers)
        if isinstance(result, Exception):
            raise result
        return result

    agent._interruptible_api_call = model_call
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation(
            "Split the confidence charts and add component breakdowns."
        )

    assert result["final_response"] == (
        "Visual QA passed. The Confidence view is shipped and live."
    )
    assert result["completed"] is True
    assert result["failed"] is False
    assert [message.get("content") for message in result["messages"]].count(
        result["final_response"]
    ) == 1


def test_visual_qa_response_retry_fails_open_on_nonretryable_error(
    agent,
    monkeypatch,
):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.visual_qa_requirement = requirement
    agent.visual_qa_config = {"mode": "enforce_explicit"}

    class AuthError(Exception):
        status_code = 401

    answers = iter([
        _response("Visual QA passed. The Confidence view is shipped and live."),
        AuthError("invalid API key"),
    ])

    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"dashboard/src/routes/confidence/+page.svelte"}
        agent._visual_qa_last_edit_order = 1
        agent._turn_runtime_stats["visual_qa_receipts"] = [
            _visual_receipt(requirement, order=2)
        ]
        result = next(answers)
        if isinstance(result, Exception):
            raise result
        return result

    agent._interruptible_api_call = model_call
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = agent.run_conversation(
            "Split the confidence charts and add component breakdowns."
        )

    assert result["final_response"].startswith("Visual QA passed")
    assert result["completed"] is True
    assert result["failed"] is False


def test_visual_qa_response_retry_blocks_tool_calls(agent, monkeypatch):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.visual_qa_requirement = requirement
    agent.visual_qa_config = {"mode": "enforce_explicit"}
    answers = iter([
        _response("Visual QA passed. The Confidence view is shipped and live."),
        _tool_response(),
    ])

    def model_call(api_kwargs):
        agent._turn_file_mutation_paths = {"dashboard/src/routes/confidence/+page.svelte"}
        agent._visual_qa_last_edit_order = 1
        agent._turn_runtime_stats["visual_qa_receipts"] = [
            _visual_receipt(requirement, order=2)
        ]
        if api_kwargs.get("tool_choice") == "none":
            assert "tools" not in api_kwargs
        return next(answers)

    agent._interruptible_api_call = model_call
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with (
        patch.object(agent, "_execute_tool_calls") as execute,
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation(
            "Split the confidence charts and add component breakdowns."
        )

    assert result["final_response"].startswith("Visual QA passed")
    assert result["completed"] is True
    execute.assert_not_called()


def test_visual_qa_response_retry_fails_open_on_content_filter(agent, monkeypatch):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.visual_qa_requirement = requirement
    agent.visual_qa_config = {"mode": "enforce_explicit"}
    answers = iter([
        _response("Visual QA passed. The Confidence view is shipped and live."),
        _content_filter_response(),
    ])

    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"dashboard/src/routes/confidence/+page.svelte"}
        agent._visual_qa_last_edit_order = 1
        agent._turn_runtime_stats["visual_qa_receipts"] = [
            _visual_receipt(requirement, order=2)
        ]
        return next(answers)

    agent._interruptible_api_call = model_call
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = agent.run_conversation(
            "Split the confidence charts and add component breakdowns."
        )

    assert result["final_response"].startswith("Visual QA passed")
    assert result["completed"] is True
    assert result["failed"] is False


def test_visual_qa_response_repair_normalizes_codex_without_streaming(
    agent,
    monkeypatch,
):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    agent.api_mode = "codex_responses"
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.visual_qa_requirement = requirement
    agent.visual_qa_config = {"mode": "enforce_explicit"}
    streamed = []
    interim = []
    progress = []
    agent.stream_delta_callback = streamed.append
    agent.interim_assistant_callback = lambda text, **_kwargs: interim.append(text)
    agent.tool_progress_callback = lambda *args: progress.append(args)
    answers = iter([object(), object()])
    agent._interruptible_api_call = MagicMock(side_effect=lambda _kwargs: next(answers))
    transport = MagicMock()
    transport.preflight_kwargs.side_effect = lambda kwargs, **_unused: kwargs
    transport.normalize_response.side_effect = [
        NormalizedResponse(
            content="Visual QA passed. The Confidence view is shipped and live.",
            tool_calls=None,
            finish_reason="stop",
        ),
        NormalizedResponse(
            content="Implemented the requested confidence chart changes. Visual QA passed.",
            tool_calls=None,
            finish_reason="stop",
        ),
    ]
    agent._get_transport = MagicMock(return_value=transport)

    def should_buffer(**_kwargs):
        agent._turn_file_mutation_paths = {"dashboard/src/routes/confidence/+page.svelte"}
        agent._visual_qa_last_edit_order = 1
        agent._turn_runtime_stats["visual_qa_receipts"] = [
            _visual_receipt(requirement, order=2)
        ]
        return True

    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    with (
        patch("agent.visual_qa.should_buffer_visual_qa_response", side_effect=should_buffer),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation(
            "Split the confidence charts and add component breakdowns."
        )

    assert result["final_response"].startswith("Implemented the requested")
    assert streamed == []
    assert interim == []
    assert progress == []
    assert agent._interruptible_api_call.call_count == 2


def test_visual_qa_response_repair_propagates_interrupt(agent, monkeypatch):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.visual_qa_requirement = requirement
    agent.visual_qa_config = {"mode": "enforce_explicit"}
    answers = iter([
        _response("Visual QA passed. The Confidence view is shipped and live."),
        InterruptedError(),
    ])

    def model_call(_kwargs):
        agent._turn_file_mutation_paths = {"dashboard/src/routes/confidence/+page.svelte"}
        agent._visual_qa_last_edit_order = 1
        agent._turn_runtime_stats["visual_qa_receipts"] = [
            _visual_receipt(requirement, order=2)
        ]
        result = next(answers)
        if isinstance(result, Exception):
            raise result
        return result

    agent._interruptible_api_call = model_call
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = agent.run_conversation(
            "Split the confidence charts and add component breakdowns."
        )

    assert result["interrupted"] is True
    assert result["completed"] is False


def test_visual_qa_draft_is_buffered_before_discord_stream_delivery(
    agent,
    monkeypatch,
):
    agent.max_iterations = 3
    agent.iteration_budget.max_total = 3
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.visual_qa_requirement = requirement
    agent.visual_qa_config = {"mode": "enforce_explicit"}
    streamed = []
    agent.stream_delta_callback = streamed.append
    answers = iter([
        _response("Visual QA passed. The Confidence view is shipped and live."),
        _response("Implemented the requested confidence chart changes. Visual QA passed."),
    ])
    def model_call(_kwargs):
        agent._turn_file_mutation_paths = {"dashboard/src/routes/confidence/+page.svelte"}
        agent._visual_qa_last_edit_order = 1
        agent._turn_runtime_stats["visual_qa_receipts"] = [
            _visual_receipt(requirement, order=2)
        ]
        return next(answers)

    agent._interruptible_api_call = MagicMock(side_effect=model_call)
    agent._interruptible_streaming_api_call = MagicMock()

    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    with (
        patch(
            "agent.visual_qa.should_buffer_visual_qa_response",
            return_value=True,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation(
            "Split the confidence charts and add component breakdowns."
        )

    assert result["final_response"].startswith("Implemented the requested")
    agent._interruptible_streaming_api_call.assert_not_called()
    assert agent._interruptible_api_call.call_count == 2
    assert streamed == []


def test_visual_response_retry_does_not_consume_verification_attempts(
    agent,
    monkeypatch,
):
    agent.max_iterations = 2
    agent.iteration_budget.max_total = 2
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.visual_qa_requirement = requirement
    agent.visual_qa_config = {"mode": "enforce_explicit"}
    answers = iter([
        _response("Visual QA passed. The Confidence view is shipped and live."),
        _response("Implemented the requested confidence chart changes. Visual QA passed."),
    ])

    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"dashboard/src/routes/confidence/+page.svelte"}
        agent._visual_qa_last_edit_order = 1
        agent._turn_runtime_stats["visual_qa_receipts"] = [
            _visual_receipt(requirement, order=2)
        ]
        return next(answers)

    agent._interruptible_api_call = model_call
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")
    verify_attempts = []

    def verification_nudge(**kwargs):
        verify_attempts.append(kwargs["attempts"])
        return None

    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=verification_nudge,
        ),
        patch(
            "agent.verification_stop.build_verification_response_nudge",
            return_value="return the complete evidenced closeout",
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation(
            "Split the confidence charts and add component breakdowns."
        )

    assert result["final_response"].startswith("Implemented the requested")
    assert verify_attempts == [0]


def test_visual_pass_does_not_synthesize_while_generic_verification_is_blocked(
    agent,
    monkeypatch,
):
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.visual_qa_requirement = requirement
    agent.visual_qa_config = {"mode": "enforce_explicit"}

    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {
            "dashboard/src/routes/confidence/+page.svelte"
        }
        agent._visual_qa_last_edit_order = 1
        agent._turn_runtime_stats["visual_qa_receipts"] = [
            _visual_receipt(requirement, order=2)
        ]
        return _response("Fresh verification passed.")

    agent._interruptible_api_call = MagicMock(side_effect=model_call)
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    with (
        patch("agent.verification_stop.build_verify_on_stop_nudge", return_value=None),
        patch(
            "agent.verification_stop.build_verification_response_nudge",
            return_value=None,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation(
            "Split the confidence charts and add component breakdowns."
        )

    assert result["final_response"] == "Fresh verification passed."
    assert agent._interruptible_api_call.call_count == 1
    assert agent._completion_response_synthesis_attempts == 0


def test_successful_visual_repair_does_not_leak_fallback_into_pre_verify(
    agent,
    monkeypatch,
):
    agent.max_iterations = 3
    agent.iteration_budget.max_total = 3
    agent._api_max_retries = 1
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.visual_qa_requirement = requirement
    agent.visual_qa_config = {"mode": "enforce_explicit"}
    answers = iter([
        _response("Visual QA passed. The Confidence view is shipped and live."),
        _response("Implemented the requested confidence chart changes. Visual QA passed."),
        httpx.ConnectError("connection reset"),
    ])

    def model_call(_api_kwargs):
        agent._turn_file_mutation_paths = {"dashboard/src/routes/confidence/+page.svelte"}
        agent._visual_qa_last_edit_order = 1
        agent._turn_runtime_stats["visual_qa_receipts"] = [
            _visual_receipt(requirement, order=2)
        ]
        result = next(answers)
        if isinstance(result, Exception):
            raise result
        return result

    agent._interruptible_api_call = model_call
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")
    pre_verify = iter(["run final verification", None])

    with (
        patch("hermes_cli.plugins.has_hook", return_value=True),
        patch("hermes_cli.plugins.invoke_hook", side_effect=lambda *_a, **_k: []),
        patch(
            "agent.conversation_loop._get_pre_verify_continue_message",
            side_effect=lambda **_kwargs: next(pre_verify),
        ),
    ):
        result = agent.run_conversation(
            "Split the confidence charts and add component breakdowns."
        )

    assert result["failed"] is True
    assert result["completed"] is False
    assert result["final_response"].startswith("API call failed")


def test_visual_response_retry_is_disabled_during_closeout_finalization(
    agent,
    monkeypatch,
):
    requirement = classify_visual_requirement(
        "Split the confidence charts and add component breakdowns.",
        worker_route="action",
    )
    agent.platform = "discord"
    agent._runtime_mode = "action"
    agent.visual_qa_requirement = requirement
    agent.visual_qa_config = {"mode": "enforce_explicit"}

    def model_call(_api_kwargs):
        agent._accepted_closeout_receipt = {"status": "passed"}
        agent._turn_file_mutation_paths = {"dashboard/src/routes/confidence/+page.svelte"}
        agent._visual_qa_last_edit_order = 1
        agent._turn_runtime_stats["visual_qa_receipts"] = [
            _visual_receipt(requirement, order=2)
        ]
        return _response("Visual QA passed. The Confidence view is shipped and live.")

    agent._interruptible_api_call = model_call
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    with patch("hermes_cli.plugins.invoke_hook", return_value=[]):
        result = agent.run_conversation(
            "Split the confidence charts and add component breakdowns."
        )

    assert result["final_response"] == (
        "Visual QA passed. The Confidence view is shipped and live."
    )
    assert result["completed"] is True
    assert agent._completion_response_synthesis_attempts == 0


def test_multiple_verification_retries_hide_candidates_until_verified(agent, monkeypatch):
    """Only the verified final candidate is delivered to the caller."""
    agent.max_iterations = 3
    agent.iteration_budget.max_total = 3
    answers = iter([
        _response("candidate one"),
        _response("candidate two"),
        _response("candidate three"),
    ])
    agent._interruptible_api_call = lambda _kwargs: next(answers)
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    # Three nudges, then None (so the third candidate is the final response).
    nudge_side_effects = ["verify it", "verify it", None]

    emitted = []
    agent.interim_assistant_callback = lambda text, **kw: emitted.append(text)

    with (
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=nudge_side_effects,
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    assert emitted == []
    assert result["final_response"] == "candidate three"
    assert result["turn_exit_reason"] == "text_response(finish_reason=stop)"
    assert result["completed"] is True
    agent._handle_max_iterations.assert_not_called()


def test_verification_false_finalizes_candidate_once(agent, monkeypatch):
    """When verification returns false/exception, the candidate is finalized once."""
    agent._interruptible_api_call = lambda _kwargs: _response("the answer")
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    emitted = []
    agent.interim_assistant_callback = lambda text, **kw: emitted.append(text)

    with (
        # build_verify_on_stop_nudge raises — simulates verification check failure
        patch(
            "agent.verification_stop.build_verify_on_stop_nudge",
            side_effect=RuntimeError("verify check crashed"),
        ),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    # No interim emission because verification did not run (exception path
    # sets _verify_nudge = None, so the candidate becomes the final response
    # without an interim emission).
    assert result["final_response"] == "the answer"
    assert result["completed"] is True
    agent._handle_max_iterations.assert_not_called()


def test_verify_on_stop_hides_candidate_from_ui_until_terminal_fallback(agent, monkeypatch):
    agent._interruptible_api_call = lambda _kwargs: _response("composed report")
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    callback_calls = []

    def capture_callback(text, *, already_streamed=None):
        callback_calls.append({"text": text, "already_streamed": already_streamed})

    agent.interim_assistant_callback = capture_callback

    with (
        patch("agent.verification_stop.build_verify_on_stop_nudge", return_value="verify it"),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    assert callback_calls == []
    assert result["final_response"] == "composed report"
    assert result["response_previewed"] is False


def test_streamed_interim_then_different_summary_not_marked_previewed(agent, monkeypatch):
    """Ordinary interim narration followed by a different non-streamed summary.

    The model streams "I'll inspect the files now" as an intermediate ack.
    _emit_interim_assistant_message is called for this ordinary narration,
    which must NOT set _response_was_previewed. Then _handle_max_iterations
    produces a different summary through the non-streaming Chat Completions
    path. The final result must NOT be marked as previewed — the interim was
    unrelated mid-turn commentary, not the final response — so the CLI renders
    the summary instead of suppressing it. (#65919 review: response-loss blocker)
    """
    agent.valid_tool_names = ["web_search"]
    agent._intent_ack_continuation = True
    agent._looks_like_codex_intermediate_ack = MagicMock(return_value=True)
    agent._interruptible_api_call = lambda _kwargs: _response("I'll inspect the files now")
    agent._handle_max_iterations = MagicMock(return_value="Here is the summary of what I found.")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "0")

    emitted = []
    agent.interim_assistant_callback = lambda text, **kw: emitted.append(text)

    with (
        patch("hermes_cli.plugins.has_hook", return_value=False),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("inspect /tmp/project")

    # The final response is the different summary from _handle_max_iterations.
    assert result["final_response"] == "Here is the summary of what I found."
    assert emitted == ["I'll inspect the files now"]
    assert agent._looks_like_codex_intermediate_ack.call_args.kwargs[
        "require_workspace"
    ] is False
    agent._handle_max_iterations.assert_called_once()
    # CRITICAL: response_previewed must be False — the interim narration was
    # NOT the final response, so the CLI must render the summary.
    assert result["response_previewed"] is False


def test_provider_streamed_verification_candidate_reused_marked_previewed(agent, monkeypatch):
    """A provider-streamed fallback is not deliberately re-emitted.

    The verification gate itself never calls the interim callback. If provider
    deltas had already exposed the text before the gate classified it, the
    pending fallback is marked previewed so the UI settles it without a second
    delivery.
    """
    agent._interruptible_api_call = lambda _kwargs: _response("composed report")
    agent._handle_max_iterations = MagicMock(return_value="replacement summary")
    monkeypatch.setenv("HERMES_VERIFY_ON_STOP", "1")

    agent._turn_file_mutation_paths = {"changed.py"}

    callback_calls = []

    def capture_callback(text, *, already_streamed=None):
        callback_calls.append({"text": text, "already_streamed": already_streamed})

    agent.interim_assistant_callback = capture_callback

    # Simulate that the candidate text was already streamed. The streaming
    # buffer is cleared after the response is processed, so mock the check
    # directly — this is the condition the test validates: when the candidate
    # was streamed, the previewed flag propagates to the finalizer.
    with (
        patch.object(agent, "_interim_content_was_streamed", return_value=True),
        patch("agent.verification_stop.build_verify_on_stop_nudge", return_value="verify it"),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        result = agent.run_conversation("edit changed.py")

    assert callback_calls == []
    assert result["final_response"] == "composed report"
    assert result["response_previewed"] is True
