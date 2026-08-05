from pathlib import Path
import sqlite3

import plugins.traces.hermes_traces_plugin as hermes_traces_plugin
from hermes_cli.plugins import PluginManager, get_bundled_plugins_dir
from plugins.traces.hermes_traces_plugin.state import State


class Registrar:
    def register_hook(self, name, callback):
        if not hasattr(self, "hooks"):
            self.hooks = {}
        self.hooks[name] = callback

    def register_session_artifact_provider(self, callback):
        self.provider = callback


class FakePublisher:
    def __init__(self, _config, _state):
        self.enqueued = []

    def enqueue(self, key):
        self.enqueued.append(key)

    def enqueue_many(self, keys):
        self.enqueued.extend(keys)


def test_bundled_plugin_exports_register():
    assert callable(hermes_traces_plugin.register)


def test_bundled_plugin_registers_session_artifact_provider():
    manager = PluginManager()
    manifests = manager._scan_directory(
        get_bundled_plugins_dir(),
        source="bundled",
        skip_names={"memory", "context_engine", "platforms", "model-providers"},
    )
    manifest = next(item for item in manifests if item.name == "traces")

    manager._load_plugin(manifest)

    assert len(manager._session_artifact_providers) == 1


def test_registers_required_hooks_and_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_traces_plugin, "Publisher", FakePublisher)
    monkeypatch.setattr(hermes_traces_plugin, "_runtimes", {})
    registrar = Registrar()

    assert hermes_traces_plugin.register(registrar) is None
    assert set(registrar.hooks) == {
        "on_session_end",
        "on_session_finalize",
        "subagent_start",
        "subagent_stop",
        "coding_worker_start",
        "coding_worker_stop",
    }
    end = registrar.hooks["on_session_end"]
    assert end("abc", "slack") is None
    assert end("", "discord") is None

    slug = end("abc", "discord")

    assert len(slug) >= 22
    assert registrar.provider("abc", "discord_feature_summary") == {
        "kind": "external_url",
        "label": "Agent Trace",
        "url": "https://sligo.sligolabs.com/traces/" + slug,
    }
    assert registrar.provider("abc", "other") is None


def test_provider_is_read_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_traces_plugin, "Publisher", FakePublisher)
    monkeypatch.setattr(hermes_traces_plugin, "_runtimes", {})
    registrar = Registrar()
    hermes_traces_plugin.register(registrar)
    registrar.hooks["on_session_end"]("abc", "discord")
    index_path = tmp_path / "state" / "plugins" / "traces" / "index.json"
    before = index_path.read_bytes()

    assert registrar.provider("abc", "discord_feature_summary") is not None
    assert index_path.read_bytes() == before


def test_finalize_creates_root_trace_when_end_hook_was_missed(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_traces_plugin, "Publisher", FakePublisher)
    monkeypatch.setattr(hermes_traces_plugin, "_runtimes", {})
    registrar = Registrar()
    hermes_traces_plugin.register(registrar)

    registrar.hooks["on_session_finalize"](
        session_id="root-only",
        platform="slack",
    )
    registrar.hooks["on_session_finalize"](
        session_id="root-only",
        platform="discord",
    )
    _config, state, publisher, _store = hermes_traces_plugin._runtime()
    record = state.get("root-only")

    assert publisher.enqueued == ["discord:root-only"]
    assert record is not None
    assert registrar.provider("root-only", "discord_feature_summary") == {
        "kind": "external_url",
        "label": "Agent Trace",
        "url": "https://sligo.sligolabs.com/traces/" + record["slug"],
    }


def test_runtime_state_is_isolated_by_active_profile(monkeypatch, tmp_path):
    first_home = tmp_path / "first"
    second_home = tmp_path / "second"
    monkeypatch.setattr(hermes_traces_plugin, "Publisher", FakePublisher)
    monkeypatch.setattr(hermes_traces_plugin, "_runtimes", {})
    registrar = Registrar()
    hermes_traces_plugin.register(registrar)

    monkeypatch.setenv("HERMES_HOME", str(first_home))
    first_slug = registrar.hooks["on_session_end"]("same-session", "discord")
    monkeypatch.setenv("HERMES_HOME", str(second_home))
    second_slug = registrar.hooks["on_session_end"]("same-session", "discord")

    assert first_slug != second_slug
    assert State(
        first_home / "state" / "plugins" / "traces" / "index.json"
    ).get("same-session")
    assert State(
        second_home / "state" / "plugins" / "traces" / "index.json"
    ).get("same-session")


def _create_lineage_db(home: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
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
        [("root", None), ("child", "root")],
    )
    connection.commit()
    connection.close()


def test_child_end_shares_child_and_root_and_exposes_root_artifact(
    monkeypatch,
    tmp_path,
):
    _create_lineage_db(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_traces_plugin, "Publisher", FakePublisher)
    monkeypatch.setattr(hermes_traces_plugin, "_runtimes", {})
    registrar = Registrar()
    hermes_traces_plugin.register(registrar)

    slug = registrar.hooks["on_session_end"](
        session_id="child",
        platform="discord",
        parent_session_id="root",
        root_session_id="root",
    )
    _config, state, publisher, _store = hermes_traces_plugin._runtime()

    assert publisher.enqueued == ["discord:child", "discord:root"]
    assert slug == state.get("root")["slug"]
    assert registrar.provider("child", "discord_feature_summary") == {
        "kind": "external_url",
        "label": "Agent Trace",
        "url": "https://sligo.sligolabs.com/traces/" + slug,
    }


def test_subagent_stop_publishes_child_without_refreshing_live_root(
    monkeypatch,
    tmp_path,
):
    _create_lineage_db(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_traces_plugin, "Publisher", FakePublisher)
    monkeypatch.setattr(hermes_traces_plugin, "_runtimes", {})
    registrar = Registrar()
    hermes_traces_plugin.register(registrar)

    registrar.hooks["subagent_start"](
        child_session_id="child",
        parent_session_id="root",
        root_session_id="root",
        platform="discord",
    )
    slug = registrar.hooks["subagent_stop"](
        child_session_id="child",
        parent_session_id="root",
        root_session_id="root",
        child_status="completed",
    )
    _config, state, publisher, _store = hermes_traces_plugin._runtime()

    assert publisher.enqueued == ["discord:child"]
    assert state.get("child") is not None
    assert slug == state.get("root")["slug"]


def test_coding_worker_is_written_only_to_private_observer_store(
    monkeypatch,
    tmp_path,
):
    _create_lineage_db(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_traces_plugin, "Publisher", FakePublisher)
    monkeypatch.setattr(hermes_traces_plugin, "_runtimes", {})
    registrar = Registrar()
    hermes_traces_plugin.register(registrar)
    payload = {
        "worker_session_id": "coding_worker_1",
        "root_session_id": "root",
        "parent_session_id": "root",
        "platform": "discord",
        "backend": "codex",
        "model": "gpt-test",
        "task": "fix parser",
        "cwd": "/workspace",
        "started_at": 10.0,
    }

    registrar.hooks["coding_worker_start"](**payload)
    observer_db = tmp_path / "state" / "plugins" / "traces" / "observer" / "state.db"
    assert not observer_db.exists()
    registrar.hooks["coding_worker_stop"](
        **payload,
        status="completed",
        ended_at=12.0,
        duration_ms=2000,
        thread_id="thread-1",
        turn_id="turn-1",
        summary="done",
        worker_messages=[{"role": "assistant", "content": "implemented"}],
    )

    assert not sqlite3.connect(tmp_path / "state.db").execute(
        "SELECT 1 FROM sessions WHERE id = 'coding_worker_1'"
    ).fetchone()
    connection = sqlite3.connect(observer_db)
    session = connection.execute(
        "SELECT source, parent_session_id, end_reason FROM sessions WHERE id = ?",
        ("coding_worker_1",),
    ).fetchone()
    messages = connection.execute(
        "SELECT role, content, observed FROM messages WHERE session_id = ? ORDER BY id",
        ("coding_worker_1",),
    ).fetchall()
    connection.close()

    _config, state, publisher, _store = hermes_traces_plugin._runtime()
    worker = state.get("coding_worker_1")
    assert session == ("tool", "root", "completed")
    assert all(row[2] == 1 for row in messages)
    assert any("Observer-only" in (row[1] or "") for row in messages)
    assert any("implemented" in (row[1] or "") for row in messages)
    assert worker["trace_home"].endswith("/state/plugins/traces/observer")
    assert publisher.enqueued == ["discord:coding_worker_1"]


def test_finalize_does_not_refresh_already_ready_root(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_traces_plugin, "Publisher", FakePublisher)
    monkeypatch.setattr(hermes_traces_plugin, "_runtimes", {})
    registrar = Registrar()
    hermes_traces_plugin.register(registrar)

    registrar.hooks["on_session_end"]("root", "discord")
    _config, state, publisher, _store = hermes_traces_plugin._runtime()
    record = state.get("root")
    state.update(
        record["key"],
        status="ready",
        shared_url="https://traces.com/s/example",
        visibility="private",
    )
    publisher.enqueued.clear()

    registrar.hooks["on_session_finalize"]("root", "discord")

    assert publisher.enqueued == []
