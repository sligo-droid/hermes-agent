from hermes_cli import web_server


def test_hermes_host_sligo_paths_redirect_before_dashboard_auth():
    from fastapi.testclient import TestClient

    client = TestClient(web_server.app)
    response = client.get(
        "/sligo?proposal=1",
        headers={"host": "hermes.sligolabs.com"},
        follow_redirects=False,
    )

    assert response.status_code == 307
    assert response.headers["location"] == "https://sligo.sligolabs.com/sligo?proposal=1"

    alias_response = client.get(
        "/command-center",
        headers={"host": "hermes.sligolabs.com"},
        follow_redirects=False,
    )
    assert alias_response.status_code == 307
    assert alias_response.headers["location"] == "https://sligo.sligolabs.com/command-center"


def test_sligo_operator_path_matches_sections_and_legacy_worker_urls():
    for path in (
        "/sligo",
        "/sligo/",
        "/sligo/work",
        "/sligo/archive",
        "/command-center",
        "/command-center/",
        "/self-improvement",
        "/self-improvement/",
        "/workers",
        "/workers/session-1",
        "/public/kanban/share-1",
        "/kanban/session-1",
    ):
        assert web_server._is_sligo_operator_path(path)

    assert not web_server._is_sligo_operator_path("/sessions")
    assert not web_server._is_sligo_operator_path("/api/plugins/kanban/self-improvement/proposals")


def test_dashboard_surface_for_host_defaults_to_combined_for_localhost():
    assert web_server.dashboard_surface_for_host("localhost:9119") == "combined"
    assert web_server.dashboard_surface_for_host("127.0.0.1:9119") == "combined"


def test_dashboard_surface_for_host_classifies_sligo_labs_hosts():
    assert web_server.dashboard_surface_for_host("hermes.sligolabs.com") == "hermes"
    assert web_server.dashboard_surface_for_host("sligo.sligolabs.com") == "sligo"


def test_dashboard_surface_for_host_strips_ports_and_case():
    assert web_server.dashboard_surface_for_host("Hermes.SligoLabs.Com:443") == "hermes"
    assert web_server.dashboard_surface_for_host("Sligo.SligoLabs.Com:443") == "sligo"


def test_dashboard_surface_for_host_can_be_overridden(monkeypatch):
    monkeypatch.setenv("HERMES_DASHBOARD_HOST", "classic.example.test")
    monkeypatch.setenv("SLIGO_DASHBOARD_HOST", "operator.example.test")

    assert web_server.dashboard_surface_for_host("classic.example.test") == "hermes"
    assert web_server.dashboard_surface_for_host("operator.example.test") == "sligo"
    assert web_server.dashboard_surface_for_host("hermes.sligolabs.com") == "combined"


def test_sligo_dashboard_url_for_path_uses_default_host():
    assert (
        web_server.sligo_dashboard_url_for_path("/workers/session-1", b"tab=done")
        == "https://sligo.sligolabs.com/workers/session-1?tab=done"
    )


def test_sligo_dashboard_url_for_path_can_be_overridden(monkeypatch):
    monkeypatch.setenv("SLIGO_DASHBOARD_HOST", "operator.example.test")

    assert (
        web_server.sligo_dashboard_url_for_path("/self-improvement")
        == "https://operator.example.test/self-improvement"
    )


def test_spa_bootstrap_injects_host_surface_and_sligo_target(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    dist = tmp_path / "web_dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><head></head><body>shell</body></html>")
    monkeypatch.setattr(web_server, "WEB_DIST", dist)
    monkeypatch.setenv("SLIGO_DASHBOARD_HOST", "operator.example.test")

    app = FastAPI()
    web_server.mount_spa(app)
    response = TestClient(app, base_url="https://operator.example.test").get(
        "/sligo",
        headers={"host": "operator.example.test"},
    )

    assert response.status_code == 200
    assert 'window.__HERMES_DASHBOARD_SURFACE__="sligo"' in response.text
    assert 'window.__HERMES_SLIGO_DASHBOARD_HOST__="operator.example.test"' in response.text
