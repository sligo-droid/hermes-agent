"""Tests for SessionStore.rewind_session — the gateway /undo [N] primitive.

The gateway /undo backs up N user turns by soft-deleting the truncated rows
in state.db (active=0, kept for audit, hidden from re-prompts/search) via
SessionDB.rewind_to_message, rather than the old hard rewrite_transcript.
load_transcript returns only the active view. See issue #21910.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource, SessionStore, build_session_key
from hermes_state import SessionDB


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = SessionDB(db_path=tmp_path / "state.db")
    s = SessionStore(sessions_dir=tmp_path / "sessions", config=GatewayConfig())
    s._db = db  # use the same DB instance the fixture seeds
    return s


def _seed(store, sid, source="telegram", turns=3):
    store._db.create_session(sid, source=source)
    for i in range(1, turns + 1):
        store._db.append_message(sid, "user", f"q{i}")
        store._db.append_message(sid, "assistant", f"a{i}")
    return sid


def test_rewind_default_one_turn(store):
    sid = _seed(store, "gw-1")
    res = store.rewind_session(sid)
    assert res["turns_undone"] == 1
    assert res["target_text"] == "q3"
    assert res["rewound_count"] == 2  # q3 + a3
    active = store.load_transcript(sid)
    assert [m["role"] for m in active] == ["user", "assistant", "user", "assistant"]


def test_rewind_n_turns(store):
    sid = _seed(store, "gw-2")
    res = store.rewind_session(sid, 2)
    assert res["turns_undone"] == 2
    assert res["target_text"] == "q2"
    assert res["rewound_count"] == 4  # q2,a2,q3,a3
    assert len(store.load_transcript(sid)) == 2  # q1,a1


def test_rewind_soft_deletes_rows_for_audit(store):
    sid = _seed(store, "gw-3")
    store.rewind_session(sid, 1)
    all_rows = store._db.get_messages(sid, include_inactive=True)
    assert len(all_rows) == 6  # nothing hard-deleted
    assert sum(1 for r in all_rows if r["active"] == 1) == 4
    assert store._db.get_session(sid)["rewind_count"] == 1


def test_rewind_clamps_to_oldest_turn(store):
    sid = _seed(store, "gw-4", turns=2)
    res = store.rewind_session(sid, 99)
    assert res["target_text"] == "q1"
    assert len(store.load_transcript(sid)) == 0


def test_rewind_empty_session_returns_none(store):
    store._db.create_session("gw-5", source="discord")
    assert store.rewind_session("gw-5") is None


def test_rewind_clamps_negative_count_to_one(store):
    sid = _seed(store, "gw-6")
    res = store.rewind_session(sid, -5)
    assert res["turns_undone"] == 1
    assert res["target_text"] == "q3"


@pytest.mark.parametrize(
    "handler_name",
    [
        "_handle_status_command",
        "_handle_resume_command",
        "_handle_reset_command",
        "_handle_stop_command",
        "_handle_undo_command",
        "_handle_model_command",
        "_handle_compress_command",
        "_handle_deny_command",
        "_handle_title_command",
    ],
)
def test_gateway_runner_uses_authoritative_slash_handler(handler_name):
    from gateway.run import GatewayRunner
    from gateway.slash_commands import GatewaySlashCommandsMixin

    assert getattr(GatewayRunner, handler_name) is getattr(
        GatewaySlashCommandsMixin,
        handler_name,
    )


@pytest.mark.asyncio
async def test_gateway_undo_forwards_count_and_evicts_cached_agent():
    from gateway.run import GatewayRunner

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
    )
    event = MessageEvent(
        text="/undo 2",
        message_type=MessageType.TEXT,
        source=source,
    )
    entry = SimpleNamespace(session_id="session-1", last_prompt_tokens=987)
    runner = object.__new__(GatewayRunner)
    runner.session_store = Mock()
    runner._async_session_store = SimpleNamespace(
        _store=runner.session_store,
        get_or_create_session=AsyncMock(return_value=entry),
        rewind_session=AsyncMock(
            return_value={
                "turns_undone": 2,
                "rewound_count": 4,
                "target_text": "second prompt",
            }
        ),
    )
    runner._evict_cached_agent = Mock()

    result = await runner._handle_undo_command(event)

    runner.async_session_store.rewind_session.assert_awaited_once_with(
        "session-1",
        2,
    )
    runner._evict_cached_agent.assert_called_once_with(build_session_key(source))
    assert entry.last_prompt_tokens == 0
    assert "second prompt" in result
