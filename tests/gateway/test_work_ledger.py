import asyncio
import os
import time
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.provider_progress import clear_provider_progress_signal, latest_provider_progress_signal
from agent.visual_qa import normalize_visual_requirement, visual_requirement_id
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


def _visual_receipt(requirement, *, order=3, status="passed", evidence_ref=""):
    normalized = normalize_visual_requirement(requirement)
    assertion_ids = [
        item["id"] for item in normalized["assertions"] if isinstance(item, dict)
    ]
    receipt = {
        "requirement_id": visual_requirement_id(normalized),
        "contract_id": "vac_" + ("a" * 24),
        "assertion_ids": assertion_ids,
        "status": status,
        "attempts": 1,
        "vision_calls": 0,
        "duration_ms": 25,
        "diagnostic_codes": ["no_horizontal_overflow_satisfied"],
        "order": order,
    }
    if evidence_ref:
        receipt["evidence_ref"] = evidence_ref
    return receipt


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


def test_ledger_persists_only_base_prompt_for_direct_question(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="intent-message")
    event.discord_action_request_intent = False
    event.discord_action_request_base_channel_prompt = "Project instructions"
    event.channel_prompt = "Project instructions\n\nDirect question overlay"

    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )

    assert item is not None
    stored = ledger.get(item["id"])
    assert stored["channel_prompt"] == "Project instructions"
    assert "discord_action_request_intent" not in stored
    assert "discord_action_request_base_channel_prompt" not in stored
    replay = ledger.event_from_item(stored)
    assert replay.discord_action_request_intent is None
    assert replay.discord_action_request_base_channel_prompt is None
    assert replay.channel_prompt == "Project instructions"


def test_legacy_ledger_item_replays_with_none_action_intent(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="legacy-message")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None
    stored = ledger.get(item["id"])
    replay = ledger.event_from_item(stored)

    assert replay.discord_action_request_intent is None


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


def test_ledger_persists_normalized_visual_requirement_from_feature_summary(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(text="Please handle this request.")
    event.feature_summary = {
        "initial_request": "Build a responsive dashboard with a mobile sidebar.",
    }

    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit", "unexpected_secret": "do-not-store"},
    )

    assert item is not None
    stored = ledger.get(item["id"])
    assert stored["visual_qa_config"] == {
        "mode": "enforce_explicit",
        "max_receipts_per_turn": 1,
        "max_followup_turns": 1,
        "max_attempts": 2,
        "max_assertions": 6,
        "max_vision_calls": 1,
        "attempt_timeout_s": 30.0,
        "total_timeout_s": 60.0,
        "max_output_chars": 6000,
    }
    requirement = stored["visual_qa_requirement"]
    assert requirement["level"] == "surface"
    assert requirement["target"].startswith("vtarget_")
    assert all(
        item["id"].startswith("vassert_") and item["kind"] == "screenshot_appearance"
        for item in requirement["assertions"]
    )
    assert "responsive dashboard" not in repr(requirement).lower()
    assert "mobile sidebar" not in repr(requirement).lower()
    replay = ledger.event_from_item(stored)
    assert replay.visual_qa_requirement == requirement
    assert replay.visual_qa_config == stored["visual_qa_config"]


def test_shadow_visual_qa_reports_missing_fresh_receipt_without_blocking(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(text="Build a responsive dashboard with a mobile sidebar.")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "shadow"},
    )
    assert item is not None

    assert ledger.mark_agent_done(
        item["id"],
        final_response="Implemented the dashboard.",
        visual_qa_receipts=[],
        visual_qa_code_mutation_observed=True,
        visual_qa_min_receipt_order=2,
    )

    stored = ledger.get(item["id"])
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert stored["completion_gate"]["visual_qa"] == {
        "mode": "shadow",
        "requirement": stored["visual_qa_requirement"],
        "code_mutation_observed": True,
        "enforced": False,
        "status": "missing",
        "receipt": None,
        "min_receipt_order": 2,
        "shadow_report": "receipt_missing",
    }
    assert ledger.mark_completed(item["id"])
    assert ledger.get(item["id"])["status"] == "completed"


def test_gateway_visual_qa_context_and_turn_state_are_bounded():
    from gateway.run import _visual_qa_context_prompt, _visual_qa_turn_result

    requirement = normalize_visual_requirement(
        {
            "level": "surface",
            "target": "responsive dashboard",
            "assertions": ["dashboard has no unintended overflow"],
        }
    )
    config = {"mode": "enforce_explicit", "max_receipts_per_turn": 1, "max_followup_turns": 1}
    prompt = _visual_qa_context_prompt(requirement, config)
    assert "mode=enforce_explicit" in prompt
    assert "call the dedicated `visual_qa` tool" in prompt
    assert "attach receipt arguments" in prompt
    assert "Generic navigation" in prompt

    agent = SimpleNamespace(
        _turn_file_mutation_paths={"/private/workspace/dashboard.tsx"},
        _visual_qa_last_edit_order=2,
    )
    state = _visual_qa_turn_result(
        agent,
        {
            "visual_qa_receipts": [
                _visual_receipt(requirement, order=1, status="failed"),
                _visual_receipt(requirement, order=3),
            ]
        },
        requirement,
    )
    assert state == {
        "receipts": [_visual_receipt(requirement, order=3)],
        "code_mutation_observed": True,
        "min_receipt_order": 3,
    }
    assert "private/workspace" not in str(state)


def test_enforced_visual_qa_requires_a_fresh_post_edit_receipt(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    stale_event = _discord_event(
        message_id="stale",
        text="Build a responsive dashboard with a mobile sidebar.",
    )
    stale = ledger.accept_event(
        stale_event,
        session_key=build_session_key(stale_event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert stale is not None
    stale_requirement = stale["visual_qa_requirement"]
    assert ledger.mark_agent_done(
        stale["id"],
        final_response="Implemented the dashboard.",
        visual_qa_receipts=[_visual_receipt(stale_requirement, order=2)],
        visual_qa_code_mutation_observed=True,
        visual_qa_min_receipt_order=3,
    )
    stale_stored = ledger.get(stale["id"])
    assert stale_stored["completion_gate"]["allowed_to_complete"] is False
    assert stale_stored["completion_gate"]["reason"] == "visual_qa_receipt_missing"

    fresh_event = _discord_event(
        message_id="fresh",
        text="Build a responsive dashboard with a mobile sidebar.",
    )
    fresh = ledger.accept_event(
        fresh_event,
        session_key=build_session_key(fresh_event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert fresh is not None
    fresh_requirement = fresh["visual_qa_requirement"]
    assert ledger.mark_agent_done(
        fresh["id"],
        final_response="Implemented the dashboard.",
        visual_qa_receipts=[
            _visual_receipt(fresh_requirement, order=2, status="failed"),
            _visual_receipt(fresh_requirement, order=3),
        ],
        visual_qa_code_mutation_observed=True,
        visual_qa_min_receipt_order=3,
    )
    fresh_stored = ledger.get(fresh["id"])
    assert fresh_stored["completion_gate"]["allowed_to_complete"] is True
    assert fresh_stored["completion_gate"]["visual_qa"]["status"] == "passed"


def test_enforced_visual_qa_blocks_unverifiable_mutation_and_strips_unsafe_receipt(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(text="Build a responsive dashboard with a mobile sidebar.")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert item is not None
    unsafe = _visual_receipt(
        item["visual_qa_requirement"],
        evidence_ref="https://example.test/check?access_token=do-not-store",
    )

    assert ledger.mark_agent_done(
        item["id"],
        final_response="Implemented the dashboard.",
        runtime_breakdown={"visual_qa_receipts": [unsafe]},
        visual_qa_receipts=[unsafe],
        visual_qa_code_mutation_observed=True,
    )

    stored = ledger.get(item["id"])
    assert stored["visual_qa_receipts"] == []
    assert stored["runtime_breakdown"]["visual_qa_receipts"] == []
    assert "do-not-store" not in str(stored["visual_qa_receipts"])
    assert "do-not-store" not in str(stored["runtime_breakdown"])
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["completion_gate"]["reason"] == "visual_qa_receipt_unverifiable"
    assert stored["completion_gate"]["visual_qa"]["status"] == "missing"


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
        "provider_no_progress",
        "result_message_id",
        "summary_status",
        "summary_updated_at",
    ):
        assert key not in stored


def test_agent_run_guard_requires_exact_active_generation(monkeypatch, tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(message_id="active-generation")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    ledger.claim(
        item["id"],
        session_key=session_key,
        run_generation=4,
        owner_pid=4242,
        process_epoch="boot-a",
    )
    assert ledger.mark_agent_running(
        item["id"],
        session_id="session-1",
        session_key=session_key,
        run_generation=4,
        owner_pid=4242,
        process_epoch="boot-a",
    )
    stored = ledger.get(item["id"])
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: pid == 4242)

    assert ledger.agent_run_active(
        stored,
        session_key=session_key,
        run_generation=4,
        process_epoch="boot-a",
        registry_active=True,
    ) is True
    assert ledger.agent_run_active(
        stored,
        session_key=session_key,
        run_generation=5,
        process_epoch="boot-a",
        registry_active=True,
    ) is False
    assert ledger.agent_run_active(
        stored,
        session_key=session_key,
        run_generation=4,
        process_epoch="boot-a",
        registry_active=False,
    ) is False


def test_run_state_cas_finalizes_only_unchanged_active_run(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(message_id="run-state-success")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_id="session-1",
        session_key=session_key,
        run_generation=4,
        owner_pid=4242,
        process_epoch="boot-a",
    )
    expected = ledger.capture_run_state(
        item["id"],
        session_key=session_key,
        run_generation=4,
        owner_pid=4242,
        process_epoch="boot-a",
    )

    assert expected == {
        "status": "agent_running",
        "active_run": {
            "session_key": session_key,
            "generation": 4,
            "owner_pid": 4242,
            "process_epoch": "boot-a",
            "lease_until": 3700.0,
        },
    }
    assert ledger.mark_agent_done(
        item["id"],
        final_response="done",
        expected_run_state=expected,
    ) is True
    stored = ledger.get(item["id"])
    assert stored["status"] == "agent_done"
    assert stored["active_run"] is None


def test_run_state_snapshot_is_bounded_and_preserves_long_identity_changes():
    prefix = "x" * 400
    first = GatewayWorkLedger.run_state_snapshot(
        {
            "status": prefix + "-status-a",
            "active_run": {
                "session_key": prefix + "-session-a",
                "generation": 1 << 80,
                "owner_pid": float("inf"),
                "process_epoch": prefix + "-epoch-a",
                "lease_until": float("nan"),
            },
        }
    )
    second = GatewayWorkLedger.run_state_snapshot(
        {
            "status": prefix + "-status-b",
            "active_run": {
                "session_key": prefix + "-session-b",
                "generation": 1 << 80,
                "owner_pid": float("inf"),
                "process_epoch": prefix + "-epoch-b",
                "lease_until": float("nan"),
            },
        }
    )

    assert len(first["status"]) <= 240
    assert len(first["active_run"]["session_key"]) <= 240
    assert len(first["active_run"]["process_epoch"]) <= 160
    assert first["active_run"]["generation"] == (1 << 63) - 1
    assert first["active_run"]["owner_pid"] == 0
    assert first["active_run"]["lease_until"] is None
    assert first["status"] != second["status"]
    assert first["active_run"]["session_key"] != second["active_run"]["session_key"]
    assert first["active_run"]["process_epoch"] != second["active_run"]["process_epoch"]


def test_run_state_cas_rejects_same_pid_new_generation_and_epoch_without_mutation(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(message_id="run-state-generation")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=4,
        owner_pid=4242,
        process_epoch="boot-a",
    )
    expected = ledger.run_state_snapshot(ledger.get(item["id"]))
    assert ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=5,
        owner_pid=4242,
        process_epoch="boot-b",
    )
    before = ledger.get(item["id"])

    assert ledger.mark_agent_done(
        item["id"],
        final_response="stale result",
        summary_status="Complete",
        expected_run_state=expected,
    ) is False
    assert ledger.get(item["id"]) == before


def test_run_state_cas_rejects_lease_renewal_without_partial_mutation(tmp_path):
    now = 100.0
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: now)
    event = _discord_event(message_id="run-state-lease")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=4,
        owner_pid=4242,
        process_epoch="boot-a",
    )
    expected = ledger.run_state_snapshot(ledger.get(item["id"]))
    now = 101.0
    assert ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=4,
        owner_pid=4242,
        process_epoch="boot-a",
    )
    before = ledger.get(item["id"])

    assert ledger.mark_agent_done(
        item["id"],
        final_response="stale result",
        runtime_breakdown={"total": 1},
        expected_run_state=expected,
    ) is False
    assert ledger.get(item["id"]) == before


def test_mark_completed_cas_rejects_run_started_after_observation(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(message_id="completed-run-race")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    assert ledger.mark_agent_done(item["id"], final_response="done")
    assert ledger.mark_summary_updated(item["id"])
    expected = ledger.run_state_snapshot(ledger.get(item["id"]))
    assert ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=2,
        owner_pid=4242,
        process_epoch="boot-b",
    )
    before = ledger.get(item["id"])

    assert ledger.mark_completed(
        item["id"],
        result_message_id="stale-result",
        expected_run_state=expected,
    ) is False
    assert ledger.get(item["id"]) == before


def test_live_gateway_does_not_keep_abandoned_turn_active(monkeypatch, tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="abandoned-turn")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    ledger.claim(
        item["id"],
        session_key=session_key,
        run_generation=2,
        owner_pid=os.getpid(),
    )
    stored = ledger.get(item["id"])
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    runner._session_run_generation = {session_key: 2}
    runner._running_agents = {"another-session": object()}
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: True)

    assert runner._work_item_agent_run_active(stored) is False


def test_gateway_run_guard_accepts_exact_process_epoch(monkeypatch, tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="exact-process-epoch")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    ledger.claim(
        item["id"],
        session_key=session_key,
        run_generation=1,
        owner_pid=5151,
        process_epoch="gateway-boot",
    )
    stored = ledger.get(item["id"])
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    runner._session_run_generation = {session_key: 1}
    runner._running_agents = {session_key: object()}
    runner._process_epoch = "gateway-boot"
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: pid == 5151)

    assert runner._work_item_agent_run_active(stored) is True


def test_pid_reuse_cannot_revive_stale_turn(monkeypatch, tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="pid-reuse")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    ledger.claim(
        item["id"],
        session_key=session_key,
        run_generation=1,
        owner_pid=5151,
        process_epoch="old-process",
    )
    stored = ledger.get(item["id"])
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: True)

    assert ledger.agent_run_active(
        stored,
        session_key=session_key,
        run_generation=1,
        process_epoch="restarted-process",
        registry_active=True,
    ) is False


def test_legacy_active_run_without_process_epoch_is_not_authoritatively_live(monkeypatch, tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="legacy-process-epoch")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    ledger.claim(
        item["id"],
        session_key=session_key,
        run_generation=1,
        owner_pid=5151,
    )
    stored = ledger.get(item["id"])
    stored["active_run"].pop("process_epoch", None)
    monkeypatch.setattr("gateway.status._pid_exists", lambda _pid: True)

    assert ledger.agent_run_active(
        stored,
        session_key=session_key,
        run_generation=1,
        process_epoch="current-process",
        registry_active=True,
    ) is False


def test_agent_run_guard_uses_platform_safe_process_helper(monkeypatch, tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="platform-safe")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    ledger.claim(
        item["id"],
        session_key=session_key,
        run_generation=3,
        owner_pid=6161,
        process_epoch="boot-safe",
    )
    stored = ledger.get(item["id"])
    seen = []
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: seen.append(pid) or True)
    monkeypatch.setattr(os, "kill", lambda *_args: pytest.fail("os.kill must not be used"))

    assert ledger.agent_run_active(
        stored,
        session_key=session_key,
        run_generation=3,
        process_epoch="boot-safe",
        registry_active=True,
    ) is True
    assert seen == [6161]


def test_mark_agent_running_clears_prior_visual_receipt_but_keeps_contract(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(text="Build a responsive dashboard with a mobile sidebar.")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert item is not None
    requirement = item["visual_qa_requirement"]
    assert ledger.mark_agent_done(
        item["id"],
        final_response="Implemented the dashboard.",
        runtime_breakdown={"visual_qa_receipts": [_visual_receipt(requirement, order=3)]},
        visual_qa_receipts=[_visual_receipt(requirement, order=3)],
        visual_qa_code_mutation_observed=True,
        visual_qa_min_receipt_order=3,
    )

    assert ledger.mark_agent_running(item["id"], session_id="retry-session")

    stored = ledger.get(item["id"])
    assert stored["visual_qa_requirement"] == requirement
    assert stored["visual_qa_config"]["mode"] == "enforce_explicit"
    assert stored["visual_qa_receipts"] == []
    assert stored["runtime_breakdown"]["visual_qa_receipts"] == []
    assert "visual_qa_code_mutation_observed" not in stored
    assert "visual_qa_min_receipt_order" not in stored


def test_mark_agent_done_persists_provider_no_progress_metadata(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None

    event_payload = {
        "event": "provider_no_progress_threshold_exceeded",
        "session_id": "sess-123",
        "gateway_session_key": session_key,
        "provider": "openai-codex",
        "model": "gpt-5.5",
        "phase": "provider_invalid_response",
        "failure_class": "invalid_response",
        "action": "degraded_partial",
        "no_progress_elapsed_s": 901.2,
        "threshold_s": 900,
        "retry_count": 2,
        "delay_class": "15m_30m",
        "last_progress_reason": "successful_tool_call",
    }

    assert ledger.mark_agent_done(
        item["id"],
        final_response="Provider no-progress guard stopped this turn after useful work was preserved.",
        session_id="sess-123",
        summary_status="Failed",
        provider_no_progress=event_payload,
        already_delivered=False,
    ) is True

    stored = ledger.get(item["id"])
    assert stored["status"] == "agent_done"
    assert stored["summary_status"] == "Failed"
    assert stored["provider_no_progress"] == event_payload
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert "sk-" not in str(stored["provider_no_progress"])


def test_work_ledger_state_transitions_record_provider_progress(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="m1")
    session_key = build_session_key(event.source)
    clear_provider_progress_signal(session_key)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None

    assert ledger.mark_agent_running(item["id"], session_id="sess-1") is True
    signal = latest_provider_progress_signal(session_key)
    assert signal is not None
    assert signal["source"] == "work_ledger"
    assert signal["phase"] == "work_ledger"
    assert signal["reason"] == "ledger_status_agent_running"
    assert signal["metadata"] == {"work_id": item["id"], "status": "agent_running"}

    assert ledger.mark_summary_updated(item["id"]) is True
    signal = latest_provider_progress_signal(session_key)
    assert signal is not None
    assert signal["reason"] == "ledger_status_summary_updated"
    assert signal["metadata"] == {"work_id": item["id"], "status": "summary_updated"}


def test_all_ledger_read_modify_write_mutations_use_file_lock(monkeypatch, tmp_path):
    import gateway.work_ledger as work_ledger

    entries = []

    @contextmanager
    def tracked_lock(path):
        entries.append(path)
        yield

    monkeypatch.setattr(work_ledger, "_ledger_file_lock", tracked_lock)
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")

    item = ledger.accept_event(
        _discord_event(message_id="locked-complete"),
        session_key="locked-complete",
        freshness_seconds=60,
    )
    ledger.claim(item["id"])
    ledger.mark_agent_running(item["id"], session_id="session-1")
    ledger.mark_agent_done(item["id"], final_response="done")
    ledger.mark_response_delivered(item["id"], result_message_id="result-1")
    ledger.mark_summary_updated(item["id"])
    ledger.mark_completed(item["id"])

    blocked = ledger.accept_event(
        _discord_event(message_id="locked-block"),
        session_key="locked-block",
        freshness_seconds=60,
    )
    ledger.mark_blocked(blocked["id"], reason="test")
    expired = ledger.accept_event(
        _discord_event(message_id="locked-expire"),
        session_key="locked-expire",
        freshness_seconds=60,
    )
    ledger.mark_expired(expired["id"])

    assert len(entries) == 11
    assert set(entries) == {ledger.path}


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
    expected_run_state = ledger.run_state_snapshot(ledger.get(item["id"]))
    ledger.mark_agent_done(
        item["id"],
        final_response="normal final answer",
        session_id="session-1",
        summary_status="Complete",
        feature_summary={"message_id": "summary-1", "_message_obj": object()},
        expected_run_state=expected_run_state,
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


def test_ledger_persists_all_bounded_confirmed_message_ids(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    item = ledger.accept_event(
        _discord_event(message_id="multi-result"),
        session_key="multi-result",
        freshness_seconds=60,
    )

    assert ledger.mark_response_delivered(
        item["id"],
        result_message_id="chunk-1",
        confirmed_message_ids=("chunk-1", "chunk-2", "chunk-2", "bad id!"),
    )

    stored = ledger.get(item["id"])
    assert stored["result_message_id"] == "chunk-1"
    assert stored["confirmed_message_ids"] == ["chunk-1", "chunk-2", "badid"]
    assert stored["delivery_outcome"] == "delivered"


def test_ledger_bounds_confirmed_message_id_arrays(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    item = ledger.accept_event(
        _discord_event(message_id="bounded-results"),
        session_key="bounded-results",
        freshness_seconds=60,
    )

    assert ledger.mark_response_delivered(
        item["id"],
        result_message_id="chunk-0",
        confirmed_message_ids=(f"chunk-{index}" for index in range(200)),
    )

    stored = ledger.get(item["id"])
    assert len(stored["confirmed_message_ids"]) == 128
    assert stored["confirmed_message_ids"][0] == "chunk-0"
    assert stored["confirmed_message_ids"][-1] == "chunk-127"


def test_ledger_partial_delivery_is_durable_uncertain_and_cannot_complete(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    item = ledger.accept_event(
        _discord_event(message_id="partial-result"),
        session_key="partial-result",
        freshness_seconds=60,
    )

    assert ledger.mark_response_delivery_uncertain(
        item["id"],
        result_message_id="chunk-1",
        confirmed_message_ids=("chunk-1",),
        reason="partial_send_confirmed",
    )
    uncertain = ledger.get(item["id"])
    assert uncertain["status"] == "response_delivered"
    assert uncertain["delivery_outcome"] == "uncertain"
    assert uncertain["confirmed_message_ids"] == ["chunk-1"]
    assert uncertain["completion_gate"]["allowed_to_complete"] is False

    assert ledger.mark_completed(item["id"])
    stored = ledger.get(item["id"])
    assert stored["status"] == "blocked"
    assert stored["delivery_outcome"] == "uncertain"
    assert stored["confirmed_message_ids"] == ["chunk-1"]


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


def test_full_lifecycle_blocks_unsynced_runtime_and_unverified_live_pickup(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Ship the runtime change")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response=(
            "PR merged and checks passed, but private runtime is not synced yet "
            "and live pickup is not verified."
        ),
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Blocked"
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["completion_gate"]["delivery_intent"] == "full_lifecycle"
    assert stored["completion_gate"]["matched_markers"] == [
        "runtime_not_synced",
        "live_pickup_unverified",
    ]


def test_full_lifecycle_allows_preserved_protected_checkout_when_production_is_current(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Proceed with the same treatment across the other sources")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response=(
            "77 official items audited; 52 published and 25 intentionally held by quality gates. "
            "Production deployment and main CI passed. Airflow runtime is current at d6e4042, clean, "
            "zero behind, no sensitive lag; all services are healthy. Preserved: the protected "
            "canonical checkout remains untouched because it has a pre-existing deleted test file and "
            "is 16 commits behind. The active worktree and production runtime are current."
        ),
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Complete"
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert stored["completion_gate"]["reason"] == "no_self_declared_delivery_gap"


def test_full_lifecycle_allows_background_watch_after_live_pickup(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Schedule the Airflow DAG")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response=(
            "PR merged, checks passed, canonical/runtime checkouts synced, and "
            "live pickup verified. The first scheduled Airflow DAG run is still "
            "running; I started a background watcher and it will report completion."
        ),
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Complete"
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert stored["completion_gate"]["delivery_intent"] == "full_lifecycle"


def test_full_lifecycle_blocks_background_watch_without_live_pickup(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Schedule the Airflow DAG")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response=(
            "PR merged and checks passed. The first scheduled Airflow DAG run is still "
            "running; I started a background watcher and it will report completion."
        ),
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Blocked"
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["completion_gate"]["reason"] == "runtime_handoff_unverified"
    assert stored["completion_gate"]["matched_markers"] == [
        "runtime_sync_unverified",
        "live_pickup_unverified",
    ]


def test_full_lifecycle_blocks_pending_ci(tmp_path):
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
        final_response="PR merged but CI is still running.",
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Blocked"
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["completion_gate"]["matched_markers"] == ["checks_not_green"]


def test_full_lifecycle_phase_timing_does_not_fake_pending_ci_or_runtime_sync(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Fix the Discord status bug")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response=(
            "PR CI passed and the canonical checkout is synced.\n\n"
            "Phase timing: inspect inherited/verified → edit/check → PR/CI/merge/sync "
            "→ manual stale embed repair done; live runtime pickup pending safe restart."
        ),
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Blocked"
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["completion_gate"]["matched_markers"] == ["live_pickup_unverified"]


def test_safe_gateway_reload_watcher_handoff_allows_final_summary(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Fix the Discord status bug")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response=(
            "PR CI passed and canonical `/home/droid/hermes` is synced clean.\n"
            "The running gateway process was started before the merge. I started "
            "`hermes-safe-gateway-reload-591.service`; it's waiting for active_agents=0, "
            "then it will send SIGUSR1.\n"
            "Phase timing: inspect → edit/check → PR/CI/merge/sync → "
            "stale emoji repair → live runtime pickup pending safe reload watcher queued."
        ),
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Complete"
    assert stored["completion_gate"]["allowed_to_complete"] is True


def test_review_request_allows_negative_findings_about_uncommitted_manifest_paths(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="review the current pipeline given we just added references")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response=(
            "Reviewed. I did not edit files.\n\n"
            "The current artifact is releasable under existing rules, but the pipeline has holes.\n\n"
            "Recommendation: require referenced manifests or stop citing uncommitted manifest paths as evidence."
        ),
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Complete"
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert stored["completion_gate"]["delivery_intent"] == "review_only"


def test_uncommitted_changes_still_block_full_lifecycle_repo_work(tmp_path):
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
        final_response="Not done yet: uncommitted changes remain in the working tree.",
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Blocked"
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["completion_gate"]["matched_markers"] == ["not_done_yet", "not_committed"]


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


def test_successful_action_request_not_blocked_by_pr_merge_downgrade_line(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Enable the production direct worker and verify the route")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response=(
            "Task list is fully complete: 5/5.\n"
            "Direct worker enabled and healthy/current.\n"
            "Production API direct path is live.\n"
            "Request generated via direct_worker.\n"
            "Focused route test passed 7/7.\n\n"
            "Verification downgrade: PR/merge verification is not verified: latest check "
            "`git -C /home/droid/workspaces/PID-airflow-runtime status --short --branch && "
            "git -C /home/droid/workspaces/PID-airflow-runtime pull --ff-only origin main && git` failure."
        ),
        summary_status="Complete",
        runtime_breakdown={
            "verification_evidence": [
                {
                    "surface": "pr",
                    "check_name": (
                        "git -C /home/droid/workspaces/PID-airflow-runtime status --short --branch && "
                        "git -C /home/droid/workspaces/PID-airflow-runtime pull --ff-only origin main && git"
                    ),
                    "status": "failure",
                    "order": 1,
                }
            ]
        },
    )

    stored = ledger.get(item["id"])
    assert stored is not None
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


def test_allowed_reclassification_resets_stale_blocked_summary_status(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Ship the UI")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="Not done yet: uncommitted changes remain in the working tree.",
        summary_status="Complete",
    )
    blocked = ledger.get(item["id"])
    assert blocked is not None
    assert blocked["summary_status"] == "Blocked"

    ledger.mark_agent_done(
        item["id"],
        final_response="Done. PR CI passed and main CI passed.",
        summary_status="Blocked",
        runtime_breakdown={
            "verification_evidence": [
                {
                    "surface": "ci",
                    "check_name": "node <<'NODE'\nconst { chromium } = require('@playwright/test');",
                    "status": "failure",
                    "order": 50,
                    "detail": "{\"ok\": false}",
                }
            ]
        },
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Complete"
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert stored["completion_gate"]["summary_status"] == "Complete"


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
    attached = runner.work_ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        mode="enforce",
    )
    assert attached is not None
    assert runner.work_ledger.get(item["id"])["closeout_authoritative"] is False
    runner.work_ledger.claim(
        item["id"],
        session_key=session_key,
        run_generation=1,
        owner_pid=os.getpid(),
    )
    # A live gateway PID is insufficient: no exact active registry generation
    # owns this abandoned turn after startup.
    runner._session_run_generation = {session_key: 2}
    runner._running_agents = {}

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
async def test_authoritative_closeout_clears_resume_pending_without_model_replay(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner._background_tasks = set()
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner.adapters = {Platform.DISCORD: adapter}

    event = _discord_event(message_id="closeout-resume")
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(
        event,
        session_key=session_key,
        freshness_seconds=60,
    )
    assert item is not None
    attached = runner.work_ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        mode="enforce",
    )
    assert attached is not None
    assert runner.work_ledger.activate_closeout(
        item["id"],
        attached,
        expected_revision=attached["revision"],
    ) is not None
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

    assert runner._schedule_resume_pending_sessions() == 0
    runner.session_store.clear_resume_pending.assert_called_once_with(session_key)
    adapter.handle_message.assert_not_called()


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
async def test_direct_question_completes_ledger_without_mutating_action_summary(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner._session_db = None
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    callbacks = []
    adapter = SimpleNamespace(
        register_post_delivery_callback=lambda session_key, callback, generation=None: callbacks.append(callback),
        update_feature_summary=AsyncMock(return_value=True),
    )
    runner.adapters = {Platform.DISCORD: adapter}

    event = _discord_event(message_id="question-1", text="What remains unfinished?")
    feature_summary = {
        "thread_id": "thread-1",
        "message_id": "summary-1",
        "initial_request": "Implement the ingestion pipeline",
        "status": "In progress",
        "outcome": "Parser implementation is still underway.",
        "kanban_board": None,
    }
    original_summary = dict(feature_summary)
    event.feature_summary = feature_summary
    event.discord_action_request_intent = False
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(
        event,
        session_key=session_key,
        freshness_seconds=60,
    )
    assert item is not None
    event.work_item_id = item["id"]
    runner.work_ledger.mark_agent_done(
        item["id"],
        final_response="The parser and deployment verification are still outstanding.",
        feature_summary=feature_summary,
    )
    runner.work_ledger.mark_response_delivered(item["id"], result_message_id="answer-1")

    runner._register_discord_summary_post_delivery(
        event=event,
        source=event.source,
        session_key=session_key,
        run_generation=9,
        session_id="session-1",
        final_response="The parser and deployment verification are still outstanding.",
        agent_result={"completed": True},
    )

    assert len(callbacks) == 1
    assert await callbacks[0]() is True
    adapter.update_feature_summary.assert_not_awaited()
    assert feature_summary == original_summary
    stored = runner.work_ledger.get(item["id"])
    assert stored["status"] == "completed"
    assert stored["final_response"].startswith("The parser")


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


@pytest.mark.asyncio
async def test_startup_replay_recollects_transient_discord_summary_artifacts(
    tmp_path,
    monkeypatch,
):
    from hermes_cli import plugins as plugin_api

    artifacts = [
        {
            "kind": "trace",
            "label": "Execution trace",
            "url": "https://artifacts.example.test/runs/replayed",
        }
    ]
    collect = MagicMock(return_value=artifacts)
    monkeypatch.setattr(plugin_api, "collect_session_artifacts", collect)
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
        summary_status="Complete",
        feature_summary=event.feature_summary,
    )

    scheduled = runner._schedule_incomplete_discord_work_items()
    if runner._background_tasks:
        await asyncio.gather(*runner._background_tasks)

    assert scheduled == 1
    collect.assert_called_once_with(
        "session-1",
        surface="discord_feature_summary",
    )
    adapter.update_feature_summary.assert_awaited_once()
    assert adapter.update_feature_summary.await_args.kwargs["artifacts"] == artifacts
    stored = runner.work_ledger.get(item["id"])
    assert stored["status"] == "completed"
    assert "artifacts" not in stored["feature_summary"]


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


def test_closeout_workspace_attachment_revision_leases_and_pending_scan(tmp_path):
    now = 100.0
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: now)
    event = _discord_event(message_id="closeout-item")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
    )
    assert item is not None

    state = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        canonical_path="/protected/canonical",
        repository="acme/example",
        branch="feature/test",
        base_branch="main",
        closeout_id="closeout-1",
        mode="enforce",
        policy={"merge": "auto"},
    )

    assert state is not None
    assert state["workspace"] == {
        "path": "/mutable/worktree",
        "canonical_path": "/protected/canonical",
        "repository": "acme/example",
        "branch": "feature/test",
        "base_branch": "main",
    }
    assert state["revision"] == 1
    assert ledger.get(item["id"])["closeout_authoritative"] is False
    assert ledger.pending_closeouts(due_at=now) == []

    activated = ledger.activate_closeout(
        item["id"],
        state,
        expected_revision=state["revision"],
    )
    assert activated is not None
    assert activated["revision"] == 2
    handoff_spans = activated["telemetry"]["phase_spans"]
    assert len(handoff_spans) == 1
    assert handoff_spans[0]["phase"] == "gateway_handoff"
    assert handoff_spans[0]["work_id"].startswith("wrk_")
    assert handoff_spans[0]["attempt_id"].startswith("att_")
    assert item["id"] not in repr(handoff_spans[0])
    assert "revision-2" not in repr(handoff_spans[0])
    assert handoff_spans[0]["metadata"]["operation"].startswith("meta_")
    assert handoff_spans[0]["metadata"]["source"].startswith("meta_")
    assert handoff_spans[0]["metadata"]["mode"] == "enforce"
    assert handoff_spans[0]["metadata"]["surface"] == "gateway"
    assert ledger.get(item["id"])["closeout_authoritative"] is True
    assert ledger.pending_closeouts(due_at=now)[0]["id"] == item["id"]

    leased = ledger.lease_closeout(
        item["id"],
        owner="watcher-1",
        lease_seconds=30,
        expected_revision=activated["revision"],
    )
    assert leased is not None
    assert leased["closeout"]["revision"] == 3
    assert leased["closeout"]["lease"] == {"owner": "watcher-1", "until": 130.0}
    assert ledger.pending_closeouts(due_at=now) == []
    now = 110.0
    assert ledger.renew_closeout_lease(
        item["id"],
        owner="watcher-2",
        lease_seconds=30,
        expected_revision=leased["closeout"]["revision"],
    ) is False
    assert ledger.renew_closeout_lease(
        item["id"],
        owner="watcher-1",
        lease_seconds=30,
        expected_revision=leased["closeout"]["revision"] - 1,
    ) is False
    assert ledger.renew_closeout_lease(
        item["id"],
        owner="watcher-1",
        lease_seconds=30,
        expected_revision=leased["closeout"]["revision"],
    ) is True
    renewed = ledger.get(item["id"])["closeout"]
    assert renewed["revision"] == leased["closeout"]["revision"]
    assert renewed["lease"] == {"owner": "watcher-1", "until": 140.0}
    assert ledger.lease_closeout(item["id"], owner="watcher-2", lease_seconds=30) is None

    updated = dict(leased["closeout"])
    updated["status"] = "waiting_for_ci"
    updated["next_due_at"] = 160.0
    released = ledger.release_closeout(
        item["id"],
        owner="watcher-1",
        expected_revision=3,
        closeout_state=updated,
    )
    assert released is not None
    assert released["revision"] == 4
    assert released["lease"] == {"owner": "", "until": None}
    assert ledger.pending_closeouts(due_at=150) == []
    assert ledger.pending_closeouts(due_at=160)[0]["closeout"]["status"] == "waiting_for_ci"


def test_fable_shadow_activation_is_observational_not_authoritative(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(message_id="fable-shadow-closeout")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
    )
    assert item is not None
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        source="fable",
        mode="shadow",
    )
    assert attached is not None
    activated = ledger.activate_closeout(
        item["id"],
        attached,
        expected_revision=attached["revision"],
    )
    assert activated is not None

    stored = ledger.get(item["id"])
    assert stored["closeout_authoritative"] is False
    assert ledger.pending_closeouts(due_at=100.0)[0]["id"] == item["id"]

    ledger.mark_agent_done(item["id"], final_response="Legacy Fable finalizer completed.")
    ledger.mark_response_delivered(item["id"], result_message_id="result-1")
    ledger.mark_summary_updated(item["id"])
    assert ledger.mark_completed(item["id"]) is True
    assert ledger.get(item["id"])["status"] == "completed"


def test_pending_closeouts_prioritizes_least_recently_claimed_items(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    work_ids = []
    for index in range(3):
        event = _discord_event(message_id=f"fair-{index}")
        item = ledger.accept_event(
            event,
            session_key=build_session_key(event.source),
            freshness_seconds=3600,
        )
        attached = ledger.attach_closeout_workspace(
            item["id"],
            workspace_path=f"/mutable/worktree-{index}",
            mode="enforce",
        )
        activated = ledger.activate_closeout(
            item["id"],
            attached,
            expected_revision=attached["revision"],
        )
        assert activated is not None
        work_ids.append(item["id"])

    leased = ledger.lease_closeout(
        work_ids[0],
        owner="watcher-1",
        lease_seconds=30,
    )
    assert leased is not None
    assert ledger.release_closeout(
        work_ids[0],
        owner="watcher-1",
        expected_revision=leased["closeout"]["revision"],
        closeout_state=leased["closeout"],
    ) is not None

    pending = ledger.pending_closeouts(due_at=100.0, limit=2)
    assert [item["id"] for item in pending] == work_ids[1:]


def test_startup_does_not_replay_model_work_after_durable_closeout_handoff(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    notifications = []
    runner.trusted_closeout_watcher = SimpleNamespace(
        notify=lambda work_id="": notifications.append(work_id)
    )
    event = _discord_event(message_id="closeout-recovery")
    item = runner.work_ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
    )
    assert item is not None
    attached = runner.work_ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        mode="enforce",
    )
    assert attached is not None
    assert runner.work_ledger.activate_closeout(
        item["id"],
        attached,
        expected_revision=attached["revision"],
    ) is not None

    assert runner._schedule_incomplete_discord_work_items() == 0
    assert notifications == [item["id"]]
    assert runner.work_ledger.get(item["id"])["status"] == "accepted"


def test_startup_closeout_finalization_does_not_overwrite_new_live_run(
    tmp_path,
    monkeypatch,
):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    runner._background_tasks = set()
    runner.adapters = {}
    runner._process_epoch = "gateway-new"
    event = _discord_event(message_id="startup-closeout-run-race")
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(
        event,
        session_key=session_key,
        freshness_seconds=3600,
    )
    attached = runner.work_ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        mode="enforce",
    )
    activated = runner.work_ledger.activate_closeout(
        item["id"],
        attached,
        expected_revision=attached["revision"],
    )
    completed = dict(activated)
    completed["status"] = "completed"
    assert runner.work_ledger.update_closeout(
        item["id"],
        completed,
        expected_revision=activated["revision"],
    ) is not None
    runner._session_run_generation = {session_key: 2}
    runner._running_agents = {session_key: object()}
    original_mark_agent_done = runner.work_ledger.mark_agent_done
    raced = False

    def race_then_finalize(work_id, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            assert runner.work_ledger.mark_agent_running(
                work_id,
                session_key=session_key,
                run_generation=2,
                owner_pid=os.getpid(),
                process_epoch="gateway-new",
            )
        return original_mark_agent_done(work_id, **kwargs)

    monkeypatch.setattr(runner.work_ledger, "mark_agent_done", race_then_finalize)

    assert runner._schedule_incomplete_discord_work_items() == 0
    stored = runner.work_ledger.get(item["id"])
    assert stored["status"] == "agent_running"
    assert stored["active_run"]["generation"] == 2
    assert stored["active_run"]["process_epoch"] == "gateway-new"
    assert stored["closeout"]["status"] == "completed"
    assert "final_response" not in stored
    assert "summary_status" not in stored


def test_blocked_closeout_finalization_is_one_atomic_cas(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(message_id="blocked-closeout-cas")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
    )
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        mode="enforce",
    )
    activated = ledger.activate_closeout(
        item["id"],
        attached,
        expected_revision=attached["revision"],
    )
    leased = ledger.lease_closeout(
        item["id"],
        owner="watcher-1",
        lease_seconds=30,
        expected_revision=activated["revision"],
    )
    blocked_state = dict(leased["closeout"])
    blocked_state["status"] = "repair_required"

    winner = ledger.finalize_blocked_closeout(
        item["id"],
        owner="watcher-1",
        expected_revision=leased["closeout"]["revision"],
        closeout_state=blocked_state,
        final_response="Trusted closeout blocked.",
        reason="trusted_closeout_repair_required",
    )
    loser = ledger.finalize_blocked_closeout(
        item["id"],
        owner="watcher-1",
        expected_revision=leased["closeout"]["revision"],
        closeout_state=blocked_state,
        final_response="duplicate",
        reason="duplicate",
    )

    assert winner is not None
    assert loser is None
    stored = ledger.get(item["id"])
    assert stored["status"] == "blocked"
    assert stored["final_response"] == "Trusted closeout blocked."
    assert stored["summary_status"] == "Blocked"
    assert stored["blocked_reason"] == "trusted_closeout_repair_required"
    assert stored["closeout"]["status"] == "repair_required"
    assert stored["terminal_delivery"] == {
        "source": "trusted_closeout",
        "status": "pending",
        "revision": 1,
        "owner": "",
        "lease_until": None,
        "attempt_count": 0,
        "retry_count": 0,
        "send_started_at": None,
        "send_confirmed_at": None,
        "result_message_id": None,
        "confirmed_message_ids": [],
        "summary_updated_at": None,
    }


def test_blocked_closeout_run_state_cas_preserves_new_live_run_and_closeout(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(message_id="blocked-closeout-run-state")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=3600)
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        mode="enforce",
    )
    activated = ledger.activate_closeout(
        item["id"],
        attached,
        expected_revision=attached["revision"],
    )
    leased = ledger.lease_closeout(
        item["id"],
        owner="watcher-1",
        lease_seconds=30,
        expected_revision=activated["revision"],
    )
    expected = ledger.run_state_snapshot(leased)
    blocked_state = dict(leased["closeout"])
    blocked_state["status"] = "repair_required"
    assert ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=2,
        owner_pid=4242,
        process_epoch="boot-b",
    )
    before = ledger.get(item["id"])

    assert ledger.finalize_blocked_closeout(
        item["id"],
        owner="watcher-1",
        expected_revision=leased["closeout"]["revision"],
        closeout_state=blocked_state,
        final_response="stale blocked response",
        reason="stale_reason",
        expected_run_state=expected,
    ) is None
    assert ledger.get(item["id"]) == before
    assert "terminal_delivery" not in before
    assert "blocked_reason" not in before


def test_closeout_compare_and_swap_rejects_stale_revision(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="closeout-cas")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
    )
    assert item is not None
    state = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        mode="shadow",
    )
    assert state is not None

    candidate = dict(state)
    candidate["status"] = "waiting_for_ci"
    assert ledger.update_closeout(item["id"], candidate, expected_revision=0) is None
    updated = ledger.update_closeout(item["id"], candidate, expected_revision=1)
    assert updated is not None
    assert updated["revision"] == 2
    assert updated["status"] == "waiting_for_ci"


def test_mark_completed_refuses_incomplete_enforced_closeout_without_replay(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="closeout-terminal")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
    )
    assert item is not None
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        mode="enforce",
    )
    assert attached is not None
    state = ledger.activate_closeout(
        item["id"],
        attached,
        expected_revision=attached["revision"],
    )
    assert state is not None
    ledger.mark_agent_done(item["id"], final_response="Implementation model work is complete.")
    ledger.mark_response_delivered(item["id"], result_message_id="result-1")
    ledger.mark_summary_updated(item["id"])

    assert ledger.mark_completed(item["id"]) is False
    stored = ledger.get(item["id"])
    assert stored["status"] == "summary_updated"
    assert stored["closeout"]["status"] == "pending"

    completed = dict(stored["closeout"])
    completed["status"] = "completed"
    assert ledger.update_closeout(
        item["id"],
        completed,
        expected_revision=stored["closeout"]["revision"],
    ) is not None
    assert ledger.mark_completed(item["id"]) is True
    assert ledger.get(item["id"])["status"] == "completed"
