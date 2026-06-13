import sqlite3
from datetime import datetime, timezone

from cron.jobs import audit_overdue_self_improvement_proposals


NOW = datetime(2026, 6, 13, 12, 30, tzinfo=timezone.utc)


def _proposal_job(job_id, **overrides):
    job = {
        "id": job_id,
        "name": f"proposal {job_id}",
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "interval", "minutes": 1440},
        "schedule_display": "every 1440m",
        "next_run_at": "2026-06-12T06:00:00+00:00",
        "last_run_at": "2026-06-11T06:00:00+00:00",
        "self_improvement_proposal": {"project": "hermes", "prong": "daily-retrospective"},
    }
    job.update(overrides)
    return job


def test_overdue_proposal_audit_reports_only_enabled_overdue_jobs(tmp_path):
    output_dir = tmp_path / "output"
    job_dir = output_dir / "overdue"
    job_dir.mkdir(parents=True)
    last_output = job_dir / "2026-06-11_06-00-00.md"
    last_output.write_text("previous proposal", encoding="utf-8")

    jobs = [
        _proposal_job("overdue"),
        _proposal_job("paused", state="paused"),
        _proposal_job("disabled", enabled=False),
        _proposal_job("fresh", next_run_at="2026-06-13T12:00:00+00:00"),
        _proposal_job("no-agent", no_agent=True),
        {
            "id": "ordinary",
            "name": "ordinary cron",
            "enabled": True,
            "state": "scheduled",
            "schedule": {"kind": "interval", "minutes": 1440},
            "next_run_at": "2026-06-12T06:00:00+00:00",
        },
    ]

    findings = audit_overdue_self_improvement_proposals(
        jobs=jobs,
        now=NOW,
        output_root=output_dir,
        log_root=tmp_path / "missing-logs",
        session_db_path=tmp_path / "missing-state.db",
    )

    assert [finding["job_id"] for finding in findings] == ["overdue"]
    finding = findings[0]
    assert finding["job_name"] == "proposal overdue"
    assert finding["expected_missed_slot"] == "2026-06-12T06:00:00+00:00"
    assert finding["next_run_at"] == "2026-06-12T06:00:00+00:00"
    assert finding["last_run_at"] == "2026-06-11T06:00:00+00:00"
    assert finding["last_output_path"] == str(last_output)
    assert finding["session"]["session_id"] is None
    assert finding["session"]["stale_open_session"] is False
    assert finding["session"]["available"] is False
    assert "session database not found" in finding["session"]["unavailable_reason"]
    assert finding["scheduler_logs"]["excerpts"] == []
    assert finding["scheduler_logs"]["available"] is False
    assert "log directory not found" in finding["scheduler_logs"]["unavailable_reason"]


def test_overdue_proposal_audit_collects_session_and_log_evidence(tmp_path):
    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(state_db)
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL NOT NULL, ended_at REAL)"
    )
    conn.execute(
        "INSERT INTO sessions (id, started_at, ended_at) VALUES (?, ?, ?)",
        ("cron_overdue_20260612_060000", NOW.timestamp() - 8 * 60 * 60, None),
    )
    conn.commit()
    conn.close()

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "agent.log").write_text(
        "2026-06-12 cron.jobs: Job 'proposal overdue' missed its scheduled time "
        "(2026-06-12T06:00:00+00:00, grace=7200s). Fast-forwarding to next run\n",
        encoding="utf-8",
    )

    findings = audit_overdue_self_improvement_proposals(
        jobs=[_proposal_job("overdue")],
        now=NOW,
        output_root=tmp_path / "output",
        log_root=log_dir,
        session_db_path=state_db,
    )

    assert len(findings) == 1
    finding = findings[0]
    assert finding["last_output_path"] is None
    assert finding["session"]["available"] is True
    assert finding["session"]["session_id"] == "cron_overdue_20260612_060000"
    assert finding["session"]["stale_open_session"] is True
    assert finding["scheduler_logs"]["available"] is True
    assert finding["scheduler_logs"]["unavailable_reason"] is None
    assert len(finding["scheduler_logs"]["excerpts"]) == 1
    assert "Fast-forwarding to next run" in finding["scheduler_logs"]["excerpts"][0]
