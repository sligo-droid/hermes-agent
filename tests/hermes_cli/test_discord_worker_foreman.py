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


def _update_worker_meta(board: str, **updates):
    from hermes_cli import kanban_db

    meta = kanban_db.read_board_metadata(board)
    worker = dict(meta["discord_worker"])
    worker.update(updates)
    meta.pop("db_path", None)
    meta["discord_worker"] = worker
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")
    return worker


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


def _create_done_task(board: str, *, title: str = "Completed task") -> str:
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title=title,
            assignee="dev",
            workspace_kind="scratch",
            initial_status="running",
            board=board,
        )
        assert kanban_db.complete_task(conn, task_id, result="done", summary="done")
        return task_id
    finally:
        conn.close()


def _create_blocked_foreman_task(board: str, *, assignee: str = "reviewer", runtime_failure: bool = True) -> str:
    from hermes_cli import kanban_db

    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Review source recovery",
            assignee=assignee,
            initial_status="running" if runtime_failure else "blocked",
            board=board,
        )
        if runtime_failure:
            assert kanban_db.claim_task(conn, task_id)
            kanban_db._record_spawn_failure(
                conn,
                task_id,
                "reviewer worker crashed before finalizing source board closure",
                failure_limit=1,
            )
        return task_id
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


def _alert_issue(**overrides):
    from hermes_cli.discord_worker_foreman import ForemanIssue

    values = {
        "kind": "worker_errored",
        "board": "discord-123",
        "task_id": "t1",
        "severity": "error",
        "title": "Worker execution failed",
        "evidence": {
            "run_id": 7,
            "run_status": "failed",
            "run_outcome": "crashed",
            "run_error": "boom",
            "session_url": "https://example.test/workers/123",
            "task_status": "blocked",
        },
    }
    values.update(overrides)
    return ForemanIssue(**values)


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
    from hermes_cli import discord_worker_foreman as foreman
    from hermes_cli.discord_worker_foreman import RunSnapshot, TaskSnapshot, detect_stale_running

    assert foreman.STALE_RUNNING_SECONDS == 30

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
        latest_run=RunSnapshot(
            id=7,
            status="running",
            outcome="",
            started_at=10,
            ended_at=None,
            last_heartbeat_at=1000,
            error="still running",
        ),
        sidecar={
            "updated_at": 4900,
            "events": [{"method": "item/completed"}],
            "tool_trace": [
                {"tool": "bash", "command": "token=abc123 /tmp/private"},
                {"tool": "patch", "status": "done"},
            ],
            "result": {"error": "timeout with sk-abcdefghijklmnopqrstuvwxyz1234567890", "timed_out": True},
        },
    )
    fresh = TaskSnapshot(
        id="fresh",
        title="Fresh heartbeat",
        assignee="dev",
        status="running",
        created_at=1,
        started_at=10,
        last_heartbeat_at=4970,
    )
    young_missing = TaskSnapshot(
        id="young-missing",
        title="Young missing heartbeat",
        assignee="dev",
        status="running",
        created_at=4500,
        started_at=4970,
        last_heartbeat_at=None,
    )

    issues = detect_stale_running(
        _snapshot((missing, old, fresh, young_missing)),
        now=5000,
    )

    assert [issue.task_id for issue in issues] == ["missing", "old"]
    old_evidence = next(issue.evidence for issue in issues if issue.task_id == "old")
    assert old_evidence["source_board"] == "discord-123"
    assert old_evidence["source_task_id"] == "old"
    assert old_evidence["source_public_board_url"] == "https://example.test/workers/123"
    assert old_evidence["source_public_ticket_url"].endswith("/tickets/old")
    assert old_evidence["latest_run_id"] == 7
    assert old_evidence["latest_run_status"] == "running"
    assert old_evidence["latest_run_error"] == "still running"
    assert old_evidence["sidecar_updated_at"] == 4900
    assert old_evidence["sidecar_event_count"] == 1
    assert old_evidence["sidecar_timed_out"] is True
    evidence_text = json.dumps(old_evidence)
    assert "sk-abcdefghijklmnopqrstuvwxyz1234567890" not in evidence_text
    assert "/tmp/private" not in evidence_text


def test_coalesce_foreman_issues_suppresses_active_same_source_board(monkeypatch):
    from hermes_cli import discord_worker_foreman as foreman
    from hermes_cli.discord_worker_foreman import (
        BoardSnapshot,
        ForemanIssue,
        TaskSnapshot,
    )

    source = ForemanIssue(
        kind="stale_running",
        board="discord-source",
        task_id="source-task",
        severity="warning",
        title="Running worker has no recent heartbeat",
        evidence={"task_status": "running"},
    )
    human = ForemanIssue(
        kind="human_intervention_required",
        board="discord-source",
        task_id="source-task",
        severity="critical",
        title="Foreman attempt requires human manual intervention",
        evidence={"task_status": "blocked"},
    )
    active_task = TaskSnapshot(
        id="foreman-task",
        title="Resolve source issue",
        assignee="dev",
        status="running",
        created_at=1,
        started_at=2,
        last_heartbeat_at=3,
    )
    active_foreman = BoardSnapshot(
        board="discord-foreman",
        thread_id="foreman-thread",
        chat_id="foreman-thread",
        session_url="https://example.test/workers/discord-foreman",
        thread_state="running",
        run_summary={},
        tasks=(active_task,),
        goal_status="active",
        phase="dev",
        request_text=foreman.render_foreman_goal_prompt(source),
    )

    monkeypatch.setattr(
        foreman,
        "collect_board_snapshots",
        lambda *, foreman_generated_only=False: (
            [active_foreman] if foreman_generated_only else []
        ),
    )

    assert foreman.coalesce_foreman_issues([source]) == []
    assert foreman.coalesce_foreman_issues([human]) == [human]
    assert foreman.coalesce_foreman_issues([source, human]) == [human]


def test_coalesce_foreman_issues_keeps_one_autonomous_issue_per_source(monkeypatch):
    from hermes_cli import discord_worker_foreman as foreman
    from hermes_cli.discord_worker_foreman import ForemanIssue

    warning = ForemanIssue(
        kind="stale_running",
        board="discord-source",
        task_id="stale-task",
        severity="warning",
        title="Running worker has no recent heartbeat",
        evidence={"task_status": "running"},
    )
    error = ForemanIssue(
        kind="worker_errored",
        board="discord-source",
        task_id="failed-task",
        severity="error",
        title="Worker execution failed",
        evidence={"task_status": "blocked"},
    )

    monkeypatch.setattr(foreman, "collect_board_snapshots", lambda **kwargs: [])

    assert foreman.coalesce_foreman_issues([warning, error]) == [error]


def test_create_foreman_master_task_uses_configurable_board_and_reuses(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_db
    from hermes_cli import discord_worker_foreman as foreman

    issue = _alert_issue()

    first = foreman.create_foreman_master_task(issue, master_board="ops", assignee="default")
    second = foreman.create_foreman_master_task(issue, master_board="ops", assignee="default")

    assert first["created"] is True
    assert second["created"] is False
    assert second["task_id"] == first["task_id"]
    assert first["master_board"] == "ops"

    conn = kanban_db.connect(board="ops")
    try:
        tasks = kanban_db.list_tasks(conn, include_archived=False)
    finally:
        conn.close()

    assert len(tasks) == 1
    task = tasks[0]
    assert task.id == first["task_id"]
    assert task.created_by == "discord-worker-foreman"
    assert task.assignee == "default"
    assert task.status == "ready"
    assert task.tenant == "discord-123"
    assert "<foreman-metadata>" in (task.body or "")


def test_create_foreman_master_task_uses_source_worker_worktree(monkeypatch, tmp_path):
    source_board = _discord_board(monkeypatch, tmp_path, slug="discord-123")
    worktree = tmp_path / "workspaces" / "hermes-discord-123"
    _update_worker_meta(source_board, worktree_path=str(worktree), project_path=str(Path(__file__).resolve().parents[2]))
    from hermes_cli import kanban_db
    from hermes_cli import discord_worker_foreman as foreman

    created = foreman.create_foreman_master_task(_alert_issue(), master_board="ops", assignee="default")

    conn = kanban_db.connect(board="ops")
    try:
        task = kanban_db.get_task(conn, created["task_id"])
    finally:
        conn.close()
    assert task is not None
    assert task.workspace_kind == "dir"
    assert task.workspace_path == str(worktree)


def test_create_foreman_master_task_uses_foreman_generated_source_worktree(monkeypatch, tmp_path):
    source_board = _discord_board(monkeypatch, tmp_path, slug="discord-123")
    worktree = tmp_path / "workspaces" / "hermes-foreman-discord-123"
    _update_worker_meta(
        source_board,
        initial_request="Foreman escalation: recover blocked board",
        worktree_path=str(worktree),
        project_path=str(Path(__file__).resolve().parents[2]),
    )
    from hermes_cli import kanban_db
    from hermes_cli import discord_worker_foreman as foreman

    created = foreman.create_foreman_master_task(_alert_issue(), master_board="ops", assignee="default")

    conn = kanban_db.connect(board="ops")
    try:
        task = kanban_db.get_task(conn, created["task_id"])
    finally:
        conn.close()
    assert task is not None
    assert task.workspace_kind == "dir"
    assert task.workspace_path == str(worktree)


def test_create_foreman_master_task_never_uses_runtime_checkout(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_db
    from hermes_cli import discord_worker_foreman as foreman

    issue = _alert_issue(evidence={"source_board": "discord-123", "source_task_id": "t1", "workspace_path": str(Path(__file__).resolve().parents[2])})
    created = foreman.create_foreman_master_task(issue, master_board="ops", assignee="default")

    conn = kanban_db.connect(board="ops")
    try:
        task = kanban_db.get_task(conn, created["task_id"])
    finally:
        conn.close()
    assert task is not None
    assert task.workspace_kind == "scratch"
    assert task.workspace_path is None
    assert foreman.active_master_foreman_source_boards(master_board="ops") == {"discord-123"}


def test_blocked_master_foreman_task_becomes_human_intervention(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import kanban_db
    from hermes_cli import discord_worker_foreman as foreman

    created = foreman.create_foreman_master_task(_alert_issue(), master_board="ops", assignee="default")
    conn = kanban_db.connect(board="ops")
    try:
        assert kanban_db.block_task(
            conn,
            created["task_id"],
            reason="Human must rotate the vendor credential.",
        )
    finally:
        conn.close()

    issues = foreman.collect_human_intervention_issues(
        now=10_000,
        blocked_board_min_age_seconds=0,
        master_board="ops",
    )

    assert [(issue.kind, issue.board, issue.task_id) for issue in issues] == [
        ("human_intervention_required", "discord-123", "t1")
    ]
    assert issues[0].evidence["foreman_board"] == "ops"
    assert issues[0].evidence["foreman_task_id"] == created["task_id"]
    assert "vendor credential" in issues[0].evidence["manual_intervention_reason"]
    assert foreman.active_master_foreman_source_boards(master_board="ops") == set()
    assert foreman.active_master_foreman_source_boards(
        master_board="ops",
        statuses=foreman.FOREMAN_MASTER_TASK_SUPPRESS_STATUSES,
    ) == {"discord-123"}


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


def test_collect_foreman_issues_detects_board_level_blocker_after_min_age(monkeypatch, tmp_path):
    board = _discord_board(monkeypatch, tmp_path)
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_foreman import collect_foreman_issues

    meta = kanban_db.read_board_metadata(board)
    worker = dict(meta["discord_worker"])
    worker.update(
        {
            "goal_status": "blocked",
            "phase": "blocked",
            "blocked_reason": "Need a private Windmill host before deployment can continue.",
            "updated_at": 100,
        }
    )
    meta.pop("db_path", None)
    meta["discord_worker"] = worker
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")

    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Stand up private Windmill execution plane",
            assignee="dev",
            initial_status="blocked",
            board=board,
        )
    finally:
        conn.close()

    issues = collect_foreman_issues(now=701, blocked_board_min_age_seconds=600)

    assert [(issue.kind, issue.board, issue.task_id) for issue in issues] == [
        ("board_stalled", board, task_id)
    ]
    evidence = issues[0].evidence
    assert evidence["board_goal_status"] == "blocked"
    assert evidence["blocked_reason"] == "Need a private Windmill host before deployment can continue."
    assert evidence["blocked_count"] == 1
    assert evidence["running_count"] == 0
    assert evidence["stalled_after_seconds"] == 600


def test_stalled_blocked_board_detector_waits_min_age_and_skips_running():
    from hermes_cli.discord_worker_foreman import (
        BoardSnapshot,
        RunSnapshot,
        TaskSnapshot,
        detect_stalled_blocked_board,
    )

    task = TaskSnapshot(
        id="t1",
        title="Wait for external host",
        assignee="dev",
        status="ready",
        created_at=1,
        started_at=100,
        last_heartbeat_at=None,
        latest_run=RunSnapshot(
            id=7,
            status="blocked",
            outcome="blocked",
            started_at=100,
            ended_at=200,
            last_heartbeat_at=None,
            error="",
        ),
    )
    snapshot = BoardSnapshot(
        board="discord-123",
        thread_id="123",
        chat_id="123",
        session_url="https://example.test/workers/123",
        thread_state="blocked",
        run_summary={},
        tasks=(task,),
        created_at=1,
        updated_at=200,
        goal_status="blocked",
        phase="blocked",
        blocked_reason="Need external host",
    )

    assert detect_stalled_blocked_board(snapshot, now=799, min_age_seconds=600) == []
    assert [issue.kind for issue in detect_stalled_blocked_board(snapshot, now=800, min_age_seconds=600)] == [
        "board_stalled"
    ]

    running = TaskSnapshot(
        id="running",
        title="Worker still active",
        assignee="dev",
        status="running",
        created_at=1,
        started_at=100,
        last_heartbeat_at=750,
    )
    running_snapshot = BoardSnapshot(
        board="discord-123",
        thread_id="123",
        chat_id="123",
        session_url="https://example.test/workers/123",
        thread_state="blocked",
        run_summary={},
        tasks=(running,),
        created_at=1,
        updated_at=100,
        goal_status="blocked",
        phase="blocked",
        blocked_reason="Need external host",
    )

    assert detect_stalled_blocked_board(running_snapshot, now=800, min_age_seconds=600) == []


def test_board_stalled_alerts_are_per_board_and_do_not_repeat_terminal_state(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due, record_alert_sent

    base_evidence = {
        "thread_state": "blocked",
        "task_status": "ready",
        "board_goal_status": "blocked",
        "board_phase": "blocked",
        "blocked_reason": "Need external host",
        "stalled_since": 100,
        "stalled_after_seconds": 600,
        "stalled_age_seconds": 601,
    }
    first = _alert_issue(
        kind="board_stalled",
        board="discord-1",
        task_id="t1",
        severity="warning",
        title="Discord worker board is blocked with no running workers",
        evidence=base_evidence,
    )
    second = _alert_issue(
        kind="board_stalled",
        board="discord-2",
        task_id="t2",
        severity="warning",
        title="Discord worker board is blocked with no running workers",
        evidence=base_evidence,
    )

    assert alerts_due([first, second], now=1000) == [first, second]
    record_alert_sent(first, now=1000)

    older = _alert_issue(
        kind="board_stalled",
        board="discord-1",
        task_id="t1",
        severity="warning",
        title="Discord worker board is blocked with no running workers",
        evidence={**base_evidence, "stalled_age_seconds": 1200},
    )
    assert alerts_due([older], now=2000, config={"cooldown_seconds": 1}) == []


def test_human_intervention_scan_alerts_blocked_foreman_board_without_assessment(monkeypatch, tmp_path):
    board = _discord_board(monkeypatch, tmp_path)
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_foreman import collect_human_intervention_issues, render_foreman_goal_prompt
    from hermes_cli.discord_worker_roles import REVIEW_LOOP_LIMIT_BLOCKED_REASON

    source = _alert_issue(
        kind="board_stalled",
        board="discord-source",
        task_id="source-task",
        severity="warning",
        title="Discord worker board is blocked with no running workers",
        evidence={
            "thread_state": "blocked",
            "task_status": "blocked",
            "blocked_reason": "Need external API credentials.",
        },
    )
    meta = kanban_db.read_board_metadata(board)
    worker = dict(meta["discord_worker"])
    worker.update(
        {
            "initial_request": render_foreman_goal_prompt(source),
            "root_goal": render_foreman_goal_prompt(source),
            "goal_status": "blocked",
            "phase": "blocked",
            "blocked_reason": REVIEW_LOOP_LIMIT_BLOCKED_REASON,
            "updated_at": 100,
        }
    )
    meta.pop("db_path", None)
    meta["discord_worker"] = worker
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")
    conn = kanban_db.connect(board=board)
    try:
        kanban_db.create_task(
            conn,
            title="Resolve foreman escalation",
            assignee="dev",
            initial_status="blocked",
            board=board,
        )
    finally:
        conn.close()

    calls = []

    def assess(issue):
        calls.append(issue.task_id)
        return {
            "requires_manual_intervention": True,
            "reason": "A human must create the API key in the vendor console.",
            "intervention_type": "api_key",
            "instructions": [
                "Open the vendor developer console for the PID project.",
                "Create a read-only API key for the data ingestion job.",
                "Add the key to the Hermes/PID secret store as PID_VENDOR_API_KEY.",
                "Ask Hermes to retry the blocked source task.",
            ],
            "confidence": "high",
        }

    first = collect_human_intervention_issues(
        now=701,
        blocked_board_min_age_seconds=600,
        assessment_fn=assess,
    )
    second = collect_human_intervention_issues(
        now=800,
        blocked_board_min_age_seconds=600,
        assessment_fn=assess,
    )

    assert calls == []
    assert [(issue.kind, issue.board, issue.task_id) for issue in first] == [
        ("human_intervention_required", "discord-source", "source-task")
    ]
    assert [(issue.kind, issue.board, issue.task_id) for issue in second] == [
        ("human_intervention_required", "discord-source", "source-task")
    ]
    assert first[0].evidence["foreman_board"] == board
    assert REVIEW_LOOP_LIMIT_BLOCKED_REASON not in first[0].evidence["manual_intervention_reason"]
    assert "Need external API credentials." in first[0].evidence["manual_intervention_reason"]
    assert first[0].evidence["source_blocked_reason"] == "Need external API credentials."
    assert first[0].evidence["manual_intervention_type"] == "foreman_blocked"
    assert first[0].evidence["manual_intervention_steps"] == [
        "Open the master Kanban recovery task linked in this alert and inspect the blocked task.",
        "Decide whether to retry or reassign the recovery worker, add missing human context, or cancel the attempt.",
        "Reply in Discord with the next action Hermes should take, then ask Hermes to retry the blocked source task if appropriate.",
    ]
    assert first[0].evidence["llm_confidence"] == "high"


def test_human_intervention_scan_records_negative_assessment_once(monkeypatch, tmp_path):
    board = _discord_board(monkeypatch, tmp_path)
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_foreman import collect_human_intervention_issues, render_foreman_goal_prompt

    source = _alert_issue(kind="board_stalled", board="discord-source", task_id="source-task")
    meta = kanban_db.read_board_metadata(board)
    worker = dict(meta["discord_worker"])
    worker.update(
        {
            "initial_request": render_foreman_goal_prompt(source),
            "root_goal": render_foreman_goal_prompt(source),
            "goal_status": "active",
            "phase": "running",
            "blocked_reason": "",
            "updated_at": 100,
        }
    )
    meta.pop("db_path", None)
    meta["discord_worker"] = worker
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")
    conn = kanban_db.connect(board=board)
    try:
        task_id = kanban_db.create_task(
            conn,
            title="Resolve foreman escalation",
            assignee="dev",
            initial_status="running",
            board=board,
        )
        assert kanban_db.claim_task(conn, task_id)
        kanban_db._record_spawn_failure(
            conn,
            task_id,
            "Autonomous retry might still fix this.",
            failure_limit=999,
        )
    finally:
        conn.close()

    calls = []

    def assess(issue):
        calls.append(issue.task_id)
        return {"requires_manual_intervention": False, "reason": "Retry autonomously."}

    assert collect_human_intervention_issues(now=701, blocked_board_min_age_seconds=600, assessment_fn=assess) == []
    assert collect_human_intervention_issues(now=800, blocked_board_min_age_seconds=600, assessment_fn=assess) == []
    assert len(calls) == 1


def _auto_close_fixture(
    monkeypatch,
    tmp_path: Path,
    *,
    suffix="auto",
    source_pr=None,
    foreman_assignee="reviewer",
    runtime_failure=True,
):
    from hermes_cli.discord_worker_foreman import render_foreman_goal_prompt

    source_board = _discord_board(monkeypatch, tmp_path, f"discord-source-{suffix}")
    source_task = _create_done_task(source_board, title="Original source task")
    pr = {
        "pr_url": "https://github.example.test/acme/repo/pull/123",
        "pr_number": "123",
        "pr_state": "MERGED",
        "pr_checks_status": "passed",
        "pr_merge_commit": "abc123def456",
    }
    if source_pr:
        pr.update(source_pr)
    _update_worker_meta(
        source_board,
        goal_status="blocked",
        phase="blocked",
        blocked_reason="Stale blocked metadata after all source tasks completed.",
        **pr,
    )

    source_issue = _alert_issue(
        kind="board_stalled",
        board=source_board,
        task_id=source_task,
        severity="warning",
        title="Source board metadata was stale after merge",
        evidence={"thread_state": "blocked", "task_status": "blocked"},
    )
    foreman_board = _discord_board(monkeypatch, tmp_path, f"foreman-{suffix}")
    prompt = render_foreman_goal_prompt(source_issue)
    _update_worker_meta(
        foreman_board,
        initial_request=prompt,
        root_goal=prompt,
        goal_status="blocked",
        phase="blocked",
        blocked_reason="Reviewer worker crashed while closing the recovery board.",
    )
    foreman_task = _create_blocked_foreman_task(
        foreman_board,
        assignee=foreman_assignee,
        runtime_failure=runtime_failure,
    )
    return source_board, source_task, foreman_board, foreman_task


def test_auto_close_completed_foreman_board_reconciles_stale_source_and_foreman(monkeypatch, tmp_path):
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_boards import board_thread_state
    from hermes_cli.discord_worker_foreman import (
        auto_close_completed_foreman_boards,
        collect_human_intervention_issues,
    )

    source_board, source_task, foreman_board, foreman_task = _auto_close_fixture(monkeypatch, tmp_path)

    closures = auto_close_completed_foreman_boards(now=10_000)

    assert closures == [
        {
            "foreman_board": foreman_board,
            "foreman_task_id": foreman_task,
            "source_board": source_board,
            "source_task_id": source_task,
            "source_issue_kind": "board_stalled",
            "pr_state": "MERGED",
            "pr_checks_status": "passed",
            "pr_merge_commit": "abc123def456",
        }
    ]
    source_worker = kanban_db.read_board_metadata(source_board)["discord_worker"]
    foreman_worker = kanban_db.read_board_metadata(foreman_board)["discord_worker"]
    assert source_worker["goal_status"] == "done"
    assert source_worker["phase"] == "complete"
    assert source_worker["blocked_reason"] == ""
    assert source_worker["terminal_summary_sync_pending"] is True
    assert source_worker["terminal_reaction_sync_pending"] is True
    assert foreman_worker["goal_status"] == "done"
    assert foreman_worker["phase"] == "complete"
    assert foreman_worker["blocked_reason"] == ""
    assert foreman_worker["terminal_summary_sync_pending"] is True
    assert foreman_worker["terminal_reaction_sync_pending"] is True
    conn = kanban_db.connect(board=foreman_board)
    try:
        tasks = {task.id: task for task in kanban_db.list_tasks(conn)}
        latest = kanban_db.latest_run(conn, foreman_task)
    finally:
        conn.close()
    assert tasks[foreman_task].status == "done"
    assert latest is not None
    assert latest.metadata["auto_closed"] is True
    assert latest.metadata["source_board"] == source_board
    assert board_thread_state(source_board) == "done"
    assert board_thread_state(foreman_board) == "done"
    assert collect_human_intervention_issues(now=10_001, blocked_board_min_age_seconds=0) == []
    assert auto_close_completed_foreman_boards(now=10_002) == []


def test_auto_close_completed_foreman_board_skips_active_source_or_blocked_pr(monkeypatch, tmp_path):
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_foreman import auto_close_completed_foreman_boards

    source_board, _, foreman_board, foreman_task = _auto_close_fixture(monkeypatch, tmp_path)
    conn = kanban_db.connect(board=source_board)
    try:
        kanban_db.create_task(
            conn,
            title="Still active source task",
            assignee="dev",
            initial_status="running",
            board=source_board,
        )
    finally:
        conn.close()

    assert auto_close_completed_foreman_boards(now=10_000) == []
    assert kanban_db.read_board_metadata(source_board)["discord_worker"]["goal_status"] == "blocked"
    conn = kanban_db.connect(board=foreman_board)
    try:
        assert {task.id: task.status for task in kanban_db.list_tasks(conn)}[foreman_task] == "blocked"
    finally:
        conn.close()

    source_board, _, foreman_board, foreman_task = _auto_close_fixture(
        monkeypatch,
        tmp_path,
        suffix="blocked-pr",
        source_pr={"pr_state": "OPEN", "pr_checks_status": "failure", "pr_merge_commit": ""},
    )

    assert auto_close_completed_foreman_boards(now=10_001) == []
    assert kanban_db.read_board_metadata(source_board)["discord_worker"]["goal_status"] == "blocked"
    conn = kanban_db.connect(board=foreman_board)
    try:
        assert {task.id: task.status for task in kanban_db.list_tasks(conn)}[foreman_task] == "blocked"
    finally:
        conn.close()


def test_auto_close_completed_foreman_board_skips_non_reviewer_or_non_runtime_blocker(monkeypatch, tmp_path):
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_foreman import auto_close_completed_foreman_boards

    source_board, _, foreman_board, foreman_task = _auto_close_fixture(
        monkeypatch,
        tmp_path,
        foreman_assignee="dev",
    )

    assert auto_close_completed_foreman_boards(now=10_000) == []
    assert kanban_db.read_board_metadata(source_board)["discord_worker"]["goal_status"] == "blocked"
    conn = kanban_db.connect(board=foreman_board)
    try:
        assert {task.id: task.status for task in kanban_db.list_tasks(conn)}[foreman_task] == "blocked"
    finally:
        conn.close()

    source_board, _, foreman_board, foreman_task = _auto_close_fixture(
        monkeypatch,
        tmp_path,
        suffix="non-runtime",
        runtime_failure=False,
    )

    assert auto_close_completed_foreman_boards(now=10_001) == []
    assert kanban_db.read_board_metadata(source_board)["discord_worker"]["goal_status"] == "blocked"
    conn = kanban_db.connect(board=foreman_board)
    try:
        assert {task.id: task.status for task in kanban_db.list_tasks(conn)}[foreman_task] == "blocked"
    finally:
        conn.close()


def test_collect_foreman_issues_reads_board_task_run_and_sidecar(monkeypatch, tmp_path):
    board = _discord_board(monkeypatch, tmp_path)
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_state import write_codex_worker_state
    from hermes_cli.discord_worker_foreman import collect_foreman_issues

    plain_sk_token = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
    task_id = _create_ready_task(board)
    conn = kanban_db.connect(board=board)
    try:
        assert kanban_db.claim_task(conn, task_id)
        kanban_db._record_spawn_failure(
            conn,
            task_id,
            "spawn failed: missing HERMES_DISCORD_WORKER_READ_TOKEN "
            f"token=abc123 {plain_sk_token} /home/user/secret.log",
            failure_limit=1,
        )
    finally:
        conn.close()
    write_codex_worker_state(
        task_id,
        board=board,
        update={
            "result": {
                "error": f"read broker not configured at /tmp/private.log token=abc123 {plain_sk_token}",
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
    assert plain_sk_token not in evidence_text


def test_build_board_snapshot_refreshes_stale_run_summary(monkeypatch, tmp_path):
    board = _discord_board(monkeypatch, tmp_path)
    from hermes_cli import kanban_db
    from hermes_cli import discord_worker_foreman as foreman

    task_id = _create_done_task(board)
    stale_summary = {
        "board": board,
        "generated_at": 1,
        "goal_status": "active",
        "phase": "planning",
        "thread_state": "running",
        "task_counts": {"running": 1},
    }
    refreshed_summary = {
        "board": board,
        "generated_at": 2,
        "goal_status": "done",
        "phase": "complete",
        "thread_state": "done",
        "task_counts": {"done": 1},
    }
    worker = _update_worker_meta(board, goal_status="done", phase="complete", updated_at=2)
    monkeypatch.setattr(foreman, "read_board_run_summary", lambda _board: stale_summary)
    monkeypatch.setattr(foreman, "persist_board_run_summary", lambda _board: refreshed_summary)
    monkeypatch.setattr(foreman, "board_thread_state", lambda _board: "done")

    snapshot = foreman._build_board_snapshot(board, worker)

    assert snapshot.run_summary == refreshed_summary
    assert [task.id for task in snapshot.tasks] == [task_id]


def test_build_board_snapshot_reuses_fresh_run_summary(monkeypatch, tmp_path):
    board = _discord_board(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_foreman as foreman

    _create_done_task(board)
    fresh_summary = {
        "board": board,
        "generated_at": 2,
        "goal_status": "done",
        "phase": "complete",
        "thread_state": "done",
        "task_counts": {"done": 1},
    }
    worker = _update_worker_meta(board, goal_status="done", phase="complete", updated_at=2)
    monkeypatch.setattr(foreman, "read_board_run_summary", lambda _board: fresh_summary)
    monkeypatch.setattr(
        foreman,
        "persist_board_run_summary",
        lambda _board: (_ for _ in ()).throw(AssertionError("fresh summary should be reused")),
    )
    monkeypatch.setattr(foreman, "board_thread_state", lambda _board: "done")

    snapshot = foreman._build_board_snapshot(board, worker)

    assert snapshot.run_summary == fresh_summary


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

    def fake_snapshot(board, worker, *, archived=False, created_at=None):
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


def test_collect_board_snapshots_records_corrupt_open_once_then_skips(
    monkeypatch,
    tmp_path,
    caplog,
):
    import logging
    import sqlite3

    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_foreman as foreman

    board = "discord-corrupt-foreman"
    db_path = tmp_path / "kanban.db"
    db_path.write_text("not sqlite", encoding="utf-8")
    worker = {"kind": "discord_worker_board", "thread_id": "1"}
    incidents = {}
    calls = {"connect": 0, "record": 0}

    monkeypatch.setattr(
        foreman.kanban_db,
        "list_boards",
        lambda include_archived=False: [{"slug": board, "discord_worker": worker}],
    )
    monkeypatch.setattr(foreman.kanban_db, "kanban_db_path", lambda board=None: db_path)
    monkeypatch.setattr(
        foreman.kanban_db,
        "is_board_paused_for_corruption",
        lambda candidate=None: incidents.get(candidate),
    )

    def connect(*args, **kwargs):
        calls["connect"] += 1
        raise sqlite3.DatabaseError("file is not a database")

    def record_incident(candidate, db_path_arg, reason, *, backup_path=None, fingerprint=None):
        calls["record"] += 1
        incident = {
            "pause_reason": "kanban_db_corruption",
            "db_path": str(db_path_arg),
            "fingerprint": fingerprint,
            "quarantine_path": str(backup_path) if backup_path is not None else None,
            "reason": reason,
        }
        incidents[candidate] = incident
        return incident

    monkeypatch.setattr(foreman.kanban_db, "connect", connect)
    monkeypatch.setattr(foreman.kanban_db, "record_corrupt_board_incident", record_incident)

    with caplog.at_level(logging.DEBUG, logger="hermes_cli.discord_worker_foreman"):
        assert foreman.collect_board_snapshots() == []
        assert foreman.collect_board_snapshots() == []

    assert calls == {"connect": 1, "record": 1}
    messages = [record.getMessage() for record in caplog.records]
    assert sum("discord foreman: board discord-corrupt-foreman database corruption incident" in msg for msg in messages) == 1
    assert any("paused for unchanged DB corruption" in msg for msg in messages)
    assert not any(record.exc_info for record in caplog.records)


def test_collect_board_snapshots_skips_foreman_generated_boards(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli import discord_worker_foreman as foreman

    meta = {
        "discord_worker": {
            "kind": "discord_worker_board",
            "thread_id": "1",
            "initial_request": "Foreman escalation: resolve a Discord worker issue.",
        },
    }
    monkeypatch.setattr(foreman.kanban_db, "list_boards", lambda include_archived=False: [dict(meta, slug="discord-1")])

    called = []
    monkeypatch.setattr(foreman, "_build_board_snapshot", lambda *args, **kwargs: called.append(args) or None)

    assert foreman.collect_board_snapshots() == []
    assert called == []


def test_collect_board_snapshots_includes_archived_discord_worker_boards(monkeypatch, tmp_path):
    board = _discord_board(monkeypatch, tmp_path)
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_foreman import collect_board_snapshots

    kanban_db.write_board_metadata(board, archived=True)

    snapshots = collect_board_snapshots()

    assert [snapshot.board for snapshot in snapshots] == [board]
    assert snapshots[0].archived is True


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


def test_alerts_due_first_send_then_suppresses_unchanged_before_cooldown(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due, record_alert_sent

    issue = _alert_issue()

    assert alerts_due([issue], now=1000) == [issue]
    record_alert_sent(issue, now=1000)

    assert alerts_due([issue], now=1200) == []


def test_alerts_due_dedupes_same_worker_problem_within_board(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due, record_alert_sent

    first = _alert_issue(
        board="discord-1",
        task_id="t1",
        evidence={**_alert_issue().evidence, "run_error": "pid 1234 not alive"},
    )
    second = _alert_issue(
        board="discord-1",
        task_id="t2",
        evidence={**_alert_issue().evidence, "run_error": "pid 5678 not alive"},
    )

    assert alerts_due([first, second], now=1000) == [first]
    record_alert_sent(first, now=1000)

    assert alerts_due([second], now=5000, config={"cooldown_seconds": 1}) == []


def test_alerts_due_does_not_suppress_same_worker_problem_on_new_board(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due, record_alert_sent

    evidence = {
        "task_status": "blocked",
        "thread_state": "blocked",
        "sidecar_error": "codex went silent for 90s after a tool result; retiring app-server session.",
        "sidecar_timed_out": False,
    }
    first = _alert_issue(board="discord-1", task_id="t1", evidence=evidence)
    second = _alert_issue(board="discord-2", task_id="t2", evidence=evidence)

    assert alerts_due([first], now=1000) == [first]
    record_alert_sent(first, now=1000)

    assert alerts_due([second], now=5000, config={"cooldown_seconds": 1}) == [second]


def test_human_intervention_alert_suppresses_matching_source_warning(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due, record_alert_sent

    source_warning = _alert_issue(
        kind="board_stalled",
        board="discord-source",
        task_id="__board__",
        severity="warning",
        title="Discord worker board is blocked with no running workers",
        evidence={
            "thread_state": "blocked",
            "task_status": "blocked",
            "blocked_reason": "merge state: DIRTY",
            "stalled_since": 100,
            "stalled_after_seconds": 300,
            "stalled_age_seconds": 600,
        },
    )
    human = _alert_issue(
        kind="human_intervention_required",
        board="discord-source",
        task_id="__board__",
        severity="critical",
        title="Foreman attempt requires human manual intervention",
        evidence={
            "source_board": "discord-source",
            "source_task_id": "__board__",
            "source_issue_kind": "board_stalled",
            "foreman_board": "foreman-abc",
            "manual_intervention_type": "foreman_blocked",
            "thread_state": "blocked",
            "task_status": "blocked",
            "run_ended_at": 500,
        },
    )

    config = {"daily_cap_per_board": 1, "terminal_suppression_age_seconds": 10_000}
    assert alerts_due([source_warning, human], now=900, config=config) == [human]
    record_alert_sent(source_warning, now=1000)

    assert alerts_due([human], now=1100, config=config) == [human]
    record_alert_sent(human, now=1100)
    matching_warning = _alert_issue(
        kind="board_stalled",
        board="discord-source",
        task_id="__board__",
        severity="warning",
        title="Discord worker board is blocked with no running workers",
        evidence={**source_warning.evidence, "stalled_age_seconds": 900},
    )
    other_task_warning = _alert_issue(
        kind="worker_errored",
        board="discord-source",
        task_id="other-source-task",
        severity="error",
        title="Worker execution failed",
        evidence={"task_status": "blocked", "run_error": "different failure"},
    )

    assert alerts_due([matching_warning], now=1200, config={**config, "daily_cap_per_board": 10}) == []
    assert alerts_due([other_task_warning], now=1200, config={**config, "daily_cap_per_board": 10}) == [
        other_task_warning
    ]

    another_human = _alert_issue(
        kind="human_intervention_required",
        board="discord-source",
        task_id="other-source-task",
        severity="critical",
        title="Foreman attempt requires human manual intervention",
        evidence={
            **human.evidence,
            "source_task_id": "other-source-task",
            "foreman_board": "foreman-def",
        },
    )
    assert alerts_due([another_human], now=1200, config=config) == []


def test_human_intervention_alerts_are_prioritized_when_tick_cap_is_reached(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due

    warning = _alert_issue(
        kind="board_stalled",
        board="discord-source-old",
        task_id="__board__",
        severity="warning",
        title="Discord worker board is blocked with no running workers",
        evidence={
            "board_created_at": 100,
            "thread_state": "blocked",
            "task_status": "blocked",
            "blocked_reason": "Needs attention",
            "stalled_since": 200,
            "stalled_after_seconds": 300,
            "stalled_age_seconds": 900,
        },
    )
    older_human = _alert_issue(
        kind="human_intervention_required",
        board="discord-old-human",
        task_id="__board__",
        severity="critical",
        title="Foreman attempt requires human manual intervention",
        evidence={
            "board_created_at": 300,
            "source_board": "discord-old-human",
            "source_task_id": "__board__",
            "foreman_board": "foreman-old",
            "manual_intervention_type": "foreman_blocked",
            "thread_state": "blocked",
            "task_status": "blocked",
            "stalled_since": 400,
            "run_ended_at": 400,
        },
    )
    newer_human = _alert_issue(
        kind="human_intervention_required",
        board="discord-new-human",
        task_id="__board__",
        severity="critical",
        title="Foreman attempt requires human manual intervention",
        evidence={
            "board_created_at": 500,
            "source_board": "discord-new-human",
            "source_task_id": "__board__",
            "foreman_board": "foreman-new",
            "manual_intervention_type": "foreman_blocked",
            "thread_state": "blocked",
            "task_status": "blocked",
            "stalled_since": 600,
            "run_ended_at": 600,
        },
    )

    due = alerts_due(
        [warning, older_human, newer_human],
        now=1000,
        config={"max_alerts_per_tick": 1, "terminal_suppression_age_seconds": 10_000},
    )

    assert due == [newer_human]


def test_alerts_due_skips_boards_before_min_created_at(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due

    old = _alert_issue(task_id="old", evidence={**_alert_issue().evidence, "board_created_at": 900})
    new = _alert_issue(task_id="new", evidence={**_alert_issue().evidence, "board_created_at": 1100})

    assert alerts_due([old, new], now=1200, config={"min_board_created_at": 1000}) == [new]


def test_alerts_due_resends_on_state_change_severity_escalation_and_cooldown(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due, record_alert_sent

    evidence = {**_alert_issue().evidence, "task_status": "running"}
    issue = _alert_issue(severity="warning", evidence=evidence)
    record_alert_sent(issue, now=1000)

    changed = _alert_issue(severity="warning", evidence={**issue.evidence, "run_error": "different"})
    escalated = _alert_issue(severity="error", evidence=evidence)

    assert alerts_due([changed], now=1200) == [changed]
    assert alerts_due([escalated], now=1200) == [escalated]
    assert alerts_due([issue], now=4600) == []


def test_terminal_alerts_do_not_repeat_after_success(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due, record_alert_sent

    issue = _alert_issue(severity="warning")
    record_alert_sent(issue, now=1000)

    changed = _alert_issue(severity="warning", evidence={**issue.evidence, "run_error": "different"})
    escalated = _alert_issue(severity="error")

    config = {"cooldown_seconds": 1, "terminal_suppression_age_seconds": 30 * 24 * 3600}
    assert alerts_due([issue], now=4600, config=config) == []
    assert alerts_due([changed], now=1200, config=config) == []
    assert alerts_due([escalated], now=1200, config=config) == []


def test_archived_board_alerts_do_not_repeat_after_success(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due, record_alert_sent

    evidence = {**_alert_issue().evidence, "board_archived": True, "task_status": "running"}
    issue = _alert_issue(evidence=evidence)
    record_alert_sent(issue, now=1000)

    changed = _alert_issue(evidence={**evidence, "run_error": "different"})
    escalated = _alert_issue(severity="critical", evidence=evidence)

    assert alerts_due([issue], now=4600, config={"cooldown_seconds": 1}) == []
    assert alerts_due([changed], now=1200, config={"cooldown_seconds": 1}) == []
    assert alerts_due([escalated], now=1200, config={"cooldown_seconds": 1}) == []


def test_terminal_thread_state_alerts_do_not_repeat_after_success(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due, record_alert_sent

    for index, thread_state in enumerate(("blocked", "errored", "done", "archived"), start=1):
        issue = _alert_issue(
            kind="stale_running",
            task_id=f"t{index}",
            severity="warning",
            evidence={
                "task_status": "running",
                "thread_state": thread_state,
                "heartbeat_age_seconds": 9999,
            },
        )
        record_alert_sent(issue, now=1000)

        changed = _alert_issue(
            kind="stale_running",
            task_id=f"t{index}",
            severity="warning",
            evidence={**issue.evidence, "heartbeat_age_seconds": 10_000},
        )
        escalated = _alert_issue(
            kind="stale_running",
            task_id=f"t{index}",
            severity="error",
            evidence=issue.evidence,
        )

        assert alerts_due([issue], now=4600, config={"cooldown_seconds": 1}) == []
        assert alerts_due([changed], now=1200, config={"cooldown_seconds": 1}) == []
        assert alerts_due([escalated], now=1200, config={"cooldown_seconds": 1}) == []


def test_active_and_running_thread_state_alerts_resend_only_on_change(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due, record_alert_sent

    for index, thread_state in enumerate(("active", "running"), start=1):
        issue = _alert_issue(
            task_id=f"t{index}",
            severity="warning",
            evidence={**_alert_issue().evidence, "task_status": "running", "thread_state": thread_state},
        )
        record_alert_sent(issue, now=1000)

        changed = _alert_issue(
            task_id=f"t{index}",
            severity="warning",
            evidence={**issue.evidence, "run_error": "different"},
        )
        escalated = _alert_issue(task_id=f"t{index}", severity="error", evidence=issue.evidence)

        assert alerts_due([changed], now=1200, config={"cooldown_seconds": 3600}) == [changed]
        assert alerts_due([escalated], now=1200, config={"cooldown_seconds": 3600}) == [escalated]
        assert alerts_due([issue], now=4600, config={"cooldown_seconds": 1}) == []


def test_old_terminal_alerts_are_suppressed_before_first_send(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due

    old = _alert_issue(evidence={**_alert_issue().evidence, "run_ended_at": 1})
    recent = _alert_issue(task_id="recent", evidence={**_alert_issue().evidence, "run_ended_at": 9_000})

    due = alerts_due([old, recent], now=10_000, config={"terminal_suppression_age_seconds": 3600})

    assert due == [recent]


def test_startup_baseline_suppresses_existing_issue_without_hiding_new_issue(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due, record_startup_baseline, startup_baseline_needed

    historical = _alert_issue(task_id="historical")
    new_issue = _alert_issue(task_id="new")

    assert startup_baseline_needed() is True
    assert record_startup_baseline([historical], now=1000) == 1
    assert startup_baseline_needed() is False

    due = alerts_due([historical, new_issue], now=4600, config={"cooldown_seconds": 1})

    assert due == [new_issue]


def test_sanitize_foreman_text_redacts_common_secret_forms():
    from hermes_cli.discord_worker_foreman import sanitize_foreman_text

    secrets = [
        "Authorization: Bearer abc123",
        "token=abc123",
        "api_key=abc123",
        "sk-abcdefghijklmnopqrstuvwxyz1234567890",
        "sk-proj-abcdefghijk1234567890",
        "ghp_abcdefghijklmnopqrst",
        "xoxb-abcdefghijklmnop",
        "eyJhbGciOiJIUzI1NiJ9.abcdef.abcdefghijklmnopqrstuvwxyz",
    ]

    sanitized = sanitize_foreman_text(" ".join(secrets))

    for secret in secrets:
        assert secret not in sanitized
    assert sanitized.count("[redacted]") == len(secrets)


def test_alert_failed_schedules_retry_without_marking_sent(monkeypatch, tmp_path):
    root = _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due, record_alert_failed

    issue = _alert_issue()

    record_alert_failed(
        issue,
        "send failed Authorization: Bearer abc123 token=abc "
        "sk-proj-abcdefghijk1234567890 ~/.hermes/config.yaml /tmp/private.log",
        now=1000,
    )

    assert alerts_due([issue], now=1200) == []
    assert alerts_due([issue], now=1300) == [issue]
    state = json.loads((root / "kanban" / "foreman-alerts.json").read_text(encoding="utf-8"))
    entry = next(iter(state["alerts"].values()))
    assert entry["last_sent_at"] is None
    assert "Authorization: Bearer abc123" not in entry["last_error"]
    assert "token=abc" not in entry["last_error"]
    assert "sk-proj-abcdefghijk1234567890" not in entry["last_error"]
    assert "~/.hermes/config.yaml" not in entry["last_error"]
    assert "/tmp/private.log" not in entry["last_error"]
    assert entry["last_error"].count("[redacted]") == 3
    assert entry["last_error"].count("[path]") == 2


def test_alert_limits_do_not_mark_overflow_sent_and_daily_cap_suppresses(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    from hermes_cli.discord_worker_foreman import alerts_due, record_alert_sent

    issues = [_alert_issue(task_id=f"t{i}") for i in range(3)]

    due = alerts_due(issues, now=1000, config={"max_alerts_per_tick": 2})

    assert [issue.task_id for issue in due] == ["t0"]
    assert alerts_due([issues[2]], now=1000) == [issues[2]]
    record_alert_sent(issues[0], now=1000)
    assert alerts_due([issues[1]], now=1000, config={"daily_cap_per_board": 1}) == []


def test_alert_retention_gc_removes_old_entries(monkeypatch, tmp_path):
    root = _home(monkeypatch, tmp_path)
    state_path = root / "kanban" / "foreman-alerts.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "version": 1,
                "alerts": {
                    "old": {
                        "first_seen_at": 10,
                        "last_sent_at": None,
                        "last_attempt_at": None,
                        "last_state_key": "",
                        "send_count": 0,
                        "failure_count": 0,
                        "next_retry_at": None,
                        "last_error": "",
                    }
                },
                "daily_counts": {"discord-123:1970-01-01": 1},
            }
        ),
        encoding="utf-8",
    )
    from hermes_cli.discord_worker_foreman import alerts_due

    assert alerts_due([], now=100_000, config={"retention_seconds": 10}) == []

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["alerts"] == {}
    assert state["daily_counts"] == {}


def test_render_foreman_alert_is_safe_bounded_and_informative():
    from hermes_cli.discord_worker_foreman import (
        DISCORD_ALERT_LIMIT,
        FOREMAN_DISCORD_MENTION,
        render_foreman_alert,
    )

    plain_sk_token = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
    issue = _alert_issue(
        title=f"Worker failed with Authorization: Bearer abc123 {plain_sk_token} at ~/.hermes/config.yaml "
        + "x" * 3000,
        evidence={
            "run_id": 42,
            "run_error": f"secret token=abc123 ghp_abcdefghijklmnopqrst {plain_sk_token} at /home/user/log.txt",
            "session_url": "https://example.test/workers/123",
            "final_text": "do not expose",
            "events": ["do not expose"],
            "prompt": "do not expose",
        },
    )

    rendered = render_foreman_alert(issue, mention="@foreman")

    assert "@foreman" in rendered
    assert FOREMAN_DISCORD_MENTION not in rendered
    assert "Board: discord-123" in rendered
    assert "Task: t1" in rendered
    assert "Run: 42" in rendered
    assert "https://example.test/workers/123" in rendered
    assert "Next action:" in rendered
    assert "[redacted]" in rendered
    assert "[path]" in rendered
    assert "final_text" not in rendered
    assert "events" not in rendered
    assert "prompt" not in rendered
    assert "token=abc123" not in rendered
    assert "Authorization: Bearer abc123" not in rendered
    assert "ghp_abcdefghijklmnopqrst" not in rendered
    assert plain_sk_token not in rendered
    assert plain_sk_token not in json.dumps(issue.to_dict())
    assert "~/.hermes/config.yaml" not in rendered
    assert "/home/user" not in rendered
    assert "/tmp/private" not in rendered
    assert len(rendered) <= DISCORD_ALERT_LIMIT
    assert "[truncated]" in rendered


def test_render_human_intervention_alert_includes_explicit_steps():
    from hermes_cli.discord_worker_foreman import (
        ForemanIssue,
        render_foreman_alert,
        render_foreman_human_intervention_embed,
    )

    source_thread_url = (
        "https://discord.com/channels/1502787243230756904/"
        "1509357480361070814/1509374379501289502"
    )

    issue = ForemanIssue(
        kind="human_intervention_required",
        board="discord-source",
        task_id="source-task",
        severity="critical",
        title="Foreman attempt requires human manual intervention",
        evidence={
            "task_status": "blocked",
            "foreman_board": "discord-foreman",
            "source_board": "discord-source",
            "source_task_id": "source-task",
            "source_discord_thread_url": source_thread_url,
            "session_url": "https://example.test/workers/discord-foreman",
            "manual_intervention_reason": "Create a vendor API key.",
            "manual_intervention_type": "api_key",
            "manual_intervention_steps": [
                "Open the vendor developer console.",
                "Create a project-scoped API key with read-only access.",
                "Store it as PID_VENDOR_API_KEY, not in chat.",
                "Tell Hermes to retry source-task.",
            ],
        },
    )

    rendered = render_foreman_alert(issue, mention="<@&admin>")

    assert rendered.startswith("<@&admin>\n**Foreman needs human input**")
    assert f"Source thread: [discord-source/source-task]({source_thread_url})" in rendered
    assert "Source board:" not in rendered
    assert "Why: Create a vendor API key." in rendered
    assert "Foreman attempt: [discord-foreman](https://example.test/workers/discord-foreman)" in rendered
    assert (
        "Next: Open the vendor developer console.; Create a project-scoped API key with read-only access.; "
        "Store it as PID_VENDOR_API_KEY, not in chat.; then ask Hermes to retry `source-task`."
    ) in rendered
    assert "**Why this needs a human**" not in rendered
    assert len(rendered) < 600
    assert "Evidence:" not in rendered
    assert "manual_intervention_steps" not in rendered

    embed = render_foreman_human_intervention_embed(issue)
    fields = {field["name"]: field["value"] for field in embed["fields"]}
    assert embed["title"] == "Foreman needs human input"
    assert fields["Source thread"] == f"[discord-source/source-task]({source_thread_url})"
    assert fields["Why"] == "Create a vendor API key."
    assert "Open the vendor developer console." in fields["Next"]


def test_render_foreman_goal_prompt_is_goal_command_safe_and_informative():
    from hermes_cli.discord_worker_foreman import render_foreman_goal_prompt

    plain_sk_token = "sk-abcdefghijklmnopqrstuvwxyz1234567890"
    issue = _alert_issue(
        title=f"Worker failed with Authorization: Bearer abc123 {plain_sk_token} at ~/.hermes/config.yaml",
        evidence={
            "run_id": 42,
            "run_error": f"secret token=abc123 ghp_abcdefghijklmnopqrst {plain_sk_token} at /home/user/log.txt",
            "session_url": "https://example.test/workers/123",
            "prompt": "do not expose",
        },
    )

    rendered = render_foreman_goal_prompt(issue)

    assert rendered.startswith("/goal Foreman escalation")
    assert "Problem:" in rendered
    assert "Goal:" in rendered
    assert "Evidence:" in rendered
    assert "Board: discord-123" in rendered
    assert "Task: t1" in rendered
    assert "https://example.test/workers/123" in rendered
    assert "Update, retry, unblock, or close" in rendered
    assert "prompt" not in rendered
    assert "Authorization: Bearer abc123" not in rendered
    assert "token=abc123" not in rendered
    assert "ghp_abcdefghijklmnopqrst" not in rendered
    assert plain_sk_token not in rendered
    assert "~/.hermes/config.yaml" not in rendered
    assert "/home/user" not in rendered


def test_foreman_goal_thread_title_names_source_board_and_task():
    from hermes_cli.discord_worker_foreman import foreman_goal_thread_title

    title = foreman_goal_thread_title(
        _alert_issue(
            evidence={
                "source_board": "discord-source",
                "source_task_id": "source-task",
            }
        )
    )

    assert title == "Foreman: fix discord-source/source-task"


def test_render_foreman_alert_uses_fixed_mention_once_in_normal_alert():
    from hermes_cli.discord_worker_foreman import FOREMAN_DISCORD_MENTION, render_foreman_alert

    rendered = render_foreman_alert(_alert_issue())

    assert rendered.count(FOREMAN_DISCORD_MENTION) == 1


def test_render_foreman_alert_uses_supplied_mention_once_in_normal_alert():
    from hermes_cli.discord_worker_foreman import FOREMAN_DISCORD_MENTION, render_foreman_alert

    rendered = render_foreman_alert(_alert_issue(), mention="@foreman")

    assert rendered.count("@foreman") == 1
    assert FOREMAN_DISCORD_MENTION not in rendered


def test_request_scoped_foreman_snapshot_uses_board_slug_url(monkeypatch, tmp_path):
    board = _discord_board(monkeypatch, tmp_path, slug="discord-4243-m-msg-1")
    from hermes_cli import kanban_db
    from hermes_cli.discord_worker_foreman import collect_board_snapshots

    meta = kanban_db.read_board_metadata(board)
    worker = dict(meta["discord_worker"])
    worker["thread_id"] = "4243"
    meta.pop("db_path", None)
    meta["discord_worker"] = worker
    kanban_db.board_metadata_path(board).write_text(json.dumps(meta), encoding="utf-8")

    snapshots = {snapshot.board: snapshot for snapshot in collect_board_snapshots()}

    assert snapshots[board].session_url == "https://example.test/workers/discord-4243-m-msg-1"
