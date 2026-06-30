import sqlite3
import time

from hermes_cli import compression_diagnostics as diag


def _make_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            source TEXT NOT NULL,
            user_id TEXT,
            model TEXT,
            model_config TEXT,
            system_prompt TEXT,
            parent_session_id TEXT,
            started_at REAL NOT NULL,
            ended_at REAL,
            end_reason TEXT,
            message_count INTEGER DEFAULT 0,
            tool_call_count INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cache_read_tokens INTEGER DEFAULT 0,
            cache_write_tokens INTEGER DEFAULT 0,
            reasoning_tokens INTEGER DEFAULT 0,
            billing_provider TEXT,
            billing_base_url TEXT,
            billing_mode TEXT,
            estimated_cost_usd REAL,
            actual_cost_usd REAL,
            cost_status TEXT,
            cost_source TEXT,
            pricing_version TEXT,
            title TEXT,
            api_call_count INTEGER DEFAULT 0,
            handoff_state TEXT,
            handoff_platform TEXT,
            handoff_error TEXT
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
    return conn


def _insert_session(conn, sid, *, started, ended=None, end_reason=None, parent=None, count=1):
    conn.execute(
        """
        INSERT INTO sessions (
            id, source, model, model_config, parent_session_id, started_at,
            ended_at, end_reason, message_count, input_tokens, output_tokens,
            billing_provider
        ) VALUES (?, 'discord', 'test-model', '{"context_length": 128000}', ?, ?, ?, ?, ?, 100, 20, 'openrouter')
        """,
        (sid, parent, started, ended, end_reason, count),
    )


def test_parse_log_lines_classifies_required_log_events():
    lines = [
        "2026-06-30 10:00:00 INFO gateway.run: Skipping transcript persistence for context-overflow failure in session s_skip to prevent session growth loop.",
        "2026-06-30 10:00:01 INFO gateway.run: Auto-resetting session s_reset after compression exhaustion.",
        "2026-06-30 10:00:02 WARNING [s_fb] agent.context_compressor: Summary generation failed - inserting deterministic fallback context summary",
        "2026-06-30 10:00:03 INFO [s_ok] agent.context_compressor: Compression #1 complete",
    ]

    events = diag.parse_log_lines(lines, since_ts=0)
    by_session = {event.session_id: event.kind for event in events}

    assert by_session == {
        "s_skip": "context_skip",
        "s_reset": "auto_reset",
        "s_fb": "compression_fallback",
        "s_ok": "compression_success",
    }


def test_aggregate_classifies_state_and_logs_without_message_content(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, "load_config", lambda: {"compression": {}})
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    now = time.time()
    conn = _make_db(db_path)
    _insert_session(conn, "s_parent", started=now - 10, ended=now - 5, end_reason="compression")
    _insert_session(conn, "s_child", started=now - 4, parent="s_parent", count=0)
    _insert_session(conn, "s_private", started=now - 3, ended=now - 2, end_reason="compression")
    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, 'assistant', ?, ?)",
        ("s_private", "SECRET TRANSCRIPT BODY tool output private content", now - 2),
    )
    conn.commit()
    conn.close()
    (log_dir / "gateway.log").write_text(
        "\n".join(
                [
                "INFO gateway.run: Skipping transcript persistence for context-overflow failure in session s_skip to prevent session growth loop.",
                "INFO gateway.run: Auto-resetting session s_reset after compression exhaustion.",
                "WARNING [s_fb] agent.context_compressor: Summary generation failed - inserting deterministic fallback context summary SECRET LOG CONTENT",
                ]
            ),
        encoding="utf-8",
    )

    rows = diag.aggregate(db_path, log_dir, since_ts=now - 60, limit=10)
    classes = {row.session_id: diag.classify(row) for row in rows}
    report = diag.format_report(rows, db_path=db_path, log_dir=log_dir, since_ts=now - 60)

    assert classes["s_parent"] == diag.CLASS_COMPRESSION_SUCCESS
    assert classes["s_child"] == diag.CLASS_ZERO_BOUNDARY
    assert classes["s_skip"] == diag.CLASS_CONTEXT_SKIP
    assert classes["s_reset"] == diag.CLASS_AUTO_RESET
    assert classes["s_fb"] == diag.CLASS_COMPRESSION_FALLBACK
    assert "SECRET TRANSCRIPT BODY" not in report
    assert "SECRET LOG CONTENT" not in report


def test_aggregate_applies_limit_and_missing_inputs(tmp_path, monkeypatch):
    monkeypatch.setattr(diag, "load_config", lambda: {"compression": {}})
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    now = time.time()
    conn = _make_db(db_path)
    for idx in range(3):
        _insert_session(conn, f"s{idx}", started=now - idx, ended=now - idx, end_reason="compression")
    conn.commit()
    conn.close()

    assert len(diag.aggregate(db_path, log_dir, since_ts=now - 60, limit=2)) == 2
    assert diag.aggregate(tmp_path / "missing.db", tmp_path / "missing-logs", since_ts=0, limit=5) == []


def test_cli_command_with_fixture_paths(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(diag, "load_config", lambda: {"compression": {"threshold": 0.7, "target_ratio": 0.35, "protect_last_n": 50}})
    db_path = tmp_path / "state.db"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    now = time.time()
    conn = _make_db(db_path)
    _insert_session(conn, "s_ok", started=now - 1, ended=now, end_reason="compression")
    conn.commit()
    conn.close()

    rc = diag.run(
        type(
            "Args",
            (),
            {"since": "24h", "limit": 5, "db_path": str(db_path), "log_dir": str(log_dir)},
        )()
    )

    output = capsys.readouterr().out
    assert rc == 0
    assert "Hermes compression diagnostics" in output
    assert "s_ok" in output
    assert diag.CLASS_COMPRESSION_SUCCESS in output
