import json
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


def test_prepare_worker_home_falls_back_when_pool_auth_is_incomplete(tmp_path, monkeypatch):
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

    worker_home = tmp_path / "worker-codex"
    _write_codex_auth(worker_home, access="stale-access", refresh="stale-refresh", id_token="")

    credential_id = prepare_codex_worker_home(worker_home)

    payload = json.loads((worker_home / "auth.json").read_text(encoding="utf-8"))
    assert credential_id is None
    assert payload["tokens"]["access_token"] == "cli-access"
    assert payload["tokens"]["refresh_token"] == "cli-refresh"
    assert payload["tokens"]["id_token"] == "cli-id"
