from __future__ import annotations

from types import SimpleNamespace

from agent import tool_executor as te
from agent.tool_executor import _coding_worker_mutation_block, _coding_worker_result_succeeded


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


def test_coding_worker_success_with_null_error_counts_as_success():
    result = '{"success": true, "status": "completed", "summary": "ok", "error": null}'

    assert _coding_worker_result_succeeded(result) is True
