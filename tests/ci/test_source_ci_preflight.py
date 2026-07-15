"""Focused tests for the dependency-free source-CI preflight."""

from __future__ import annotations

import importlib.util
from pathlib import Path


_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "source_ci_preflight.py"
_SPEC = importlib.util.spec_from_file_location("source_ci_preflight", _PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Failed to load source_ci_preflight.py")
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)


def test_critical_findings_cover_only_high_signal_patterns():
    findings = _MOD.critical_findings(
        ["setup.py", "plugins/risk.pth"],
        """+++ b/file.py
+exec(base64.b64decode(payload))
+subprocess.run(chr(115) + command)
""",
    )

    assert findings == [
        ".pth file added or modified: plugins/risk.pth",
        "install-hook file added or modified: setup.py",
        "base64 decode passed directly to exec/eval",
        "subprocess call with an encoded or obfuscated command",
    ]


def test_critical_findings_ignore_low_signal_source_changes():
    assert _MOD.critical_findings(
        ["agent/tool.py"],
        """+++ b/agent/tool.py
+encoded = base64.b64encode(value)
+subprocess.run([\"git\", \"status\"])
""",
    ) == []


def test_dependency_bounds_reject_unbounded_added_ranges():
    assert _MOD.unbounded_dependency_specs(
        """+++ b/pyproject.toml
+  \"safe>=1.2,<2\",
+  \"unbounded[extra]>=3.0\",
+  \"exact==4.0\",
+  \"package @ git+https://example.test/repo@deadbeef\",
"""
    ) == ["unbounded[extra]>=3.0"]


def test_mcp_catalog_path_detection():
    assert _MOD.is_mcp_catalog_path("optional-mcps/example/manifest.yaml")
    assert _MOD.is_mcp_catalog_path("hermes_cli/mcp_catalog.py")
    assert not _MOD.is_mcp_catalog_path("skills/github/github-pr-workflow/SKILL.md")


def test_preflight_requires_mcp_review_label_for_catalog_changes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        _MOD,
        "_changed_paths",
        lambda *_args: (["optional-mcps/example/manifest.yaml"], True),
    )
    monkeypatch.setattr(_MOD, "_scan_diff", lambda *_args: ("", True))
    monkeypatch.setattr(_MOD, "_pyproject_diff", lambda *_args: ("", True))
    monkeypatch.setattr(_MOD, "_pr_labels", lambda *_args: ([], ""))

    findings = _MOD.run_preflight(tmp_path, base="base", head="head", pr_number="42")

    assert findings == ["MCP catalog changes require the mcp-catalog-reviewed label"]


def test_preflight_fails_loudly_when_the_diff_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(_MOD, "_changed_paths", lambda *_args: ([], False))
    monkeypatch.setattr(_MOD, "_scan_diff", lambda *_args: ("", False))
    monkeypatch.setattr(_MOD, "_pyproject_diff", lambda *_args: ("", False))

    findings = _MOD.run_preflight(tmp_path, base="missing", head="missing")

    assert findings == ["could not obtain a complete Git diff for source-CI preflight"]
