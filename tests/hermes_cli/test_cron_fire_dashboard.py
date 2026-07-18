"""Tests for the Chronos cron-fire webhook ON THE DASHBOARD APP (web_server).

Regression guard for the relocation bug: the fire webhook MUST live on the
dashboard FastAPI app (`hermes_cli.web_server.app`) — the agent's public HTTP
surface on hosted deployments — not only on the aiohttp APIServerAdapter (which
hosted agents don't expose). It must:
  - be a registered route on the dashboard app,
  - be in PUBLIC_API_PATHS so the dashboard cookie gate doesn't 401 it before
    the JWT verifier runs,
  - reject a bad/missing NAS-JWT with 401 (the JWT is the real gate),
  - 400 on missing job_id,
  - on a valid token, resolve the job's profile and run fire_due in the
    background, returning 202.
"""

import threading
import time

import pytest
from starlette.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth.public_paths import PUBLIC_API_PATHS


FIRE_AT = "2026-07-18T12:00:00+00:00"


@pytest.fixture(autouse=True)
def _reset_cron_fire_state():
    with web_server._CRON_FIRE_STATE_LOCK:
        web_server._CRON_FIRE_ACTIVE.clear()
        web_server._CRON_FIRE_TOKEN_EXPIRY.clear()
    yield
    with web_server._CRON_FIRE_STATE_LOCK:
        web_server._CRON_FIRE_ACTIVE.clear()
        web_server._CRON_FIRE_TOKEN_EXPIRY.clear()


def _process(*, returncode=0, wait_event=None):
    class Process:
        def __init__(self):
            self.terminated = False

        def wait(self):
            if wait_event is not None:
                wait_event.wait(timeout=5)
            return returncode

        def terminate(self):
            self.terminated = True

    return Process()


def _client(auth_required: bool):
    prev_auth = getattr(web_server.app.state, "auth_required", None)
    prev_host = getattr(web_server.app.state, "bound_host", None)
    web_server.app.state.auth_required = auth_required
    web_server.app.state.bound_host = None
    client = TestClient(web_server.app)
    return client, prev_auth, prev_host


def _restore(prev_auth, prev_host):
    if prev_auth is None:
        if hasattr(web_server.app.state, "auth_required"):
            delattr(web_server.app.state, "auth_required")
    else:
        web_server.app.state.auth_required = prev_auth
    if prev_host is None:
        if hasattr(web_server.app.state, "bound_host"):
            delattr(web_server.app.state, "bound_host")
    else:
        web_server.app.state.bound_host = prev_host


def test_profile_lookup_matches_job_id_not_another_job_name(monkeypatch):
    monkeypatch.setattr(
        web_server,
        "_cron_profile_dicts",
        lambda: [{"name": "first"}, {"name": "second"}],
    )

    def list_jobs(profile, func_name, include_disabled):
        if profile == "first":
            return [{"id": "other-id", "name": "target-id"}]
        return [{"id": "target-id", "name": "actual job"}]

    monkeypatch.setattr(web_server, "_call_cron_for_profile", list_jobs)

    assert web_server._find_cron_job_profile("target-id") == "second"


def test_occurrence_match_compares_equivalent_iso_instants(monkeypatch):
    monkeypatch.setattr(
        web_server,
        "_call_cron_for_profile",
        lambda profile, func_name, job_id: {
            "id": job_id,
            "next_run_at": "2026-07-18T12:00:00+00:00",
        },
    )

    assert web_server._cron_fire_matches_current_occurrence(
        "default",
        "job-1",
        "2026-07-18T12:00:00Z",
    ) is True
    assert web_server._cron_fire_matches_current_occurrence(
        "default",
        "job-1",
        "not-a-timestamp",
    ) is False


def test_profile_fire_uses_isolated_profile_process(monkeypatch, tmp_path):
    seen = {}
    process = _process()

    def popen(command, **kwargs):
        seen["command"] = command
        seen.update(kwargs)
        return process

    monkeypatch.setenv("ANTHROPIC_API_KEY", "default-profile-secret")
    monkeypatch.setenv("PATH", "/safe/bin")
    monkeypatch.setattr(web_server, "get_hermes_home", lambda: tmp_path / "default")
    monkeypatch.setattr(
        web_server,
        "_cron_profile_home",
        lambda profile: ("secondary", tmp_path / "secondary"),
    )
    monkeypatch.setattr(web_server.subprocess, "Popen", popen)

    started = web_server._start_cron_job_for_profile(
        "secondary",
        "job-1",
        FIRE_AT,
    )

    assert started is process
    assert seen["command"] == [
        web_server.sys.executable,
        "-m",
        "hermes_cli.cron_fire_worker",
        "job-1",
        FIRE_AT,
    ]
    assert seen["env"]["HERMES_HOME"] == str(tmp_path / "secondary")
    assert seen["env"]["HERMES_PROFILE"] == "secondary"
    assert seen["env"]["PATH"] == "/safe/bin"
    assert "ANTHROPIC_API_KEY" not in seen["env"]
    assert seen["stdin"] is web_server.subprocess.DEVNULL


def test_cron_fire_worker_loads_profile_env_before_active_provider(monkeypatch):
    import cron.scheduler_provider as scheduler_provider
    import hermes_cli.env_loader as env_loader
    from hermes_cli import cron_fire_worker

    seen = []

    class Provider:
        def fire_due(self, job_id, **kwargs):
            seen.append(("fire", job_id, kwargs))
            return True

    monkeypatch.setattr(
        env_loader,
        "load_hermes_dotenv",
        lambda: seen.append(("env",)),
    )

    def resolve():
        assert seen == [("env",)]
        return Provider()

    monkeypatch.setattr(scheduler_provider, "resolve_cron_scheduler", resolve)

    assert cron_fire_worker.main(["job-1", FIRE_AT]) == 0
    assert seen == [
        ("env",),
        (
            "fire",
            "job-1",
            {"fire_at": FIRE_AT, "adapters": None, "loop": None},
        ),
    ]


def test_route_registered_on_dashboard_app():
    """The fire webhook is served by the dashboard app (the hosted-agent public
    surface), not only the aiohttp adapter."""
    paths = {r.path for r in web_server.app.routes if hasattr(r, "path")}
    assert "/api/cron/fire" in paths


def test_fire_path_is_public():
    """Must bypass the dashboard cookie gate so the NAS bearer-JWT callback
    reaches the verifier (the JWT is the real auth)."""
    assert "/api/cron/fire" in PUBLIC_API_PATHS


def test_bad_token_401(monkeypatch):
    """Invalid NAS-JWT -> 401, even with the dashboard auth gate ENGAGED
    (proves the route is reachable past the cookie gate and the verifier is the
    gate). fire_due must NOT run."""
    fired = []
    monkeypatch.setattr(
        "cron.scheduler_provider.verify_cron_fire_token",
        lambda token, **kwargs: None,  # verification fails
    )
    monkeypatch.setattr(web_server, "_find_cron_job_profiles", lambda jid: ["default"])
    monkeypatch.setattr(
        web_server,
        "_cron_fire_matches_current_occurrence",
        lambda profile, job_id, fire_at: True,
    )
    monkeypatch.setattr(
        web_server,
        "_start_cron_job_for_profile",
        lambda p, j, f: fired.append((p, j, f)),
    )

    client, pa, ph = _client(auth_required=True)
    try:
        resp = client.post("/api/cron/fire",
                           headers={"Authorization": "Bearer forged"},
                           json={"job_id": "abc", "fire_at": FIRE_AT})
        assert resp.status_code == 401
        assert fired == []
    finally:
        _restore(pa, ph)
        client.close()


def test_missing_job_id_400(monkeypatch):
    monkeypatch.setattr(
        "cron.scheduler_provider.verify_cron_fire_token",
        lambda token, **kwargs: {"purpose": "cron_fire"},
    )
    client, pa, ph = _client(auth_required=False)
    try:
        resp = client.post("/api/cron/fire",
                           headers={"Authorization": "Bearer good"},
                           json={})
        assert resp.status_code == 400
    finally:
        _restore(pa, ph)
        client.close()


def test_missing_fire_at_400(monkeypatch):
    monkeypatch.setattr(
        "cron.scheduler_provider.verify_cron_fire_token",
        lambda token, **kwargs: {"purpose": "cron_fire"},
    )
    monkeypatch.setattr(web_server, "_find_cron_job_profiles", lambda jid: [])

    client, pa, ph = _client(auth_required=False)
    try:
        response = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer good"},
            json={"job_id": "j1"},
        )
        assert response.status_code == 400
        assert response.json() == {"error": "missing fire_at"}
    finally:
        _restore(pa, ph)
        client.close()


def test_unknown_job_200_gone(monkeypatch):
    """Valid token but the job isn't found in any profile -> 200 'gone'
    (NAS shouldn't retry a fire for a cancelled/completed job)."""
    monkeypatch.setattr(
        "cron.scheduler_provider.verify_cron_fire_token",
        lambda token, **kwargs: {"purpose": "cron_fire"},
    )
    monkeypatch.setattr(web_server, "_find_cron_job_profiles", lambda jid: [])
    client, pa, ph = _client(auth_required=False)
    try:
        resp = client.post("/api/cron/fire",
                           headers={"Authorization": "Bearer good"},
                           json={"job_id": "ghost", "fire_at": FIRE_AT})
        assert resp.status_code == 200
        assert resp.json().get("status") == "gone"
    finally:
        _restore(pa, ph)
        client.close()


def test_deleted_secondary_job_retry_verifies_against_secondary_profile(monkeypatch):
    checked = []
    monkeypatch.setattr(web_server, "_find_cron_job_profiles", lambda jid: [])
    monkeypatch.setattr(
        web_server,
        "_cron_profile_names",
        lambda: ["default", "secondary"],
    )

    def verify(token, profile):
        checked.append(profile)
        if profile == "secondary":
            return {"purpose": "cron_fire"}
        return None

    monkeypatch.setattr(web_server, "_verify_cron_fire_token_for_profile", verify)

    client, pa, ph = _client(auth_required=False)
    try:
        response = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer secondary-token"},
            json={"job_id": "deleted-job", "fire_at": FIRE_AT},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "gone", "job_id": "deleted-job"}
    finally:
        _restore(pa, ph)
        client.close()

    assert checked == ["default", "secondary"]


def test_duplicate_job_id_uses_only_authorized_profile(monkeypatch):
    matched = []
    monkeypatch.setattr(
        web_server,
        "_find_cron_job_profiles",
        lambda jid: ["default", "secondary"],
    )
    monkeypatch.setattr(
        web_server,
        "_verify_cron_fire_token_for_profile",
        lambda token, profile: (
            {"purpose": "cron_fire"} if profile == "secondary" else None
        ),
    )
    monkeypatch.setattr(
        web_server,
        "_cron_fire_matches_current_occurrence",
        lambda profile, job_id, fire_at: matched.append(profile) or False,
    )

    client, pa, ph = _client(auth_required=False)
    try:
        response = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer secondary-token"},
            json={"job_id": "duplicate-job", "fire_at": FIRE_AT},
        )
        assert response.status_code == 202
    finally:
        _restore(pa, ph)
        client.close()

    assert matched == ["secondary"]


def test_duplicate_job_id_rejects_ambiguous_authorization(monkeypatch):
    monkeypatch.setattr(
        web_server,
        "_find_cron_job_profiles",
        lambda jid: ["default", "secondary"],
    )
    monkeypatch.setattr(
        web_server,
        "_verify_cron_fire_token_for_profile",
        lambda token, profile: {"purpose": "cron_fire"},
    )

    client, pa, ph = _client(auth_required=False)
    try:
        response = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer shared-token"},
            json={"job_id": "duplicate-job", "fire_at": FIRE_AT},
        )
        assert response.status_code == 409
        assert response.json() == {"error": "ambiguous cron job profile"}
    finally:
        _restore(pa, ph)
        client.close()


def test_valid_token_accepts_and_fires(monkeypatch):
    """Valid token + known job -> 202 and fire_due invoked for the resolved
    profile."""
    fired = []
    monkeypatch.setattr(
        "cron.scheduler_provider.verify_cron_fire_token",
        lambda token, **kwargs: {"purpose": "cron_fire", "aud": "agent:x"},
    )
    monkeypatch.setattr(web_server, "_find_cron_job_profiles", lambda jid: ["default"])
    monkeypatch.setattr(
        web_server,
        "_cron_fire_matches_current_occurrence",
        lambda profile, job_id, fire_at: True,
    )
    monkeypatch.setattr(
        web_server,
        "_start_cron_job_for_profile",
        lambda p, j, f: fired.append((p, j, f)) or _process(),
    )

    client, pa, ph = _client(auth_required=False)
    try:
        resp = client.post("/api/cron/fire",
                           headers={"Authorization": "Bearer good"},
                           json={"job_id": "j1", "fire_at": FIRE_AT})
        assert resp.status_code == 202
        assert resp.json()["job_id"] == "j1"
    finally:
        _restore(pa, ph)
        client.close()
    assert fired == [("default", "j1", FIRE_AT)]


def test_fire_token_uses_resolved_profile_config(monkeypatch, tmp_path):
    seen = []

    def verify(token, **kwargs):
        seen.append((token, kwargs))
        return {"purpose": "cron_fire"}

    monkeypatch.setattr("cron.scheduler_provider.verify_cron_fire_token", verify)
    monkeypatch.setattr(web_server, "_find_cron_job_profiles", lambda jid: ["secondary"])
    monkeypatch.setattr(
        web_server,
        "_cron_profile_home",
        lambda profile: ("secondary", tmp_path / "secondary"),
    )
    monkeypatch.setattr(
        web_server,
        "_cron_fire_matches_current_occurrence",
        lambda profile, job_id, fire_at: False,
    )

    client, pa, ph = _client(auth_required=False)
    try:
        response = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer profile-token"},
            json={"job_id": "j1", "fire_at": FIRE_AT},
        )
        assert response.status_code == 202
    finally:
        _restore(pa, ph)
        client.close()

    assert seen == [
        (
            "profile-token",
            {"hermes_home": str(tmp_path / "secondary")},
        )
    ]


def test_stale_fire_occurrence_does_not_spawn_worker(monkeypatch):
    started = []
    monkeypatch.setattr(
        "cron.scheduler_provider.verify_cron_fire_token",
        lambda token, **kwargs: {"purpose": "cron_fire"},
    )
    monkeypatch.setattr(web_server, "_find_cron_job_profiles", lambda jid: ["default"])
    monkeypatch.setattr(
        web_server,
        "_cron_fire_matches_current_occurrence",
        lambda profile, job_id, fire_at: False,
    )
    monkeypatch.setattr(
        web_server,
        "_start_cron_job_for_profile",
        lambda *args: started.append(args),
    )

    client, pa, ph = _client(auth_required=False)
    try:
        response = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer retry-token"},
            json={"job_id": "j1", "fire_at": FIRE_AT},
        )
        assert response.status_code == 202
        assert started == []
    finally:
        _restore(pa, ph)
        client.close()


def test_replayed_token_does_not_spawn_another_worker(monkeypatch):
    release = threading.Event()
    started = []
    monkeypatch.setattr(
        "cron.scheduler_provider.verify_cron_fire_token",
        lambda token, **kwargs: {
            "purpose": "cron_fire",
            "exp": time.time() + 60,
        },
    )
    monkeypatch.setattr(web_server, "_find_cron_job_profiles", lambda jid: ["default"])
    monkeypatch.setattr(
        web_server,
        "_cron_fire_matches_current_occurrence",
        lambda profile, job_id, fire_at: True,
    )
    monkeypatch.setattr(
        web_server,
        "_start_cron_job_for_profile",
        lambda profile, job_id, fire_at: started.append(
            (profile, job_id, fire_at)
        )
        or _process(wait_event=release),
    )

    client, pa, ph = _client(auth_required=False)
    try:
        first = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer one-use-token"},
            json={"job_id": "j1", "fire_at": FIRE_AT},
        )
        second = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer one-use-token"},
            json={"job_id": "j1", "fire_at": FIRE_AT},
        )
        assert first.status_code == 202
        assert second.status_code == 202
        assert started == [("default", "j1", FIRE_AT)]
    finally:
        release.set()
        _restore(pa, ph)
        client.close()


def test_worker_start_failure_is_retryable(monkeypatch):
    attempts = []
    monkeypatch.setattr(
        "cron.scheduler_provider.verify_cron_fire_token",
        lambda token, **kwargs: {
            "purpose": "cron_fire",
            "exp": time.time() + 60,
        },
    )
    monkeypatch.setattr(web_server, "_find_cron_job_profiles", lambda jid: ["default"])
    monkeypatch.setattr(
        web_server,
        "_cron_fire_matches_current_occurrence",
        lambda profile, job_id, fire_at: True,
    )

    def fail_to_start(profile, job_id, fire_at):
        attempts.append((profile, job_id, fire_at))
        raise OSError("process limit")

    monkeypatch.setattr(web_server, "_start_cron_job_for_profile", fail_to_start)

    client, pa, ph = _client(auth_required=False)
    try:
        for _ in range(2):
            response = client.post(
                "/api/cron/fire",
                headers={"Authorization": "Bearer retryable-token"},
                json={"job_id": "j1", "fire_at": FIRE_AT},
            )
            assert response.status_code == 503
            assert response.json() == {"error": "cron worker unavailable"}
    finally:
        _restore(pa, ph)
        client.close()

    assert attempts == [
        ("default", "j1", FIRE_AT),
        ("default", "j1", FIRE_AT),
    ]


def test_forced_basic_auth_passes_bearer_to_fire_verifier(monkeypatch):
    """Legacy forced Basic must not consume the webhook's bearer credential."""
    seen_tokens = []

    def verify(token, **kwargs):
        seen_tokens.append(token)
        return {"purpose": "cron_fire"}

    monkeypatch.setenv("HERMES_DASHBOARD_REQUIRE_BASIC_AUTH", "1")
    monkeypatch.setattr(
        "cron.scheduler_provider.verify_cron_fire_token",
        verify,
    )
    monkeypatch.setattr(web_server, "_find_cron_job_profiles", lambda jid: [])

    client, pa, ph = _client(auth_required=False)
    try:
        resp = client.post(
            "/api/cron/fire",
            headers={"Authorization": "Bearer nas-jwt"},
            json={"job_id": "gone", "fire_at": FIRE_AT},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "gone"
    finally:
        _restore(pa, ph)
        client.close()

    assert seen_tokens == ["nas-jwt"]
