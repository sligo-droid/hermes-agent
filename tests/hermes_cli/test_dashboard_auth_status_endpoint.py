"""Phase 7 — /api/status exposes auth-gate state + AuthWidget integration.

The dashboard's status endpoint now reports ``auth_required`` and
``auth_providers`` so the AuthWidget + StatusPage can render the
correct "gated / loopback" badge without a separate round trip. This
test asserts both shapes (gated and loopback).

The AuthWidget itself is .tsx — no Python test here. The widget's
behaviour (renders nothing on 401, shows truncated user_id, etc.) is
documented in AuthWidget.tsx; covered manually via the Phase 4.2
smoke test against staging Portal.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hermes_cli import web_server
from hermes_cli.dashboard_auth import clear_providers, register_provider
from tests.hermes_cli.conftest_dashboard_auth import StubAuthProvider


@pytest.fixture
def gated_client():
    clear_providers()
    register_provider(StubAuthProvider())
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "fly-app.fly.dev"
    web_server.app.state.bound_port = 443
    web_server.app.state.auth_required = True
    client = TestClient(
        web_server.app,
        base_url="https://fly-app.fly.dev",
        client=("192.0.2.25", 50000),
    )
    yield client
    clear_providers()
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


@pytest.fixture
def loopback_client():
    clear_providers()
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "127.0.0.1"
    web_server.app.state.bound_port = 8080
    web_server.app.state.auth_required = False
    client = TestClient(
        web_server.app,
        base_url="http://127.0.0.1:8080",
        client=("127.0.0.1", 50000),
    )
    yield client
    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


def test_status_reports_auth_required_in_gated_mode(gated_client):
    # No ``_login()`` call — ``/api/status`` is in the shared
    # ``PUBLIC_API_PATHS`` allowlist precisely so external probes (and
    # the SPA's pre-login bootstrap) can read the gate's shape without
    # a cookie. Hit it cold.
    r = gated_client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_required"] is True
    assert body["auth_providers"] == ["stub"]


def test_status_reports_auth_disabled_in_loopback_mode(loopback_client):
    r = loopback_client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["auth_required"] is False
    # Loopback mode has no registered providers (the Nous plugin's env
    # vars aren't set in test).
    assert body["auth_providers"] == []


def test_status_preserves_existing_fields(loopback_client):
    """Defence-in-depth: adding auth_required/auth_providers must not
    have dropped any previous field (the dashboard's React StatusPage
    relies on the full payload shape)."""
    r = loopback_client.get("/api/status")
    body = r.json()
    expected_keys = {
        "version", "release_date", "hermes_home", "config_path", "env_path",
        "config_version", "latest_config_version", "gateway_running",
        "gateway_pid", "gateway_health_url", "gateway_state",
        "gateway_platforms", "gateway_exit_reason", "gateway_updated_at",
        "active_sessions", "auth_required", "auth_providers",
    }
    missing = expected_keys - set(body.keys())
    assert not missing, f"/api/status dropped fields: {missing}"


# Host-local detail (absolute paths, PID, internal gateway URL) is deployment
# recon a liveness probe never needs. ``/api/status`` bypasses dashboard auth
# (it is in ``PUBLIC_API_PATHS``), so on a network-exposed bind it must not
# leak that detail to anonymous callers.
_HOST_DETAIL_FIELDS = frozenset({
    "hermes_home", "config_path", "env_path", "gateway_pid",
    "gateway_health_url",
})


def test_status_withholds_host_detail_in_gated_mode(gated_client):
    """On a gated (non-loopback) bind, the public ``/api/status`` probe must
    expose only the liveness + auth-gate shape — never absolute host paths,
    the gateway PID, or the internal gateway health URL. The endpoint
    bypasses dashboard auth, so anyone who can reach the host hits it cold."""
    r = gated_client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    # Liveness / auth-gate shape stays public.
    for key in ("version", "gateway_state", "auth_required", "auth_providers"):
        assert key in body, f"liveness field {key!r} must stay public"
    # Deployment recon must be withheld from the anonymous public probe.
    leaked = _HOST_DETAIL_FIELDS & set(body.keys())
    assert not leaked, f"/api/status leaked host detail under the gate: {leaked}"


def test_public_status_sanitizes_platform_errors_and_exit_reason(
    gated_client,
    monkeypatch,
):
    import gateway.config as gateway_config

    class GatewayConfig:
        def get_connected_platforms(self):
            return [type("Platform", (), {"value": "discord"})()]

    monkeypatch.setattr(gateway_config, "load_gateway_config", GatewayConfig)
    # Status uses the bounded PID cache so repeated public probes do not scan
    # the process table on every request.
    monkeypatch.setattr(web_server, "get_running_pid_cached", lambda: 1234)
    monkeypatch.setattr(
        web_server,
        "read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {
                "discord": {
                    "state": "connected",
                    "error_message": "credential-shaped-private-detail",
                }
            },
            "exit_reason": "private-upstream-response-body",
        },
    )

    body = gated_client.get("/api/status").json()

    assert body["gateway_platforms"] == {"discord": {"state": "connected"}}
    assert body["gateway_exit_reason"] is None
    assert "credential-shaped-private-detail" not in str(body)
    assert "private-upstream-response-body" not in str(body)


def test_status_includes_host_detail_in_loopback_mode(loopback_client):
    """A direct loopback request preserves the full local operator payload."""
    r = loopback_client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    missing = _HOST_DETAIL_FIELDS - set(body.keys())
    assert not missing, f"loopback /api/status should keep host detail: {missing}"


@pytest.mark.parametrize(
    "value",
    ["localhost", "localhost:8080", "127.0.0.1:8080", "::1", "[::1]:8080"],
)
def test_status_loopback_host_detection_supports_ports_and_ipv6(value):
    assert web_server._is_loopback_host(value) is True


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Forwarded-Host": "agent.example.com"},
        {"X-Forwarded-Host": "127.0.0.1:8080"},
        {"X-Forwarded-For": "203.0.113.7"},
        {"X-Forwarded-For": "127.0.0.1"},
        {"X-Forwarded-Proto": "http"},
        {"Forwarded": "for=203.0.113.7;host=agent.example.com"},
        {"CF-Ray": "8f00-example"},
        {"CF-Connecting-IP": "127.0.0.1"},
        {"CF-Visitor": '{"scheme":"https"}'},
    ],
)
def test_status_edge_or_non_loopback_forwarding_only_increases_redaction(
    loopback_client,
    headers,
):
    """Untrusted proxy/edge metadata can redact, but never grant local detail."""
    r = loopback_client.get("/api/status", headers=headers)
    assert r.status_code == 200
    leaked = _HOST_DETAIL_FIELDS & set(r.json())
    assert not leaked, f"/api/status leaked host detail with headers {headers}: {leaked}"


def test_status_non_loopback_peer_redacts_even_with_loopback_host():
    """A spoofed loopback Host header cannot make a remote peer host-local."""
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)
    web_server.app.state.bound_host = "0.0.0.0"
    web_server.app.state.auth_required = False
    client = TestClient(
        web_server.app,
        base_url="http://127.0.0.1:8080",
        client=("198.51.100.12", 50000),
    )
    try:
        r = client.get("/api/status")
        assert r.status_code == 200
        assert not (_HOST_DETAIL_FIELDS & set(r.json()))
    finally:
        client.close()
        web_server.app.state.bound_host = prev_host
        web_server.app.state.auth_required = prev_required
