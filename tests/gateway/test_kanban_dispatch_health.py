from gateway.run import _kanban_dispatch_health_candidate
from hermes_cli import kanban_db as kb


class _FakeDiscordWorkerBoards:
    def __init__(self, *, discord=True, executable=True, paused=False, fail=False):
        self.discord = discord
        self.executable = executable
        self.paused = paused
        self.fail = fail

    def is_discord_worker_board(self, board):
        if self.fail:
            raise RuntimeError("metadata unavailable")
        return self.discord

    def is_executable_worker_board(self, board):
        return self.executable

    def is_paused_or_cancelled(self, board):
        return self.paused


def test_kanban_dispatch_health_candidate_keeps_non_discord_boards():
    fake = _FakeDiscordWorkerBoards(discord=False, executable=False, paused=True)

    assert _kanban_dispatch_health_candidate("default", fake) is True


def test_kanban_dispatch_health_candidate_keeps_active_discord_boards():
    fake = _FakeDiscordWorkerBoards(discord=True, executable=True, paused=False)

    assert _kanban_dispatch_health_candidate("discord-1", fake) is True


def test_kanban_dispatch_health_candidate_skips_paused_discord_boards():
    fake = _FakeDiscordWorkerBoards(discord=True, executable=True, paused=True)

    assert _kanban_dispatch_health_candidate("discord-1", fake) is False


def test_kanban_dispatch_health_candidate_skips_inactive_discord_boards():
    fake = _FakeDiscordWorkerBoards(discord=True, executable=False, paused=False)

    assert _kanban_dispatch_health_candidate("discord-1", fake) is False


def test_kanban_dispatch_health_candidate_fails_open():
    fake = _FakeDiscordWorkerBoards(fail=True)

    assert _kanban_dispatch_health_candidate("discord-1", fake) is True


def test_corrupt_board_quarantine_state_skips_health_open(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    board = "gateway-corrupt"
    db_path = kb.kanban_db_path(board)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"not sqlite")
    monkeypatch.setattr(kb.time, "time", lambda: 5000)
    kb.record_corrupt_board_incident(
        board,
        db_path,
        "sqlite refused to open file: file is not a database",
        fingerprint=kb._db_content_fingerprint(db_path),
    )

    state = kb.corrupt_board_quarantine_state(board, now=5001)

    assert state["skipped"] is True
    assert state["open_allowed"] is False
    assert state["next_retry"] == 5000 + kb.CORRUPT_BOARD_RETRY_SECONDS


def test_board_schema_ready_records_missing_tasks_once_per_window(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(kb.time, "time", lambda: 7000)
    board = "gateway-schema-missing-tasks"
    db_path = kb.kanban_db_path(board)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    conn = kb._sqlite_connect(db_path)
    try:
        conn.execute("CREATE TABLE kanban_notify_subs (task_id TEXT)")
        conn.commit()
        ready, incident = kb.board_schema_ready(
            conn,
            board=board,
            operation="dispatcher",
            required_tables=("tasks",),
        )
    finally:
        conn.close()

    assert ready is False
    assert incident is not None
    assert "missing required table(s): tasks" in incident["reason"]
    state = kb.corrupt_board_quarantine_state(board, now=7001)
    assert state["skipped"] is True
    assert state["open_allowed"] is False


def test_board_schema_ready_keeps_healthy_board_available(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    board = "gateway-schema-ready"
    conn = kb.connect(board=board)
    try:
        ready, incident = kb.board_schema_ready(
            conn,
            board=board,
            operation="dispatcher",
            required_tables=("tasks",),
        )
    finally:
        conn.close()

    assert ready is True
    assert incident is None


def test_discord_worker_dispatch_skips_board_missing_tasks(tmp_path, monkeypatch, caplog):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(kb.time, "time", lambda: 9000)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import discord_worker_dispatch as dwd

    board = "discord-missing-tasks"
    db_path = kb.kanban_db_path(board)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)
    conn = kb._sqlite_connect(db_path)
    try:
        conn.execute("CREATE TABLE kanban_notify_subs (task_id TEXT)")
        conn.commit()
    finally:
        conn.close()
    kb._write_board_metadata_raw(board, {"slug": board, "name": "Missing Tasks"})
    kb._INITIALIZED_PATHS.add(str(db_path.resolve()))
    monkeypatch.setattr(dwb, "is_discord_worker_board", lambda slug: slug == board)
    monkeypatch.setattr(dwb, "reconcile_board", lambda slug: None)
    monkeypatch.setattr(dwb, "ensure_code_island_for_board", lambda slug: True)
    monkeypatch.setattr(dwb, "is_executable_worker_board", lambda slug: True)
    monkeypatch.setattr(dwb, "is_paused_or_cancelled", lambda slug: False)

    with caplog.at_level("ERROR"):
        result = dwd.dispatch_discord_worker_boards([board], spawn_fn=lambda *args: None)
        result += dwd.dispatch_discord_worker_boards([board], spawn_fn=lambda *args: None)

    assert result == [(board, None), (board, None)]
    messages = [record.getMessage() for record in caplog.records]
    schema_events = [msg for msg in messages if "schema readiness failed for discord_dispatcher" in msg]
    assert len(schema_events) == 1
    assert "missing required table(s): tasks" in schema_events[0]
    assert kb.corrupt_board_quarantine_state(board, now=9001)["skipped"] is True
