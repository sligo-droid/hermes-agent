"""Tests for CLI background process notification mode filtering."""

import cli as cli_mod


class _FakeRegistry:
    def __init__(self, consumed=()):
        self._consumed = set(consumed)

    def is_completion_consumed(self, session_id):
        return session_id in self._consumed


def _set_mode(monkeypatch, mode):
    monkeypatch.delenv("HERMES_BACKGROUND_NOTIFICATIONS", raising=False)
    display = cli_mod.CLI_CONFIG.setdefault("display", {})
    monkeypatch.setitem(display, "background_process_notifications", mode)


def test_cli_notification_mode_all_enqueues_completion_and_watch(monkeypatch):
    _set_mode(monkeypatch, "all")

    assert cli_mod._should_enqueue_process_notification(
        {"type": "completion", "session_id": "proc", "exit_code": 0},
        _FakeRegistry(),
    ) is True
    assert cli_mod._should_enqueue_process_notification(
        {"type": "watch_match", "session_id": "proc"},
        _FakeRegistry(),
    ) is True


def test_cli_notification_mode_result_suppresses_watch(monkeypatch):
    _set_mode(monkeypatch, "result")

    assert cli_mod._should_enqueue_process_notification(
        {"type": "completion", "session_id": "proc", "exit_code": 0},
        _FakeRegistry(),
    ) is True
    assert cli_mod._should_enqueue_process_notification(
        {"type": "watch_disabled", "session_id": "proc"},
        _FakeRegistry(),
    ) is False


def test_cli_notification_mode_error_only_enqueues_failed_completion(monkeypatch):
    _set_mode(monkeypatch, "error")

    assert cli_mod._should_enqueue_process_notification(
        {"type": "completion", "session_id": "ok", "exit_code": 0},
        _FakeRegistry(),
    ) is False
    assert cli_mod._should_enqueue_process_notification(
        {"type": "completion", "session_id": "ok_str", "exit_code": "0"},
        _FakeRegistry(),
    ) is False
    assert cli_mod._should_enqueue_process_notification(
        {"type": "completion", "session_id": "bad", "exit_code": 1},
        _FakeRegistry(),
    ) is True
    assert cli_mod._should_enqueue_process_notification(
        {"type": "completion", "session_id": "bad_str", "exit_code": "2"},
        _FakeRegistry(),
    ) is True


def test_cli_notification_mode_off_suppresses_everything(monkeypatch):
    _set_mode(monkeypatch, "off")

    assert cli_mod._should_enqueue_process_notification(
        {"type": "completion", "session_id": "bad", "exit_code": 1},
        _FakeRegistry(),
    ) is False
    assert cli_mod._should_enqueue_process_notification(
        {"type": "watch_match", "session_id": "proc"},
        _FakeRegistry(),
    ) is False


def test_cli_notification_env_override_wins_over_config(monkeypatch):
    display = cli_mod.CLI_CONFIG.setdefault("display", {})
    monkeypatch.setitem(display, "background_process_notifications", "all")
    monkeypatch.setenv("HERMES_BACKGROUND_NOTIFICATIONS", "off")

    assert cli_mod._should_enqueue_process_notification(
        {"type": "completion", "session_id": "bad", "exit_code": 1},
        _FakeRegistry(),
    ) is False


def test_cli_notification_boolean_config_modes(monkeypatch):
    _set_mode(monkeypatch, False)
    assert cli_mod._load_cli_background_notifications_mode() == "off"

    _set_mode(monkeypatch, True)
    assert cli_mod._load_cli_background_notifications_mode() == "all"


def test_cli_consumed_completion_is_skipped(monkeypatch):
    _set_mode(monkeypatch, "all")

    assert cli_mod._should_enqueue_process_notification(
        {"type": "completion", "session_id": "proc", "exit_code": 1},
        _FakeRegistry(consumed={"proc"}),
    ) is False
