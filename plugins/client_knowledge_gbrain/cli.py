"""Redacted operator CLI for the client-knowledge intake queue."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
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

    reviews = subs.add_parser("reviews", help="List pending redacted reviews")
    reviews.add_argument("--db-path", default="")

    notify = subs.add_parser(
        "notify-reviews-once", help="Attempt bounded pending Discord review delivery"
    )
    notify.add_argument("--db-path", default="")

    adopt_review = subs.add_parser(
        "adopt-review-message", help="Verify and adopt one exact uncertain Discord review message"
    )
    adopt_review.add_argument("--review-id", required=True)
    adopt_review.add_argument("--message-id", required=True)
    adopt_review.add_argument("--db-path", default="")

    requeue_review = subs.add_parser(
        "requeue-review-notification",
        help="Requeue an uncertain review after the operator proves no message exists",
    )
    requeue_review.add_argument("--review-id", required=True)
    requeue_review.add_argument("--confirm-absent", action="store_true")
    requeue_review.add_argument("--db-path", default="")

    migrate_legacy = subs.add_parser(
        "migrate-legacy-review",
        help="Convert one qualifying persisted legacy review to per-item review",
    )
    migrate_legacy.add_argument("--review-id", required=True)
    migrate_legacy.add_argument("--db-path", default="")

    restore_revision = subs.add_parser(
        "restore-item-revision",
        help="Restore one candidate after bounded Other-revision failures",
    )
    restore_revision.add_argument("--item-id", required=True)
    restore_revision.add_argument("--db-path", default="")

    run_once = subs.add_parser(
        "run-once",
        help="Run bounded Notion, extraction, synthesis, and review stages",
    )
    run_once.add_argument("--db-path", default="")
    run_once.add_argument(
        "--dry-run",
        action="store_true",
        help="Report configured stage enablement without running downstream work",
    )

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
        if action == "run-once" and bool(getattr(args, "dry_run", False)):
            from hermes_cli.config import load_config

            config = load_config() or {}
            raw = config.get("client_knowledge", {})
            raw = raw if isinstance(raw, dict) else {}
            stage_enablement = {}
            for name in (
                "notion", "extraction", "synthesis", "review_notifications",
            ):
                stage_config = raw.get(name)
                stage_enablement[name] = bool(
                    stage_config.get("enabled", False)
                    if isinstance(stage_config, dict)
                    else False
                )
            path_arg = str(getattr(args, "db_path", "") or "").strip()
            ledger_path = (
                resolve_store_path()
                if not path_arg
                else Path(path_arg).expanduser()
            )
            if path_arg and not ledger_path.is_absolute():
                raise ValueError("db_path must be absolute")
            print(json.dumps({
                "mode": "dry_run",
                "downstream_writes": False,
                "ledger_writes": False,
                "ledger_exists": ledger_path.is_file(),
                "stage_enablement": stage_enablement,
            }, sort_keys=True))
            return 0
        store = _store(getattr(args, "db_path", ""))
        if action == "status":
            from hermes_cli.config import load_config

            config = load_config() or {}
            client_knowledge = config.get("client_knowledge", {})
            synthesis = (
                client_knowledge.get("synthesis", {})
                if isinstance(client_knowledge, dict)
                else {}
            )
            print(json.dumps({
                "stats": store.stats(),
                "cutover": store.client_knowledge_cutover_status(),
                "pipeline": {
                    "live_stage": "synthesized",
                    "synthesis_enabled": bool(
                        synthesis.get("enabled", False)
                        if isinstance(synthesis, dict)
                        else False
                    ),
                    "legacy_live_stages": [],
                    "persistent_component_views": [
                        "client-knowledge-review",
                        "client-knowledge-review-item",
                    ],
                },
            }, sort_keys=True))
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
        if action == "reviews":
            reviews = store.list_open_reviews(limit=50)
            syntheses = store.list_open_synthesis_reviews(limit=50)
            print(json.dumps({
                "reviews": [
                    {
                        "review_id": row["review_id"],
                        "artifact_id": row["artifact_id"],
                        "project_key": row["project_key"],
                        "state": row["state"],
                        "reason_code": row["reason_code"],
                        "notification_state": row["notification_state"],
                        "revision_state": row.get("revision_state"),
                        "revision_attempt_count": row.get("revision_attempt_count"),
                        "revision_error_class": row.get("revision_error_class"),
                    }
                    for row in reviews
                ],
                "syntheses": syntheses,
            }, sort_keys=True))
            return 0
        if action == "notify-reviews-once":
            from .derived import DerivedStore
            from .review import run_notification_once

            result = run_notification_once(store=store, derived=DerivedStore())
            print(json.dumps(result, sort_keys=True))
            return 0
        if action == "adopt-review-message":
            from .review import fetch_and_reconcile_notification

            changed = fetch_and_reconcile_notification(
                store, str(args.review_id), str(args.message_id)
            )
            print(json.dumps({"review_id": str(args.review_id), "adopted": changed}, sort_keys=True))
            return 0 if changed else 1
        if action == "requeue-review-notification":
            if not bool(args.confirm_absent):
                raise ValueError("operator absence confirmation is required")
            identity = str(args.review_id)
            changed = store.requeue_synthesis_notification(identity)
            if not changed:
                changed = store.requeue_replacement_item_notification(identity)
            if not changed:
                changed = store.requeue_review_notification(identity)
            print(json.dumps({"review_id": str(args.review_id), "requeued": changed}, sort_keys=True))
            return 0 if changed else 1
        if action == "restore-item-revision":
            item_id = str(args.item_id or "").strip().lower()
            if len(item_id) != 64 or any(ch not in "0123456789abcdef" for ch in item_id):
                raise ValueError("item_id must be a canonical opaque id")
            changed = store.restore_synthesis_item_revision(item_id)
            print(json.dumps({"item_id": item_id, "restored": changed}, sort_keys=True))
            return 0 if changed else 1
        if action == "migrate-legacy-review":
            import asyncio

            from .derived import DerivedStore
            from .legacy_migration import migrate_legacy_review
            from .review import send_pending_review_notifications

            review_id = str(args.review_id or "").strip().lower()
            if len(review_id) != 64 or any(ch not in "0123456789abcdef" for ch in review_id):
                raise ValueError("review_id must be a canonical opaque id")
            store.preflight_legacy_review_migration(review_id)
            derived = DerivedStore()
            synthesis_id = migrate_legacy_review(
                review_id, store=store, derived=derived
            )
            delivery = asyncio.run(
                send_pending_review_notifications(store=store, derived=derived, force=True)
            )
            print(json.dumps({
                "review_id": review_id,
                "synthesis_id": synthesis_id,
                "delivery": delivery,
            }, sort_keys=True))
            return 0
        if action == "run-once":
            from agent.plugin_llm import PluginLlm
            from hermes_cli.config import load_config
            from .derived import DerivedStore
            from .extraction import run_extraction_once
            from .synthesis import run_synthesis_once
            from .review import run_notification_once
            from .notion_archive import run_notion_once
            from .spool import RawSpool

            config = load_config() or {}
            spool = RawSpool()
            derived = DerivedStore()
            result = {
                "notion_archive": run_notion_once(store=store, spool=spool, config=config),
                "extraction": run_extraction_once(
                    store=store, spool=spool, derived=derived, config=config
                ),
                "synthesis": run_synthesis_once(
                    store=store,
                    derived=derived,
                    llm=PluginLlm(plugin_id="client-knowledge-gbrain"),
                    config=config,
                ),
                "review_notifications": run_notification_once(
                    store=store,
                    derived=derived,
                    config=config,
                    llm=PluginLlm(plugin_id="client-knowledge-gbrain"),
                ),
            }
            print(json.dumps({"mode": "client_knowledge_pipeline", **result, "stats": store.stats()}, sort_keys=True))
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
