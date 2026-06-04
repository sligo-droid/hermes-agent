from __future__ import annotations


def test_codex_command_execution_adds_bounded_tool_trace(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "hermes"))
    from hermes_cli.discord_worker_state import read_codex_worker_state, record_codex_worker_event

    record_codex_worker_event(
        "task-1",
        board="board-a",
        event={
            "method": "item/started",
            "params": {"item": {"type": "commandExecution", "command": "pnpm ignored"}},
        },
    )

    for idx in range(85):
        record_codex_worker_event(
            "task-1",
            board="board-a",
            event={
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "commandExecution",
                        "command": f"pnpm test {idx}",
                        "status": "completed",
                        "exitCode": 0,
                        "aggregatedOutput": "ok" * 2000,
                    }
                },
            },
        )

    state = read_codex_worker_state("task-1", board="board-a")
    trace = state["tool_trace"]
    assert len(trace) == 80
    assert trace[-1]["source"] == "codex"
    assert trace[-1]["tool"] == "commandExecution"
    assert trace[-1]["command"] == "pnpm test 84"
    assert trace[-1]["status"] == "completed"
    assert trace[-1]["exit_code"] == 0
    assert "...[truncated" in trace[-1]["output"]
    assert state["events"][-1]["item_type"] == "commandExecution"
    assert all(entry["command"] != "pnpm ignored" for entry in trace)


def test_opencode_tool_use_and_bash_events_add_tool_trace(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / "hermes"))
    from hermes_cli.discord_worker_state import read_codex_worker_state, record_codex_worker_event

    record_codex_worker_event(
        "task-2",
        board="board-a",
        event={
            "method": "opencode/tool_use",
            "params": {
                "item": {
                    "type": "tool_use",
                    "name": "bash",
                    "input": {"command": "scripts/run_tests.sh tests/hermes_cli"},
                    "status": "completed",
                    "output": "passed",
                }
            },
        },
    )
    record_codex_worker_event(
        "task-2",
        board="board-a",
        event={
            "method": "opencode/bash",
            "params": {"item": {"type": "bash", "command": "git status", "output": "clean"}},
        },
    )

    trace = read_codex_worker_state("task-2", board="board-a")["tool_trace"]
    assert trace[0] == {
        "ts": trace[0]["ts"],
        "source": "opencode",
        "tool": "bash",
        "status": "completed",
        "command": "scripts/run_tests.sh tests/hermes_cli",
        "output": "passed",
    }
    assert trace[1]["source"] == "opencode"
    assert trace[1]["tool"] == "bash"
    assert trace[1]["command"] == "git status"
