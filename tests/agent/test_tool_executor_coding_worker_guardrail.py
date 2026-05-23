from __future__ import annotations

from types import SimpleNamespace

from agent.tool_executor import _coding_worker_mutation_block


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
