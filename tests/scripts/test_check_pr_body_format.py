from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


def _load_module():
    path = Path(__file__).resolve().parents[2] / "scripts" / "check_pr_body_format.py"
    spec = importlib.util.spec_from_file_location("check_pr_body_format", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_detects_escaped_summary_markdown():
    mod = _load_module()
    body = r"## Summary\n- fixed the bug\n\n## Tests\n- scripts/run_tests.sh"

    assert mod.has_escaped_markdown_newlines(body) is True


def test_detects_bold_summary_heading_with_escaped_newline():
    mod = _load_module()
    body = r"**Summary\n**\n- fixed the bug\n\n**Tests\n**\n- scripts/run_tests.sh"

    assert mod.has_escaped_markdown_newlines(body) is True


def test_allows_normal_multiline_markdown():
    mod = _load_module()
    body = "## Summary\n- fixed the bug\n\n## Tests\n- scripts/run_tests.sh"

    assert mod.has_escaped_markdown_newlines(body) is False


def test_allows_literal_newline_discussion_inside_real_body():
    mod = _load_module()
    body = "## Summary\n- fixed parsing of literal `\\n` text\n\n## Tests\n- scripts/run_tests.sh"

    assert mod.has_escaped_markdown_newlines(body) is False


def test_allows_plain_single_line_text_that_mentions_newline_escape():
    mod = _load_module()
    body = r"Fix parsing of literal `\n-` in user-provided strings."

    assert mod.has_escaped_markdown_newlines(body) is False


def test_loads_body_from_github_event(tmp_path):
    mod = _load_module()
    event_path = tmp_path / "event.json"
    event_path.write_text(
        json.dumps({"pull_request": {"body": r"Summary:\n- escaped body"}}),
        encoding="utf-8",
    )

    assert mod.load_body_from_event(event_path) == r"Summary:\n- escaped body"
    assert mod.main(["--event-path", str(event_path)]) == 1


def test_project_state_requirement_passes_when_operational_change_updates_state():
    mod = _load_module()

    ok, message = mod.check_project_state_requirement(
        "## Summary\n- Update gateway behavior",
        ["gateway/run.py", "docs/project-state.md"],
    )

    assert ok is True
    assert "docs/project-state.md" in message


def test_project_state_requirement_passes_with_not_needed_justification():
    mod = _load_module()
    body = "## Summary\n- Update kanban command\n\nProject-state: not needed - test-only CLI plumbing"

    ok, message = mod.check_project_state_requirement(
        body,
        ["hermes_cli/kanban.py"],
    )

    assert ok is True
    assert "Project-state: not needed" in message


def test_project_state_requirement_rejects_placeholder_not_needed_marker():
    mod = _load_module()
    body = "Project-state: not needed <!-- If applicable, replace this comment with a short reason. -->"

    ok, message = mod.check_project_state_requirement(
        body,
        ["cron/jobs.py"],
    )

    assert ok is False
    assert "Operational PRs must update docs/project-state.md" in message


def test_project_state_requirement_fails_operational_change_without_evidence():
    mod = _load_module()

    ok, message = mod.check_project_state_requirement(
        "## Summary\n- Change gateway runtime behavior",
        ["gateway/run.py"],
    )

    assert ok is False
    assert "Project-state: not needed" in message


@pytest.mark.parametrize(
    "changed_path",
    [
        "web/src/App.tsx",
        "web/src/index.css",
        "web/src/lib/api.ts",
        "web/src/pages/CommandCenterPage.tsx",
    ],
)
def test_project_state_requirement_fails_command_center_dashboard_change_without_evidence(changed_path):
    mod = _load_module()

    ok, message = mod.check_project_state_requirement(
        "## Summary\n- Change Command Center dashboard behavior",
        [changed_path],
    )

    assert ok is False
    assert "Project-state: not needed" in message


@pytest.mark.parametrize(
    "changed_path",
    [
        "web/src/App.tsx",
        "web/src/index.css",
        "web/src/lib/api.ts",
        "web/src/pages/CommandCenterPage.tsx",
    ],
)
def test_project_state_requirement_passes_command_center_dashboard_change_with_not_needed_justification(changed_path):
    mod = _load_module()

    ok, message = mod.check_project_state_requirement(
        "## Summary\n- Refactor Command Center dashboard\n\nProject-state: not needed - no operational behavior changed",
        [changed_path],
    )

    assert ok is True
    assert "Project-state: not needed" in message


def test_project_state_requirement_allows_docs_only_non_operational_change():
    mod = _load_module()

    ok, message = mod.check_project_state_requirement(
        "## Summary\n- Fix docs typo",
        ["docs/usage.md"],
    )

    assert ok is True
    assert "No operational" in message


def test_project_state_requirement_allows_tests_only_change():
    mod = _load_module()

    ok, message = mod.check_project_state_requirement(
        "## Summary\n- Add tests",
        ["tests/gateway/test_run.py"],
    )

    assert ok is True
    assert "No operational" in message


@pytest.mark.parametrize(
    "changed_path",
    [
        "web/src/pages/ModelsPage.tsx",
        "web/src/pages/PluginsPage.tsx",
        "web/src/lib/dashboard-flags.ts",
        "web/src/themes/context.tsx",
    ],
)
def test_project_state_requirement_allows_unrelated_frontend_change(changed_path):
    mod = _load_module()

    ok, message = mod.check_project_state_requirement(
        "## Summary\n- Update unrelated dashboard UI",
        [changed_path],
    )

    assert ok is True
    assert "No operational" in message


def test_main_checks_changed_files_file(tmp_path):
    mod = _load_module()
    changed_files = tmp_path / "changed-files.txt"
    changed_files.write_text("gateway/run.py\n", encoding="utf-8")

    result = mod.main(
        [
            "--body",
            "## Summary\n- Change gateway runtime behavior",
            "--changed-files",
            str(changed_files),
        ]
    )

    assert result == 1
