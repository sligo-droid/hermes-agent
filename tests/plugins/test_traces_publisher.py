import json
import subprocess
from types import SimpleNamespace

import pytest

from plugins.traces.hermes_traces_plugin.config import Config
from plugins.traces.hermes_traces_plugin.publisher import Publisher
from plugins.traces.hermes_traces_plugin.state import State


def result(payload, returncode=0):
    return SimpleNamespace(returncode=returncode, stdout=json.dumps(payload))


def test_publisher_shares_exact_session_with_private_visibility(monkeypatch, tmp_path):
    state = State(tmp_path / "index.json")
    record = state.create("local-id")
    captured = {}

    def run(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return result(
            {
                "ok": True,
                "data": {
                    "traceId": "remote-id",
                    "sharedUrl": "https://traces.com/x",
                    "visibility": "private",
                },
            }
        )

    monkeypatch.setattr("subprocess.run", run)
    publisher = Publisher(
        Config(tmp_path, executable="traces"),
        state,
        start_worker=False,
    )

    saved = publisher.publish(record["key"])

    assert saved["status"] == "ready"
    assert saved["remote_trace_id"] == "remote-id"
    assert captured["argv"] == [
        "traces",
        "share",
        "--trace-id",
        "local-id",
        "--agent",
        "hermes",
        "--visibility",
        "private",
        "--json",
    ]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["env"]["HERMES_HOME"] == str(tmp_path)
    assert captured["kwargs"]["env"]["TRACES_HERMES_DIR"] == str(tmp_path)


def test_publisher_uses_record_specific_observer_home(monkeypatch, tmp_path):
    state = State(tmp_path / "index.json")
    observer_home = tmp_path / "observer"
    record = state.create("coding-1", trace_home=observer_home)
    captured = {}

    def run(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        return result(
            {
                "ok": True,
                "data": {
                    "traceId": "remote-id",
                    "sharedUrl": "https://traces.com/x",
                    "visibility": "private",
                },
            }
        )

    monkeypatch.setattr("subprocess.run", run)
    Publisher(Config(tmp_path), state, start_worker=False).publish(record["key"])

    assert captured["kwargs"]["env"]["HERMES_HOME"] == str(tmp_path)
    assert captured["kwargs"]["env"]["TRACES_HERMES_DIR"] == str(observer_home)


def test_refresh_uses_local_id_and_preserves_last_good_values(monkeypatch, tmp_path):
    state = State(tmp_path / "index.json")
    record = state.create("local-session")
    state.update(
        record["key"],
        status="ready",
        shared_url="https://traces.com/old",
        visibility="private",
    )
    captured = {}

    def run(argv, **_kwargs):
        captured["argv"] = argv
        return result({"ok": True, "data": {"traceId": "remote-new"}})

    monkeypatch.setattr("subprocess.run", run)
    publisher = Publisher(Config(tmp_path), state, start_worker=False)

    publisher.publish(record["key"])

    saved = state.get_key(record["key"])
    assert captured["argv"] == [
        Config(tmp_path).executable,
        "refresh",
        "--trace-id",
        "local-session",
        "--json",
    ]
    assert saved["session_id"] == "local-session"
    assert saved["trace_id"] == "local-session"
    assert saved["remote_trace_id"] == "remote-new"
    assert saved["shared_url"] == "https://traces.com/old"


def test_refresh_failure_keeps_last_good_trace_available(monkeypatch, tmp_path):
    state = State(tmp_path / "index.json")
    record = state.create("local-session")
    state.update(
        record["key"],
        status="ready",
        shared_url="https://traces.com/old",
        visibility="private",
    )
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: result({}, returncode=1),
    )

    Publisher(Config(tmp_path), state, start_worker=False).publish(record["key"])

    saved = state.get_key(record["key"])
    assert saved["status"] == "ready"
    assert saved["shared_url"] == "https://traces.com/old"
    assert saved["error"] == "command_failed"


@pytest.mark.parametrize(
    "shared_url,visibility",
    [
        ("http://traces.com/x", "private"),
        ("https://evil.example/x", "private"),
        ("https://user:pass@traces.com/x", "private"),
        ("https://traces.com:8443/x", "private"),
        ("https://traces.com/" + "x" * 2_048, "private"),
        ("https://traces.com/x", "direct"),
    ],
)
def test_publisher_rejects_untrusted_or_nonprivate_response(
    monkeypatch,
    tmp_path,
    shared_url,
    visibility,
):
    state = State(tmp_path / "index.json")
    record = state.create("id")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: result(
            {
                "ok": True,
                "data": {
                    "sharedUrl": shared_url,
                    "visibility": visibility,
                },
            }
        ),
    )

    Publisher(Config(tmp_path), state, start_worker=False).publish(record["key"])

    assert state.get_key(record["key"])["error"] == "invalid_response"


def test_publisher_rejects_unbounded_remote_trace_id(monkeypatch, tmp_path):
    state = State(tmp_path / "index.json")
    record = state.create("id")
    monkeypatch.setattr(
        "subprocess.run",
        lambda *_args, **_kwargs: result(
            {
                "ok": True,
                "data": {
                    "traceId": "x" * 513,
                    "sharedUrl": "https://traces.com/x",
                    "visibility": "private",
                },
            }
        ),
    )

    Publisher(Config(tmp_path), state, start_worker=False).publish(record["key"])

    assert state.get_key(record["key"])["error"] == "invalid_response"


@pytest.mark.parametrize(
    "runner,expected_error",
    [
        (lambda: result({}, returncode=1), "command_failed"),
        (lambda: SimpleNamespace(returncode=0, stdout="not-json"), "invalid_json"),
        (
            lambda: (_ for _ in ()).throw(
                subprocess.TimeoutExpired(cmd="traces", timeout=1)
            ),
            "timeout",
        ),
        (
            lambda: (_ for _ in ()).throw(FileNotFoundError("traces")),
            "execution_failed",
        ),
        (
            lambda: (_ for _ in ()).throw(TypeError("unexpected")),
            "unexpected_error",
        ),
    ],
)
def test_publisher_records_bounded_fail_open_errors(
    monkeypatch,
    tmp_path,
    runner,
    expected_error,
):
    state = State(tmp_path / "index.json")
    record = state.create("id")
    monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: runner())

    Publisher(Config(tmp_path), state, start_worker=False).publish(record["key"])

    saved = state.get_key(record["key"])
    assert saved["status"] == "error"
    assert saved["error"] == expected_error


def test_enqueue_coalesces_duplicate_pending_work(tmp_path):
    state = State(tmp_path / "index.json")
    publisher = Publisher(Config(tmp_path), state, start_worker=False)

    publisher.enqueue("discord:one")
    publisher.enqueue("discord:one")
    publisher.enqueue("discord:two")

    assert list(publisher._pending) == ["discord:one", "discord:two"]
    assert publisher._pending_keys == {"discord:one", "discord:two"}


def test_enqueue_many_preserves_child_before_root_refresh(tmp_path):
    state = State(tmp_path / "index.json")
    publisher = Publisher(Config(tmp_path), state, start_worker=False)
    publisher.enqueue("discord:root")

    publisher.enqueue_many(["discord:child", "discord:root"])

    assert list(publisher._pending) == ["discord:child", "discord:root"]
