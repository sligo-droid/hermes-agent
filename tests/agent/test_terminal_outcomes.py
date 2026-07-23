from __future__ import annotations

import json
import subprocess
from pathlib import Path

from agent.terminal_outcomes import (
    classify_terminal_outcome,
    exact_lock_pnpm_install_block,
    inspect_repo_closeout_receipt,
)
from hermes_cli.worktree_runtime import WorktreeRecord


def test_vitest_markdown_import_analysis_is_source_parse_not_install_advice():
    result = classify_terminal_outcome(
        command="pnpm test",
        exit_code=0,
        error=None,
        output=(
            "[plugin:vite:import-analysis] Failed to parse source for import analysis "
            "because the content contains invalid JS syntax. You may need to install "
            "appropriate plugins to handle the .md file format."
        ),
    )

    assert result == {
        "kind": "source_parse",
        "semantic_failure": True,
        "dependency_installation_indicated": False,
        "summary": (
            "Vite/Vitest import analysis could not parse Markdown as source; "
            "dependency installation is not indicated."
        ),
    }


def test_terminal_classifier_distinguishes_common_failure_classes():
    assert classify_terminal_outcome(
        command="python app.py", output="ModuleNotFoundError: No module named 'x'", exit_code=1
    )["kind"] == "dependency_missing"
    assert classify_terminal_outcome(
        command="missing", output="missing: command not found", exit_code=127
    )["kind"] == "command_context"
    assert classify_terminal_outcome(
        command="pytest", output="2 tests failed", exit_code=0
    )["kind"] == "test_failure"
    assert classify_terminal_outcome(
        command="false", output="", exit_code=1
    )["kind"] == "unknown"


def _exact_lock_tree(tmp_path: Path):
    primary = tmp_path / "primary"
    worktree = tmp_path / "worktree"
    for root in (primary, worktree):
        package = root / "ui"
        package.mkdir(parents=True)
        (package / "package.json").write_text('{"name":"ui"}', encoding="utf-8")
        (package / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (primary / "ui" / "node_modules").mkdir()
    (worktree / "ui" / "node_modules").symlink_to(
        primary / "ui" / "node_modules",
        target_is_directory=True,
    )
    return primary, worktree


def test_exact_lock_blocks_nested_pnpm_dir_install(monkeypatch, tmp_path):
    primary, worktree = _exact_lock_tree(tmp_path)
    monkeypatch.setattr(
        "hermes_cli.worktree_runtime.repo_root_for_path",
        lambda path: worktree,
    )
    monkeypatch.setattr(
        "hermes_cli.worktree_runtime.git_worktree_records",
        lambda root: [WorktreeRecord(str(primary)), WorktreeRecord(str(worktree))],
    )

    for command in (
        "pnpm --dir .. install",
        "pnpm -C .. i",
        "pnpm install --dir ..",
    ):
        block = exact_lock_pnpm_install_block(command, worktree / "ui" / "src")
        assert block is not None
        assert block["code"] == "exact_lock_install_blocked"
        assert block["package_root"] == str(worktree / "ui")
        assert "unlink node_modules" in block["remediation"]


def test_exact_lock_allows_non_install_pnpm_commands(monkeypatch, tmp_path):
    primary, worktree = _exact_lock_tree(tmp_path)
    monkeypatch.setattr(
        "hermes_cli.worktree_runtime.repo_root_for_path",
        lambda path: worktree,
    )
    monkeypatch.setattr(
        "hermes_cli.worktree_runtime.git_worktree_records",
        lambda root: [WorktreeRecord(str(primary)), WorktreeRecord(str(worktree))],
    )

    for command in (
        "pnpm exec vitest",
        "pnpm test",
        "pnpm check",
        "pnpm lint",
        "pnpm -C ../ui exec vitest",
    ):
        assert exact_lock_pnpm_install_block(command, worktree / "ui" / "src") is None


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def test_closeout_receipt_accepts_only_clean_tracked_final_script(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes-test@example.invalid")
    script = repo / "scripts" / "closeout.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    _git(repo, "add", "scripts/closeout.sh")
    _git(repo, "commit", "-m", "add closeout")
    head = _git(repo, "rev-parse", "HEAD")
    classification = classify_terminal_outcome(
        command="./scripts/closeout.sh", output="", exit_code=0
    )

    receipt = inspect_repo_closeout_receipt(
        command="echo preparing; ./scripts/closeout.sh",
        cwd=repo,
        exit_code=0,
        classification=classification,
        output=json.dumps(
            {"status": "passed", "head_sha": head, "secret": "drop-me"}
        ),
    )

    assert receipt == {
        "schema_version": 1,
        "status": "passed",
        "head_sha": head,
        "script": "scripts/closeout.sh",
    }
    assert inspect_repo_closeout_receipt(
        command="./scripts/closeout.sh --dry-run",
        cwd=repo,
        exit_code=0,
        classification=classification,
        output=json.dumps({"status": "passed", "head_sha": head}),
    ) is None


def test_closeout_receipt_rejects_dirty_or_mismatched_head(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Hermes Test")
    _git(repo, "config", "user.email", "hermes-test@example.invalid")
    (repo / "closeout").write_text("#!/bin/sh\n", encoding="utf-8")
    _git(repo, "add", "closeout")
    _git(repo, "commit", "-m", "add closeout")
    head = _git(repo, "rev-parse", "HEAD")
    classification = classify_terminal_outcome(
        command="./closeout", output="", exit_code=0
    )

    assert inspect_repo_closeout_receipt(
        command="./closeout",
        cwd=repo,
        exit_code=0,
        classification=classification,
        output=json.dumps({"status": "passed", "head_sha": "0" * 40}),
    ) is None

    (repo / "dirty.txt").write_text("dirty", encoding="utf-8")
    assert inspect_repo_closeout_receipt(
        command="./closeout",
        cwd=repo,
        exit_code=0,
        classification=classification,
        output=json.dumps({"status": "passed", "head_sha": head}),
    ) is None
