"""Guard the stable parallel CI check topology."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"


def test_source_ci_has_parallel_lanes_and_always_created_basic_gate():
    source = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")

    assert "name: Basic Tests" in source
    assert "  classify:\n    name: classify" in source
    assert "  python:\n    name: python\n    needs: classify" in source
    assert "  frontend:\n    name: frontend\n    needs: classify" in source
    assert "  site:\n    name: site\n    needs: classify" in source
    assert "  basic:\n    name: basic\n    if: always()" in source
    assert "needs: [classify, python, frontend, site]" in source
    assert "paths-ignore:" not in source
    assert "paths:" not in source
    assert "group: merge-gate-${{ github.event.pull_request.number || github.ref }}" in source
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in source
    assert "fetch-depth: 0" not in source
    assert source.count("fetch-depth: 1") == 4
    assert "scripts/ci/resolve_changed_range.py" in source
    assert "--unshallow" not in source
    assert source.index("Resolve bounded changed range") < source.index("Classify changed surfaces")
    assert source.index("Classify changed surfaces") < source.index("Run defense-in-depth source preflight")
    assert source.index("Run defense-in-depth source preflight") < source.index("  python:\n")
    assert source.index("Run defense-in-depth source preflight") < source.index("  frontend:\n")
    assert source.index("Run defense-in-depth source preflight") < source.index("  site:\n")
    assert "Verify conditional lane results" in source
    assert '[[ "$CLASSIFY_RESULT" == "success" ]]' in source
    assert 'elif [[ "$required" == "false" ]]; then' in source
    assert '[[ "$result" == "skipped" || "$result" == "success" ]]' in source
    assert 'classifier output was invalid: ${required:-<empty>}' in source


def test_source_ci_keeps_expected_lane_commands():
    source = (WORKFLOWS / "tests.yml").read_text(encoding="utf-8")

    assert source.count("npm ci") == 1
    assert "npm run --workspace ui-tui typecheck" in source
    assert "npm run --workspace web typecheck" in source
    assert "npm run --workspace apps/bootstrap-installer typecheck" in source
    assert "npm run --workspace apps/desktop typecheck" in source
    assert "npm run --workspace apps/shared typecheck" in source
    assert "npm run --workspace apps/desktop build" in source
    assert "scripts/run_tests.sh --smoke" in source
    assert "website/scripts/generate-skill-docs.py --check" in source


def test_legacy_source_ci_workflows_are_removed():
    assert not (WORKFLOWS / "typecheck.yml").exists()
    assert not (WORKFLOWS / "supply-chain-audit.yml").exists()
    assert not (WORKFLOWS / "docs-source-integrity.yml").exists()


def test_js_autofix_install_is_self_contained_and_retried():
    source = (WORKFLOWS / "js-autofix.yml").read_text(encoding="utf-8")

    assert "uses: ./.github/actions/retry" not in source
    assert "for attempt in 1 2 3" in source
    assert "npm ci --ignore-scripts" in source
    assert "sleep 10" in source


def test_js_autofix_only_opens_draft_prs_and_never_merges():
    source = (WORKFLOWS / "js-autofix.yml").read_text(encoding="utf-8")

    assert "gh pr create" in source
    assert "--draft" in source
    assert "gh pr merge" not in source
    assert "gh pr ready" not in source


def test_pr_body_is_a_separate_trusted_base_gate():
    source = (WORKFLOWS / "pr-body-format.yml").read_text(encoding="utf-8")

    assert "pull_request_target:" in source
    assert "name: pr body" in source
    assert "group: pr-body-${{ github.event.pull_request.number }}" in source
    assert "cancel-in-progress: true" in source
    assert "merge-gate-" not in source
    assert "ref: ${{ github.event.pull_request.base.sha }}" in source
    assert "persist-credentials: false" in source
    assert "Fetch exact PR commits without checkout" in source
    assert "git fetch --no-tags --depth=256 origin" in source
    assert "+refs/pull/${PR_NUMBER}/head:refs/remotes/pull/${PR_NUMBER}/head" in source
    assert '[[ "$(git rev-parse HEAD^{commit})" == "$BASE_SHA" ]]' in source
    assert '[[ "$resolved_head" == "$HEAD_SHA" ]]' in source
    assert "Run trusted-base source-CI preflight" in source
    assert "python scripts/ci/source_ci_preflight.py" in source
    assert "uv pip install" not in source
    assert "npm ci" not in source
    assert source.index("Checkout trusted base code") < source.index(
        "Fetch exact PR commits without checkout"
    )
    assert source.index("Fetch exact PR commits without checkout") < source.index(
        "Run trusted-base source-CI preflight"
    )
    assert source.index("Run trusted-base source-CI preflight") < source.index(
        "Check PR body Markdown and project-state hygiene"
    )
