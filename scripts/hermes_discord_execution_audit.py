#!/usr/bin/env python3
"""Repo-managed daily Hermes Discord execution audit and live installer."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any


def _default_repo() -> Path:
    configured = os.environ.get("HERMES_REPO", "").strip()
    if configured:
        return Path(configured).expanduser()
    source_checkout = Path(__file__).resolve().parent.parent
    if (
        (source_checkout / "pyproject.toml").is_file()
        and (source_checkout / "cron" / "__init__.py").is_file()
    ):
        return source_checkout
    return Path("/home/droid/hermes")


REPO = _default_repo()
HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
sys.path.insert(0, str(REPO))

from cron.discord_execution_audit import (  # noqa: E402
    AUDIT_PRONG,
    AUDIT_PROJECT,
    AUDIT_PROPOSAL_PROMPT,
    build_audit_report,
)


SCRIPT_NAME = "hermes_discord_execution_audit.py"
DEFAULT_JOB_ID = "9832b5241a0d"
CANONICAL_JOB_NAME = "Daily Hermes Discord execution audit"
CANONICAL_SCHEDULE = "30 5 * * *"
ENTRYPOINT_SOURCE_LABEL = "repo-managed-hermes-discord-execution-audit-v1"
REPO_SCRIPT = REPO / "scripts" / SCRIPT_NAME
COLLECTOR_SOURCE = REPO / "cron" / "discord_execution_audit.py"
LIVE_SCRIPT = HERMES_HOME / "scripts" / SCRIPT_NAME
CRON_JOBS = HERMES_HOME / "cron" / "jobs.json"


def sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_jobs() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = json.loads(CRON_JOBS.read_text(encoding="utf-8"))
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, list):
        raise ValueError("jobs.json does not contain a jobs list")
    return payload, [item for item in jobs if isinstance(item, dict)]


def _find_job(jobs: list[dict[str, Any]], job_id: str | None = None) -> dict[str, Any] | None:
    if job_id:
        return next((item for item in jobs if str(item.get("id") or "") == job_id), None)
    for item in jobs:
        proposal = item.get("self_improvement_proposal")
        if isinstance(proposal, dict) and (
            proposal.get("project") == AUDIT_PROJECT and proposal.get("prong") == AUDIT_PRONG
        ):
            return item
    legacy_names = {"pid_discord_execution_audit.py", SCRIPT_NAME}
    return next(
        (
            item
            for item in jobs
            if Path(str(item.get("script") or "")).name in legacy_names
        ),
        None,
    )


def _resolved_script(job: dict[str, Any] | None) -> Path | None:
    if not job:
        return None
    raw = Path(str(job.get("script") or "")).expanduser()
    return raw.resolve() if raw.is_absolute() else (HERMES_HOME / "scripts" / raw).resolve()


def cron_entrypoint_provenance(job_id: str | None = None) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "cron_jobs_exists": CRON_JOBS.is_file(),
        "cron_job_id": None,
        "cron_job_name": None,
        "cron_job_script": None,
        "cron_job_script_resolved_path": None,
        "cron_job_schedule": None,
        "cron_job_enabled": None,
        "cron_job_state": None,
        "cron_project": None,
        "cron_prong": None,
        "cron_invokes_live_script": False,
    }
    if not CRON_JOBS.is_file():
        return facts
    try:
        _payload, jobs = _load_jobs()
        job = _find_job(jobs, job_id)
    except Exception as exc:
        facts["cron_jobs_read_error"] = f"{type(exc).__name__}: {exc}"
        return facts
    if not job:
        return facts
    proposal = job.get("self_improvement_proposal") if isinstance(job.get("self_improvement_proposal"), dict) else {}
    resolved = _resolved_script(job)
    schedule = job.get("schedule_display")
    if not schedule and isinstance(job.get("schedule"), dict):
        schedule = job["schedule"].get("display") or job["schedule"].get("expr")
    facts.update(
        {
            "cron_job_id": job.get("id"),
            "cron_job_name": job.get("name"),
            "cron_job_script": job.get("script"),
            "cron_job_script_resolved_path": str(resolved) if resolved else None,
            "cron_job_schedule": schedule,
            "cron_job_enabled": job.get("enabled"),
            "cron_job_state": job.get("state"),
            "cron_project": proposal.get("project"),
            "cron_prong": proposal.get("prong"),
            "cron_invokes_live_script": bool(resolved and resolved == LIVE_SCRIPT.resolve()),
        }
    )
    return facts


def script_provenance(job_id: str | None = None) -> dict[str, Any]:
    source_sha = sha256_file(REPO_SCRIPT)
    live_sha = sha256_file(LIVE_SCRIPT)
    facts = {
        "script_entrypoint_source": ENTRYPOINT_SOURCE_LABEL,
        "script_running_path": str(Path(__file__).resolve()),
        "script_source_path": str(REPO_SCRIPT),
        "script_live_path": str(LIVE_SCRIPT),
        "script_source_exists": REPO_SCRIPT.is_file(),
        "script_live_exists": LIVE_SCRIPT.is_file(),
        "script_source_sha256": source_sha,
        "script_live_sha256": live_sha,
        "script_matches_source": bool(source_sha and live_sha and source_sha == live_sha),
        "collector_source_path": str(COLLECTOR_SOURCE),
        "collector_source_exists": COLLECTOR_SOURCE.is_file(),
        "collector_source_sha256": sha256_file(COLLECTOR_SOURCE),
    }
    facts.update(cron_entrypoint_provenance(job_id))
    facts["script_pickup_ready"] = bool(
        facts["script_matches_source"]
        and facts["collector_source_exists"]
        and facts.get("cron_invokes_live_script")
        and facts.get("cron_project") == AUDIT_PROJECT
        and facts.get("cron_prong") == AUDIT_PRONG
    )
    return facts


def _atomic_write_jobs(payload: dict[str, Any]) -> None:
    CRON_JOBS.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".jobs_", suffix=".tmp", dir=str(CRON_JOBS.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CRON_JOBS)
        CRON_JOBS.chmod(0o600)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _canonical_job(job: dict[str, Any]) -> dict[str, Any]:
    from cron.jobs import compute_next_run, parse_schedule

    updated = dict(job)
    schedule = parse_schedule(CANONICAL_SCHEDULE)
    updated.update(
        {
            "name": CANONICAL_JOB_NAME,
            "prompt": AUDIT_PROPOSAL_PROMPT,
            "script": SCRIPT_NAME,
            "schedule": schedule,
            "schedule_display": schedule.get("display") or CANONICAL_SCHEDULE,
            "self_improvement_proposal": {
                "project": AUDIT_PROJECT,
                "prong": AUDIT_PRONG,
            },
        }
    )
    if updated.get("enabled", True) and updated.get("state") != "paused":
        updated["next_run_at"] = compute_next_run(schedule)
    return updated


def _worktree_install_blocked() -> bool:
    parts = REPO.resolve().parts
    in_claude_worktree = ".claude" in parts and "worktrees" in parts
    override = os.environ.get("HERMES_ALLOW_WORKTREE_INSTALL", "").strip().lower()
    return in_claude_worktree and override not in {"1", "true", "yes"}


def install_live(*, job_id: str | None = DEFAULT_JOB_ID, dry_run: bool = False) -> int:
    if _worktree_install_blocked():
        print(
            "ERROR refusing live install from a temporary worktree; merge to the canonical "
            "checkout first",
            file=sys.stderr,
        )
        return 1
    if not REPO_SCRIPT.is_file():
        print(f"ERROR source script missing: {REPO_SCRIPT}", file=sys.stderr)
        return 1
    if not CRON_JOBS.is_file():
        print(f"ERROR cron jobs file missing: {CRON_JOBS}", file=sys.stderr)
        return 1
    try:
        payload, jobs = _load_jobs()
    except Exception as exc:
        print(f"ERROR cannot read cron jobs: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    job = _find_job(jobs, job_id)
    if not job:
        print(f"ERROR Discord audit cron job not found: {job_id or 'auto'}", file=sys.stderr)
        return 1

    source_sha = sha256_file(REPO_SCRIPT)
    live_sha = sha256_file(LIVE_SCRIPT)
    had_live_script = LIVE_SCRIPT.is_file()
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    archive_dir = HERMES_HOME / "scripts" / "archive"
    script_backup = archive_dir / f"{SCRIPT_NAME}.{stamp}.bak"
    jobs_backup = archive_dir / f"cron-jobs.{stamp}.json.bak"
    proposed_job = _canonical_job(job)

    print(f"job_id={job.get('id')}")
    print(f"source={REPO_SCRIPT}")
    print(f"destination={LIVE_SCRIPT}")
    print(f"script_archive={script_backup}")
    print(f"jobs_archive={jobs_backup}")
    print(f"source_sha256={source_sha}")
    print(f"current_live_sha256={live_sha}")
    print(f"matches_source={bool(source_sha and live_sha and source_sha == live_sha)}")
    print(f"canonical_name={proposed_job['name']}")
    print(f"canonical_schedule={proposed_job['schedule_display']}")
    print(f"canonical_script={proposed_job['script']}")
    print(f"canonical_proposal={AUDIT_PROJECT}/{AUDIT_PRONG}")
    if dry_run:
        print("dry_run=true")
        return 0

    archive_dir.mkdir(parents=True, exist_ok=True)
    LIVE_SCRIPT.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(CRON_JOBS, jobs_backup)
    if had_live_script:
        shutil.copy2(LIVE_SCRIPT, script_backup)
    shutil.copy2(REPO_SCRIPT, LIVE_SCRIPT)
    LIVE_SCRIPT.chmod(LIVE_SCRIPT.stat().st_mode | 0o111)
    installed_sha = sha256_file(LIVE_SCRIPT)
    if not source_sha or installed_sha != source_sha:
        print("ERROR installed script hash does not match source", file=sys.stderr)
        return 1

    updated_jobs = []
    for current in payload["jobs"]:
        if isinstance(current, dict) and current.get("id") == job.get("id"):
            updated_jobs.append(proposed_job)
        else:
            updated_jobs.append(current)
    payload["jobs"] = updated_jobs
    payload["updated_at"] = dt.datetime.now(dt.UTC).isoformat()
    _atomic_write_jobs(payload)

    print(f"installed_live_sha256={installed_sha}")
    print("installed_matches_source=true")
    print(f"rollback_script={script_backup if had_live_script else 'none (no prior live script)'}")
    print(f"rollback_jobs={jobs_backup}")
    return 0


def status_report(*, job_id: str | None = DEFAULT_JOB_ID) -> dict[str, Any]:
    report: dict[str, Any] = {
        "provenance": script_provenance(job_id),
    }
    try:
        report["audit"] = build_audit_report(hermes_home=HERMES_HOME)
    except Exception as exc:
        report["audit_error"] = f"{type(exc).__name__}: {exc}"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true", help="Print provenance and safe audit status")
    parser.add_argument("--install-live", action="store_true", help="Install the repo script and migrate the cron job")
    parser.add_argument("--job-id", default=DEFAULT_JOB_ID, help="Existing cron job ID to migrate")
    parser.add_argument("--dry-run", action="store_true", help="Describe installation without changing files")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = parser.parse_args(argv)

    if args.install_live:
        return install_live(job_id=args.job_id or None, dry_run=args.dry_run)
    if args.dry_run:
        parser.error("--dry-run requires --install-live")
    if args.status:
        payload = status_report(job_id=args.job_id or None)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    payload = build_audit_report(hermes_home=HERMES_HOME)
    print(json.dumps(payload, indent=2 if args.pretty else None, sort_keys=True))
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    return 0 if source.get("ledger_status") == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
