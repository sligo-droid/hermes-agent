import json
import time
from pathlib import Path
from types import SimpleNamespace


class FakePool:
    def __init__(self, entry):
        self._entry = entry

    def has_credentials(self):
        return True

    def current(self):
        return self._entry

    def select(self):
        return self._entry


def _write_codex_auth(path: Path, *, access: str, refresh: str, id_token: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": access,
                    "refresh_token": refresh,
                    "id_token": id_token,
                    "account_id": "acct-" + access,
                },
            }
        ),
        encoding="utf-8",
    )


def _write_pool_auth(hermes_home: Path, entries: list[dict]) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "auth.json").write_text(
        json.dumps(
            {
                "version": 1,
                "credential_pool": {"openai-codex": entries},
            }
        ),
        encoding="utf-8",
    )


def test_prepare_worker_home_writes_complete_pool_auth(tmp_path, monkeypatch):
    from agent import credential_pool
    from agent.codex_worker_auth import prepare_codex_worker_home

    source_home = tmp_path / "source-codex"
    _write_codex_auth(source_home, access="cli-access", refresh="cli-refresh", id_token="cli-id")
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    entry = SimpleNamespace(
        id="pool-1",
        access_token="pool-access",
        refresh_token="pool-refresh",
        id_token="pool-id",
        account_id="acct-pool",
        last_status=None,
        last_refresh="2026-05-20T00:00:00Z",
    )
    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: FakePool(entry))

    credential_id = prepare_codex_worker_home(tmp_path / "worker-codex")

    payload = json.loads((tmp_path / "worker-codex" / "auth.json").read_text(encoding="utf-8"))
    assert credential_id == "pool-1"
    assert payload["tokens"]["access_token"] == "pool-access"
    assert payload["tokens"]["refresh_token"] == "pool-refresh"
    assert payload["tokens"]["id_token"] == "pool-id"
    assert payload["tokens"]["account_id"] == "acct-pool"


def test_prepare_worker_home_accepts_pool_auth_without_id_token(tmp_path, monkeypatch):
    from agent import credential_pool
    from agent.codex_worker_auth import prepare_codex_worker_home

    source_home = tmp_path / "source-codex"
    _write_codex_auth(source_home, access="cli-access", refresh="cli-refresh", id_token="cli-id")
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    entry = SimpleNamespace(
        id="pool-1",
        access_token="pool-access",
        refresh_token="pool-refresh",
        last_status=None,
    )
    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: FakePool(entry))

    credential_id = prepare_codex_worker_home(tmp_path / "worker-codex")

    payload = json.loads((tmp_path / "worker-codex" / "auth.json").read_text(encoding="utf-8"))
    assert credential_id == "pool-1"
    assert payload["tokens"]["access_token"] == "pool-access"
    assert payload["tokens"]["refresh_token"] == "pool-refresh"
    assert "id_token" not in payload["tokens"]


def test_prepare_worker_home_falls_back_when_pool_auth_is_incomplete(tmp_path, monkeypatch):
    from agent import credential_pool
    from agent.codex_worker_auth import prepare_codex_worker_home

    source_home = tmp_path / "source-codex"
    _write_codex_auth(source_home, access="cli-access", refresh="cli-refresh", id_token="cli-id")
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    entry = SimpleNamespace(
        id="pool-1",
        access_token="pool-access",
        last_status=None,
    )
    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: FakePool(entry))

    worker_home = tmp_path / "worker-codex"
    _write_codex_auth(worker_home, access="stale-access", refresh="stale-refresh", id_token="")

    credential_id = prepare_codex_worker_home(worker_home)

    payload = json.loads((worker_home / "auth.json").read_text(encoding="utf-8"))
    assert credential_id is None
    assert payload["tokens"]["access_token"] == "cli-access"
    assert payload["tokens"]["refresh_token"] == "cli-refresh"
    assert payload["tokens"]["id_token"] == "cli-id"


def test_prepare_worker_home_can_disable_fallback_auth_copy(tmp_path, monkeypatch):
    from agent import credential_pool
    from agent.codex_worker_auth import prepare_codex_worker_home

    source_home = tmp_path / "source-codex"
    _write_codex_auth(source_home, access="cli-access", refresh="cli-refresh", id_token="cli-id")
    monkeypatch.setenv("CODEX_HOME", str(source_home))

    entry = SimpleNamespace(
        id="pool-1",
        access_token="pool-access",
        last_status=None,
    )
    monkeypatch.setattr(credential_pool, "load_pool", lambda provider: FakePool(entry))

    worker_home = tmp_path / "worker-codex"
    _write_codex_auth(worker_home, access="failed-access", refresh="failed-refresh", id_token="")

    credential_id = prepare_codex_worker_home(worker_home, allow_fallback=False)

    payload = json.loads((worker_home / "auth.json").read_text(encoding="utf-8"))
    assert credential_id is None
    assert payload["tokens"]["access_token"] == "failed-access"
    assert payload["tokens"]["refresh_token"] == "failed-refresh"


def test_prepare_worker_home_skips_auth_failed_pool_credentials(tmp_path, monkeypatch):
    from agent.codex_worker_auth import prepare_codex_worker_home

    now = time.time()
    _write_pool_auth(
        tmp_path / "hermes-home",
        [
            {
                "id": "cred-1",
                "label": "primary",
                "auth_type": "oauth",
                "priority": 0,
                "source": "manual:device_code",
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "id_token": "id-1",
                "last_status": "exhausted",
                "last_status_at": now,
                "last_error_code": 401,
                "last_error_reason": "token_invalidated",
                "last_error_reset_at": now + 3600,
            },
            {
                "id": "cred-2",
                "label": "secondary",
                "auth_type": "oauth",
                "priority": 1,
                "source": "manual:device_code",
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "id_token": "id-2",
                "last_status": "exhausted",
                "last_status_at": now,
                "last_error_code": 401,
                "last_error_reason": "token_revoked",
                "last_error_reset_at": now + 3600,
            },
            {
                "id": "cred-3",
                "label": "working",
                "auth_type": "oauth",
                "priority": 2,
                "source": "manual:device_code",
                "access_token": "access-3",
                "refresh_token": "refresh-3",
                "id_token": "id-3",
            },
        ],
    )
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))

    credential_id = prepare_codex_worker_home(tmp_path / "worker-codex")

    payload = json.loads((tmp_path / "worker-codex" / "auth.json").read_text(encoding="utf-8"))
    assert credential_id == "cred-3"
    assert payload["tokens"]["access_token"] == "access-3"
    assert payload["tokens"]["refresh_token"] == "refresh-3"


def test_mark_worker_credential_auth_failed_exhausts_pool_entry(tmp_path, monkeypatch):
    from agent.codex_worker_auth import mark_codex_worker_credential_auth_failed
    from agent.credential_pool import STATUS_EXHAUSTED, load_pool

    hermes_home = tmp_path / "hermes-home"
    _write_pool_auth(
        hermes_home,
        [
            {
                "id": "cred-1",
                "label": "primary",
                "auth_type": "oauth",
                "priority": 0,
                "source": "manual:device_code",
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "id_token": "id-1",
            },
            {
                "id": "cred-2",
                "label": "working",
                "auth_type": "oauth",
                "priority": 1,
                "source": "manual:device_code",
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "id_token": "id-2",
            },
        ],
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    marked = mark_codex_worker_credential_auth_failed(
        "cred-1",
        message="Your access token could not be refreshed because your refresh token was revoked.",
    )

    pool = load_pool("openai-codex")
    entries = {entry.id: entry for entry in pool.entries()}
    assert marked is True
    assert entries["cred-1"].last_status == STATUS_EXHAUSTED
    assert entries["cred-1"].last_error_code == 401
    assert entries["cred-1"].last_error_reason == "worker_auth_failed"
    assert pool.select().id == "cred-2"


def test_sync_worker_home_accepts_refreshed_tokens_without_id_token(tmp_path, monkeypatch):
    from agent.codex_worker_auth import sync_codex_worker_home
    from agent.credential_pool import load_pool

    hermes_home = tmp_path / "hermes-home"
    _write_pool_auth(
        hermes_home,
        [
            {
                "id": "cred-1",
                "label": "primary",
                "auth_type": "oauth",
                "priority": 0,
                "source": "manual:device_code",
                "access_token": "old-access",
                "refresh_token": "old-refresh",
                "last_status": "exhausted",
                "last_error_code": 401,
            },
        ],
    )
    worker_home = tmp_path / "worker-codex"
    worker_home.mkdir()
    (worker_home / "auth.json").write_text(
        json.dumps(
            {
                "auth_mode": "chatgpt",
                "tokens": {
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    sync_codex_worker_home(worker_home, "cred-1")

    entry = load_pool("openai-codex").entries()[0]
    assert entry.access_token == "new-access"
    assert entry.refresh_token == "new-refresh"
    assert entry.last_status is None
    assert entry.last_error_code is None
