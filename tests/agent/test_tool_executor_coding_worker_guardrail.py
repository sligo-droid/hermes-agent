from __future__ import annotations

import json
from types import SimpleNamespace

from agent import tool_executor as te
from agent.tool_executor import _coding_worker_mutation_block, _coding_worker_result_attempted


def _agent(**overrides):
    data = {
        "api_mode": "chat_completions",
        "_coding_worker_required_this_turn": True,
        "_coding_worker_used_this_turn": False,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_coding_worker_guardrail_blocks_mutating_terminal_before_delegate():
    message = _coding_worker_mutation_block(
        _agent(),
        "terminal",
        {"command": "apply_patch <<'PATCH'\n*** Begin Patch\n*** End Patch\nPATCH"},
    )

    assert message is not None
    assert "delegate_coding_task" in message


def test_coding_worker_guardrail_still_blocks_mutation_if_exposed_before_delegate():
    message = _coding_worker_mutation_block(
        _agent(),
        "write_file",
        {"path": "/tmp/hermes.py", "content": "edited"},
    )

    assert message is not None
    assert "delegate_coding_task" in message


def test_coding_worker_guardrail_does_not_block_non_hermes_turn_mutation():
    assert _coding_worker_mutation_block(
        _agent(_coding_worker_required_this_turn=False),
        "write_file",
        {"path": "/tmp/app.py", "content": "edited"},
    ) is None


def test_coding_worker_guardrail_allows_read_only_terminal_before_delegate():
    assert _coding_worker_mutation_block(
        _agent(),
        "terminal",
        {"command": "git status --short --branch"},
    ) is None


def test_coding_worker_guardrail_allows_terminal_after_delegate():
    assert _coding_worker_mutation_block(
        _agent(_coding_worker_used_this_turn=True),
        "terminal",
        {"command": "git add . && git commit -m fix"},
    ) is None


def test_coding_worker_guardrail_allows_user_systemd_service_write(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(te, "_known_hermes_roots_for_guard", lambda: (tmp_path / "hermes",))

    assert _coding_worker_mutation_block(
        _agent(),
        "write_file",
        {"path": "~/.config/systemd/user/qmd-pid.service", "content": "[Unit]\nDescription=QMD\n"},
    ) is None


def test_coding_worker_guardrail_allows_user_systemd_service_replace_patch(monkeypatch, tmp_path):
    home = tmp_path / "home"
    service = home / ".config" / "systemd" / "user" / "qmd-pid.service"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(te, "_known_hermes_roots_for_guard", lambda: (tmp_path / "hermes",))

    assert _coding_worker_mutation_block(
        _agent(),
        "patch",
        {"mode": "replace", "path": str(service), "old_string": "old", "new_string": "new"},
    ) is None


def test_coding_worker_guardrail_still_blocks_repo_service_file(monkeypatch, tmp_path):
    home = tmp_path / "home"
    hermes_root = tmp_path / "hermes"
    service = hermes_root / "packaging" / "qmd-pid.service"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(te, "_known_hermes_roots_for_guard", lambda: (hermes_root,))

    message = _coding_worker_mutation_block(
        _agent(),
        "write_file",
        {"path": str(service), "content": "[Unit]\nDescription=QMD\n"},
    )

    assert message is not None
    assert "delegate_coding_task" in message


def test_coding_worker_success_with_null_error_counts_as_attempt():
    result = '{"success": true, "status": "completed", "summary": "ok", "error": null}'

    assert _coding_worker_result_attempted(result) is True


def test_coding_worker_partial_result_counts_as_attempt_and_unblocks():
    result = '{"success": false, "status": "partial", "summary": "edited", "error": "timeout"}'
    agent = _agent()

    if _coding_worker_result_attempted(result):
        agent._coding_worker_used_this_turn = True

    assert _coding_worker_mutation_block(
        agent,
        "terminal",
        {"command": "git add . && git commit -m fix"},
    ) is None


def test_coding_worker_plain_string_result_counts_as_attempt():
    assert _coding_worker_result_attempted("worker returned an error") is True
    assert _coding_worker_result_attempted({"success": False}) is False


def test_delegate_preflight_suppresses_empty_probe_without_attempt(monkeypatch):
    agent = _agent(session_cwd="/tmp")

    args, result = te._preflight_delegate_coding_task({"task": "", "context": ""}, agent)

    assert args["task"] == ""
    assert json.loads(result)["error"] == "delegate_coding_task requires a non-empty task."
    assert agent._coding_worker_used_this_turn is False


def test_delegate_preflight_repairs_required_canonical_cwd(monkeypatch, tmp_path):
    canonical = tmp_path / "hermes"
    worktree = tmp_path / "workspaces" / "hermes"
    canonical.mkdir()
    worktree.mkdir(parents=True)
    agent = _agent(session_cwd=str(canonical))

    monkeypatch.setattr(
        "tools.coding_worker_tool._mutable_worktree_for_canonical_cwd",
        lambda workdir: str(worktree),
    )
    monkeypatch.setattr(
        "tools.canonical_repo_guard.canonical_main_worker_violation",
        lambda workdir: "BLOCKED: delegate_coding_task was pointed at a protected canonical checkout",
    )

    args, result = te._preflight_delegate_coding_task({"task": "fix bug", "cwd": str(canonical)}, agent)

    assert result is None
    assert args["cwd"] == str(worktree)
    assert "routing repair" in args["context"]


def test_delegate_preflight_keeps_real_invalid_canonical_call_visible(monkeypatch, tmp_path):
    canonical = tmp_path / "hermes"
    canonical.mkdir()
    agent = _agent(_coding_worker_required_this_turn=False, session_cwd=str(canonical))
    monkeypatch.setattr(
        "tools.canonical_repo_guard.canonical_main_worker_violation",
        lambda workdir: "BLOCKED: delegate_coding_task was pointed at a protected canonical checkout",
    )

    args, result = te._preflight_delegate_coding_task({"task": "fix bug", "cwd": str(canonical)}, agent)

    assert result is None
    assert args["cwd"] == str(canonical)
