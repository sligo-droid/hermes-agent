import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from scripts import hermes_discord_execution_audit as entrypoint


def _job(**overrides):
    job = {
        "id": entrypoint.DEFAULT_JOB_ID,
        "name": "Daily PID Discord execution audit",
        "prompt": "legacy prompt",
        "script": "pid_discord_execution_audit.py",
        "schedule": {"kind": "cron", "expr": "30 5 * * *", "display": "30 5 * * *"},
        "schedule_display": "30 5 * * *",
        "enabled": False,
        "state": "paused",
        "paused_reason": "cutover",
        "next_run_at": "2026-07-19T05:30:00-04:00",
        "repeat": {"count": 41, "last_success": True},
        "deliver": "local",
        "profile": "operator",
        "last_run_at": "2026-07-18T05:30:00-04:00",
        "self_improvement_proposal": {
            "project": "hermes",
            "prong": "discord_execution_audit",
        },
    }
    job.update(overrides)
    return job


def _setup(monkeypatch, tmp_path, *, job=None, live_text="#!/usr/bin/env python3\nprint('old')\n"):
    repo = tmp_path / "repo"
    home = tmp_path / "home"
    source = repo / "scripts" / entrypoint.SCRIPT_NAME
    collector = repo / "cron" / "discord_execution_audit.py"
    live = home / "scripts" / entrypoint.SCRIPT_NAME
    jobs = home / "cron" / "jobs.json"
    source.parent.mkdir(parents=True)
    collector.parent.mkdir(parents=True)
    live.parent.mkdir(parents=True)
    jobs.parent.mkdir(parents=True)
    source.write_text("#!/usr/bin/env python3\nprint('new')\n", encoding="utf-8")
    source.chmod(0o755)
    collector.write_text("# collector source\n", encoding="utf-8")
    live.write_text(live_text, encoding="utf-8")
    live.chmod(0o700)
    jobs.write_text(json.dumps({"jobs": [job or _job()], "updated_at": "old"}), encoding="utf-8")
    monkeypatch.setattr(entrypoint, "REPO", repo)
    monkeypatch.setattr(entrypoint, "HERMES_HOME", home)
    monkeypatch.setattr(entrypoint, "REPO_SCRIPT", source)
    monkeypatch.setattr(entrypoint, "COLLECTOR_SOURCE", collector)
    monkeypatch.setattr(entrypoint, "LIVE_SCRIPT", live)
    monkeypatch.setattr(entrypoint, "CRON_JOBS", jobs)
    return repo, home, source, live, jobs


def test_default_repo_does_not_treat_profile_cron_state_as_source_checkout(tmp_path, monkeypatch):
    home = tmp_path / "home"
    live = home / "scripts" / entrypoint.SCRIPT_NAME
    live.parent.mkdir(parents=True)
    (home / "cron").mkdir()
    live.write_text("# live wrapper\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_REPO", raising=False)
    monkeypatch.setattr(entrypoint, "__file__", str(live))

    assert entrypoint._default_repo() == Path("/home/droid/hermes")


def test_default_repo_accepts_real_source_checkout(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    script = repo / "scripts" / entrypoint.SCRIPT_NAME
    script.parent.mkdir(parents=True)
    (repo / "cron").mkdir()
    (repo / "cron" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    script.write_text("# source wrapper\n", encoding="utf-8")
    monkeypatch.delenv("HERMES_REPO", raising=False)
    monkeypatch.setattr(entrypoint, "__file__", str(script))

    assert entrypoint._default_repo() == repo


def test_install_live_refuses_temporary_worktree_source(tmp_path, monkeypatch, capsys):
    _setup(monkeypatch, tmp_path)
    worktree_repo = tmp_path / "repo" / ".claude" / "worktrees" / "audit"
    monkeypatch.setattr(entrypoint, "REPO", worktree_repo)
    monkeypatch.delenv("HERMES_ALLOW_WORKTREE_INSTALL", raising=False)

    assert entrypoint.install_live(dry_run=True) == 1
    assert "merge to the canonical checkout first" in capsys.readouterr().err


def test_install_live_dry_run_does_not_modify_script_or_job(tmp_path, monkeypatch, capsys):
    _repo, _home, source, live, jobs = _setup(monkeypatch, tmp_path)
    before_script = live.read_text(encoding="utf-8")
    before_jobs = jobs.read_text(encoding="utf-8")

    assert entrypoint.install_live(dry_run=True) == 0

    assert live.read_text(encoding="utf-8") == before_script
    assert jobs.read_text(encoding="utf-8") == before_jobs
    output = capsys.readouterr().out
    assert f"source={source}" in output
    assert "dry_run=true" in output
    assert "canonical_proposal=hermes/discord_execution_audit" in output


def test_install_live_backs_up_and_migrates_job_without_losing_runtime_state(
    tmp_path,
    monkeypatch,
    capsys,
):
    _repo, home, source, live, jobs = _setup(monkeypatch, tmp_path)

    assert entrypoint.install_live() == 0

    installed_job = json.loads(jobs.read_text(encoding="utf-8"))["jobs"][0]
    assert installed_job["id"] == entrypoint.DEFAULT_JOB_ID
    assert installed_job["name"] == entrypoint.CANONICAL_JOB_NAME
    assert installed_job["script"] == entrypoint.SCRIPT_NAME
    assert installed_job["schedule_display"] == entrypoint.CANONICAL_SCHEDULE
    assert installed_job["self_improvement_proposal"] == {
        "project": "hermes",
        "prong": "discord_execution_audit",
    }
    assert installed_job["prompt"] == entrypoint.AUDIT_PROPOSAL_PROMPT
    for key, value in {
        "enabled": False,
        "state": "paused",
        "paused_reason": "cutover",
        "next_run_at": "2026-07-19T05:30:00-04:00",
        "repeat": {"count": 41, "last_success": True},
        "deliver": "local",
        "profile": "operator",
        "last_run_at": "2026-07-18T05:30:00-04:00",
    }.items():
        assert installed_job[key] == value

    script_backups = list((home / "scripts" / "archive").glob(f"{entrypoint.SCRIPT_NAME}.*.bak"))
    jobs_backups = list((home / "scripts" / "archive").glob("cron-jobs.*.json.bak"))
    assert len(script_backups) == 1
    assert len(jobs_backups) == 1
    assert "print('old')" in script_backups[0].read_text(encoding="utf-8")
    assert json.loads(jobs_backups[0].read_text(encoding="utf-8"))["jobs"][0]["name"].startswith("Daily PID")
    assert live.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert os.access(live, os.X_OK)
    assert entrypoint.sha256_file(live) == entrypoint.sha256_file(source)
    assert "installed_matches_source=true" in capsys.readouterr().out


def test_install_live_reports_no_script_rollback_when_no_prior_live_script(
    tmp_path,
    monkeypatch,
    capsys,
):
    _repo, home, _source, live, _jobs = _setup(monkeypatch, tmp_path)
    live.unlink()

    assert entrypoint.install_live() == 0

    assert list((home / "scripts" / "archive").glob(f"{entrypoint.SCRIPT_NAME}.*.bak")) == []
    assert "rollback_script=none (no prior live script)" in capsys.readouterr().out


def test_install_live_rejects_unknown_job_without_changes(tmp_path, monkeypatch, capsys):
    _repo, _home, _source, live, jobs = _setup(
        monkeypatch,
        tmp_path,
        job=_job(id="different-job"),
    )
    before_live = live.read_text(encoding="utf-8")
    before_jobs = jobs.read_text(encoding="utf-8")

    assert entrypoint.install_live(job_id="missing-job") == 1

    assert live.read_text(encoding="utf-8") == before_live
    assert jobs.read_text(encoding="utf-8") == before_jobs
    assert "not found" in capsys.readouterr().err


def test_provenance_detects_wrong_cron_entrypoint(tmp_path, monkeypatch):
    _repo, _home, source, live, jobs = _setup(
        monkeypatch,
        tmp_path,
        job=_job(script="/tmp/wrong-audit.py"),
    )
    shutil.copy2(source, live)

    facts = entrypoint.script_provenance()

    assert facts["script_matches_source"] is True
    assert facts["cron_job_script"] == "/tmp/wrong-audit.py"
    assert facts["cron_invokes_live_script"] is False
    assert facts["script_pickup_ready"] is False
    assert facts["cron_project"] == "hermes"
    assert jobs.exists()


def test_provenance_is_ready_after_matching_install(tmp_path, monkeypatch):
    _repo, _home, _source, _live, _jobs = _setup(monkeypatch, tmp_path)

    assert entrypoint.install_live() == 0
    facts = entrypoint.script_provenance()

    assert facts["script_matches_source"] is True
    assert facts["cron_invokes_live_script"] is True
    assert facts["cron_job_name"] == entrypoint.CANONICAL_JOB_NAME
    assert facts["cron_job_schedule"] == entrypoint.CANONICAL_SCHEDULE
    assert facts["script_pickup_ready"] is True


def test_installed_script_executes_collector_in_temporary_profile(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    live = home / "scripts" / entrypoint.SCRIPT_NAME
    ledger = home / "gateway" / "work_ledger.json"
    live.parent.mkdir(parents=True)
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps({"version": 1, "items": {}}), encoding="utf-8")
    shutil.copy2(repo / "scripts" / entrypoint.SCRIPT_NAME, live)
    live.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_REPO": str(repo),
            "HERMES_TIMEZONE": "UTC",
        }
    )

    result = subprocess.run(
        [sys.executable, str(live)],
        env=env,
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["project"] == "hermes"
    assert payload["prong"] == "discord_execution_audit"
    assert payload["source"]["ledger_status"] == "ok"
    assert payload["source"]["accepted_requests"] == 0
    assert payload["selected_candidate"] is None
    assert payload["smooth"] is False


def test_installed_script_fails_when_execution_ledger_is_unavailable(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    live = home / "scripts" / entrypoint.SCRIPT_NAME
    live.parent.mkdir(parents=True)
    shutil.copy2(repo / "scripts" / entrypoint.SCRIPT_NAME, live)
    env = os.environ.copy()
    env.update(
        {
            "HERMES_HOME": str(home),
            "HERMES_REPO": str(repo),
            "HERMES_TIMEZONE": "UTC",
        }
    )

    result = subprocess.run(
        [sys.executable, str(live)],
        env=env,
        cwd=repo,
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["source"]["ledger_status"] == "missing"
    assert payload["smooth"] is False


def test_status_report_contains_safe_collector_and_provenance(tmp_path, monkeypatch):
    _repo, home, _source, _live, _jobs = _setup(monkeypatch, tmp_path)
    monkeypatch.setattr(
        entrypoint,
        "build_audit_report",
        lambda **_kwargs: {
            "schema_version": "hermes.discord_execution_audit.v1",
            "selected_candidate": None,
            "source": {"accepted_requests": 0},
        },
    )

    report = entrypoint.status_report()

    assert report["provenance"]["script_entrypoint_source"] == entrypoint.ENTRYPOINT_SOURCE_LABEL
    assert report["audit"]["selected_candidate"] is None
    assert report["audit"]["source"]["accepted_requests"] == 0
    assert str(home) in report["provenance"]["script_live_path"]
