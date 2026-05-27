from gateway.run import _kanban_dispatch_health_candidate


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
