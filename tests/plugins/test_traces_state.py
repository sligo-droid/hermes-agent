import json
import os
import stat
import threading

from plugins.traces.hermes_traces_plugin.state import State


def test_state_creates_stable_random_slug_and_private_files(tmp_path):
    state = State(tmp_path / "index.json")

    first = state.create("one")
    second = state.create("one")
    other = state.create("two")

    assert first["slug"] == second["slug"]
    assert first["slug"] != other["slug"]
    assert len(first["slug"]) >= 22
    assert stat.S_IMODE(os.stat(state.path.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(state.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(state.lock_path).st_mode) == 0o600
    stored = json.loads(state.path.read_text(encoding="utf-8"))
    assert stored["sessions"][first["key"]]["status"] == "pending"


def test_update_preserves_last_good_url(tmp_path):
    state = State(tmp_path / "index.json")
    record = state.create("one")
    state.update(
        record["key"],
        status="ready",
        shared_url="https://traces.com/a",
    )

    state.update(
        record["key"],
        status="error",
        error="timeout",
        shared_url=None,
    )

    saved = state.get_key(record["key"])
    assert saved["shared_url"] == "https://traces.com/a"
    assert saved["status"] == "error"


def test_version_and_corrupt_state_are_tolerated(tmp_path):
    state = State(tmp_path / "index.json")
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text("not json", encoding="utf-8")

    assert state.get("x") is None
    record = state.create("x")

    saved = json.loads(state.path.read_text(encoding="utf-8"))
    assert saved["version"] == 1
    assert record["session_id"] == "x"


def test_concurrent_updates_do_not_drop_records(tmp_path):
    state = State(tmp_path / "index.json")
    threads = [
        threading.Thread(target=state.create, args=(f"session-{index}",))
        for index in range(20)
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    saved = json.loads(state.path.read_text(encoding="utf-8"))
    assert len(saved["sessions"]) == 20


def test_invalid_session_or_platform_is_rejected(tmp_path):
    state = State(tmp_path / "index.json")

    for session_id, platform in (
        ("", "discord"),
        ("id", ""),
        ("x" * 513, "discord"),
        ("id", "x" * 65),
    ):
        try:
            state.create(session_id, platform)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid state key was accepted")
