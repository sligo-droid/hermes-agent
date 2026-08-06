"""Redacted operator CLI for the client-knowledge intake queue."""

from __future__ import annotations

import argparse
import json
from typing import Any

from .store import IntakeStore, resolve_store_path


def register_cli(subparser: argparse.ArgumentParser) -> None:
    subs = subparser.add_subparsers(dest="client_knowledge_action")

    status = subs.add_parser("status", help="Show redacted intake queue counts")
    status.add_argument("--db-path", default="")

    listing = subs.add_parser("list", aliases=["ls"], help="List redacted intake jobs")
    listing.add_argument("--status", default="")
    listing.add_argument("--limit", type=int, default=20)
    listing.add_argument("--db-path", default="")

    show = subs.add_parser("show", help="Show one redacted intake job")
    show.add_argument("job_id")
    show.add_argument("--db-path", default="")

    retry = subs.add_parser("retry", help="Retry a failed or quarantined intake job")
    retry.add_argument("job_id")
    retry.add_argument("--db-path", default="")

    quarantine = subs.add_parser("quarantine", help="Quarantine an intake job")
    quarantine.add_argument("job_id")
    quarantine.add_argument("--db-path", default="")

    reconcile = subs.add_parser("reconcile", help="Recover stale intake leases")
    reconcile.add_argument("--db-path", default="")

    run_once = subs.add_parser(
        "run-once",
        help="Run a bounded batch of Notion source-archive jobs",
    )
    run_once.add_argument("--db-path", default="")

    gmail_poll = subs.add_parser(
        "gmail-poll-once",
        help="Run one bounded present-forward Gmail poll",
    )
    gmail_poll.add_argument("--db-path", default="")

    notion_preflight = subs.add_parser(
        "notion-preflight",
        help="Inspect or add the minimal Notion source-archive schema",
    )
    notion_preflight.add_argument("--project", required=True)
    notion_preflight.add_argument("--apply-schema", action="store_true")
    notion_preflight.add_argument("--run-fixed-fixtures", action="store_true")
    notion_preflight.add_argument("--db-path", default="")

    subparser.set_defaults(func=client_knowledge_command)


def _store(path_arg: str | None) -> IntakeStore:
    return IntakeStore(path_arg.strip() if path_arg and path_arg.strip() else resolve_store_path())


def _redacted_job(job: dict[str, Any]) -> dict[str, Any]:
    """Return only the fields safe for an operator-facing queue view."""
    return {
        "job_id": job.get("job_id"),
        "artifact_id": job.get("artifact_id"),
        "stage": job.get("stage"),
        "status": job.get("status"),
        "attempt_count": job.get("attempt_count", 0),
        "max_attempts": job.get("max_attempts", 0),
        "last_error_class": job.get("last_error_class"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "next_retry_at": job.get("next_retry_at"),
        "lease_expires_at": job.get("lease_expires_at"),
    }


def _error_payload(exc: Exception) -> dict[str, str]:
    # Never expose exception text: it can contain provider responses, paths,
    # original filenames, credentials, or arbitrary untrusted payload content.
    return {"error_class": exc.__class__.__name__}


def _job_id(value: Any) -> str:
    job_id = str(value or "").strip().lower()
    if len(job_id) != 32 or any(ch not in "0123456789abcdef" for ch in job_id):
        raise ValueError("job_id must be a canonical opaque id")
    return job_id


def client_knowledge_command(args: argparse.Namespace) -> int:
    action = getattr(args, "client_knowledge_action", None)
    if not action:
        print(json.dumps({"error_class": "usage"}, sort_keys=True))
        return 2
    try:
        store = _store(getattr(args, "db_path", ""))
        if action == "status":
            print(json.dumps({"stats": store.stats()}, sort_keys=True))
            return 0
        if action in {"list", "ls"}:
            jobs = store.list_jobs(
                status=str(getattr(args, "status", "") or "").strip() or None,
                limit=getattr(args, "limit", 20),
            )
            print(json.dumps({"jobs": [_redacted_job(job) for job in jobs]}, sort_keys=True))
            return 0
        if action == "show":
            job = store.get_job(_job_id(args.job_id))
            print(json.dumps({"job": _redacted_job(job) if job else None}, sort_keys=True))
            return 0 if job else 1
        if action == "retry":
            job_id = _job_id(args.job_id)
            print(json.dumps({"job_id": job_id, "changed": store.retry(job_id)}, sort_keys=True))
            return 0
        if action == "quarantine":
            job_id = _job_id(args.job_id)
            print(json.dumps({"job_id": job_id, "changed": store.quarantine(job_id)}, sort_keys=True))
            return 0
        if action == "reconcile":
            print(json.dumps({"recovered": store.reconcile(), "stats": store.stats()}, sort_keys=True))
            return 0
        if action == "run-once":
            from hermes_cli.config import load_config
            from .notion_archive import run_notion_once
            from .spool import RawSpool

            result = run_notion_once(store=store, spool=RawSpool(), config=load_config())
            print(json.dumps({"mode": "notion_archive", **result, "stats": store.stats()}, sort_keys=True))
            return 0
        if action == "gmail-poll-once":
            from hermes_cli.config import load_config
            from .gmail_poller import run_gmail_once
            from .spool import RawSpool

            result = run_gmail_once(store=store, spool=RawSpool(), config=load_config())
            print(json.dumps({"mode": "gmail_poll", **result}, sort_keys=True))
            return 0
        if action == "notion-preflight":
            import os

            from hermes_cli.config import load_config
            from .notion import NotionClient
            from .notion_archive import (
                NotionArchiveSettings,
                NotionArchiveWorker,
                ProjectNotionConfig,
                run_fixed_sandbox_fixtures,
            )
            from .spool import RawSpool

            config = load_config() or {}
            settings = NotionArchiveSettings.from_config(config)
            project = ProjectNotionConfig.from_config(config, str(args.project))
            api_key = settings.api_key or os.getenv("NOTION_API_KEY", "").strip()
            with NotionClient(api_key, timeout=settings.timeout) as client:
                worker = NotionArchiveWorker(store, RawSpool(), client, settings, config)
                result = worker.preflight(project, apply_schema=bool(args.apply_schema))
                if args.run_fixed_fixtures:
                    result["fixtures"] = run_fixed_sandbox_fixtures(
                        store=store,
                        spool=worker.spool,
                        client=client,
                        config=config,
                        project_key=project.project_key,
                    )
            print(json.dumps(result, sort_keys=True))
            return 0
        print(json.dumps({"error_class": "unknown_action"}, sort_keys=True))
        return 2
    except Exception as exc:
        print(json.dumps(_error_payload(exc), sort_keys=True))
        return 1


__all__ = ["client_knowledge_command", "register_cli"]
