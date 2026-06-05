from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import web_server


pytestmark = pytest.mark.xdist_group("dashboard_auth_app_state")


@pytest.fixture
def dashboard_app(tmp_path, monkeypatch):
    prev_host = getattr(web_server.app.state, "bound_host", None)
    prev_port = getattr(web_server.app.state, "bound_port", None)
    prev_required = getattr(web_server.app.state, "auth_required", None)

    dist = tmp_path / "web_dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><head></head><body>shell</body></html>")
    monkeypatch.setattr(web_server, "WEB_DIST", dist)
    monkeypatch.delenv("HERMES_DASHBOARD_REQUIRE_BASIC_AUTH", raising=False)

    def make_client(host: str, *, auth_required: bool = False) -> TestClient:
        web_server.app.state.auth_required = auth_required
        web_server.app.state.bound_host = host
        web_server.app.state.bound_port = 9119

        app = FastAPI()
        app.state.auth_required = auth_required
        app.state.bound_host = host
        app.state.bound_port = 9119
        app.middleware("http")(web_server.auth_middleware)

        @app.get("/api/sessions")
        async def api_sessions():
            return {"sessions": []}

        web_server.mount_spa(app)
        return TestClient(app, base_url=f"http://{host}:9119")

    yield make_client

    web_server.app.state.bound_host = prev_host
    web_server.app.state.bound_port = prev_port
    web_server.app.state.auth_required = prev_required


@pytest.mark.parametrize("path", ["/", "/command-center", "/sligo"])
def test_loopback_spa_routes_skip_basic_auth_and_inject_token(dashboard_app, path):
    response = dashboard_app("127.0.0.1").get(path)

    assert response.status_code == 200
    assert "__HERMES_SESSION_TOKEN__" in response.text


def test_loopback_missing_asset_skips_basic_auth(dashboard_app):
    response = dashboard_app("127.0.0.1").get("/assets/nonexistent.css")

    assert response.status_code == 404
    assert "WWW-Authenticate" not in response.headers


def test_loopback_protected_api_still_requires_session_token(dashboard_app):
    response = dashboard_app("127.0.0.1").get("/api/sessions")

    assert response.status_code == 401


def test_loopback_protected_api_accepts_session_token(dashboard_app):
    response = dashboard_app("127.0.0.1").get(
        "/api/sessions",
        headers={web_server._SESSION_HEADER_NAME: web_server._SESSION_TOKEN},
    )

    assert response.status_code == 200


def test_non_loopback_insecure_bind_still_requires_basic_auth(dashboard_app):
    response = dashboard_app("192.0.2.10").get("/command-center")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")


def test_env_flag_forces_basic_auth_on_loopback(dashboard_app, monkeypatch):
    monkeypatch.setenv("HERMES_DASHBOARD_REQUIRE_BASIC_AUTH", "1")

    response = dashboard_app("127.0.0.1").get("/command-center")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"].startswith("Basic")
