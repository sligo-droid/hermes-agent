import json
import sqlite3

from plugins.traces.hermes_traces_plugin.hermes_store import HermesStore


def _main_db(home):
    connection = sqlite3.connect(home / "state.db")
    connection.execute(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            parent_session_id TEXT,
            started_at REAL NOT NULL
        )
        """
    )
    connection.executemany(
        "INSERT INTO sessions(id, parent_session_id, started_at) VALUES (?, ?, 1)",
        [("root", None), ("child", "root"), ("grandchild", "child")],
    )
    connection.commit()
    connection.close()


def test_resolve_root_walks_native_session_lineage(tmp_path):
    _main_db(tmp_path)
    store = HermesStore(tmp_path, tmp_path / "observer")

    assert store.resolve_root("grandchild") == "root"
    assert store.resolve_root("missing", parent_session_id="parent-hint") == "parent-hint"
    assert store.resolve_root("grandchild", root_session_id="explicit") == "explicit"


def test_observer_store_replaces_start_with_bounded_stop_trace(tmp_path):
    _main_db(tmp_path)
    observer_home = tmp_path / "observer"
    store = HermesStore(tmp_path, observer_home)
    start = {
        "worker_session_id": "coding-1",
        "root_session_id": "root",
        "parent_session_id": "child",
        "backend": "codex",
        "model": "gpt-test",
        "task": "fix parser",
        "cwd": "/workspace",
        "started_at": 10.0,
    }

    assert store.write_coding_worker(start) == "coding-1"
    assert store.write_coding_worker(
        {
            **start,
            "status": "completed",
            "ended_at": 12.0,
            "duration_ms": 2000,
            "summary": "done",
            "worker_messages": [
                {"role": "assistant", "content": "x" * 100_001},
                *[
                    {"role": "assistant", "content": f"message-{index}"}
                    for index in range(500)
                ],
            ],
        }
    ) == "coding-1"

    assert not sqlite3.connect(tmp_path / "state.db").execute(
        "SELECT 1 FROM sessions WHERE id = 'coding-1'"
    ).fetchone()
    connection = sqlite3.connect(observer_home / "state.db")
    session = connection.execute(
        "SELECT source, parent_session_id, ended_at, end_reason, message_count "
        "FROM sessions WHERE id = 'coding-1'"
    ).fetchone()
    messages = connection.execute(
        "SELECT content, observed FROM messages WHERE session_id = 'coding-1' ORDER BY id"
    ).fetchall()
    connection.close()

    assert session[:4] == ("tool", "child", 12.0, "completed")
    assert session[4] == len(messages)
    assert len(messages) <= 403
    assert all(observed == 1 for _content, observed in messages)
    assert sum("Coding worker started" in (content or "") for content, _ in messages) == 1
    assert any("Coding worker completed" in (content or "") for content, _ in messages)
    assert max(len(content or "") for content, _ in messages) <= 100_000


def test_tool_result_name_is_inferred_from_preceding_tool_call(tmp_path):
    _main_db(tmp_path)
    observer_home = tmp_path / "observer"
    store = HermesStore(tmp_path, observer_home)
    store.write_coding_worker(
        {
            "worker_session_id": "coding-tools",
            "root_session_id": "root",
            "parent_session_id": "child",
            "backend": "codex",
            "task": "run tests",
            "started_at": 10.0,
            "ended_at": 11.0,
            "status": "completed",
            "worker_messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "terminal",
                                "arguments": '{"cmd":"pytest"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "content": "passed",
                },
            ],
        }
    )

    connection = sqlite3.connect(observer_home / "state.db")
    result = connection.execute(
        "SELECT tool_name FROM messages WHERE session_id = ? AND role = 'tool'",
        ("coding-tools",),
    ).fetchone()
    connection.close()

    assert result == ("terminal",)


def test_opencode_tool_update_becomes_tool_call_and_result_messages(tmp_path):
    _main_db(tmp_path)
    observer_home = tmp_path / "observer"
    store = HermesStore(tmp_path, observer_home)
    store.write_coding_worker(
        {
            "worker_session_id": "coding-opencode",
            "root_session_id": "root",
            "parent_session_id": "child",
            "backend": "opencode",
            "task": "run tests",
            "started_at": 10.0,
            "ended_at": 11.0,
            "status": "completed",
            "worker_messages": [],
            "worker_events": [
                {
                    "type": "part.updated",
                    "part": {
                        "type": "tool",
                        "callID": "call-shell",
                        "tool": "terminal",
                        "state": {
                            "status": "completed",
                            "input": {"cmd": "pytest"},
                            "output": "passed",
                        },
                    },
                }
            ],
        }
    )

    connection = sqlite3.connect(observer_home / "state.db")
    rows = connection.execute(
        "SELECT role, content, tool_call_id, tool_calls, tool_name "
        "FROM messages WHERE session_id = ? ORDER BY id",
        ("coding-opencode",),
    ).fetchall()
    connection.close()

    assistant_call = next(row for row in rows if row[3])
    tool_result = next(row for row in rows if row[0] == "tool")
    assert json.loads(assistant_call[3])[0]["function"]["name"] == "terminal"
    assert tool_result[:3] == ("tool", "passed", "call-shell")
    assert tool_result[4] == "terminal"
    assert not any("[coding worker part.updated]" in (row[1] or "") for row in rows)
