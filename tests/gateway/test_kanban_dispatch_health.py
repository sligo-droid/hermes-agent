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
