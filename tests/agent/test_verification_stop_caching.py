"""Verification-loop synthetic scaffolding must never reach durable session state.

verify_on_stop / pre_verify inject a synthetic user nudge to keep the agent
going one more turn before it can claim completion. The assistant candidate and
the user nudge are both private continuation scaffolding: they remain in model
history for role alternation and prompt-cache continuity, but neither may reach
SQLite or JSON before verification succeeds. This test file verifies:

  - The verification-loop flags remain registered in
    ``_EPHEMERAL_SCAFFOLDING_FLAGS``.
  - The DB flush drops both the candidate and its nudge.
  - The JSON log drops both the candidate and its nudge.
"""

import json
import sys
from unittest.mock import MagicMock

import pytest


def _fresh_run_agent(hermes_home):
    for mod in list(sys.modules):
        if mod == "run_agent" or mod.startswith("agent.") or mod.startswith("tools.") or mod.startswith("hermes_"):
            del sys.modules[mod]
    import run_agent  # noqa: F401
    return sys.modules["run_agent"]


def test_verification_flags_registered_as_ephemeral(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    ra = _fresh_run_agent(tmp_path)

    assert "_verification_stop_synthetic" in ra._EPHEMERAL_SCAFFOLDING_FLAGS
    assert "_pre_verify_synthetic" in ra._EPHEMERAL_SCAFFOLDING_FLAGS

    # Both assistant candidates and user nudges are scaffolding when flagged.
    assert ra._is_ephemeral_scaffolding(
        {"role": "assistant", "content": "done", "_verification_stop_synthetic": True}
    )
    assert ra._is_ephemeral_scaffolding(
        {"role": "user", "content": "[System: run tests]", "_pre_verify_synthetic": True}
    )
    assert ra._is_ephemeral_scaffolding(
        {"role": "user", "content": "[System: run tests]", "_verification_stop_synthetic": True}
    )
    # Real, unflagged messages are not scaffolding.
    assert not ra._is_ephemeral_scaffolding({"role": "user", "content": "hi"})
    assert not ra._is_ephemeral_scaffolding({"role": "assistant", "content": "premature done"})


def _make_agent(ra, session_id, tmp_path):
    agent = ra.AIAgent(
        session_id=session_id,
        api_key="test-key",
        base_url="https://example.invalid/v1",
        provider="openai-compat",
        model="test-model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._session_db = MagicMock()
    agent._session_db_created = True
    agent._session_json_enabled = True
    agent.logs_dir = tmp_path / "logs"
    agent.logs_dir.mkdir(parents=True, exist_ok=True)
    return agent


def test_db_flush_drops_verification_candidate_and_nudge(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    ra = _fresh_run_agent(tmp_path)
    agent = _make_agent(ra, "sess_db", tmp_path)

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "premature done", "_verification_stop_synthetic": True},
        {"role": "user", "content": "[System: run tests]", "_verification_stop_synthetic": True},
        {"role": "assistant", "content": "verified and clean"},
    ]

    agent._flush_messages_to_session_db(messages, conversation_history=[])

    persisted = [
        msg.get("content")
        for _args, kwargs in agent._session_db.append_messages_batch.call_args_list
        for msg in kwargs["messages"]
    ]
    assert "hi" in persisted
    assert "verified and clean" in persisted
    assert "premature done" not in persisted
    assert "[System: run tests]" not in persisted


def test_json_log_drops_verification_candidate_and_nudge(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    ra = _fresh_run_agent(tmp_path)
    agent = _make_agent(ra, "sess_json", tmp_path)

    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "premature done", "_pre_verify_synthetic": True},
        {"role": "user", "content": "[System: run tests]", "_pre_verify_synthetic": True},
        {"role": "assistant", "content": "verified and clean"},
    ]

    agent._save_session_log(messages)

    log_file = agent.logs_dir / "session_sess_json.json"
    assert log_file.exists()
    data = json.loads(log_file.read_text(encoding="utf-8"))
    contents = [m.get("content") for m in data["messages"]]
    assert contents == ["hi", "verified and clean"]
    assert all(not m.get("_pre_verify_synthetic") for m in data["messages"])
