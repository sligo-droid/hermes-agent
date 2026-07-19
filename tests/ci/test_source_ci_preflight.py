"""Focused tests for the dependency-free source-CI preflight."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path


_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "source_ci_preflight.py"
_SPEC = importlib.util.spec_from_file_location("source_ci_preflight", _PATH)
if _SPEC is None or _SPEC.loader is None:
    raise ImportError("Failed to load source_ci_preflight.py")
_MOD = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MOD)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )


def _initial_commit_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Tests")
    (root / "setup.py").write_text("print('install hook')\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[project]\ndependencies = ["unsafe>=1.0"]\n',
        encoding="utf-8",
    )
    _git(root, "add", "setup.py", "pyproject.toml")
    _git(root, "commit", "-m", "initial")
    return root, _git(root, "rev-parse", "HEAD").stdout.strip()


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


def test_initial_push_scans_committed_root_tree_before_install(tmp_path):
    root, head = _initial_commit_repo(tmp_path)

    findings = _MOD.run_preflight(root, base="0" * 40, head=head)

    assert "install-hook file added or modified: setup.py" in findings
    assert "PyPI dependency without an upper bound: unsafe>=1.0" in findings
    assert "could not obtain a complete Git diff for source-CI preflight" not in findings


def test_unreliable_force_push_range_scans_committed_head_tree(tmp_path):
    root, head = _initial_commit_repo(tmp_path)

    findings = _MOD.run_preflight(root, base="f" * 40, head=head)

    assert "install-hook file added or modified: setup.py" in findings
    assert "PyPI dependency without an upper bound: unsafe>=1.0" in findings
    assert "could not obtain a complete Git diff for source-CI preflight" not in findings


def test_unreliable_range_scans_unsafe_earlier_commit_not_only_tip(tmp_path):
    root, _unsafe_head = _initial_commit_repo(tmp_path)
    (root / "README.txt").write_text("innocuous tip\n", encoding="utf-8")
    _git(root, "add", "README.txt")
    _git(root, "commit", "-m", "innocuous last commit")
    head = _git(root, "rev-parse", "HEAD").stdout.strip()

    findings = _MOD.run_preflight(root, base="f" * 40, head=head)

    assert "install-hook file added or modified: setup.py" in findings
    assert "PyPI dependency without an upper bound: unsafe>=1.0" in findings
    assert "could not obtain a complete Git diff for source-CI preflight" not in findings
