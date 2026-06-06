import os
import tempfile
from pathlib import Path
from unittest.mock import patch


def test_recent_compression_lineage_reports_zero_message_children():
    from hermes_state import SessionDB

    with tempfile.TemporaryDirectory() as tmpdir:
        db = SessionDB(db_path=Path(tmpdir) / "state.db")
        db.create_session("root", "cli")
        db.end_session("root", "compression")
        db.create_session("child1", "cli", parent_session_id="root")
        db.update_token_counts("child1", input_tokens=150_000, absolute=True)
        db.end_session("child1", "compression")
        db.create_session("child2", "cli", parent_session_id="child1")
        db.update_token_counts("child2", input_tokens=160_000, absolute=True)

        lineage = db.get_recent_compression_lineage("child2")

    assert [row["id"] for row in lineage] == ["child2", "child1", "root"]
    assert lineage[0]["message_count"] == 0
    assert lineage[0]["input_tokens"] == 160_000
    assert lineage[1]["end_reason"] == "compression"


def test_recent_compression_lineage_excludes_out_of_window_rows():
    from hermes_state import SessionDB

    with tempfile.TemporaryDirectory() as tmpdir:
        db = SessionDB(db_path=Path(tmpdir) / "state.db")
        db.create_session("stale_root", "cli")
        db.end_session("stale_root", "compression")
        db.create_session("current", "cli", parent_session_id="stale_root")
        db.end_session("current", "compression")

        stale_started_at = 1.0
        db._conn.execute(
            "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
            (stale_started_at, stale_started_at, "stale_root"),
        )
        db._conn.commit()

        lineage = db.get_recent_compression_lineage("current", window_seconds=60)

    assert [row["id"] for row in lineage] == ["current"]


def test_compress_context_trips_churn_breaker_before_session_rotation():
    from agent.conversation_compression import CompressionChurnError, compress_context
    from hermes_state import SessionDB
    from run_agent import AIAgent

    with tempfile.TemporaryDirectory() as tmpdir:
        db = SessionDB(db_path=Path(tmpdir) / "state.db")
        db.create_session("root", "cli")
        db.end_session("root", "compression")
        db.create_session("child1", "cli", parent_session_id="root")
        db.end_session("child1", "compression")
        db.create_session("child2", "cli", parent_session_id="child1")
        db.update_token_counts(
            "child2",
            input_tokens=175_000,
            api_call_count=4,
            absolute=True,
        )

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=db,
                session_id="child2",
                skip_context_files=True,
                skip_memory=True,
            )
        agent._build_system_prompt = lambda _system_message: "system"
        agent.commit_memory_session = lambda _messages: None
        agent.context_compressor.compress = lambda _messages, **_kwargs: [
            {"role": "user", "content": "compressed summary"}
        ]

        messages = [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "x" * 400_000},
            {"role": "user", "content": "continue"},
        ]

        with patch(
            "agent.conversation_compression.estimate_request_tokens_rough",
            return_value=171_000,
        ):
            try:
                compress_context(agent, messages, "system", approx_tokens=175_000)
            except CompressionChurnError as exc:
                details = exc.details
            else:
                raise AssertionError("expected CompressionChurnError")

        assert agent.session_id == "child2"
        assert details["code"] == "compression_loop_churn"
        assert "empty_child_similar_tokens" in details["reasons"]
        assert details["lineage_root_session_id"] == "root"
        assert details["current_session_id"] == "child2"
        assert details["original_tokens"] == 175_000
        assert details["compressed_tokens"] == 171_000
        assert details["largest_message_candidate"]["index"] == 1
        assert details["largest_message_candidate"]["chars"] == 400_000
        assert details["lineage"][0]["api_call_count"] == 4


def test_compress_context_ignores_recovered_large_zero_message_ancestor():
    from agent.conversation_compression import compress_context
    from hermes_state import SessionDB
    from run_agent import AIAgent

    with tempfile.TemporaryDirectory() as tmpdir:
        db = SessionDB(db_path=Path(tmpdir) / "state.db")
        db.create_session("root", "cli")
        db.end_session("root", "compression")

        db.create_session("empty_child", "cli", parent_session_id="root")
        db.update_token_counts("empty_child", input_tokens=175_000, absolute=True)
        db.end_session("empty_child", "compression")

        db.create_session("current", "cli", parent_session_id="empty_child")
        for idx in range(4):
            db.append_message("current", "user", f"durable progress {idx}")
        db.update_token_counts("current", input_tokens=85_000, absolute=True)

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=db,
                session_id="current",
                skip_context_files=True,
                skip_memory=True,
            )
        agent._build_system_prompt = lambda _system_message: "system"
        agent.commit_memory_session = lambda _messages: None
        agent.context_compressor.compress = lambda _messages, **_kwargs: [
            {"role": "user", "content": "compressed summary"}
        ]

        messages = [
            {"role": "user", "content": "meaningful work"},
            {"role": "assistant", "content": "progress"},
        ]

        with patch(
            "agent.conversation_compression.estimate_request_tokens_rough",
            return_value=30_000,
        ):
            compressed, new_system_prompt = compress_context(
                agent,
                messages,
                "system",
                approx_tokens=85_000,
            )

        assert compressed == [{"role": "user", "content": "compressed summary"}]
        assert new_system_prompt == "system"
        assert agent.session_id != "current"


def test_compress_context_allows_meaningful_progress_lineage():
    from agent.conversation_compression import compress_context
    from hermes_state import SessionDB
    from run_agent import AIAgent

    with tempfile.TemporaryDirectory() as tmpdir:
        db = SessionDB(db_path=Path(tmpdir) / "state.db")
        db.create_session("root", "cli")
        for idx in range(4):
            db.append_message("root", "user", f"root progress {idx}")
        db.update_token_counts("root", input_tokens=190_000, absolute=True)
        db.end_session("root", "compression")

        db.create_session("child1", "cli", parent_session_id="root")
        for idx in range(4):
            db.append_message("child1", "assistant", f"child1 progress {idx}")
        db.update_token_counts("child1", input_tokens=120_000, absolute=True)
        db.end_session("child1", "compression")

        db.create_session("child2", "cli", parent_session_id="child1")
        for idx in range(4):
            db.append_message("child2", "user", f"child2 progress {idx}")
        db.update_token_counts("child2", input_tokens=80_000, absolute=True)

        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                quiet_mode=True,
                session_db=db,
                session_id="child2",
                skip_context_files=True,
                skip_memory=True,
            )
        agent._build_system_prompt = lambda _system_message: "system"
        agent.commit_memory_session = lambda _messages: None
        agent.context_compressor.compress = lambda _messages, **_kwargs: [
            {"role": "user", "content": "compressed summary"}
        ]

        messages = [
            {"role": "user", "content": "meaningful work"},
            {"role": "assistant", "content": "progress"},
        ]

        with patch(
            "agent.conversation_compression.estimate_request_tokens_rough",
            return_value=30_000,
        ):
            compressed, new_system_prompt = compress_context(
                agent,
                messages,
                "system",
                approx_tokens=80_000,
            )

        assert compressed == [{"role": "user", "content": "compressed summary"}]
        assert new_system_prompt == "system"
        assert agent.session_id != "child2"
        lineage = db.get_recent_compression_lineage(agent.session_id)
        assert [row["id"] for row in lineage[:3]] == [agent.session_id, "child2", "child1"]


def test_compression_churn_result_is_structured_failure(tmp_path):
    from agent.conversation_compression import CompressionChurnError
    from agent.conversation_loop import _compression_churn_result
    from hermes_state import SessionDB
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "state.db")
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id="current",
            skip_context_files=True,
            skip_memory=True,
        )
    agent._flush_status_buffer = lambda: None
    agent._vprint = lambda *args, **kwargs: None
    persisted = {}
    agent._persist_session = lambda messages, history: persisted.update(
        {"messages": messages, "history": history}
    )
    messages = [{"role": "user", "content": "hello"}]
    details = {
        "code": "compression_loop_churn",
        "largest_message_candidate": {"index": 0, "role": "user", "chars": 5},
    }

    result = _compression_churn_result(
        agent,
        messages,
        conversation_history=[],
        api_call_count=2,
        exc=CompressionChurnError(details),
    )

    assert result["completed"] is False
    assert result["failed"] is True
    assert result["compression_exhausted"] is True
    assert result["compression_loop"] == details
    assert persisted["messages"] == messages
