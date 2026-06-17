import asyncio
import os
import time
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from gateway.work_ledger import GatewayWorkLedger, classify_delivery_completion


DISCORD_EPOCH_SECONDS = 1_420_070_400.0


def _discord_snowflake_at(timestamp: float) -> str:
    return str(int((timestamp - DISCORD_EPOCH_SECONDS) * 1000) << 22)


def _discord_event(message_id="m1", text="do the work"):
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="thread-1",
        chat_type="thread",
        user_id="user-1",
        thread_id="thread-1",
        guild_id="guild-1",
        parent_chat_id="channel-1",
        message_id=message_id,
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id=message_id,
    )


def _repo_discord_event(message_id="m1", text="do the work"):
    event = _discord_event(message_id=message_id, text=text)
    event.source.project_path = "/home/droid/hermes"
    event.source.project_github_url = "https://github.com/sligohub/hermes-agent"
    return event


def test_ledger_deduplicates_discord_message_ids(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="m1")
    event.goal_thread_context = "[Goal thread context]\n[Alice] prior detail"
    session_key = build_session_key(event.source)

    first = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    second = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)

    assert first is not None
    assert second is not None
    assert first["id"] == second["id"]
    assert first["_existing"] is False
    assert second["_existing"] is True
    assert len(ledger.incomplete_items()) == 1


def test_ledger_strips_transient_summary_objects(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="m1")
    event.feature_summary = {
        "thread_id": "thread-1",
        "message_id": "summary-1",
        "kanban_board": {"slug": "discord-thread-1", "public_url": "https://example.test/board"},
        "_thread_obj": object(),
        "_message_obj": object(),
    }
    event.project_summary = {
        "channel_id": "channel-1",
        "_channel_obj": object(),
    }
    session_key = build_session_key(event.source)

    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)

    assert item is not None
    stored = ledger.get(item["id"])
    assert stored["feature_summary"] == {
        "thread_id": "thread-1",
        "message_id": "summary-1",
        "kanban_board": {"slug": "discord-thread-1", "public_url": "https://example.test/board"},
    }
    assert stored["project_summary"] == {"channel_id": "channel-1"}


def test_mark_agent_running_clears_stale_terminal_fields(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(message_id="m1")
    event.feature_summary = {
        "message_id": "summary-1",
        "initial_request": "Fix the bug",
    }
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    ledger.mark_agent_done(
        item["id"],
        final_response="Operation interrupted: waiting for model response.",
        session_id="session-1",
        summary_status="Interrupted",
        feature_summary=event.feature_summary,
    )
    ledger.mark_response_delivered(item["id"], result_message_id="stale-result")

    assert ledger.mark_agent_running(item["id"], session_id="session-2") is True

    stored = ledger.get(item["id"])
    assert stored["status"] == "agent_running"
    assert stored["session_id"] == "session-2"
    for key in (
        "agent_done_at",
        "completion_gate",
        "final_response",
        "result_message_id",
        "summary_status",
        "summary_updated_at",
    ):
        assert key not in stored


def test_ledger_skips_completed_and_expires_stale_items(tmp_path):
    now = 1000.0
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: now)
    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=1)

    assert item is not None
    now = 1002.0
    assert ledger.incomplete_items() == []
    assert ledger.get(item["id"])["status"] == "expired"

    fresh = ledger.accept_event(_discord_event(message_id="m2"), session_key=session_key, freshness_seconds=60)
    assert fresh is not None
    ledger.mark_completed(fresh["id"], result_message_id="result-1")
    assert ledger.incomplete_items() == []
    assert ledger.get(fresh["id"])["result_message_id"] == "result-1"


def test_ledger_expires_old_discord_message_ids(tmp_path):
    now = time.time()
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: now)
    old_message_id = _discord_snowflake_at(now - (8 * 24 * 60 * 60))
    event = _discord_event(message_id=old_message_id)
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=3600)

    assert item is not None
    assert ledger.incomplete_items() == []
    assert ledger.get(item["id"])["status"] == "expired"


def test_ledger_keeps_finished_delivery_phases_incomplete_until_completed(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)

    assert item is not None
    ledger.claim(item["id"])
    ledger.mark_agent_running(item["id"], session_id="session-1")
    ledger.mark_agent_done(
        item["id"],
        final_response="normal final answer",
        session_id="session-1",
        summary_status="Complete",
        feature_summary={"message_id": "summary-1", "_message_obj": object()},
    )
    assert ledger.get(item["id"])["status"] == "agent_done"
    assert ledger.get(item["id"])["feature_summary"] == {"message_id": "summary-1"}
    assert ledger.incomplete_items()[0]["final_response"] == "normal final answer"

    ledger.mark_response_delivered(item["id"], result_message_id="result-1")
    assert ledger.get(item["id"])["status"] == "response_delivered"
    assert ledger.incomplete_items()[0]["result_message_id"] == "result-1"

    ledger.mark_summary_updated(item["id"])
    assert ledger.get(item["id"])["status"] == "summary_updated"
    assert ledger.incomplete_items()[0]["status"] == "summary_updated"

    ledger.mark_completed(item["id"])
    assert ledger.incomplete_items() == []


def test_ledger_records_discord_board_final_response_provenance(tmp_path, monkeypatch):
    import gateway.work_ledger as work_ledger

    calls = []
    monkeypatch.setattr(
        work_ledger,
        "_record_discord_board_final_response",
        lambda item, result_message_id=None: calls.append(
            (item.get("final_response"), result_message_id)
        ),
    )
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="m1")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(item["id"], final_response="normal final answer")
    ledger.mark_response_delivered(item["id"], result_message_id="result-1")

    assert calls == [
        ("normal final answer", None),
        ("normal final answer", "result-1"),
    ]


def test_repo_backed_self_declared_incomplete_response_is_blocked(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event()
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="Not done yet: no commit, PR, or deploy. Current working tree has the feature changes but is not committed.",
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored["summary_status"] == "Blocked"
    assert stored["completion_gate"]["allowed_to_complete"] is False
    ledger.mark_summary_updated(item["id"])
    ledger.mark_completed(item["id"])
    stored = ledger.get(item["id"])
    assert stored["status"] == "blocked"
    assert ledger.incomplete_items() == []


def test_explicit_pr_only_request_allows_intentionally_unmerged_final(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Open a PR but don't merge it")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="PR opened and intentionally left unmerged per instruction.",
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored["summary_status"] == "Complete"
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert stored["completion_gate"]["delivery_intent"] == "pr_only"
    ledger.mark_summary_updated(item["id"])
    ledger.mark_completed(item["id"])
    assert ledger.get(item["id"])["status"] == "completed"


def test_default_repo_project_pr_opened_but_unmerged_is_blocked(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Implement the feature")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="PR opened but not merged/deployed.",
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["summary_status"] == "Blocked"


def test_latest_production_browser_timeout_blocks_shipped_verified_completion(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Ship the production modal")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="Shipped and verified in production; browser modal is visible.",
        summary_status="Complete",
        runtime_breakdown={
            "verification_evidence": [
                {
                    "surface": "production_browser",
                    "check_name": "python -m hermes_cli.worker_frontend_smoke --url https://app.example --route /modal --browser chromium",
                    "status": "timeout",
                    "order": 1,
                    "detail": "Command timed out after 30 seconds",
                }
            ]
        },
    )

    stored = ledger.get(item["id"])
    assert stored["summary_status"] == "Blocked"
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["completion_gate"]["reason"] == "latest_verification_evidence_negative"
    blocked = stored["completion_gate"]["verification_constraints"]["blocked_surfaces"]
    assert blocked[0]["surface"] == "production_browser"
    assert "worker_frontend_smoke" in blocked[0]["check_name"]


def test_downgraded_final_response_still_blocks_intact_success_claim(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Ship the production modal")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response=(
            "Shipped and verified in production; browser modal is visible.\n\n"
            "Verification downgrade: production browser verification is not verified: "
            "latest check `browser modal smoke` timeout."
        ),
        summary_status="Complete",
        runtime_breakdown={
            "verification_evidence": [
                {
                    "surface": "production_browser",
                    "check_name": "browser modal smoke",
                    "status": "timeout",
                    "order": 1,
                }
            ]
        },
    )

    stored = ledger.get(item["id"])
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["summary_status"] == "Blocked"


def test_rewritten_downgraded_final_response_allows_operator_summary_with_blocked_surface(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Ship the production modal")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response=(
            "CI passed via scripts/run_tests.sh.\n\n"
            "Verification downgrade: production browser verification is not verified: "
            "latest check `browser modal smoke` timeout."
        ),
        summary_status="Complete",
        runtime_breakdown={
            "verification_evidence": [
                {
                    "surface": "production_browser",
                    "check_name": "browser modal smoke",
                    "status": "timeout",
                    "order": 1,
                }
            ]
        },
    )

    stored = ledger.get(item["id"])
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert stored["summary_status"] == "Complete"


def test_later_successful_production_browser_check_allows_verified_claim(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Ship the production modal")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="Production browser modal verified visible after npm run browser:smoke -- --prod --modal.",
        summary_status="Complete",
        runtime_breakdown={
            "verification_evidence": [
                {
                    "surface": "production_browser",
                    "check_name": "npm run browser:smoke -- --prod --modal",
                    "status": "failure",
                    "order": 1,
                    "detail": "modal missing",
                },
                {
                    "surface": "production_browser",
                    "check_name": "npm run browser:smoke -- --prod --modal",
                    "status": "success",
                    "order": 2,
                    "detail": "modal visible",
                },
            ]
        },
    )

    stored = ledger.get(item["id"])
    assert stored["summary_status"] == "Complete"
    assert stored["completion_gate"]["allowed_to_complete"] is True


def test_verification_gate_keeps_independent_ci_claim_separate(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Ship the production modal")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="CI passed via scripts/run_tests.sh. Production browser modal is not verified: browser modal smoke failed.",
        summary_status="Complete",
        runtime_breakdown={
            "verification_evidence": [
                {"surface": "browser", "check_name": "browser modal smoke", "status": "failure", "order": 1},
                {"surface": "ci", "check_name": "scripts/run_tests.sh tests/unit", "status": "success", "order": 2},
            ]
        },
    )

    stored = ledger.get(item["id"])
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert stored["summary_status"] == "Complete"


def test_verified_project_summary_metadata_downgraded_on_latest_failed_evidence(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Ship the production modal")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="Production browser modal verified visible.",
        summary_status="Complete",
        project_summary={"channel_id": "channel-1", "production_ui": {"status": "verified"}},
        runtime_breakdown={
            "verification_evidence": [
                {
                    "surface": "production_browser",
                    "check_name": "browser modal smoke",
                    "status": "failure",
                    "order": 1,
                    "detail": "modal missing",
                }
            ]
        },
    )

    stored = ledger.get(item["id"])
    assert stored["summary_status"] == "Blocked"
    assert stored["project_summary"]["production_ui"]["status"] == "not_verified"
    assert stored["project_summary"]["verification_guard"]["status"] == "not_verified"


def test_generic_discord_incomplete_wording_preserves_old_completion_behavior(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(text="answer this question")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="Not done yet: no commit, PR, or deploy.",
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored["summary_status"] == "Complete"
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert classify_delivery_completion(stored)["reason"] == "not_repo_backed"
    ledger.mark_summary_updated(item["id"])
    ledger.mark_completed(item["id"])
    assert ledger.get(item["id"])["status"] == "completed"


def test_review_only_request_allows_no_delivery_artifacts_when_review_finished(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Review only; do not implement anything")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="Review complete. No commit, PR, or deploy was needed for this review-only request.",
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored["summary_status"] == "Complete"
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert stored["completion_gate"]["delivery_intent"] == "review_only"
    assert stored["completion_gate"]["reason"] == "intentional_review_only_terminal"


def test_open_pr_for_review_still_requires_pr_artifact(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Open a PR for review but don't merge it")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="Review complete. No PR opened.",
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored["completion_gate"]["delivery_intent"] == "pr_only"
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["summary_status"] == "Blocked"


def test_for_review_without_pr_scope_does_not_disable_delivery_gate(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Implement the feature for review")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="Review complete. No commit, PR, or deploy.",
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored["completion_gate"]["delivery_intent"] == "full_lifecycle"
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["summary_status"] == "Blocked"


@pytest.mark.asyncio
async def test_post_delivery_summary_uses_blocked_status_for_gated_work(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    callbacks = []
    adapter = SimpleNamespace(
        register_post_delivery_callback=lambda session_key, callback, generation=None: callbacks.append(callback)
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)

    event = _repo_discord_event(message_id="m1")
    event.feature_summary = {"message_id": "summary-1", "initial_request": "Implement it"}
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    event.work_item_id = item["id"]
    final_response = "Not done yet: no commit, PR, or deploy. Current working tree has the feature changes but is not committed."
    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response=final_response,
        session_id="session-1",
        summary_status="Complete",
        feature_summary=event.feature_summary,
    )

    runner._register_discord_summary_post_delivery(
        event=event,
        source=event.source,
        session_key=session_key,
        run_generation=None,
        session_id="session-1",
        final_response=final_response,
        agent_result={},
    )

    assert len(callbacks) == 1
    assert await callbacks[0]() is True
    runner._update_discord_summaries.assert_awaited_once()
    assert runner._update_discord_summaries.await_args.kwargs["status"] == "Blocked"
    stored = runner.work_ledger.get(item["id"])
    assert stored["status"] == "blocked"
    assert stored["summary_status"] == "Blocked"


@pytest.mark.asyncio
async def test_startup_replays_only_incomplete_discord_work(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner.adapters = {Platform.DISCORD: type("Adapter", (), {})()}
    runner.adapters[Platform.DISCORD].handle_message = AsyncMock()
    runner._background_tasks = set()

    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    runner.work_ledger.claim(item["id"])
    # Simulate a previous gateway process.  An alive current PID would not be
    # replayed, because that would duplicate active work.
    data = runner.work_ledger._read()
    data["items"][item["id"]]["claim_pid"] = os.getpid() + 10_000_000
    runner.work_ledger._write(data)

    scheduled = runner._schedule_incomplete_discord_work_items()
    await asyncio.sleep(0)

    assert scheduled == 1
    runner.adapters[Platform.DISCORD].handle_message.assert_awaited_once()
    replay = runner.adapters[Platform.DISCORD].handle_message.await_args.args[0]
    assert replay.work_replay is True
    assert replay.work_item_id == item["id"]
    assert replay.text == "do the work"
    assert replay.goal_thread_context == event.goal_thread_context


@pytest.mark.asyncio
async def test_startup_auto_resume_reuses_original_discord_work_item(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner._background_tasks = set()
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner.adapters = {Platform.DISCORD: adapter}

    event = _discord_event(message_id="m1")
    event.feature_summary = {
        "message_id": "summary-1",
        "initial_request": "do the work",
    }
    event.goal_thread_context = "[Goal thread context]\n[Alice] details"
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response="",
        session_id="session-1",
        summary_status="Interrupted",
        feature_summary=event.feature_summary,
    )
    entry = SimpleNamespace(
        session_key=session_key,
        origin=event.source,
        resume_pending=True,
        suspended=False,
        resume_reason="restart_timeout",
        last_resume_marked_at=datetime.now(),
        updated_at=datetime.now(),
    )
    runner.session_store = MagicMock()
    runner.session_store._entries = {session_key: entry}

    scheduled = runner._schedule_resume_pending_sessions()
    await asyncio.sleep(0)

    assert scheduled == 1
    adapter.handle_message.assert_awaited_once()
    resumed = adapter.handle_message.await_args.args[0]
    assert resumed.internal is True
    assert resumed.work_item_id == item["id"]
    assert resumed.work_replay is True
    assert resumed.feature_summary == event.feature_summary
    assert resumed.goal_thread_context == event.goal_thread_context
    assert resumed.message_id == "m1"


@pytest.mark.asyncio
async def test_startup_defers_interrupted_discord_work_for_resume_pending_session(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner._background_tasks = set()
    adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        _send_with_retry=AsyncMock(),
        update_feature_summary=AsyncMock(return_value=True),
    )
    runner.adapters = {Platform.DISCORD: adapter}

    event = _discord_event(message_id="m1")
    event.feature_summary = {
        "message_id": "summary-1",
        "initial_request": "do the work",
    }
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response="",
        session_id="session-1",
        summary_status="Interrupted",
        feature_summary=event.feature_summary,
    )
    runner.session_store = SimpleNamespace(
        _entries={
            session_key: SimpleNamespace(
                resume_pending=True,
                suspended=False,
                resume_reason="restart_timeout",
                last_resume_marked_at=datetime.now(),
                updated_at=datetime.now(),
            )
        }
    )

    scheduled = runner._schedule_incomplete_discord_work_items()

    assert scheduled == 0
    adapter.handle_message.assert_not_called()
    adapter._send_with_retry.assert_not_awaited()
    adapter.update_feature_summary.assert_not_awaited()
    assert runner.work_ledger.get(item["id"])["status"] == "agent_done"


@pytest.mark.asyncio
async def test_post_delivery_summary_recovers_existing_discord_work_item(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner.adapters = {}
    runner._update_discord_summaries = AsyncMock(return_value=True)
    captured = {}

    def register_callback(session_key, callback, *, generation=None):
        captured["session_key"] = session_key
        captured["callback"] = callback
        captured["generation"] = generation

    adapter = SimpleNamespace(register_post_delivery_callback=register_callback)
    runner.adapters = {Platform.DISCORD: adapter}

    event = _discord_event(message_id="m1")
    event.feature_summary = {"message_id": "summary-1", "initial_request": "do the work"}
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None

    # Recreate the auto-resume shape from production: the synthesized event has
    # the original source/message id but may not carry the prior ledger handle.
    resume_event = _discord_event(message_id="m1")

    runner._register_discord_summary_post_delivery(
        event=resume_event,
        source=resume_event.source,
        session_key=session_key,
        run_generation=7,
        session_id="session-2",
        final_response="final answer",
        agent_result={"api_calls": 1},
    )

    assert captured["session_key"] == session_key
    assert captured["generation"] == 7
    assert resume_event.work_item_id == item["id"]
    assert resume_event.feature_summary == event.feature_summary

    assert await captured["callback"]() is True

    runner._update_discord_summaries.assert_awaited_once()
    assert runner._update_discord_summaries.await_args.kwargs["feature_summary"] == event.feature_summary
    assert runner.work_ledger.get(item["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_startup_delivers_agent_done_work_without_rerunning_agent(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner._background_tasks = set()
    runner._session_db = None
    adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        _send_with_retry=AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="result-1")
        ),
        update_feature_summary=AsyncMock(return_value=True),
    )
    runner.adapters = {Platform.DISCORD: adapter}

    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response="normal final answer",
        session_id="session-1",
        summary_status="Complete",
        feature_summary={"message_id": "summary-1"},
    )

    scheduled = runner._schedule_incomplete_discord_work_items()
    if runner._background_tasks:
        await asyncio.gather(*runner._background_tasks)

    assert scheduled == 1
    adapter.handle_message.assert_not_called()
    adapter._send_with_retry.assert_awaited_once()
    assert adapter._send_with_retry.await_args.kwargs["content"] == "normal final answer"
    adapter.update_feature_summary.assert_awaited_once()
    assert runner.work_ledger.get(item["id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_startup_delivers_repo_incomplete_work_as_blocked_not_completed(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner._background_tasks = set()
    runner._session_db = None
    adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        _send_with_retry=AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="result-1")
        ),
        update_feature_summary=AsyncMock(return_value=True),
    )
    runner.adapters = {Platform.DISCORD: adapter}

    event = _repo_discord_event(message_id="m1")
    event.feature_summary = {"message_id": "summary-1", "initial_request": "Implement it"}
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response="Not done yet: no commit, PR, or deploy. Current working tree has the feature changes but is not committed.",
        session_id="session-1",
        summary_status="Complete",
        feature_summary=event.feature_summary,
    )

    scheduled = runner._schedule_incomplete_discord_work_items()
    if runner._background_tasks:
        await asyncio.gather(*runner._background_tasks)

    assert scheduled == 1
    adapter.handle_message.assert_not_called()
    adapter._send_with_retry.assert_awaited_once()
    adapter.update_feature_summary.assert_awaited_once()
    assert adapter.update_feature_summary.await_args.kwargs["status"] == "Blocked"
    stored = runner.work_ledger.get(item["id"])
    assert stored["status"] == "blocked"
    assert stored["summary_status"] == "Blocked"


@pytest.mark.asyncio
async def test_startup_updates_agent_done_summary_without_final_response(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner._background_tasks = set()
    runner._session_db = None
    adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        _send_with_retry=AsyncMock(),
        update_feature_summary=AsyncMock(return_value=True),
    )
    runner.adapters = {Platform.DISCORD: adapter}

    event = _discord_event(message_id="m1")
    event.feature_summary = {
        "message_id": "summary-1",
        "initial_request": "do the work",
    }
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response="",
        session_id="session-1",
        summary_status="Interrupted",
        feature_summary=event.feature_summary,
    )

    scheduled = runner._schedule_incomplete_discord_work_items()
    if runner._background_tasks:
        await asyncio.gather(*runner._background_tasks)

    assert scheduled == 1
    adapter.handle_message.assert_not_called()
    adapter._send_with_retry.assert_not_awaited()
    adapter.update_feature_summary.assert_awaited_once()
    assert adapter.update_feature_summary.await_args.kwargs["status"] == "Interrupted"
    assert runner.work_ledger.get(item["id"])["status"] == "completed"


def test_discord_worker_reference_context_resolves_bare_message_ids(monkeypatch):
    from hermes_cli import discord_worker_boards as dwb

    def fake_fetch(channel_id, message_id):
        if channel_id == "parent-1" and message_id == "1507176047022575776":
            return {
                "id": message_id,
                "content": "reported bug details",
                "author": {"username": "alice"},
            }
        return None

    monkeypatch.setattr(dwb, "_fetch_discord_message_reference", fake_fetch)

    refs = dwb._discord_reference_context(
        "fix the bug reported in message 1507176047022575776",
        {"chat_id": "thread-1", "parent_channel_id": "parent-1"},
    )

    assert len(refs) == 1
    assert refs[0]["id"] == "1507176047022575776"
    assert refs[0]["channel_id"] == "parent-1"
    assert refs[0]["content"] == "reported bug details"
