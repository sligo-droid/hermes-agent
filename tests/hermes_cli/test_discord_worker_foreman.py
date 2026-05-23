from __future__ import annotations

import argparse
import json
from pathlib import Path


def _home(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HERMES_PUBLIC_KANBAN_BASE_URL", "https://example.test")
    return root


def _discord_board(monkeypatch, tmp_path: Path, slug: str = "discord-123") -> str:
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_db

    kanban_db.create_board(slug)
    meta_path = kanban_db.board_metadata_path(slug)
    meta = kanban_db.read_board_metadata(slug)
    meta.pop("db_path", None)
    meta["discord_worker"] = {
        "kind": "discord_worker_board",
        "thread_id": slug.removeprefix("discord-"),
        "chat_id": slug.removeprefix("discord-"),
        "goal_status": "active",
    }
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    kanban_db.init_db(board=slug)
    return slug


def _create_ready_task(board: str, *, title: str = "Worker task") -> str:
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=board)
    try:
        return kanban_db.create_task(
            conn,
            title=title,
            assignee="dev",
            workspace_kind="scratch",
            initial_status="running",
            board=board,
        )
    finally:
        conn.close()


def _snapshot(task, *, sidecar=None):
    from hermes_cli.discord_worker_foreman import BoardSnapshot

    return BoardSnapshot(
        board="discord-123",
        thread_id="123",
        chat_id="123",
        session_url="https://example.test/workers/123",
        thread_state="running",
        run_summary={},
        tasks=(task if isinstance(task, tuple) else (task,)),
    )


def test_worker_errored_detector_uses_latest_failed_run_only():
    from hermes_cli.discord_worker_foreman import (
        RunSnapshot,
        TaskSnapshot,
        detect_worker_errored,
    )

    task = TaskSnapshot(
        id="t1",
        title="Task",
        assignee="dev",
        status="blocked",
        created_at=1,
        started_at=2,
        last_heartbeat_at=None,
        latest_run=RunSnapshot(
            id=7,
            status="spawn_failed",
            outcome="spawn_failed",
            started_at=2,
            ended_at=3,
            last_heartbeat_at=None,
            error="cannot spawn worker",
        ),
    )

    issues = detect_worker_errored(_snapshot(task))

    assert [issue.kind for issue in issues] == ["worker_errored"]
    assert issues[0].evidence["run_outcome"] == "spawn_failed"
    assert issues[0].evidence["run_error"] == "cannot spawn worker"


def test_stale_running_detector_flags_missing_and_old_heartbeat():
    from hermes_cli.discord_worker_foreman import TaskSnapshot, detect_stale_running


    missing = TaskSnapshot(
        id="missing",
        title="Missing heartbeat",
        assignee="dev",
        status="running",
        created_at=1,
        started_at=10,
        last_heartbeat_at=None,
    )
    old = TaskSnapshot(
        id="old",
        title="Old heartbeat",
        assignee="dev",
        status="running",
        created_at=1,
        started_at=10,
        last_heartbeat_at=1000,
    )
    fresh = TaskSnapshot(
        id="fresh",
        title="Fresh heartbeat",
        assignee="dev",
        status="running",
        created_at=1,
        started_at=10,
        last_heartbeat_at=4500,
    )

    issues = detect_stale_running(_snapshot((missing, old, fresh)), now=5000)

    assert [issue.task_id for issue in issues] == ["missing", "old"]


def test_missing_read_broker_detector_matches_allowlisted_errors():
    from hermes_cli.discord_worker_foreman import TaskSnapshot, detect_missing_read_broker

    task = TaskSnapshot(
        id="t1",
        title="Task",
        assignee="dev",
        status="blocked",
        created_at=1,
        started_at=2,
        last_heartbeat_at=None,
        last_failure_error="HERMES_DISCORD_WORKER_READ_URL is required",
    )

    issues = detect_missing_read_broker(_snapshot(task))


    assert [issue.kind for issue in issues] == ["missing_read_broker"]
    assert "HERMES_DISCORD_WORKER_READ_URL" in issues[0].evidence["error_excerpt"]


def test_collect_foreman_issues_reads_board_task_run_and_sidecar(monkeypatch, tmp_path):
    board = _discord_board(monkeypatch, tmp_path)
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_state import write_codex_worker_state
    from hermes_cli.discord_worker_foreman import collect_foreman_issues

    task_id = _create_ready_task(board)
    conn = kanban_db.connect(board=board)
    try:
        assert kanban_db.claim_task(conn, task_id)
        kanban_db._record_spawn_failure(
            conn,
            task_id,
            "spawn failed: missing HERMES_DISCORD_WORKER_READ_TOKEN token=abc123 /home/user/secret.log",
            failure_limit=1,
        )
    finally:
        conn.close()
    write_codex_worker_state(
        task_id,
        board=board,
        update={
            "result": {
                "error": "read broker not configured at /tmp/private.log token=abc123",
                "final_text": "do not expose",
                "exit_code": 2,
                "run_profile": {"env": {"TOKEN": "abc"}},
            }
        },
    )

    issues = collect_foreman_issues(now=10_000)
    payload = [issue.to_dict() for issue in issues]

    assert {issue["kind"] for issue in payload} >= {"worker_errored", "missing_read_broker"}
    evidence_text = json.dumps(payload)
    assert "final_text" not in evidence_text
    assert "run_profile" not in evidence_text
    assert "/home/user" not in evidence_text
    assert "/tmp/private" not in evidence_text
    assert "token=abc123" not in evidence_text


def test_duplicate_resolved_db_paths_are_scanned_once(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_foreman as foreman

    seen = []
    same_path = tmp_path / "same" / "kanban.db"
    meta = {
        "discord_worker": {"kind": "discord_worker_board", "thread_id": "1"},
    }

    monkeypatch.setattr(
        foreman.kanban_db,
        "list_boards",
        lambda include_archived=False: [dict(meta, slug="discord-1"), dict(meta, slug="discord-2")],
    )
    monkeypatch.setattr(foreman.kanban_db, "kanban_db_path", lambda board=None: same_path)

    def fake_snapshot(board, worker):
        seen.append(board)
        return foreman.BoardSnapshot(
            board=board,
            thread_id=str(worker.get("thread_id") or ""),
            chat_id="",
            session_url="",
            thread_state="active",
            run_summary={},
            tasks=(),
        )

    monkeypatch.setattr(foreman, "_build_board_snapshot", fake_snapshot)

    snapshots = foreman.collect_board_snapshots()

    assert [snapshot.board for snapshot in snapshots] == ["discord-1"]
    assert seen == ["discord-1"]


def test_foreman_scan_json_cli_smoke(monkeypatch, tmp_path, capsys):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban as kanban_cli

    parser = argparse.ArgumentParser()
    subp = parser.add_subparsers(dest="command")
    kanban_cli.build_parser(subp)
    ns = parser.parse_args(["kanban", "foreman", "scan", "--json"])

    assert kanban_cli.kanban_command(ns) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"count": 0, "issues": []}
