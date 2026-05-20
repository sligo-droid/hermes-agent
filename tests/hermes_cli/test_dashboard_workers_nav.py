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
