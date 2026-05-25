from __future__ import annotations

import importlib.util
import json
from pathlib import Path


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
