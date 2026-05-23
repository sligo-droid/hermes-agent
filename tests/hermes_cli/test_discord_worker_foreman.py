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

    def fake_snapshot(board, worker, *, archived=False):
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
    assert alerts_due([issue], now=4600) == [issue]


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


def test_active_and_running_thread_state_alerts_still_repeat(monkeypatch, tmp_path):
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
        assert alerts_due([issue], now=4600, config={"cooldown_seconds": 1}) == [issue]


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

    assert [issue.task_id for issue in due] == ["t0", "t1"]
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

    assert FOREMAN_DISCORD_MENTION in rendered
    assert rendered.count(FOREMAN_DISCORD_MENTION) == 1
    assert "@foreman" not in rendered
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


def test_render_foreman_alert_uses_fixed_mention_once_in_normal_alert():
    from hermes_cli.discord_worker_foreman import FOREMAN_DISCORD_MENTION, render_foreman_alert

    rendered = render_foreman_alert(_alert_issue(), mention="@foreman")

    assert rendered.count(FOREMAN_DISCORD_MENTION) == 1
    assert "@foreman" not in rendered
