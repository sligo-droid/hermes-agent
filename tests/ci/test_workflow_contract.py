"""Guard the intentionally small CI check topology."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_source_ci_is_one_always_created_basic_gate():
    source = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")

    assert "name: Basic Tests" in source
    assert "    name: basic" in source
    assert "paths-ignore:" not in source
    assert "paths:" not in source
    assert "group: merge-gate-${{ github.event.pull_request.number || github.ref }}" in source
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in source
    assert "needs:" not in source
    assert "fetch-depth: 0" in source
    assert source.index("Classify changed surfaces") < source.index("Run source-CI preflight")
    assert source.index("Run source-CI preflight") < source.index("Install Python dependencies")
    assert source.index("Run source-CI preflight") < source.index("Install frontend dependencies")
    assert source.count("npm ci") == 1
    assert "npm run --workspace ui-tui typecheck" in source
    assert "npm run --workspace web typecheck" in source
    assert "npm run --workspace apps/bootstrap-installer typecheck" in source
    assert "npm run --workspace apps/desktop typecheck" in source
    assert "npm run --workspace apps/shared typecheck" in source
    assert "npm run --workspace apps/desktop build" in source


def test_legacy_source_ci_workflows_are_removed():
    assert not (WORKFLOWS / "typecheck.yml").exists()
    assert not (WORKFLOWS / "supply-chain-audit.yml").exists()
    assert not (WORKFLOWS / "docs-source-integrity.yml").exists()


def test_pr_body_is_a_separate_trusted_base_gate():
    source = (WORKFLOWS / "pr-body-format.yml").read_text(encoding="utf-8")

    assert "pull_request_target:" in source
    assert "name: pr body" in source
    assert "group: pr-body-${{ github.event.pull_request.number }}" in source
    assert "cancel-in-progress: true" in source
    assert "merge-gate-" not in source
