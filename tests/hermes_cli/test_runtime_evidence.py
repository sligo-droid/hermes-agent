from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hermes_cli import runtime_evidence as rt


def _init_state_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                user_id TEXT,
                model TEXT,
                parent_session_id TEXT,
                started_at REAL NOT NULL,
                ended_at REAL,
                end_reason TEXT,
                message_count INTEGER DEFAULT 0,
                tool_call_count INTEGER DEFAULT 0,
                api_call_count INTEGER DEFAULT 0,
                handoff_platform TEXT,
                handoff_error TEXT,
                title TEXT
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                timestamp REAL NOT NULL
            );
            """
        )
        conn.execute(
            "INSERT INTO sessions "
            "(id, source, user_id, model, started_at, message_count, tool_call_count, api_call_count, title) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("sess-1", "discord", "user-1", "test-model", 100.0, 2, 1, 3, "diag"),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ("sess-1", "user", "hello", 101.0),
        )
        conn.execute(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            ("sess-1", "assistant", "hi", 109.0),
        )
        conn.commit()
    finally:
        conn.close()


def _init_kanban_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                assignee TEXT,
                status TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                started_at INTEGER,
                completed_at INTEGER,
                workspace_kind TEXT,
                workspace_path TEXT,
                branch_name TEXT,
                consecutive_failures INTEGER DEFAULT 0,
                last_failure_error TEXT,
                current_run_id INTEGER,
                session_id TEXT
            );
            CREATE TABLE task_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                profile TEXT,
                status TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                ended_at INTEGER,
                outcome TEXT,
                summary TEXT,
                metadata TEXT,
                error TEXT
            );
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                run_id INTEGER,
                kind TEXT NOT NULL,
                payload TEXT,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE kanban_notify_subs (
                task_id TEXT NOT NULL,
                platform TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                thread_id TEXT NOT NULL DEFAULT '',
                user_id TEXT,
                created_at INTEGER NOT NULL,
                last_event_id INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.execute(
            "INSERT INTO tasks "
            "(id, title, assignee, status, created_at, started_at, workspace_kind, "
            "workspace_path, branch_name, consecutive_failures, last_failure_error, current_run_id, session_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "t_1234",
                "diagnose runtime",
                "worker",
                "blocked",
                1000,
                1010,
                "worktree",
                "/tmp/worktree",
                "fix/runtime-evidence",
                2,
                "spawn_failed: bad profile",
                7,
                "sess-1",
            ),
        )
        conn.execute(
            "INSERT INTO task_runs "
            "(id, task_id, profile, status, started_at, ended_at, outcome, summary, metadata, error) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                7,
                "t_1234",
                "worker",
                "blocked",
                1010,
                1070,
                "spawn_failed",
                "could not start worker",
                json.dumps({"pr_url": "https://github.example/pr/1", "token": "should-not-match-key-filter"}),
                "api_key=super-secret failed",
            ),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, ?, ?, ?)",
            ("t_1234", 7, "spawn_failed", json.dumps({"error": "api_key=super-secret"}), 1071),
        )
        conn.execute(
            "INSERT INTO kanban_notify_subs (task_id, platform, chat_id, thread_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("t_1234", "discord", "chat-1", "thread-1", 1000),
        )
        conn.commit()
    finally:
        conn.close()


def test_collect_runtime_evidence_reads_state_kanban_and_sanitized_logs(tmp_path: Path) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()
    _init_state_db(home / "state.db")
    _init_kanban_db(home / "kanban.db")
    board_dir = home / "kanban" / "boards" / "default"
    board_dir.mkdir(parents=True)
    (board_dir / "board.json").write_text(
        json.dumps({"github_repo": "owner/repo", "branch": "main", "description": "ignored"}),
        encoding="utf-8",
    )
    logs = home / "logs"
    logs.mkdir()
    (logs / "gateway.log").write_text(
        "\n".join(
            [
                "2026-05-28 10:00:00 INFO unrelated line",
                "2026-05-28 10:00:01 INFO response ready: platform=discord chat=thread-1 time=12.5s api_calls=4 response=321 chars",
                "2026-05-28 10:00:02 ERROR thread-1 blocked api_key=abc123",
            ]
        ),
        encoding="utf-8",
    )

    bundle = rt.collect_runtime_evidence(
        rt.RuntimeEvidenceRequest(thread_id="thread-1", session_id="sess-1", hermes_home=home)
    )

    assert bundle["session"]["found"] is True
    assert bundle["session"]["session"]["api_call_count"] == "3"
    assert bundle["session"]["session"]["message_window"]["messages"] == 2
    assert bundle["kanban"]["found"] is True
    assert bundle["kanban"]["board_metadata"] == {"branch": "main", "github_repo": "owner/repo"}
    assert bundle["kanban"]["tasks"][0]["branch_name"] == "fix/runtime-evidence"
    assert bundle["kanban"]["runs"][0]["duration_seconds"] == 60
    assert bundle["kanban"]["runs"][0]["metadata"] == {"pr_url": "https://github.example/pr/1"}
    assert "super-secret" not in json.dumps(bundle)
    assert bundle["logs"]["response_ready"][0]["api_calls"] == 4
    assert "[REDACTED]" in bundle["logs"]["crash_or_block"][0]["line"]


def test_main_json_output_is_parseable(tmp_path: Path, capsys) -> None:
    home = tmp_path / ".hermes"
    home.mkdir()

    rc = rt.main(["--home", str(home), "--thread-id", "thread-1", "--json"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["inputs"]["thread_id"] == "thread-1"
    assert out["session"]["found"] is False
