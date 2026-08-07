"""Behavior contracts for incremental tool-call persistence (#49045).

A destructive or process-terminating tool that runs during tool execution
must not lose the just-executed assistant(tool_calls) block or the tool
results that were produced before it fired.  These tests pin the contract:

    1. run_conversation flushes the assistant tool-call turn to the session
       DB BEFORE handing control to _execute_tool_calls (so a tool that
       restarts/kills the process never orphans the tool-call block).
    2. The SEQUENTIAL tool path flushes each tool result to the session DB
       immediately after appending it — BEFORE the next tool dispatches.
    3. The CONCURRENT tool path flushes each tool result in append order.

These exercise the REAL production dispatch surfaces:

    * sequential -> ``run_agent.handle_function_call`` (tool_executor ~1256/1298)
    * concurrent -> ``agent._invoke_tool`` (tool_executor ~539)

Mocking the genuine dispatch surface keeps the tests deterministic (no real
``web_search`` / network) AND mutation-survivable: the ordering assertions
read snapshots captured at flush time, so removing any production flush call
makes the corresponding assertion fail.
"""

import copy
import time
import threading
from types import SimpleNamespace
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from agent.conversation_compression import CompressionChurnError
from agent.prompt_builder import format_steer_marker
from agent.tool_dispatch_helpers import make_tool_result_message
from run_agent import AIAgent


def _make_tool_defs(*names: str) -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": f"{name} tool",
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in names
    ]


def _make_agent():
    hermes_home = Path(tempfile.mkdtemp(prefix="hermes-test-home-"))
    (hermes_home / "logs").mkdir(parents=True, exist_ok=True)
    with (
        patch(
            "run_agent.get_tool_definitions",
            return_value=_make_tool_defs("web_search"),
        ),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
        patch("run_agent._hermes_home", hermes_home),
        patch("agent.model_metadata.fetch_model_metadata", return_value={}),
    ):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.client = MagicMock()
    agent._cached_system_prompt = "You are helpful."
    agent._use_prompt_caching = False
    agent.tool_delay = 0
    agent.compression_enabled = False
    agent.save_trajectories = False
    return agent


def _mock_tool_call(name="web_search", arguments="{}", call_id="call_1"):
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _mock_response(content="Hello", finish_reason="stop", tool_calls=None):
    msg = SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model="test/model", usage=None)


# ---------------------------------------------------------------------------
# Contract 1: run_conversation persists the assistant tool-call block BEFORE
# tool execution begins.
# ---------------------------------------------------------------------------
def test_run_conversation_flushes_assistant_tool_call_before_execution():
    agent = _make_agent()
    tool_call = _mock_tool_call(call_id="c1")
    agent.client.chat.completions.create.side_effect = [
        _mock_response(content="", finish_reason="tool_calls", tool_calls=[tool_call]),
        _mock_response(content="done", finish_reason="stop"),
    ]

    # Record a deep snapshot of the message list at every flush so the
    # assertion does not depend on later mutations.
    flush_snapshots: list[list] = []

    def _record_flush(messages, conversation_history=None):
        flush_snapshots.append(copy.deepcopy(messages))

    agent._flush_messages_to_session_db = MagicMock(side_effect=_record_flush)

    # Capture observations at execute time into module-level lists rather than
    # asserting inside _execute_tool_calls — run_conversation's outer loop
    # swallows exceptions, so an in-callback assertion would never surface.
    executed = {"count": 0}
    snapshot_at_execute: list = []

    def _fake_execute(assistant_message, messages, effective_task_id, api_call_count=0):
        executed["count"] += 1
        # Record the DB state observed at the moment tool execution begins.
        snapshot_at_execute.append(
            copy.deepcopy(flush_snapshots[-1]) if flush_snapshots else None
        )
        # Simulate the tool producing a result (as the real path would).
        messages.append(make_tool_result_message("web_search", "search result", "c1"))

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_execute_tool_calls", side_effect=_fake_execute),
    ):
        result = agent.run_conversation("search something")

    assert executed["count"] == 1, "_execute_tool_calls was never reached"
    # The assistant tool-call block MUST have been flushed before execution.
    last = snapshot_at_execute[0]
    assert last is not None, "no flush occurred before tool execution"
    assert last[-1]["role"] == "assistant"
    assert last[-1]["tool_calls"][0]["id"] == "c1"
    assert result["final_response"] == "done"


def test_steer_during_provider_call_skips_every_unstarted_tool():
    agent = _make_agent()
    tool_calls = [
        _mock_tool_call(arguments='{"query":"one"}', call_id="c1"),
        _mock_tool_call(arguments='{"query":"two"}', call_id="c2"),
    ]
    responses = iter(
        [
            _mock_response(content="", finish_reason="tool_calls", tool_calls=tool_calls),
            _mock_response(content="replanned", finish_reason="stop"),
        ]
    )

    def _provider_call(**_kwargs):
        response = next(responses)
        if response.choices[0].finish_reason == "tool_calls":
            assert agent.steer("use a safer approach") is True
        return response

    agent.client.chat.completions.create.side_effect = _provider_call
    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
        patch.object(agent, "_execute_tool_calls") as execute,
    ):
        result = agent.run_conversation("search something")

    execute.assert_not_called()
    tool_results = [m for m in result["messages"] if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in tool_results] == ["c1", "c2"]
    assert all("was not started" in str(m["content"]) for m in tool_results)
    assert "use a safer approach" in str(tool_results[-1]["content"])
    assert result["final_response"] == "replanned"


def test_late_final_response_steer_becomes_one_continuation():
    agent = _make_agent()
    provider_entered = threading.Event()
    release_provider = threading.Event()

    def _provider_call(**_kwargs):
        provider_entered.set()
        assert release_provider.wait(timeout=10)
        return _mock_response(content="done", finish_reason="stop")

    agent.client.chat.completions.create.side_effect = _provider_call
    result_box = {}

    def _run():
        with (
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result_box["result"] = agent.run_conversation("finish")

    thread = threading.Thread(target=_run)
    thread.start()
    assert provider_entered.wait(timeout=10)
    assert agent.steer("one continuation") is True
    release_provider.set()
    thread.join(timeout=20)
    assert not thread.is_alive()

    result = result_box["result"]
    assert result["pending_steer"] == "one continuation"
    assert agent.steer("too late") is False


def test_early_structured_return_closes_intake_and_preserves_continuation():
    agent = _make_agent()
    agent.compression_enabled = True
    agent.context_compressor.threshold_tokens = 1
    agent.context_compressor.should_compress = lambda _tokens: bool(
        agent._steer_intake_open
    )
    original_open = agent._open_steer_intake

    def _open_with_pending(*, supported=True):
        original_open(supported=supported)
        assert agent.steer("preserve this continuation") is True

    agent._open_steer_intake = _open_with_pending
    agent._compress_context = MagicMock(
        side_effect=CompressionChurnError(
            {"message": "deterministic compression churn"}
        )
    )

    with (
        patch(
            "agent.conversation_loop.estimate_messages_tokens_rough",
            return_value=100,
        ),
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation("trigger early compression return")

    assert result["compression_exhausted"] is True
    assert result["pending_steer"] == "preserve this continuation"
    assert agent.steer("accepted after return would be lost") is False
    agent.client.chat.completions.create.assert_not_called()


def test_pre_provider_steer_never_mutates_historical_tool_result():
    agent = _make_agent()
    old_tool = {
        "role": "tool",
        "name": "web_search",
        "tool_call_id": "old-call",
        "content": "stable historical result",
    }
    history = [
        {"role": "user", "content": "old request"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "old-call",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        },
        old_tool,
    ]
    original_open = agent._open_steer_intake

    def _open_with_pending(*, supported=True):
        original_open(supported=supported)
        assert agent.steer("new-turn guidance") is True

    agent._open_steer_intake = _open_with_pending
    agent.client.chat.completions.create.return_value = _mock_response(
        content="done",
        finish_reason="stop",
    )

    with (
        patch.object(agent, "_persist_session"),
        patch.object(agent, "_save_trajectory"),
        patch.object(agent, "_cleanup_task_resources"),
    ):
        result = agent.run_conversation(
            "new request",
            conversation_history=history,
        )

    historical = next(
        msg
        for msg in result["messages"]
        if msg.get("tool_call_id") == "old-call"
    )
    assert historical["content"] == "stable historical result"
    assert old_tool["content"] == "stable historical result"
    assert result["pending_steer"] == "new-turn guidance"
    assert agent.steer("too late") is False


# ---------------------------------------------------------------------------
# Contract 2: the SEQUENTIAL path flushes each tool result immediately, BEFORE
# the next tool dispatches.  Dispatch goes through run_agent.handle_function_call
# (the real production surface), which we mock for determinism.
# ---------------------------------------------------------------------------
def test_execute_tool_calls_sequential_flushes_each_tool_result_before_next_dispatch():
    agent = _make_agent()
    tool_calls = [
        _mock_tool_call(name="web_search", call_id="c1"),
        _mock_tool_call(name="web_search", call_id="c2"),
    ]
    messages: list = []
    assistant_message = SimpleNamespace(content="", tool_calls=tool_calls)

    # Ordered event log interleaving real dispatches and DB flushes.
    events: list = []

    def _fake_dispatch(function_name, function_args, effective_task_id, **kwargs):
        # The result for call N must have been flushed before call N+1 fires.
        events.append(("dispatch", kwargs.get("tool_call_id")))
        return f"result-{kwargs.get('tool_call_id')}"

    def _record_flush(flush_messages, conversation_history=None):
        # Snapshot the tail tool result that triggered this flush.
        tail = flush_messages[-1]
        events.append(("flush", tail.get("role"), tail.get("tool_call_id")))

    agent._flush_messages_to_session_db = MagicMock(side_effect=_record_flush)

    with (
        patch("run_agent.handle_function_call", side_effect=_fake_dispatch) as disp,
        patch(
            "agent.tool_executor.maybe_persist_tool_result",
            side_effect=lambda **kwargs: kwargs["content"],
        ),
    ):
        agent._execute_tool_calls_sequential(assistant_message, messages, "task-1")

    # The mock proves we exercised the REAL sequential dispatch surface.
    assert disp.call_count == 2, "sequential path did not dispatch via handle_function_call"

    # Both tool results landed, in order.
    assert [m["role"] for m in messages] == ["tool", "tool"]
    assert [m["tool_call_id"] for m in messages] == ["c1", "c2"]

    # Ordering contract: each tool result is flushed AFTER its own dispatch
    # and BEFORE the next dispatch. Expected interleaving:
    #   dispatch c1 -> flush c1 -> dispatch c2 -> flush c2
    assert events == [
        ("dispatch", "c1"),
        ("flush", "tool", "c1"),
        ("dispatch", "c2"),
        ("flush", "tool", "c2"),
    ]


# ---------------------------------------------------------------------------
# Contract 3: the CONCURRENT path flushes each collected tool result in append
# order.  Dispatch goes through agent._invoke_tool (the real concurrent
# surface), which we mock for determinism.
# ---------------------------------------------------------------------------
def test_execute_tool_calls_concurrent_flushes_each_tool_result_in_order():
    agent = _make_agent()
    tool_calls = [
        _mock_tool_call(name="web_search", call_id="c1"),
        _mock_tool_call(name="web_search", call_id="c2"),
    ]
    messages: list = []
    assistant_message = SimpleNamespace(content="", tool_calls=tool_calls)

    invoked_ids: list = []

    def _fake_invoke(function_name, function_args, effective_task_id, tool_call_id, **kwargs):
        invoked_ids.append(tool_call_id)
        return f"result-{tool_call_id}"

    # Each flush must observe exactly one more tool result than the previous
    # flush, in append order — i.e. the tail tool_call_id sequence is c1, c2.
    flushed_tool_ids: list = []
    flush_lengths: list = []

    def _record_flush(flush_messages, conversation_history=None):
        flushed_tool_ids.append(flush_messages[-1]["tool_call_id"])
        flush_lengths.append(len([m for m in flush_messages if m.get("role") == "tool"]))

    agent._flush_messages_to_session_db = MagicMock(side_effect=_record_flush)

    with (
        patch.object(agent, "_invoke_tool", side_effect=_fake_invoke) as inv,
        patch(
            "agent.tool_executor.maybe_persist_tool_result",
            side_effect=lambda **kwargs: kwargs["content"],
        ),
    ):
        agent._execute_tool_calls_concurrent(assistant_message, messages, "task-1")

    # Proves the real concurrent dispatch surface was exercised.
    assert inv.call_count == 2, "concurrent path did not dispatch via _invoke_tool"
    assert sorted(invoked_ids) == ["c1", "c2"]

    # Results appended in deterministic order.
    assert [m["tool_call_id"] for m in messages] == ["c1", "c2"]

    # Each tool result was flushed exactly once, in append order, with the
    # running tool count growing by one each time (1 then 2).  Removing either
    # production flush call breaks one of these assertions.
    assert flushed_tool_ids == ["c1", "c2"]
    assert flush_lengths == [1, 2]


def test_final_budget_and_steer_mutations_replace_incremental_sqlite_rows(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "sessions.db")
    agent = _make_agent()
    agent._session_db = db
    agent.session_id = "final-rewrite"
    agent._session_db_created = False
    agent._last_flushed_db_idx = 0
    agent._ensure_db_session()
    agent._open_steer_intake()

    tool_call = _mock_tool_call(name="web_search", call_id="c1")
    assistant_message = SimpleNamespace(content="", tool_calls=[tool_call])
    messages: list = []

    def _budget(tool_messages, **_kwargs):
        tool_messages[-1]["content"] = "aggregate-budgeted-result"
        agent.steer("keep the final guidance")
        return tool_messages

    with (
        patch("run_agent.handle_function_call", return_value="unbudgeted-result"),
        patch(
            "agent.tool_executor.maybe_persist_tool_result",
            side_effect=lambda **kwargs: kwargs["content"],
        ),
        patch("agent.tool_executor.enforce_turn_budget", side_effect=_budget),
    ):
        agent._execute_tool_calls_sequential(assistant_message, messages, "task-1")

    rows = db.get_messages(agent.session_id)
    assert len(rows) == 1
    assert rows[0]["content"] == (
        "aggregate-budgeted-result"
        + format_steer_marker("keep the final guidance")
    )


def test_final_rewrite_preserves_concurrent_external_sqlite_append(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "sessions.db")
    agent = _make_agent()
    agent._session_db = db
    agent.session_id = "final-rewrite-cas"
    agent._session_db_created = False
    agent._last_flushed_db_idx = 0
    agent._ensure_db_session()

    messages = [{"role": "tool", "content": "original", "tool_call_id": "c1"}]
    agent._flush_messages_to_session_db(messages)
    db.append_message(
        session_id=agent.session_id,
        role="user",
        content="external concurrent row",
    )
    messages[0]["content"] = "rewritten"

    agent._rewrite_messages_to_session_db(messages)

    rows = db.get_messages(agent.session_id)
    assert [row["content"] for row in rows] == ["original", "external concurrent row"]


def test_incremental_flush_stamps_timestamp_for_final_rewrite(tmp_path):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "sessions.db")
    agent = _make_agent()
    agent._session_db = db
    agent.session_id = "timestamped-final-rewrite"
    agent._session_db_created = False
    agent._last_flushed_db_idx = 0
    agent._ensure_db_session()
    messages = [{"role": "assistant", "content": "first durable event"}]

    before = time.time()
    agent._flush_messages_to_session_db(messages)
    after = time.time()

    assert before <= messages[0]["timestamp"] <= after
    messages.append({"role": "assistant", "content": "later event"})
    agent._flush_messages_to_session_db(messages)
    assert messages[1]["timestamp"] >= messages[0]["timestamp"]

    agent._rewrite_messages_to_session_db(messages)
    rows = db.get_messages(agent.session_id)
    assert [row["timestamp"] for row in rows] == [
        messages[0]["timestamp"],
        messages[1]["timestamp"],
    ]


def test_final_rewrite_rejects_same_count_concurrent_sqlite_replacement(tmp_path):
    """A stale final rewrite must not pass a same-row-count ABA change."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "sessions.db")
    agent = _make_agent()
    agent._session_db = db
    agent.session_id = "final-rewrite-revision-cas"
    agent._session_db_created = False
    agent._last_flushed_db_idx = 0
    agent._ensure_db_session()

    messages = [{"role": "tool", "content": "original", "tool_call_id": "c1"}]
    agent._flush_messages_to_session_db(messages)
    stale_token = agent._last_flushed_db_snapshot_token

    assert db.replace_messages(
        agent.session_id,
        [{"role": "tool", "content": "concurrent rewrite", "tool_call_id": "c1"}],
    ) is True
    assert db.message_count(agent.session_id) == len(messages)

    messages[0]["content"] = "stale final rewrite"
    agent._rewrite_messages_to_session_db(messages)

    rows = db.get_messages(agent.session_id)
    assert [row["content"] for row in rows] == ["concurrent rewrite"]
    assert agent._last_flushed_db_snapshot_token == stale_token


def test_incremental_append_revision_cas_blocks_aba_final_rewrite(tmp_path):
    """Concurrent replacement invalidates all remaining local write authority."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "sessions.db")
    agent = _make_agent()
    agent._session_db = db
    agent.session_id = "incremental-append-aba"
    agent._session_db_created = False
    agent._last_flushed_db_idx = 0
    agent._ensure_db_session()

    local_messages = [{"role": "user", "content": "local A"}]
    agent._flush_messages_to_session_db(local_messages)
    assert agent._last_flushed_db_snapshot_token == (agent.session_id, 1)

    assert db.replace_messages(
        agent.session_id,
        [
            {"role": "user", "content": "concurrent X"},
            {"role": "assistant", "content": "concurrent Y"},
        ],
        expected_transcript_revision=1,
    ) is True
    assert db.get_session(agent.session_id)["transcript_revision"] == 2

    local_messages.append({"role": "assistant", "content": "local B"})
    agent._flush_messages_to_session_db(local_messages)

    assert agent._session_db_append_conflict_session_id == agent.session_id
    assert agent._last_flushed_db_snapshot_token is None
    assert [row["content"] for row in db.get_messages(agent.session_id)] == [
        "concurrent X",
        "concurrent Y",
    ]

    # Same row count would satisfy the legacy count guard, but lost append CAS
    # authority must also disable the stale local final rewrite.
    agent._rewrite_messages_to_session_db(local_messages)
    assert [row["content"] for row in db.get_messages(agent.session_id)] == [
        "concurrent X",
        "concurrent Y",
    ]
