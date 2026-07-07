import sqlite3
from types import SimpleNamespace

from gateway.config import PlatformConfig

import plugins.platforms.discord.adapter as discord_platform  # noqa: E402
from plugins.platforms.discord.adapter import DiscordAdapter  # noqa: E402


class FakeChannel:
    id = "200"
    name = "pid"
    guild = SimpleNamespace(id="guild-1", name="Guild")
    parent_id = None
    parent = None
    category = None


def _make_adapter() -> DiscordAdapter:
    return DiscordAdapter(PlatformConfig(enabled=True, token="test-token", extra={}))


def test_shared_session_db_reused_for_mapping_and_resolve(monkeypatch):
    calls = {"constructors": 0}

    class CountingSessionDB:
        def __init__(self):
            calls["constructors"] += 1

        def list_discord_project_mappings(self):
            return [{"channel_id": "100"}]

        def close(self):
            pass

    seen_session_dbs = []
    resolved_context = SimpleNamespace(resolved=True, channel_id="200", parent_channel_id=None)

    def fake_resolve(channel, *, session_db=None, workspace_root=None, config=None):
        seen_session_dbs.append(session_db)
        return resolved_context

    monkeypatch.setattr("hermes_state.SessionDB", CountingSessionDB)
    monkeypatch.setattr(discord_platform, "resolve_discord_project_context", fake_resolve)

    adapter = _make_adapter()

    assert adapter._discord_project_mapping_root_channel_ids() == {"100"}
    assert adapter._discord_project_mapping_root_channel_ids() == {"100"}
    assert adapter._resolve_discord_project_context_with_shared_db(FakeChannel) is resolved_context
    assert adapter._resolve_discord_project_context_with_shared_db(FakeChannel) is resolved_context

    assert calls["constructors"] == 1
    assert len({id(db) for db in seen_session_dbs}) == 1
    assert seen_session_dbs[0] is getattr(adapter, "_hot_session_db")


def test_relevant_root_channel_ids_ttl_cache_reuses_union(monkeypatch):
    adapter = _make_adapter()
    calls = {"project_mapping": 0}

    def project_mapping_ids():
        calls["project_mapping"] += 1
        return {"42"}

    monkeypatch.setattr(discord_platform.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(adapter, "_discord_allowed_channel_ids", lambda: set())
    monkeypatch.setattr(adapter, "_discord_channel_cwd_root_channel_ids", lambda: set())
    monkeypatch.setattr(adapter, "_discord_project_mapping_root_channel_ids", project_mapping_ids)
    monkeypatch.setattr(adapter, "_discord_free_response_channels", lambda: set())
    monkeypatch.setattr(adapter, "_discord_action_request_channels", lambda: set())
    monkeypatch.setattr(adapter, "_discord_no_thread_channel_ids", lambda: set())
    monkeypatch.setattr(adapter, "_read_discord_root_mention_recovery_state", lambda: {})
    monkeypatch.setattr(adapter, "_discord_ignored_channel_ids", lambda: set())

    assert adapter._discord_relevant_root_channel_ids() == ["42"]
    assert adapter._discord_relevant_root_channel_ids() == ["42"]
    assert calls["project_mapping"] == 1


def test_bootstrap_context_invalidates_warm_relevant_channel_cache():
    adapter = _make_adapter()
    adapter._relevant_root_channels_cache = (100.0, frozenset({"111"}))
    context = SimpleNamespace(resolved=True, channel_id="222", parent_channel_id=None)

    adapter._invalidate_relevant_root_channels_cache_for_context(context)

    assert adapter._relevant_root_channels_cache is None


def test_shared_session_db_failure_falls_back_and_clears_cached_handle(monkeypatch):
    calls = {"constructors": 0, "closes": 0}

    class FailingSessionDB:
        def __init__(self):
            calls["constructors"] += 1

        def list_discord_project_mappings(self):
            raise sqlite3.Error("locked")

        def close(self):
            calls["closes"] += 1

    monkeypatch.setattr("hermes_state.SessionDB", FailingSessionDB)
    adapter = _make_adapter()

    assert adapter._discord_project_mapping_root_channel_ids() == set()
    assert getattr(adapter, "_hot_session_db") is None
    assert calls["constructors"] == 2
    assert calls["closes"] == 2
