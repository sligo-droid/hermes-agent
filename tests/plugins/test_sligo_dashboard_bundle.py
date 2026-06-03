from __future__ import annotations

from pathlib import Path


def test_sligo_dashboard_renders_parse_error_runs_without_proposals():
    bundle = (Path(__file__).resolve().parents[2] / "plugins" / "sligo" / "dashboard" / "dist" / "index.js").read_text(encoding="utf-8")

    assert "parseFailures" in bundle
    assert "Parse failures" in bundle
    assert "Cron proposal parse failures" in bundle
    assert "No proposal cards were created" in bundle
    assert "Parse error" in bundle
    assert "sha256:" in bundle
