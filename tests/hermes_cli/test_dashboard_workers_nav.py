"""Static dashboard tests for the public Workers navigation link."""
from pathlib import Path


def test_workers_nav_link_appears_under_plugins_and_reload_document():
    app_tsx = Path(__file__).resolve().parents[2] / "web" / "src" / "App.tsx"

    content = app_tsx.read_text(encoding="utf-8")

    assert "const WORKERS_NAV_ITEM: NavItem = {" in content
    assert 'path: "/workers"' in content
    assert 'label: "Workers"' in content
    assert "icon: Wrench" in content
    assert "reloadDocument: true" in content
    assert "pluginItems: [WORKERS_NAV_ITEM, ...nav.pluginItems]" in content
    assert "reloadDocument={item.reloadDocument}" in content


def test_operator_default_surface_prioritizes_workers_without_removing_admin_routes():
    app_tsx = Path(__file__).resolve().parents[2] / "web" / "src" / "App.tsx"

    content = app_tsx.read_text(encoding="utf-8")

    assert 'return <Navigate to="/workers" replace />;' in content
    assert '"/sessions": SessionsPage' in content
    assert '"/config": ConfigPage' in content
    assert '"/env": EnvPage' in content
    assert '"/plugins": PluginsPage' in content
    assert 'pluginItems: [WORKERS_NAV_ITEM, ...nav.pluginItems]' in content


def test_worker_console_uses_shared_dashboard_websocket_auth():
    page_tsx = (
        Path(__file__).resolve().parents[2]
        / "web"
        / "src"
        / "pages"
        / "WorkerConsolePage.tsx"
    )

    content = page_tsx.read_text(encoding="utf-8")

    assert "buildWsAuthParam" in content
    assert "new URLSearchParams({ [authParam[0]]: authParam[1] })" in content
    assert "__HERMES_SESSION_TOKEN__" not in content
