from datetime import datetime, timedelta, timezone

from cron.jobs import detect_missed_cron_runs, save_jobs


def _job(job_id, next_run_at, *, now, **overrides):
    job = {
        "id": job_id,
        "name": f"Job {job_id}",
        "prompt": "status",
        "schedule": {"kind": "interval", "minutes": 60, "display": "every 60m"},
        "schedule_display": "every 60m",
        "repeat": {"times": None, "completed": 0},
        "enabled": True,
        "state": "scheduled",
        "paused_at": None,
        "paused_reason": None,
        "created_at": (now - timedelta(days=1)).isoformat(),
        "next_run_at": next_run_at,
        "last_run_at": None,
        "last_status": None,
        "last_error": None,
        "deliver": "local",
        "origin": None,
    }
    job.update(overrides)
    return job


def test_detect_missed_cron_runs_reports_enabled_overdue_job(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    output_dir = tmp_path / "cron" / "output" / "overdue"
    output_dir.mkdir(parents=True)
    output_path = output_dir / "2026-07-06_11-00-00.md"
    output_path.write_text("previous output", encoding="utf-8")

    save_jobs([
        _job(
            "overdue",
            (now - timedelta(minutes=20)).isoformat(),
            now=now,
            last_run_at=(now - timedelta(hours=2)).isoformat(),
            last_status="error",
        )
    ])

    status = detect_missed_cron_runs(now=now, grace_seconds=300)

    assert status["missed_count"] == 1
    assert status["grace_seconds"] == 300
    assert status["offenders"] == [
        {
            "job_id": "overdue",
            "job_name": "Job overdue",
            "last_run_at": (now - timedelta(hours=2)).isoformat(),
            "next_run_at": (now - timedelta(minutes=20)).isoformat(),
            "overdue_seconds": 1200,
            "overdue_age": "20m",
            "last_status": "error",
            "last_output_path": str(output_path),
        }
    ]


def test_detect_missed_cron_runs_excludes_non_actionable_jobs():
    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(minutes=20)).isoformat()
    jobs = [
        _job("disabled", old, now=now, enabled=False),
        _job("paused", old, now=now, state="paused"),
        _job("completed", old, now=now, state="completed"),
        _job("future", (now + timedelta(minutes=5)).isoformat(), now=now),
        _job("within-grace", (now - timedelta(minutes=2)).isoformat(), now=now),
        _job("one-shot-cleared", None, now=now, schedule={"kind": "once", "run_at": old}),
        _job("created-after-slot", old, now=now, created_at=(now - timedelta(minutes=10)).isoformat()),
        _job("manual-queued", old, now=now, manual_run={"state": "queued"}),
        _job("manual-running", old, now=now, manual_run={"state": "running"}),
        _job("claimed", old, now=now, fire_claim={"claimed_at": (now - timedelta(minutes=1)).isoformat()}),
        _job("already-rescheduled", (now + timedelta(hours=1)).isoformat(), now=now),
    ]

    status = detect_missed_cron_runs(jobs=jobs, now=now, grace_seconds=300)

    assert status["missed_count"] == 0
    assert status["offenders"] == []


def test_detect_missed_cron_runs_reports_expired_claims():
    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(minutes=20)).isoformat()
    jobs = [
        _job(
            "expired-claim",
            old,
            now=now,
            fire_claim={"claimed_at": (now - timedelta(minutes=30)).isoformat()},
        )
    ]

    status = detect_missed_cron_runs(jobs=jobs, now=now, grace_seconds=300, claim_ttl_seconds=600)

    assert status["missed_count"] == 1
    assert status["offenders"][0]["job_id"] == "expired-claim"


def test_detect_missed_cron_runs_handles_timezone_aware_timestamps():
    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    next_run_at = "2026-07-06T07:30:00-04:00"
    jobs = [_job("tz", next_run_at, now=now, created_at="2026-07-05T08:00:00-04:00")]

    status = detect_missed_cron_runs(jobs=jobs, now=now, grace_seconds=300)

    assert status["missed_count"] == 1
    assert status["offenders"][0]["overdue_seconds"] == 1800
    assert status["offenders"][0]["overdue_age"] == "30m"
