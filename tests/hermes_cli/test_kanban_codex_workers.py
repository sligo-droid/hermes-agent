from __future__ import annotations

from pathlib import Path

import pytest


def _home(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_CODEX_WORKER_RUNNER", raising=False)
    return root


def _claimed_planner(monkeypatch, tmp_path: Path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_boards as dwb
    from hermes_cli import kanban_db

    board = dwb.set_goal(thread_id="9001", goal="Plan with Codex")
    conn = kanban_db.connect(board=board.slug)
    try:
        task = kanban_db.list_tasks(conn, include_archived=False)[0]
        claimed = kanban_db.claim_task(conn, task.id)
    finally:
        conn.close()
    assert claimed is not None
    return board, claimed


def test_codex_role_worker_defaults_to_host_runner(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers

    board, task = _claimed_planner(monkeypatch, tmp_path)
    workspace = tmp_path / "repo"
    captured = {}

    class Proc:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        captured.update(
            {
                "cmd": cmd,
                "cwd": cwd,
                "env": env,
                "start_new_session": start_new_session,
            }
        )
        stdout.write(b"host worker launched\n")
        stdout.flush()
        return Proc()

    monkeypatch.setattr(workers, "_worker_config", lambda: {"codex_home_root": str(tmp_path / "homes")})
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    pid = workers.spawn_codex_worker(task, str(workspace), board=board.slug)

    assert pid == 4321
    assert captured["cmd"][1:] == ["-m", "hermes_cli.kanban_codex_worker"]
    assert captured["cwd"] == str(workspace.resolve())
    assert captured["env"]["HERMES_CODEX_WORKER_ROLE"] == "planner"
    assert captured["env"]["HERMES_KANBAN_BOARD"] == board.slug
    assert captured["env"]["CODEX_HOME"].endswith("/homes/" + task.id)
    assert captured["start_new_session"] is True


def test_docker_runner_logs_immediate_registry_failure(monkeypatch, tmp_path):
    from hermes_cli import kanban_codex_workers as workers
    from hermes_cli import kanban_db

    board, task = _claimed_planner(monkeypatch, tmp_path)

    class Proc:
        pid = 9876

        def poll(self):
            return 125

    def fake_popen(cmd, cwd, stdout, stderr, env, start_new_session):
        stdout.write(b"docker: error from registry: denied\n")
        stdout.flush()
        return Proc()

    monkeypatch.setattr(
        workers,
        "_worker_config",
        lambda: {
            "runner": "docker",
            "docker_image": "ghcr.io/nousresearch/hermes-codex-worker:latest",
            "codex_home_root": str(tmp_path / "homes"),
        },
    )
    monkeypatch.setattr(workers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(workers.subprocess, "Popen", fake_popen)

    with pytest.raises(RuntimeError, match="exited immediately with code 125"):
        workers.spawn_codex_worker(task, str(tmp_path / "repo"), board=board.slug)

    log = kanban_db.read_worker_log(task.id, board=board.slug)
    assert log is not None
    assert "ghcr.io/nousresearch/hermes-codex-worker:latest" in log
    assert "error from registry: denied" in log
