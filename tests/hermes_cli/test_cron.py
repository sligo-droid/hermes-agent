"""Tests for hermes_cli.cron command handling."""

from argparse import Namespace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cron.jobs import create_job, get_job, list_jobs, resume_job, update_job
from hermes_cli import cron as cron_cli
from hermes_cli.gateway import GatewayRuntimeHealth
from hermes_cli.cron import _print_overdue_proposal_findings, cron_command, cron_list, cron_status


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


class TestCronCommandLifecycle:
    def test_pause_resume_run(self, tmp_cron_dir, capsys):
        job = create_job(prompt="Check server status", schedule="every 1h")

        cron_command(Namespace(cron_command="pause", job_id=job["id"]))
        paused = get_job(job["id"])
        assert paused["state"] == "paused"

        cron_command(Namespace(cron_command="resume", job_id=job["id"]))
        resumed = get_job(job["id"])
        assert resumed["state"] == "scheduled"

        cron_command(Namespace(cron_command="run", job_id=job["id"]))
        triggered = get_job(job["id"])
        assert triggered["state"] == "scheduled"

        out = capsys.readouterr().out
        assert "Paused job" in out
        assert "Resumed job" in out
        assert "Triggered job" in out

    def test_edit_can_replace_and_clear_skills(self, tmp_cron_dir, capsys):
        job = create_job(
            prompt="Combine skill outputs",
            schedule="every 1h",
            skill="blogwatcher",
        )

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule="every 2h",
                prompt="Revised prompt",
                name="Edited Job",
                deliver=None,
                repeat=None,
                skill=None,
                skills=["maps", "blogwatcher"],
                profile="default",
                clear_skills=False,
            )
        )
        updated = get_job(job["id"])
        assert updated["skills"] == ["maps", "blogwatcher"]
        assert updated["name"] == "Edited Job"
        assert updated["prompt"] == "Revised prompt"
        assert updated["schedule_display"] == "every 120m"
        assert updated["profile"] == "default"

        cron_command(
            Namespace(
                cron_command="edit",
                job_id=job["id"],
                schedule=None,
                prompt=None,
                name=None,
                deliver=None,
                repeat=None,
                skill=None,
                skills=None,
                profile="",
                clear_skills=True,
            )
        )
        cleared = get_job(job["id"])
        assert cleared["skills"] == []
        assert cleared["skill"] is None
        assert cleared["profile"] is None

        out = capsys.readouterr().out
        assert "Updated job" in out

    def test_create_with_multiple_skills(self, tmp_cron_dir, capsys):
        cron_command(
            Namespace(
                cron_command="create",
                schedule="every 1h",
                prompt="Use both skills",
                name="Skill combo",
                deliver=None,
                repeat=None,
                skill=None,
                skills=["blogwatcher", "maps"],
                profile="default",
            )
        )
        out = capsys.readouterr().out
        assert "Created job" in out

        jobs = list_jobs()
        assert len(jobs) == 1
        assert jobs[0]["skills"] == ["blogwatcher", "maps"]
        assert jobs[0]["name"] == "Skill combo"
        assert jobs[0]["profile"] == "default"

    def test_list_does_not_crash_when_repeat_is_null(self, tmp_cron_dir, capsys):
        from cron.jobs import load_jobs, save_jobs

        create_job(prompt="One shot", schedule="every 1h")
        jobs = load_jobs()
        jobs[0]["repeat"] = None
        save_jobs(jobs)

        cron_command(Namespace(cron_command="list", all=True))

        assert "Repeat:    ∞" in capsys.readouterr().out

    def test_list_does_not_crash_when_deliver_is_null(self, tmp_cron_dir, capsys):
        from cron.jobs import load_jobs, save_jobs

        create_job(prompt="No deliver", schedule="every 1h")
        jobs = load_jobs()
        jobs[0]["deliver"] = None
        save_jobs(jobs)

        cron_command(Namespace(cron_command="list", all=True))

        assert "Deliver:   local" in capsys.readouterr().out

    def test_list_renders_terminal_auto_pause_metadata(self, tmp_cron_dir, capsys, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.gateway.get_gateway_runtime_health",
            lambda: GatewayRuntimeHealth(True, "process", pid=123),
        )
        job = create_job(prompt="", schedule="every 1h", script="finite.sh", no_agent=True)
        update_job(
            job["id"],
            {
                "enabled": False,
                "state": "paused",
                "paused_reason": "terminal success: DONE marker",
                "last_terminal_output_path": "/tmp/terminal.md",
                "disable_on_terminal_success": True,
            },
        )

        cron_list(show_all=True)

        out = capsys.readouterr().out
        assert "auto-pause on DONE/terminal_success marker" in out
        assert "terminal success: DONE marker" in out
        assert "/tmp/terminal.md" in out

    def test_status_renders_terminal_auto_paused_jobs(self, tmp_cron_dir, capsys, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.gateway.get_gateway_runtime_health",
            lambda: GatewayRuntimeHealth(False, "none"),
        )
        monkeypatch.setattr("cron.jobs.audit_overdue_self_improvement_proposals", lambda: [])
        job = create_job(prompt="", schedule="every 1h", script="finite.sh", no_agent=True)
        update_job(
            job["id"],
            {
                "enabled": False,
                "state": "paused",
                "paused_reason": "terminal success: DONE marker",
                "last_terminal_output_path": "/tmp/terminal.md",
            },
        )

        cron_status()

        out = capsys.readouterr().out
        assert "Terminal auto-paused job" in out
        assert "terminal success: DONE marker" in out
        assert "/tmp/terminal.md" in out

    def test_status_ignores_resumed_terminal_success_jobs(self, tmp_cron_dir, capsys, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.gateway.get_gateway_runtime_health",
            lambda: GatewayRuntimeHealth(False, "none"),
        )
        monkeypatch.setattr("cron.jobs.audit_overdue_self_improvement_proposals", lambda: [])
        job = create_job(prompt="", schedule="every 1h", script="finite.sh", no_agent=True)
        update_job(
            job["id"],
            {
                "enabled": False,
                "state": "paused",
                "paused_reason": "terminal success: DONE marker",
                "last_terminal_output_path": "/tmp/terminal.md",
            },
        )
        resume_job(job["id"])

        cron_status()

        out = capsys.readouterr().out
        assert "1 active job" in out
        assert "Terminal auto-paused job" not in out
        assert "/tmp/terminal.md" not in out

    def test_list_all_keeps_terminal_output_for_resumed_jobs(self, tmp_cron_dir, capsys, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.gateway.get_gateway_runtime_health",
            lambda: GatewayRuntimeHealth(True, "process", pid=123),
        )
        job = create_job(prompt="", schedule="every 1h", script="finite.sh", no_agent=True)
        update_job(
            job["id"],
            {
                "enabled": False,
                "state": "paused",
                "paused_reason": "terminal success: DONE marker",
                "last_terminal_output_path": "/tmp/terminal.md",
                "disable_on_terminal_success": True,
            },
        )
        resume_job(job["id"])

        cron_list(show_all=True)

        out = capsys.readouterr().out
        assert "[active]" in out
        assert "Paused:" not in out
        assert "Terminal output: /tmp/terminal.md" in out

    def test_status_uses_fresh_runtime_heartbeat_when_pid_is_not_visible(self, tmp_cron_dir, capsys, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.gateway.get_gateway_runtime_health",
            lambda: GatewayRuntimeHealth(
                True,
                "runtime_heartbeat",
                pid=1167299,
                gateway_state="running",
                active_agents=4,
            ),
        )
        monkeypatch.setattr("cron.jobs.audit_overdue_self_improvement_proposals", lambda: [])
        create_job(prompt="", schedule="every 1h")

        cron_status()

        out = capsys.readouterr().out
        assert "Gateway is running" in out
        assert "Runtime heartbeat: running (PID: 1167299)" in out
        assert "Process/service not visible from this namespace" in out
        assert "Gateway is not running" not in out

    def test_status_renders_missed_cron_runs(self, tmp_cron_dir, capsys, monkeypatch):
        now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
        monkeypatch.setattr("cron.jobs._hermes_now", lambda: now)
        monkeypatch.setattr(
            "hermes_cli.gateway.get_gateway_runtime_health",
            lambda: GatewayRuntimeHealth(True, "process", pid=123),
        )
        monkeypatch.setattr("cron.jobs.get_ticker_heartbeat_age", lambda: 10)
        monkeypatch.setattr("cron.jobs.get_ticker_success_age", lambda: 10)
        monkeypatch.setattr("cron.jobs.audit_overdue_self_improvement_proposals", lambda: [])
        job = create_job(prompt="", schedule="every 1h", name="Status overdue")
        update_job(
            job["id"],
            {
                "next_run_at": (now - timedelta(minutes=20)).isoformat(),
                "created_at": (now - timedelta(days=1)).isoformat(),
                "last_run_at": (now - timedelta(hours=2)).isoformat(),
                "last_status": "error",
            },
        )

        cron_status()

        out = capsys.readouterr().out
        assert "cron job(s) missed scheduled fire" in out
        assert "Status overdue" in out
        assert "Overdue: 20m" in out
        assert "Last status: error" in out


def test_overdue_proposal_findings_render_operator_evidence(capsys):
    _print_overdue_proposal_findings(
        [
            {
                "job_id": "17abff6b6061",
                "job_name": "Daily retrospective",
                "expected_missed_slot": "2026-06-12T06:00:00-04:00",
                "next_run_at": "2026-06-12T06:00:00-04:00",
                "last_run_at": "2026-06-11T06:18:08-04:00",
                "last_output_path": "/tmp/output.md",
                "session": {
                    "available": True,
                    "session_id": "cron_17abff6b6061_20260613_060016",
                    "stale_open_session": False,
                },
                "scheduler_logs": {
                    "available": True,
                    "excerpts": ["agent.log: missed its scheduled time"],
                },
            }
        ]
    )

    out = capsys.readouterr().out
    assert "Overdue self-improvement proposal cron" in out
    assert "17abff6b6061 (Daily retrospective)" in out
    assert "Expected missed slot: 2026-06-12T06:00:00-04:00" in out
    assert "Last output: /tmp/output.md" in out
    assert "cron_17abff6b6061_20260613_060016" in out
    assert "agent.log: missed its scheduled time" in out


def test_cron_create_warns_when_builtin_gateway_is_absent(tmp_cron_dir, monkeypatch, capsys):
    monkeypatch.setattr(cron_cli, "_active_cron_provider_name", lambda: "builtin")
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])

    cron_command(
        Namespace(
            cron_command="create",
            schedule="0 11 * * *",
            prompt="Daily report",
            name="Daily 1130",
            deliver=None,
            repeat=None,
            skill=None,
            skills=None,
            profile=None,
            script=None,
            workdir=None,
            no_agent=False,
        )
    )

    out = capsys.readouterr().out
    assert "Created job" in out
    assert "Gateway is not running" in out


def test_cron_list_warns_when_builtin_gateway_is_absent(monkeypatch, capsys):
    monkeypatch.setattr(cron_cli, "_active_cron_provider_name", lambda: "builtin")
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])
    monkeypatch.setattr(
        "cron.jobs.list_jobs",
        lambda include_disabled=False: [
            {
                "id": "job-1",
                "name": "Nightly docs",
                "schedule_display": "every day",
                "state": "scheduled",
                "enabled": True,
                "next_run_at": "2026-06-01T00:00:00Z",
                "deliver": ["local"],
            }
        ],
    )

    cron_cli.cron_list()

    out = capsys.readouterr().out
    assert "Gateway is not running" in out
    assert "Nightly docs" in out


def test_cron_status_reports_external_provider_without_ticker_warning(
    tmp_cron_dir, monkeypatch, capsys
):
    create_job(prompt="Ping", schedule="every 2m")
    monkeypatch.setattr(cron_cli, "_active_cron_provider_name", lambda: "chronos")

    cron_cli.cron_status()

    out = capsys.readouterr().out
    assert "chronos" in out
    assert "managed scheduler" in out
    assert "STALLED" not in out
    assert "Gateway is not running" not in out
    assert "1 active job(s)" in out


def test_cron_create_is_quiet_for_external_provider_without_gateway(
    tmp_cron_dir, monkeypatch, capsys
):
    monkeypatch.setattr(cron_cli, "_active_cron_provider_name", lambda: "chronos")
    monkeypatch.setattr("hermes_cli.gateway.find_gateway_pids", lambda: [])

    cron_command(
        Namespace(
            cron_command="create",
            schedule="every 2m",
            prompt="Ping",
            name="Ping",
            deliver=None,
            repeat=None,
            skill=None,
            skills=None,
            profile=None,
            script=None,
            workdir=None,
            no_agent=False,
        )
    )

    out = capsys.readouterr().out
    assert "Created job" in out
    assert "Gateway is not running" not in out


def test_cron_tick_invokes_scheduler_tick_with_verbose(monkeypatch):
    calls = []
    monkeypatch.setattr("cron.scheduler.tick", lambda verbose=False: calls.append(verbose))

    cron_cli.cron_tick()

    assert calls == [True]


def test_cron_create_success_prints_job_details(monkeypatch, capsys):
    monkeypatch.setattr(
        cron_cli,
        "_cron_api",
        lambda **kwargs: {
            "success": True,
            "job_id": "job-1",
            "name": "Nightly docs",
            "schedule": "every day",
            "skills": ["docs"],
            "next_run_at": "2026-06-01T00:00:00Z",
            "job": {
                "script": "scripts/build_docs.py",
                "no_agent": True,
                "workdir": "/tmp/repo",
            },
        },
    )
    monkeypatch.setattr(cron_cli, "_warn_if_gateway_not_running", lambda: None)

    rc = cron_cli.cron_create(
        SimpleNamespace(
            schedule="every day",
            prompt="refresh docs",
            name="Nightly docs",
            deliver=None,
            repeat=None,
            skill="docs",
            skills=None,
            script="scripts/build_docs.py",
            workdir="/tmp/repo",
            no_agent=True,
        )
    )

    out = capsys.readouterr().out
    assert rc == 0
    assert "Created job: job-1" in out
    assert "Skills: docs" in out
    assert "Script: scripts/build_docs.py" in out
    assert "Mode: no-agent" in out
    assert "Workdir: /tmp/repo" in out
    assert "Next run: 2026-06-01T00:00:00Z" in out


def test_cron_create_failure_returns_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        cron_cli,
        "_cron_api",
        lambda **kwargs: {"success": False, "error": "boom"},
    )

    rc = cron_cli.cron_create(
        SimpleNamespace(
            schedule="every day",
            prompt="refresh docs",
            name=None,
            deliver=None,
            repeat=None,
            skill=None,
            skills=None,
            script=None,
            workdir=None,
            no_agent=False,
        )
    )

    assert rc == 1
    assert "Failed to create job: boom" in capsys.readouterr().out
