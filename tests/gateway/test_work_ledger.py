import asyncio
import json
import os
import time
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.provider_progress import clear_provider_progress_signal, latest_provider_progress_signal
from agent.visual_qa import (
    classify_visual_requirement,
    normalize_visual_requirement,
    visual_requirement_id,
    visual_requirement_uses_orchestrator_contract,
)
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
        discord_runtime_mode="action",
    )


def _repo_discord_event(message_id="m1", text="do the work"):
    event = _discord_event(message_id=message_id, text=text)
    event.source.project_path = "/home/droid/hermes"
    event.source.project_github_url = "https://github.com/sligohub/hermes-agent"
    return event


def test_busy_discord_command_is_not_accepted_into_durable_work_ledger(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(message_id="busy-opus", text="/opus continue")
    event.message_type = MessageType.COMMAND
    session_key = build_session_key(event.source)
    runner._running_agents = {session_key: object()}

    accepted = runner._accept_discord_work_item(event, session_key)

    assert accepted is None
    assert runner.work_ledger.incomplete_items() == []


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
    if visual_requirement_uses_orchestrator_contract(normalized):
        receipt["coverage_ids"] = assertion_ids
        receipt["assertion_ids"] = ["vassert_" + ("c" * 24)]
        receipt["diagnostic_codes"] = ["appearance_satisfied"]
    if evidence_ref:
        receipt["evidence_ref"] = evidence_ref
    return receipt


def _activated_visual_closeout(
    ledger: GatewayWorkLedger,
    item: dict,
    *,
    head_sha: str,
    source: str = "direct",
) -> dict:
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        repository="acme/example",
        branch="feature/visual",
        source=source,
        mode="enforce",
        policy={
            "merge": "auto",
            "require_local_verification": True,
            "require_visual_qa": True,
        },
    )
    assert attached is not None
    attached["local_verification"] = {"status": "passed", "head_sha": head_sha}
    attached["visual_qa"] = {"status": "pending", "head_sha": head_sha}
    attached["pr"]["head_sha"] = head_sha
    attached["ci"]["head_sha"] = head_sha
    activated = ledger.activate_closeout(
        item["id"],
        attached,
        expected_revision=attached["revision"],
    )
    assert activated is not None
    return activated


def _exact_synced_closeout(state: dict) -> dict:
    result = deepcopy(state)
    head_sha = "a" * 40
    merge_sha = "b" * 40
    result["status"] = "post_merge_complete"
    result["local_verification"] = {"status": "passed", "head_sha": head_sha}
    result["pr"].update(
        {
            "state": "MERGED",
            "head_sha": head_sha,
            "merge_sha": merge_sha,
        }
    )
    result["ci"] = {"status": "passed", "head_sha": head_sha}
    result["post_merge"].update(
        {
            "target_sha": merge_sha,
            "canonical_sync": {
                "status": "passed",
                "observed_sha": merge_sha,
            },
        }
    )
    return result


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


def test_ledger_compaction_preserves_live_and_pending_terminal_records(tmp_path):
    now = [10 * 24 * 60 * 60.0]
    path = tmp_path / "work_ledger.json"
    ledger = GatewayWorkLedger(path, now_fn=lambda: now[0])
    old = now[0] - (8 * 24 * 60 * 60)
    items = {
        "active": {"id": "active", "status": "agent_running", "updated_at": old},
        "terminal-active-run": {
            "id": "terminal-active-run",
            "status": "completed",
            "updated_at": old,
            "active_run": {"generation": 4},
        },
        "delivery": {
            "id": "delivery",
            "status": "blocked",
            "updated_at": old,
            "terminal_delivery": {"status": "pending", "summary_updated_at": None},
        },
        "blocked": {"id": "blocked", "status": "blocked", "updated_at": old},
        "reaction": {
            "id": "reaction",
            "status": "completed",
            "updated_at": old,
            "terminal_reaction_sync_pending": True,
        },
        "closeout": {
            "id": "closeout",
            "status": "completed",
            "updated_at": old,
            "closeout_authoritative": True,
            "closeout_activated_at": old,
            "closeout": {"status": "waiting_for_ci", "next_due_at": now[0]},
        },
        "quiescent": {"id": "quiescent", "status": "completed", "updated_at": old},
    }
    path.write_text(json.dumps({"version": 2, "items": deepcopy(items)}), encoding="utf-8")

    ledger.accept_event(
        _discord_event(message_id="compact-trigger"),
        session_key="compact-trigger",
        freshness_seconds=60,
    )

    stored = json.loads(path.read_text(encoding="utf-8"))["items"]
    for work_id in (
        "active",
        "terminal-active-run",
        "delivery",
        "blocked",
        "reaction",
        "closeout",
    ):
        assert stored[work_id] == items[work_id]
    assert stored["quiescent"] == {
        "id": "quiescent",
        "status": "completed",
        "tombstone": True,
        "tombstoned_at": now[0],
        "tombstone_expires_at": now[0] + (30 * 24 * 60 * 60),
    }


def test_ledger_compaction_tombstones_then_expires_duplicate_suppression(tmp_path):
    now = [10 * 24 * 60 * 60.0]
    path = tmp_path / "work_ledger.json"
    ledger = GatewayWorkLedger(path, now_fn=lambda: now[0])
    event = _discord_event(message_id="old-duplicate")
    work_id = ledger.id_for_event(event, "duplicate-session")
    assert work_id is not None
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "items": {
                    work_id: {
                        "id": work_id,
                        "status": "completed",
                        "updated_at": now[0] - (8 * 24 * 60 * 60),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    ledger.accept_event(
        _discord_event(message_id="tombstone-trigger"),
        session_key="tombstone-trigger",
        freshness_seconds=60,
    )
    duplicate = ledger.accept_event(event, session_key="duplicate-session", freshness_seconds=60)

    assert duplicate is not None
    assert duplicate["_existing"] is True
    assert duplicate["tombstone"] is True
    now[0] += (30 * 24 * 60 * 60) + (60 * 60)
    ledger.accept_event(
        _discord_event(message_id="prune-trigger"),
        session_key="prune-trigger",
        freshness_seconds=60,
    )
    replay_after_horizon = ledger.accept_event(
        event,
        session_key="duplicate-session",
        freshness_seconds=60,
    )

    assert replay_after_horizon is not None
    assert replay_after_horizon["_existing"] is False


def test_ledger_compaction_runs_only_after_its_bounded_cadence(tmp_path):
    now = [10 * 24 * 60 * 60.0]
    path = tmp_path / "work_ledger.json"
    ledger = GatewayWorkLedger(path, now_fn=lambda: now[0])
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "last_compacted_at": now[0],
                "items": {
                    "old": {
                        "id": "old",
                        "status": "completed",
                        "updated_at": now[0] - (8 * 24 * 60 * 60),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    ledger.accept_event(
        _discord_event(message_id="before-cadence"),
        session_key="before-cadence",
        freshness_seconds=60,
    )
    assert ledger.get("old").get("tombstone") is not True

    now[0] += 60 * 60
    ledger.accept_event(
        _discord_event(message_id="at-cadence"),
        session_key="at-cadence",
        freshness_seconds=60,
    )
    assert ledger.get("old")["tombstone"] is True


def test_ledger_hard_budget_bypasses_compaction_cadence(tmp_path, monkeypatch):
    now = [10 * 24 * 60 * 60.0]
    path = tmp_path / "work_ledger.json"
    ledger = GatewayWorkLedger(path, now_fn=lambda: now[0])
    items = {
        f"old-{index}": {
            "id": f"old-{index}",
            "status": "completed",
            "updated_at": now[0] - (3 * 60 * 60),
            "text": "x" * 2_000,
        }
        for index in range(8)
    }
    path.write_text(
        json.dumps(
            {"version": 2, "last_compacted_at": now[0], "items": items},
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("gateway.work_ledger.DEFAULT_LEDGER_TARGET_BYTES", 4_000)
    monkeypatch.setattr("gateway.work_ledger.DEFAULT_LEDGER_HARD_BYTES", 8_000)

    ledger.accept_event(
        _discord_event(message_id="budget-trigger"),
        session_key="budget-trigger",
        freshness_seconds=60,
    )

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert sum(
        item.get("tombstone") is True for item in stored["items"].values()
    ) > 0
    assert len(path.read_bytes()) <= 4_000


def test_ledger_uses_compact_json_serialization(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    for index in range(4):
        item = ledger.accept_event(
            _discord_event(message_id=f"compact-json-{index}", text="x" * 20),
            session_key=f"compact-json-{index}",
            freshness_seconds=60,
        )
        assert item is not None

    raw = ledger.path.read_bytes()
    pretty = json.dumps(json.loads(raw), indent=2, sort_keys=True).encode("utf-8")

    assert len(raw) < len(pretty) * 0.8


def test_visual_requirement_uses_reply_and_goal_thread_context(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="contextual", text="Please proceed with that.")
    event.reply_to_text = "Let's repair the local district maps."
    event.goal_thread_context = "Use a nonpartisan color scheme on the dashboard."

    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )

    assert item is not None
    assert item["visual_qa_requirement"]["level"] == "surface"


def test_plan_only_visual_request_has_no_ledger_requirement(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(
        message_id="plan-only-visual",
        text=(
            "The search bar at the top of the page is jarring because it breaks up "
            "the flow of the layout. Please create a plan for making this improvement."
        ),
    )

    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )

    assert item is not None
    assert item["visual_qa_requirement"] == {
        "level": "none",
        "target": "",
        "assertions": [],
    }


def test_post_edit_visual_promotion_tightens_closeout_policy(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="promote", text="Implement the requested behavior.")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert item is not None
    assert item["visual_qa_requirement"]["level"] == "none"
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        repository="acme/example",
        branch="feature/rendered-fallback",
        source="direct",
        mode="enforce",
        policy={
            "merge": "auto",
            "require_local_verification": True,
            "require_visual_qa": False,
        },
    )
    assert attached is not None
    promoted = classify_visual_requirement(
        "Fix the local district maps.",
        worker_route="action",
    )

    stored = ledger.promote_visual_qa_requirement(item["id"], promoted)

    assert stored is not None
    assert stored["visual_qa_requirement"]["level"] == "surface"
    assert stored["closeout"]["policy"]["require_visual_qa"] is True
    assert stored["closeout"]["visual_qa"] == {"status": "pending"}


def test_ledger_persists_effective_and_base_prompts_for_read_only_recovery(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="intent-message")
    event.discord_runtime_mode = "read_only"
    event.discord_action_escalation_allowed = True
    event.discord_action_request_base_channel_prompt = "Project instructions"
    event.channel_prompt = "Project instructions\n\nRead-only runtime overlay"

    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )

    assert item is not None
    stored = ledger.get(item["id"])
    assert stored["channel_prompt"] == "Project instructions\n\nRead-only runtime overlay"
    assert "discord_action_request_intent" not in stored
    assert stored["discord_action_request_base_channel_prompt"] == "Project instructions"
    replay = ledger.event_from_item(stored)
    assert replay.discord_runtime_mode == "read_only"
    assert replay.discord_action_request_intent is None
    assert replay.discord_action_request_base_channel_prompt == "Project instructions"
    assert replay.channel_prompt == "Project instructions\n\nRead-only runtime overlay"


def test_legacy_ledger_prompt_is_base_prompt_compatibility_fallback(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="legacy-prompt")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    stored = ledger.get(item["id"])
    stored.pop("discord_action_request_base_channel_prompt", None)

    replay = ledger.event_from_item(stored)

    assert replay.discord_action_request_base_channel_prompt == stored["channel_prompt"]


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

    assert replay.discord_runtime_mode == "action"
    assert replay.discord_action_request_intent is None


def test_drain_ledger_persists_read_only_runtime_authority_and_lifecycle(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="read-only-drain", text="audit only")
    event.discord_runtime_mode = "read_only"
    event.discord_runtime_reason = "classified_read_only"
    event.discord_action_escalation_allowed = True
    event.discord_explicit_no_action_denial = False
    event.participates_in_work_lifecycle = False

    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
        drain_recovery=True,
    )

    assert item is not None
    stored = ledger.get(item["id"])
    assert stored["discord_runtime_mode"] == "read_only"
    assert stored["discord_runtime_reason"] == "classified_read_only"
    assert stored["discord_action_escalation_allowed"] is True
    assert stored["participates_in_work_lifecycle"] is False
    replay = ledger.event_from_item(stored)
    assert replay.discord_runtime_mode == "read_only"
    assert replay.discord_runtime_reason == "classified_read_only"
    assert replay.discord_action_escalation_allowed is True
    assert replay.participates_in_work_lifecycle is False
    assert replay.discord_drain_recovery is True


def test_legacy_ledger_row_without_runtime_metadata_defaults_to_action(tmp_path):
    path = tmp_path / "work_ledger.json"
    ledger = GatewayWorkLedger(path)
    event = _discord_event(message_id="legacy-runtime")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = 1
    raw = payload["items"][item["id"]]
    for field in (
        "discord_runtime_mode",
        "discord_runtime_reason",
        "discord_action_escalation_allowed",
        "discord_explicit_no_action_denial",
        "participates_in_work_lifecycle",
        "drain_recovery",
    ):
        raw.pop(field, None)
    path.write_text(json.dumps(payload), encoding="utf-8")

    replay = ledger.event_from_item(ledger.get(item["id"]))

    assert replay.discord_runtime_mode == "action"
    assert replay.participates_in_work_lifecycle is True
    assert replay.discord_action_escalation_allowed is False


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
        "max_vision_calls": 2,
        "attempt_timeout_s": 30.0,
        "total_timeout_s": 60.0,
        "max_output_chars": 6000,
    }
    requirement = stored["visual_qa_requirement"]
    assert requirement["level"] == "surface"
    assert requirement["target"].startswith("vtarget_")
    assert all(
        item["id"].startswith("vassert_") and item["kind"] == "orchestrator_contract"
        for item in requirement["assertions"]
    )
    assert "responsive dashboard" not in repr(requirement).lower()
    assert "mobile sidebar" not in repr(requirement).lower()
    replay = ledger.event_from_item(stored)
    assert replay.visual_qa_requirement == requirement
    assert replay.visual_qa_config == stored["visual_qa_config"]


def test_ledger_requires_visual_qa_for_direct_rendered_defect_language(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(text="Please handle this request.")
    event.feature_summary = {
        "initial_request": (
            "in the Issue Attention graph in the State Brief page:\n"
            "-the bar graphs clip through the x axis\n"
            "-we should lightly label the y axis"
        ),
    }

    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )

    assert item is not None
    stored = ledger.get(item["id"])
    assert stored["visual_qa_requirement"]["level"] == "surface"
    assert stored["visual_qa_requirement"]["assertions"]


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
    assert stored["final_response"] == "Implemented the dashboard."
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

    requirement = classify_visual_requirement(
        "Build a responsive dashboard with a mobile sidebar.",
        worker_route="action",
    )
    config = {"mode": "enforce_explicit", "max_receipts_per_turn": 1, "max_followup_turns": 1}
    prompt = _visual_qa_context_prompt(requirement, config)
    assert "mode=enforce_explicit" in prompt
    assert "call `visual_qa`" in prompt
    assert "you own the transient semantic contract" in prompt
    assert "use `browser_authenticate`" in prompt
    assert "never type or inspect credentials" in prompt
    assert "smallest relevant target/region" in prompt
    assert "bounded inspector—not your prose—decides pass/fail" in prompt
    assert "exact current-head Vercel preview URL" in prompt
    assert "localhost or production URL cannot satisfy preview visual QA" in prompt
    assert "one repository-native preview launcher" in prompt
    assert "call `visual_qa` once" in prompt
    assert "multiple stacked panels to fit in one viewport" in prompt
    assert "retry at most once" in prompt
    assert "do not build ad hoc screenshot scripts" in prompt
    assert "attach receipt arguments" in prompt
    assert "Generic navigation" in prompt
    assert "answer the entire accepted request" in prompt
    assert "what changed, relevant verification, and shipped/PR/deploy state" in prompt
    assert "Do not turn the response into a visual-QA report" in prompt
    assert "use only the phrase `Visual QA passed`" in prompt
    assert "receipt IDs, confidence, assertion details, or visual observations" in prompt
    assert "screenshot artifacts are attached automatically" in prompt

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
        "requirement": requirement,
        "min_receipt_order": 3,
    }
    assert "private/workspace" not in str(state)


def test_read_only_visual_qa_confirmation_gets_ephemeral_requirement():
    from gateway.run import _promote_read_only_visual_qa_request

    requirement = _promote_read_only_visual_qa_request(
        {"level": "none", "target": "", "assertions": []},
        "Confirm you can use visual QA successfully now and mimic closeout.",
        "read_only",
    )

    assert requirement["level"] == "surface"
    assert requirement["assertions"][0]["kind"] == "orchestrator_contract"


def test_read_only_non_qa_review_does_not_gain_visual_requirement():
    from gateway.run import _promote_read_only_visual_qa_request

    requirement = _promote_read_only_visual_qa_request(
        {"level": "none", "target": "", "assertions": []},
        "Review this screenshot and explain the current layout.",
        "read_only",
    )

    assert requirement["level"] == "none"


def test_resumed_turn_cannot_erase_prior_visual_mutation_boundary(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(
        message_id="visual-restart-resume",
        text="Build a responsive dashboard with a mobile sidebar.",
    )
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert item is not None
    assert ledger.mark_agent_done(
        item["id"],
        final_response="Restarting before visual QA.",
        visual_qa_code_mutation_observed=True,
        visual_qa_min_receipt_order=7,
    )

    assert ledger.mark_agent_done(
        item["id"],
        final_response="Resumed verification turn.",
        visual_qa_code_mutation_observed=False,
        visual_qa_min_receipt_order=0,
    )

    stored = ledger.get(item["id"])
    assert stored["visual_qa_code_mutation_observed"] is True
    assert stored["visual_qa_min_receipt_order"] == 7
    assert stored["completion_gate"]["reason"] == "visual_qa_receipt_missing"


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
    assert stale_stored["final_response"].startswith(
        "⚠️ **Completion blocked.** Enforced visual QA is active"
    )
    assert stale_stored["final_response"].endswith("Implemented the dashboard.")
    assert "Gate reason: visual qa receipt missing." in stale_stored["final_response"]
    assert "None" not in stale_stored["final_response"]

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
    assert fresh_stored["final_response"] == "Implemented the dashboard."


def test_enforced_visual_qa_reports_uncertain_receipt_instead_of_missing(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(
        message_id="uncertain-visual-receipt",
        text="Build a responsive dashboard with a mobile sidebar.",
    )
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
        visual_qa_receipts=[
            _visual_receipt(requirement, order=3, status="uncertain")
        ],
        visual_qa_code_mutation_observed=True,
        visual_qa_min_receipt_order=3,
    )

    stored = ledger.get(item["id"])
    assert stored["visual_qa_receipts"][0]["status"] == "uncertain"
    assert stored["completion_gate"]["reason"] == "visual_qa_receipt_uncertain"
    assert "Gate reason: visual qa receipt uncertain." in stored["final_response"]


def test_enforced_visual_qa_streamed_block_appends_notice_without_none(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(
        message_id="streamed-visual-block",
        text="Build a responsive dashboard with a mobile sidebar.",
    )
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert item is not None

    assert ledger.mark_agent_done(
        item["id"],
        final_response="Fresh verification passed.",
        visual_qa_receipts=[],
        visual_qa_code_mutation_observed=True,
        visual_qa_min_receipt_order=2,
        already_delivered=True,
    )

    stored = ledger.get(item["id"])
    assert stored["status"] == "response_delivered"
    assert stored["final_response"].startswith("Fresh verification passed.\n\n")
    assert "⚠️ **Completion blocked.**" in stored["final_response"]
    assert "Gate reason: visual qa receipt missing." in stored["final_response"]
    assert "None" not in stored["final_response"]


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


def test_work_ledger_persists_only_allowlisted_closeout_receipt_fields(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(message_id="closeout-receipt")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    assert ledger.mark_agent_done(
        item["id"],
        final_response="Done.",
        runtime_breakdown={
            "wall_s": 1,
            "closeout_receipt": {
                "status": "passed",
                "head_sha": "d" * 40,
                "script": "closeout",
                "raw_output": "do-not-store",
                "token": "also-do-not-store",
            },
        },
    )

    stored = ledger.get(item["id"])
    assert stored["runtime_breakdown"]["closeout_receipt"] == {
        "schema_version": 1,
        "status": "passed",
        "head_sha": "d" * 40,
        "script": "closeout",
    }
    assert stored["closeout_receipt"] == stored["runtime_breakdown"]["closeout_receipt"]
    assert "do-not-store" not in repr(stored["runtime_breakdown"])


def test_mark_agent_done_adopts_repo_native_closeout_receipt(tmp_path):
    now = 123.0
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: now)
    event = _repo_discord_event(message_id="repo-native-closeout")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None
    pending = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        canonical_path="/protected/canonical",
        repository="acme/example",
        branch="feature/repo-native",
        source="direct",
        mode="enforce",
        policy={"require_local_verification": True},
    )
    assert pending is not None
    assert pending["status"] == "pending"
    assert ledger.get(item["id"])["closeout_authoritative"] is False

    head_sha = "e" * 40
    assert ledger.mark_agent_done(
        item["id"],
        final_response="Implemented and verified.",
        runtime_breakdown={
            "closeout_receipt": {
                "schema_version": 1,
                "status": "deployed",
                "head_sha": head_sha,
                "script": "scripts/local_lifecycle/closeout.sh",
                "raw_output": "do-not-store",
                "token": "also-do-not-store",
            },
        },
    )

    stored = ledger.get(item["id"])
    evidence = {
        "schema_version": 1,
        "status": "deployed",
        "head_sha": head_sha,
        "script": "scripts/local_lifecycle/closeout.sh",
    }
    assert stored["closeout_authoritative"] is True
    assert stored["closeout_activated_at"] == now
    assert stored["closeout_receipt"] == evidence
    assert stored["runtime_breakdown"]["closeout_receipt"] == evidence
    assert stored["closeout"]["status"] == "completed"
    assert stored["closeout"]["source"] == "repo_native"
    assert stored["closeout"]["mode"] == "enforce"
    assert stored["closeout"]["revision"] == pending["revision"] + 1
    assert stored["closeout"]["lease"] == {"owner": "", "until": None}
    assert stored["closeout"]["next_due_at"] is None
    assert stored["closeout"]["local_verification"] == {
        "status": "passed",
        "head_sha": head_sha,
    }
    assert stored["closeout"]["pr"]["number"] == ""
    assert stored["closeout"]["pr"]["merge_sha"] == ""
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert ledger.pending_closeouts(due_at=now) == []
    assert "do-not-store" not in repr(stored)
    assert "also-do-not-store" not in repr(stored)


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


def test_active_run_renewal_extends_exact_owner_lease_and_freshness(tmp_path):
    now = [100.0]
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: now[0])
    event = _discord_event(message_id="heartbeat")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=10)
    assert item is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=7,
        owner_pid=4242,
        process_epoch="boot-a",
    )

    now[0] = 3690.0
    renewed = ledger.renew_active_run(
        item["id"],
        session_key=session_key,
        run_generation=7,
        owner_pid=4242,
        process_epoch="boot-a",
        lease_seconds=30,
    )

    assert renewed is not None
    assert renewed["lease_until"] == 3720.0
    assert renewed["active_run"]["lease_until"] == 3720.0
    assert renewed["expires_at"] == 3720.0
    assert renewed["updated_at"] == 3690.0


@pytest.mark.parametrize(
    "override",
    [
        {"session_key": "wrong"},
        {"run_generation": 8},
        {"owner_pid": 4343},
        {"process_epoch": "boot-b"},
    ],
)
def test_active_run_renewal_rejects_wrong_exact_owner(tmp_path, override):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(message_id=f"wrong-owner-{next(iter(override))}")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=7,
        owner_pid=4242,
        process_epoch="boot-a",
    )
    kwargs = {
        "session_key": session_key,
        "run_generation": 7,
        "owner_pid": 4242,
        "process_epoch": "boot-a",
        **override,
    }

    assert ledger.renew_active_run(item["id"], **kwargs) is None


def test_expiration_terminalizes_active_run_and_marks_reaction_repair(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(message_id="expired-run")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(event, session_key=session_key, freshness_seconds=60)
    assert item is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=3,
        owner_pid=4242,
        process_epoch="boot-a",
    )

    assert ledger.mark_expired(item["id"])
    stored = ledger.get(item["id"])
    assert stored["status"] == "expired"
    assert stored["active_run"] is None
    assert stored["lease_until"] is None
    assert stored["summary_status"] == "Interrupted"
    assert stored["terminal_reaction_sync_pending"] is True
    assert stored["terminal_reaction_state"] == "errored"


def test_discord_thread_reaction_uses_latest_terminal_after_incomplete_clears(tmp_path):
    now = [100.0]
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: now[0])
    first_event = _discord_event(message_id="first")
    first = ledger.accept_event(
        first_event,
        session_key=build_session_key(first_event.source),
        freshness_seconds=60,
    )
    assert first is not None
    ledger.mark_expired(first["id"])

    now[0] = 200.0
    second_event = _discord_event(message_id="second")
    second = ledger.accept_event(
        second_event,
        session_key=build_session_key(second_event.source),
        freshness_seconds=60,
    )
    assert second is not None
    assert ledger.discord_thread_reaction_state(second) == "running"
    assert ledger.mark_completed(second["id"])
    assert ledger.discord_thread_reaction_state(second) == "done"


def test_completion_marks_terminal_reaction_for_later_discord_sync(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="completed-reaction")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    assert ledger.mark_completed(item["id"])

    stored = ledger.get(item["id"])
    assert stored["terminal_reaction_state"] == "done"
    assert stored["terminal_reaction_sync_pending"] is True
    assert [pending["id"] for pending in ledger.pending_terminal_reaction_items()] == [
        item["id"]
    ]


def test_terminal_reaction_identity_is_profile_scoped(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    items = []
    for message_id, profile in (("first", None), ("second", "reviewer")):
        event = _discord_event(message_id=message_id)
        event.source.profile = profile
        item = ledger.accept_event(
            event,
            session_key=build_session_key(event.source),
            freshness_seconds=60,
        )
        assert item is not None
        assert ledger.mark_completed(item["id"])
        items.append(item)

    pending = ledger.pending_terminal_reaction_items()
    assert {item["id"] for item in pending} == {item["id"] for item in items}

    assert ledger.mark_discord_thread_reaction_synced(items[1])
    assert [item["id"] for item in ledger.pending_terminal_reaction_items()] == [
        items[0]["id"]
    ]


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

    expected_run_state = ledger.run_state_snapshot(ledger.get(item["id"]))
    assert ledger.mark_summary_updated(
        item["id"],
        expected_run_state=expected_run_state,
    ) is True
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

    fresh = ledger.accept_event(
        _discord_event(message_id="m2"),
        session_key=session_key,
        freshness_seconds=60,
    )
    assert fresh is not None
    ledger.mark_completed(fresh["id"], result_message_id="result-1")
    assert ledger.incomplete_items() == []
    assert ledger.get(fresh["id"])["result_message_id"] == "result-1"


def _seed_dev_merge_item(path, *, closeout_status="pr_published"):
    event = _discord_event(message_id="seed-merge")
    session_key = build_session_key(event.source)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "items": {
                    "work-merge": {
                        "id": "work-merge",
                        "platform": "discord",
                        "source": event.source.to_dict(),
                        "session_key": session_key,
                        "discord_runtime_mode": "action",
                        "participates_in_work_lifecycle": True,
                        "discord_pr_generation": 1,
                        "status": "completed",
                        "created_at": 1.0,
                        "updated_at": 2.0,
                        "result_message_id": "final-1",
                        "confirmed_message_ids": ["final-1", "final-2"],
                        "preview_delivery": {"result_message_id": "preview-1"},
                        "closeout": {
                            "status": closeout_status,
                            "pr": {"url": "https://github.com/acme/example/pull/7"},
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def test_dev_merge_claim_requires_delivered_final_response(tmp_path):
    path = tmp_path / "ledger.json"
    _seed_dev_merge_item(path)
    ledger = GatewayWorkLedger(path, now_fn=lambda: 100.0)

    assert ledger.claim_dev_merge_for_message(
        chat_id="thread-1",
        message_id="preview-1",
        actor_id="dev-1",
    ) is None
    assert ledger.claim_dev_merge_for_message(
        chat_id="different-thread",
        message_id="final-1",
        actor_id="dev-1",
    ) is None

    claim = ledger.claim_dev_merge_for_message(
        chat_id="thread-1",
        message_id="final-2",
        actor_id="dev-1",
    )

    assert claim["_dev_merge_claim"] == "claimed"
    assert claim["dev_merge"]["message_id"] == "final-2"


def test_dev_merge_claim_requires_published_closeout(tmp_path):
    path = tmp_path / "ledger.json"
    _seed_dev_merge_item(path, closeout_status="waiting_for_ci")
    ledger = GatewayWorkLedger(path, now_fn=lambda: 100.0)

    assert ledger.claim_dev_merge_for_message(
        chat_id="thread-1",
        message_id="final-1",
        actor_id="dev-1",
    ) is None


def test_dev_merge_claim_selects_exact_final_message_across_pr_generations(tmp_path):
    path = tmp_path / "ledger.json"
    event = _discord_event(message_id="seed")
    session_key = build_session_key(event.source)
    source = event.source.to_dict()
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "discord_pr_lifecycles": {
                    session_key: {
                        "generation": 2,
                        "status": "active",
                        "updated_at": 4.0,
                    }
                },
                "items": {
                    "pr-1": {
                        "id": "pr-1",
                        "platform": "discord",
                        "source": source,
                        "session_key": session_key,
                        "discord_pr_generation": 1,
                        "status": "completed",
                        "updated_at": 2.0,
                        "result_message_id": "final-1",
                        "closeout": {
                            "status": "pr_published",
                            "pr": {"url": "https://github.com/acme/example/pull/1"},
                        },
                        "dev_merge": {"status": "merged", "revision": 1},
                    },
                    "pr-2": {
                        "id": "pr-2",
                        "platform": "discord",
                        "source": source,
                        "session_key": session_key,
                        "discord_pr_generation": 2,
                        "status": "completed",
                        "updated_at": 4.0,
                        "result_message_id": "final-2",
                        "closeout": {
                            "status": "pr_published",
                            "pr": {"url": "https://github.com/acme/example/pull/2"},
                        },
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    ledger = GatewayWorkLedger(path, now_fn=lambda: 100.0)

    old = ledger.claim_dev_merge_for_message(
        chat_id="thread-1",
        message_id="final-1",
        actor_id="dev-1",
    )
    current = ledger.claim_dev_merge_for_message(
        chat_id="thread-1",
        message_id="final-2",
        actor_id="dev-1",
    )

    assert old["_dev_merge_claim"] == "already_merged"
    assert old["closeout"]["pr"]["url"].endswith("/1")
    assert current["_dev_merge_claim"] == "claimed"
    assert current["closeout"]["pr"]["url"].endswith("/2")


def test_dev_merge_claim_is_leased_and_merged_result_is_idempotent(tmp_path):
    path = tmp_path / "ledger.json"
    _seed_dev_merge_item(path)
    ledger = GatewayWorkLedger(path, now_fn=lambda: 100.0)
    first = ledger.claim_dev_merge_for_message(
        chat_id="thread-1",
        message_id="final-1",
        actor_id="dev-1",
    )

    second = ledger.claim_dev_merge_for_message(
        chat_id="thread-1",
        message_id="final-1",
        actor_id="dev-1",
    )

    assert second["_dev_merge_claim"] == "in_progress"
    assert ledger.finish_dev_merge(
        "work-merge",
        attempt_id=first["_dev_merge_attempt_id"],
        outcome="merged",
        message="Merged: https://github.com/acme/example/pull/7",
        pr_url="https://github.com/acme/example/pull/7",
    )
    third = ledger.claim_dev_merge_for_message(
        chat_id="thread-1",
        message_id="final-1",
        actor_id="dev-1",
    )
    assert third["_dev_merge_claim"] == "already_merged"


def test_merged_dev_pr_rolls_same_thread_into_next_pr_generation(tmp_path):
    path = tmp_path / "ledger.json"
    _seed_dev_merge_item(path)
    ledger = GatewayWorkLedger(path, now_fn=lambda: 100.0)
    session_key = build_session_key(_discord_event().source)
    claim = ledger.claim_dev_merge_for_message(
        chat_id="thread-1",
        message_id="final-1",
        actor_id="dev-1",
    )
    assert ledger.finish_dev_merge(
        "work-merge",
        attempt_id=claim["_dev_merge_attempt_id"],
        outcome="merged",
        message="Merged: https://github.com/acme/example/pull/7",
        pr_url="https://github.com/acme/example/pull/7",
    )

    first_followup = ledger.accept_event(
        _discord_event(message_id="followup-1"),
        session_key=session_key,
        freshness_seconds=60,
    )
    second_followup = ledger.accept_event(
        _discord_event(message_id="followup-2"),
        session_key=session_key,
        freshness_seconds=60,
    )

    assert first_followup["discord_pr_generation"] == 2
    assert first_followup["discord_pr_rollover"] is True
    assert second_followup["discord_pr_generation"] == 2
    assert second_followup["discord_pr_rollover"] is False
    assert ledger.discord_pr_generation(session_key) == 2
    replay = ledger.event_from_item(first_followup)
    assert replay.discord_pr_generation == 2
    assert replay.discord_pr_rollover is True


def test_read_only_followup_does_not_advance_merged_pr_generation(tmp_path):
    path = tmp_path / "ledger.json"
    _seed_dev_merge_item(path)
    ledger = GatewayWorkLedger(path, now_fn=lambda: 100.0)
    session_key = build_session_key(_discord_event().source)
    claim = ledger.claim_dev_merge_for_message(
        chat_id="thread-1",
        message_id="final-1",
        actor_id="dev-1",
    )
    assert ledger.finish_dev_merge(
        "work-merge",
        attempt_id=claim["_dev_merge_attempt_id"],
        outcome="merged",
        message="Merged: https://github.com/acme/example/pull/7",
        pr_url="https://github.com/acme/example/pull/7",
    )
    question = _discord_event(message_id="question", text="what did that change?")
    question.discord_runtime_mode = "read_only"
    question.participates_in_work_lifecycle = False

    read_only = ledger.accept_event(
        question,
        session_key=session_key,
        freshness_seconds=60,
    )
    action = ledger.accept_event(
        _discord_event(message_id="next-action"),
        session_key=session_key,
        freshness_seconds=60,
    )

    assert read_only["discord_pr_generation"] == 1
    assert read_only["discord_pr_rollover"] is False
    assert action["discord_pr_generation"] == 2
    assert action["discord_pr_rollover"] is True


def test_blocked_dev_merge_keeps_followups_in_current_pr_generation(tmp_path):
    path = tmp_path / "ledger.json"
    _seed_dev_merge_item(path)
    ledger = GatewayWorkLedger(path, now_fn=lambda: 100.0)
    session_key = build_session_key(_discord_event().source)
    claim = ledger.claim_dev_merge_for_message(
        chat_id="thread-1",
        message_id="final-1",
        actor_id="dev-1",
    )
    assert ledger.finish_dev_merge(
        "work-merge",
        attempt_id=claim["_dev_merge_attempt_id"],
        outcome="blocked",
        message="Checks changed.",
        pr_url="https://github.com/acme/example/pull/7",
    )

    followup = ledger.accept_event(
        _discord_event(message_id="followup-blocked"),
        session_key=session_key,
        freshness_seconds=60,
    )

    assert followup["discord_pr_generation"] == 1
    assert followup["discord_pr_rollover"] is False


def test_merged_pr_generation_survives_terminal_item_compaction(tmp_path):
    path = tmp_path / "ledger.json"
    _seed_dev_merge_item(path)
    now = [100.0]
    ledger = GatewayWorkLedger(path, now_fn=lambda: now[0])
    session_key = build_session_key(_discord_event().source)
    claim = ledger.claim_dev_merge_for_message(
        chat_id="thread-1",
        message_id="final-1",
        actor_id="dev-1",
    )
    assert ledger.finish_dev_merge(
        "work-merge",
        attempt_id=claim["_dev_merge_attempt_id"],
        outcome="merged",
        message="Merged: https://github.com/acme/example/pull/7",
        pr_url="https://github.com/acme/example/pull/7",
    )

    now[0] += 8 * 24 * 60 * 60
    other = _discord_event(message_id="other-thread")
    other.source.chat_id = "thread-2"
    other.source.thread_id = "thread-2"
    ledger.accept_event(
        other,
        session_key=build_session_key(other.source),
        freshness_seconds=60,
    )
    assert ledger.get("work-merge")["tombstone"] is True

    followup = ledger.accept_event(
        _discord_event(message_id="late-followup"),
        session_key=session_key,
        freshness_seconds=60,
    )

    assert followup["discord_pr_generation"] == 2
    assert followup["discord_pr_rollover"] is True


def test_legacy_merged_pr_materializes_lifecycle_before_compaction(tmp_path):
    path = tmp_path / "ledger.json"
    _seed_dev_merge_item(path)
    ledger = GatewayWorkLedger(path, now_fn=lambda: 100.0)
    session_key = build_session_key(_discord_event().source)
    claim = ledger.claim_dev_merge_for_message(
        chat_id="thread-1",
        message_id="final-1",
        actor_id="dev-1",
    )
    assert ledger.finish_dev_merge(
        "work-merge",
        attempt_id=claim["_dev_merge_attempt_id"],
        outcome="merged",
        message="Merged",
        pr_url="https://github.com/acme/example/pull/7",
    )
    legacy = json.loads(path.read_text(encoding="utf-8"))
    legacy.pop("discord_pr_lifecycles", None)
    legacy["items"]["work-merge"]["updated_at"] = 100.0
    legacy.pop("last_compacted_at", None)
    path.write_text(json.dumps(legacy), encoding="utf-8")

    now = 100.0 + (8 * 24 * 60 * 60)
    compacting_ledger = GatewayWorkLedger(path, now_fn=lambda: now)
    other = _discord_event(message_id="other-thread")
    other.source.chat_id = "thread-2"
    other.source.thread_id = "thread-2"
    compacting_ledger.accept_event(
        other,
        session_key=build_session_key(other.source),
        freshness_seconds=60,
    )

    assert compacting_ledger.get("work-merge")["tombstone"] is True
    followup = compacting_ledger.accept_event(
        _discord_event(message_id="legacy-late-followup"),
        session_key=session_key,
        freshness_seconds=60,
    )
    assert followup["discord_pr_generation"] == 2
    assert followup["discord_pr_rollover"] is True


def test_lifecycle_compaction_keeps_irreplaceable_merged_state(tmp_path, monkeypatch):
    import gateway.work_ledger as work_ledger_module

    monkeypatch.setattr(work_ledger_module, "_MAX_DISCORD_PR_LIFECYCLES", 1)
    path = tmp_path / "ledger.json"
    old_session = build_session_key(_discord_event().source)
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "items": {},
                "discord_pr_lifecycles": {
                    old_session: {
                        "generation": 1,
                        "status": "merged",
                        "updated_at": 1.0,
                    },
                    "discord:newer": {
                        "generation": 1,
                        "status": "active",
                        "updated_at": 2.0,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    ledger = GatewayWorkLedger(path, now_fn=lambda: 100.0)
    ledger._write(ledger._read())

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["discord_pr_lifecycles"][old_session]["status"] == "merged"
    followup = ledger.accept_event(
        _discord_event(message_id="after-lifecycle-compaction"),
        session_key=old_session,
        freshness_seconds=60,
    )
    assert followup["discord_pr_generation"] == 2
    assert followup["discord_pr_rollover"] is True


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


def test_delivery_start_fence_allows_only_one_inflight_logical_send(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    item = ledger.accept_event(
        _discord_event(message_id="delivery-fence"),
        session_key="delivery-fence",
        freshness_seconds=60,
    )
    expected_run_state = ledger.run_state_snapshot(item)

    assert ledger.mark_response_delivery_started(
        item["id"],
        expected_run_state=expected_run_state,
    )
    assert not ledger.mark_response_delivery_started(
        item["id"],
        expected_run_state=expected_run_state,
    )
    assert ledger.release_response_delivery_attempt(
        item["id"],
        expected_run_state=expected_run_state,
    )
    assert ledger.mark_response_delivery_started(
        item["id"],
        expected_run_state=expected_run_state,
    )


def test_summary_update_rejects_stale_run_state(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    item = ledger.accept_event(
        _discord_event(message_id="summary-run-state-cas"),
        session_key="summary-run-state-cas",
        freshness_seconds=60,
    )
    assert ledger.mark_response_delivered(item["id"], result_message_id="result-1")
    expected_run_state = ledger.run_state_snapshot(ledger.get(item["id"]))
    assert ledger.mark_agent_running(
        item["id"],
        session_key="summary-run-state-cas",
        run_generation=2,
        owner_pid=2222,
        process_epoch="replacement-process",
    )

    assert not ledger.mark_summary_updated(
        item["id"],
        expected_run_state=expected_run_state,
    )
    stored = ledger.get(item["id"])
    assert stored["status"] == "agent_running"
    assert stored["active_run"]["generation"] == 2


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


def test_ledger_accumulates_text_and_media_delivery_message_ids(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _discord_event(message_id="delivery-media-ids")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    assert ledger.mark_response_delivered(
        item["id"],
        result_message_id="text-1",
        confirmed_message_ids=("text-1",),
    )
    assert ledger.mark_response_delivered(
        item["id"],
        result_message_id="media-1",
        confirmed_message_ids=("media-1", "media-2"),
    )

    stored = ledger.get(item["id"])
    assert stored["result_message_id"] == "text-1"
    assert stored["confirmed_message_ids"] == ["text-1", "media-1", "media-2"]
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


def test_terminal_action_worktree_paths_require_every_user_to_be_old_and_terminal(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    first = ledger.accept_event(
        _repo_discord_event(message_id="terminal-one"),
        session_key="terminal-one",
        freshness_seconds=60,
    )
    second = ledger.accept_event(
        _repo_discord_event(message_id="terminal-two"),
        session_key="terminal-two",
        freshness_seconds=60,
    )
    assert first is not None and second is not None
    for item in (first, second):
        attached = ledger.attach_closeout_workspace(
            item["id"],
            workspace_path="/tmp/project-discord-action-shared",
            repository="acme/project",
            branch="feature/test",
            mode="enforce",
        )
        assert attached is not None

    ledger.mark_completed(first["id"])
    assert ledger.terminal_action_worktree_paths(older_than=200) == []

    ledger.mark_blocked(second["id"], reason="done")
    assert ledger.terminal_action_worktree_paths(older_than=99) == []
    assert ledger.terminal_action_worktree_paths(older_than=200) == [
        "/tmp/project-discord-action-shared"
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


def test_failed_repo_action_response_cannot_complete_work_item(tmp_path):
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
        final_response="Session too large for the model's context window. Use /compact.",
        summary_status="Failed",
    )

    stored = ledger.get(item["id"])
    assert stored["completion_gate"] == {
        "allowed_to_complete": False,
        "summary_status": "Failed",
        "terminal_status": "blocked",
        "reason": "agent_turn_failed",
        "delivery_intent": "full_lifecycle",
        "repo_backed": True,
    }
    ledger.mark_response_delivered(item["id"], result_message_id="error-message")
    ledger.mark_summary_updated(item["id"])
    ledger.mark_completed(item["id"])
    assert ledger.get(item["id"])["status"] == "blocked"


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


def test_close_pr_after_green_checks_is_intentional_unmerged_completion(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(
        text=(
            "Update the smoke line. Open a PR and let checks run. "
            "After checks pass, close the PR while leaving main unchanged."
        )
    )
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response=(
            "Daily smoke completed. Checks passed. "
            "PR status: Closed, not merged. Main unchanged before and after closure."
        ),
        summary_status="Complete",
        runtime_breakdown={
            "verification_evidence": [
                {
                    "surface": "pr",
                    "status": "success",
                    "order": 1,
                    "unmerged_confirmed": True,
                    "detail": '{"unmerged_confirmed":true}',
                },
                {
                    "surface": "main_branch",
                    "status": "success",
                    "order": 2,
                    "detail": '{"proven":true}',
                },
            ]
        },
    )

    stored = ledger.get(item["id"])
    assert stored["summary_status"] == "Complete"
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert stored["completion_gate"]["delivery_intent"] == "pr_only"
    assert stored["completion_gate"]["reason"] == "intentional_narrow_scope_terminal"


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


def test_merge_never_policy_allows_unmerged_pr_after_passed_visual_qa(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Adjust the dashboard panel layout and spacing")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert item is not None
    requirement = item["visual_qa_requirement"]
    assert ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        policy={"merge": "never"},
    ) is not None

    ledger.mark_agent_done(
        item["id"],
        final_response=(
            "Fresh verification:\n"
            "- Focused tests: 6 passed\n"
            "- Svelte check: 0 errors, 0 warnings\n"
            "- Vercel: passed\n"
            "PR opened but not merged; approval is pending."
        ),
        summary_status="Complete",
        runtime_breakdown={
            "verification_evidence": [
                {
                    "surface": "verification",
                    "check_name": "pnpm --dir dashboard build",
                    "status": "failure",
                    "order": 1,
                }
            ]
        },
        visual_qa_receipts=[_visual_receipt(requirement, order=3)],
        visual_qa_code_mutation_observed=True,
        visual_qa_min_receipt_order=2,
    )

    stored = ledger.get(item["id"])
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert stored["completion_gate"]["reason"] == "no_self_declared_delivery_gap"
    assert stored["completion_gate"]["visual_qa"]["status"] == "passed"
    assert stored["final_response"].endswith("PR opened but not merged; approval is pending.")


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


def test_pr_only_closeout_may_leave_canonical_sync_optional(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Open a PR only; do not merge it")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/tmp/project-worktree",
        canonical_path="/tmp/project",
        repository="acme/project",
        branch="feature/test",
        mode="enforce",
        policy={"post_merge_requirements": {"canonical_sync": False}},
    )

    assert attached is not None
    assert attached["policy"]["post_merge_requirements"]["canonical_sync"] is False


def test_preview_delivery_is_exact_head_durable_and_idempotent(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _repo_discord_event(message_id="preview-ready")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/tmp/project-worktree",
        repository="acme/project",
        branch="feature/test",
        mode="enforce",
        policy={"require_preview": True},
    )
    head_sha = "a" * 40
    state = deepcopy(attached)
    state["pr"].update(
        {
            "url": "https://github.com/acme/project/pull/7",
            "state": "OPEN",
            "head_sha": head_sha,
        }
    )
    state["preview"] = {
        "provider": "vercel",
        "status": "ready",
        "observed_sha": head_sha,
        "url": "https://feature-test.vercel.app",
        "deployment_id": "42",
    }

    updated = ledger.update_closeout(
        item["id"],
        state,
        expected_revision=attached["revision"],
    )
    assert updated is not None
    delivery = ledger.get(item["id"])["preview_delivery"]
    assert delivery["status"] == "pending"
    assert delivery["head_sha"] == head_sha

    claimed = ledger.claim_preview_delivery(item["id"], owner="sender-1")
    assert claimed["preview_delivery"]["status"] == "delivering"
    assert ledger.begin_preview_send_attempt(item["id"], owner="sender-1") is True
    assert ledger.complete_preview_delivery(
        item["id"],
        owner="sender-1",
        result_message_id="message-7",
    ) is True
    completed = ledger.get(item["id"])["preview_delivery"]
    assert completed["status"] == "completed"
    assert completed["result_message_id"] == "message-7"
    assert ledger.claim_preview_delivery(item["id"], owner="sender-2") is None


def test_preview_delivery_retries_safe_failures_and_fences_uncertain_sends(tmp_path):
    now = [100.0]
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: now[0])
    event = _repo_discord_event(message_id="preview-retry")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/tmp/project-worktree",
        repository="acme/project",
        branch="feature/test",
        mode="enforce",
        policy={"require_preview": True},
    )
    head_sha = "b" * 40
    state = deepcopy(attached)
    state["pr"].update(
        {
            "url": "https://github.com/acme/project/pull/8",
            "state": "OPEN",
            "head_sha": head_sha,
        }
    )
    state["preview"] = {
        "provider": "vercel",
        "status": "ready",
        "observed_sha": head_sha,
        "url": "https://feature-retry.vercel.app",
    }
    assert ledger.update_closeout(
        item["id"],
        state,
        expected_revision=attached["revision"],
    ) is not None

    assert ledger.claim_preview_delivery(item["id"], owner="sender-1") is not None
    assert ledger.fail_preview_delivery(
        item["id"], owner="sender-1", uncertain=False
    ) is True
    assert ledger.get(item["id"])["preview_delivery"]["status"] == "pending"
    assert ledger.claim_preview_delivery(item["id"], owner="sender-early") is None

    now[0] = 102.0
    assert ledger.claim_preview_delivery(item["id"], owner="sender-2") is not None
    assert ledger.begin_preview_send_attempt(item["id"], owner="sender-2") is True
    assert ledger.fail_preview_delivery(
        item["id"], owner="sender-2", uncertain=True
    ) is True
    assert ledger.get(item["id"])["preview_delivery"]["status"] == "uncertain"
    assert ledger.claim_preview_delivery(item["id"], owner="sender-3") is None


def test_preview_retry_backoff_does_not_starve_newer_threads(tmp_path):
    now = [100.0]
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: now[0])
    work_ids = []
    for index, head_char in enumerate(("a", "b", "c"), start=1):
        event = _repo_discord_event(message_id=f"preview-fair-{index}")
        item = ledger.accept_event(
            event,
            session_key=build_session_key(event.source),
            freshness_seconds=60,
        )
        attached = ledger.attach_closeout_workspace(
            item["id"],
            workspace_path="/tmp/project-worktree",
            repository="acme/project",
            branch=f"feature/test-{index}",
            mode="enforce",
            policy={"require_preview": True},
        )
        head_sha = head_char * 40
        state = deepcopy(attached)
        state["pr"].update(
            {
                "url": f"https://github.com/acme/project/pull/{index}",
                "state": "OPEN",
                "head_sha": head_sha,
            }
        )
        state["preview"] = {
            "provider": "vercel",
            "status": "ready",
            "observed_sha": head_sha,
            "url": f"https://feature-{index}.vercel.app",
        }
        assert ledger.update_closeout(
            item["id"],
            state,
            expected_revision=attached["revision"],
        ) is not None
        work_ids.append(item["id"])

    for index, work_id in enumerate(work_ids[:2], start=1):
        owner = f"failing-{index}"
        assert ledger.claim_preview_delivery(work_id, owner=owner) is not None
        assert ledger.fail_preview_delivery(work_id, owner=owner, uncertain=False) is True

    pending = ledger.pending_preview_deliveries(limit=2)
    assert [item["id"] for item in pending] == [work_ids[2]]
    for work_id in work_ids[:2]:
        assert ledger.get(work_id)["preview_delivery"]["next_attempt_at"] == 102.0


def test_head_advance_cancels_unclaimed_old_preview_delivery(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _repo_discord_event(message_id="preview-head-advance")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/tmp/project-worktree",
        repository="acme/project",
        branch="feature/test",
        mode="enforce",
        policy={"require_preview": True},
    )
    old_head = "c" * 40
    ready = deepcopy(attached)
    ready["pr"].update(
        {
            "url": "https://github.com/acme/project/pull/9",
            "state": "OPEN",
            "head_sha": old_head,
        }
    )
    ready["preview"] = {
        "provider": "vercel",
        "status": "ready",
        "observed_sha": old_head,
        "url": "https://feature-old.vercel.app",
    }
    persisted = ledger.update_closeout(
        item["id"],
        ready,
        expected_revision=attached["revision"],
    )
    assert persisted is not None
    assert ledger.get(item["id"])["preview_delivery"]["status"] == "pending"

    new_head = "d" * 40
    advanced = deepcopy(persisted)
    advanced["pr"]["head_sha"] = new_head
    advanced["preview"] = {
        "provider": "vercel",
        "status": "pending",
        "observed_sha": new_head,
        "url": "",
    }
    assert ledger.update_closeout(
        item["id"],
        advanced,
        expected_revision=persisted["revision"],
    ) is not None

    delivery = ledger.get(item["id"])["preview_delivery"]
    assert delivery["status"] == "cancelled"
    assert delivery["cancelled_reason"] == "pr_head_advanced"
    assert ledger.claim_preview_delivery(item["id"], owner="sender-old") is None


def test_terminal_closeout_keeps_pending_preview_discoverable(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _repo_discord_event(message_id="preview-terminal-retry")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/tmp/project-worktree",
        repository="acme/project",
        branch="feature/test",
        mode="enforce",
        policy={"require_preview": True},
    )
    head_sha = "e" * 40
    terminal = deepcopy(attached)
    terminal["status"] = "pr_published"
    terminal["next_due_at"] = None
    terminal["pr"].update(
        {
            "url": "https://github.com/acme/project/pull/10",
            "state": "OPEN",
            "head_sha": head_sha,
        }
    )
    terminal["preview"] = {
        "provider": "vercel",
        "status": "ready",
        "observed_sha": head_sha,
        "url": "https://feature-terminal.vercel.app",
    }
    assert ledger.update_closeout(
        item["id"],
        terminal,
        expected_revision=attached["revision"],
    ) is not None

    assert ledger.pending_closeouts(due_at=100.0) == []
    pending = ledger.pending_preview_deliveries()
    assert [row["id"] for row in pending] == [item["id"]]
    assert pending[0]["preview_delivery"]["status"] == "pending"


def test_optional_canonical_sync_does_not_hide_private_runtime_lag(tmp_path):
    item = {
        "platform": "discord",
        "source": {"project_path": "/tmp/project"},
        "text": "Ship the runtime change",
        "closeout": {
            "policy": {
                "post_merge_requirements": {"canonical_sync": False}
            }
        },
    }

    gate = classify_delivery_completion(
        item,
        "PR merged, but the private runtime is still behind and not synced.",
    )

    assert gate["allowed_to_complete"] is False
    assert gate["matched_markers"] == ["runtime_not_synced"]


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


def test_read_only_diagnostic_answer_is_not_blocked_by_reported_runtime_lag(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="why do we have no poll data since july 31")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    final_response = (
        "The scraper is working. The displayed date is the poll fieldwork end date, "
        "not the ingestion date. Three newer polls appeared after the daily scrape. "
        "One issue remains: the Airflow runtime is now behind sensitive commits, so "
        "its write guard may block today's scrape until runtime reconciliation completes."
    )
    ledger.mark_agent_done(
        item["id"],
        final_response=final_response,
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Complete"
    assert stored["final_response"] == final_response
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert stored["completion_gate"]["delivery_intent"] == "read_only"
    assert stored["completion_gate"]["reason"] == "answered_read_only_request"
    assert stored["completion_gate"]["matched_markers"] == ["runtime_not_synced"]


def test_question_that_requests_a_fix_keeps_full_lifecycle_gate(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="why is the scraper stale, and can you fix it?")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="The runtime is behind and not synced yet.",
        summary_status="Complete",
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Blocked"
    assert stored["completion_gate"]["allowed_to_complete"] is False
    assert stored["completion_gate"]["delivery_intent"] == "full_lifecycle"


def test_review_only_no_mutation_is_not_blocked_by_incidental_ci_lookup(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(
        text=(
            "do a review of our races tab and individual races pages. "
            "Make the review thorough and individual."
        )
    )
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response=(
            "Review complete. Local development required authentication; "
            "recommendations are source-grounded and no files were changed."
        ),
        summary_status="Complete",
        visual_qa_code_mutation_observed=False,
        runtime_breakdown={
            "mutation_generation": 0,
            "mutation_boundary": 0,
            "verification_evidence": [
                {
                    "surface": "ci",
                    "check_name": (
                        "gh run list --repo sligo-labs/PID "
                        "--workflow 'Deploy Local Dashboard' --limit 3"
                    ),
                    "status": "failure",
                    "order": 24,
                    "detail": "could not find any workflows named Deploy Local Dashboard",
                }
            ],
        },
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Complete"
    assert stored["completion_gate"]["allowed_to_complete"] is True
    assert stored["completion_gate"]["delivery_intent"] == "review_only"


def test_review_intent_with_observed_mutation_keeps_negative_evidence_gate(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="review only: inspect the page and report findings")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
    )
    assert item is not None

    ledger.mark_agent_done(
        item["id"],
        final_response="Updated the page and CI passed.",
        summary_status="Complete",
        visual_qa_code_mutation_observed=True,
        runtime_breakdown={
            "mutation_generation": 1,
            "mutation_boundary": 2,
            "verification_evidence": [
                {
                    "surface": "ci",
                    "check_name": "scripts/run_tests.sh tests/ui",
                    "status": "failure",
                    "order": 3,
                    "detail": "1 failed",
                }
            ],
        },
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Blocked"
    assert stored["completion_gate"]["reason"] == "latest_verification_evidence_negative"


def test_non_visual_verification_block_uses_generic_completion_notice(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    event = _repo_discord_event(text="Update only the requested documentation line.")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert item is not None
    assert item["visual_qa_requirement"]["level"] == "none"

    ledger.mark_agent_done(
        item["id"],
        final_response="PR checks passed; the PR was closed and main stayed unchanged.",
        summary_status="Complete",
        runtime_breakdown={
            "verification_evidence": [
                {
                    "surface": "pr",
                    "check_name": "gh pr view 1092 --json baseRefOid",
                    "status": "failure",
                    "order": 1,
                    "detail": "Unknown JSON field: baseRefOid",
                }
            ]
        },
    )

    stored = ledger.get(item["id"])
    assert stored is not None
    assert stored["summary_status"] == "Blocked"
    assert stored["completion_gate"]["reason"] == "latest_verification_evidence_negative"
    assert stored["final_response"].startswith(
        "⚠️ **Completion blocked.** The work ledger did not authorize completion."
    )
    assert "Enforced visual QA is active" not in stored["final_response"]


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
async def test_startup_replays_drain_recovery_as_read_only_without_lifecycle(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner.adapters = {Platform.DISCORD: adapter}
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._session_run_generation = {}

    event = _discord_event(message_id="read-only-restart", text="audit only")
    event.discord_runtime_mode = "read_only"
    event.discord_runtime_reason = "explicit_no_implementation"
    event.discord_explicit_no_action_denial = True
    event.participates_in_work_lifecycle = False
    item = runner.work_ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=60,
        drain_recovery=True,
    )
    assert item is not None
    runner.work_ledger.claim(item["id"])

    scheduled = runner._schedule_incomplete_discord_work_items()
    await asyncio.sleep(0)

    assert scheduled == 1
    adapter.handle_message.assert_awaited_once()
    replay = adapter.handle_message.await_args.args[0]
    assert replay.work_item_id == item["id"]
    assert replay.discord_runtime_mode == "read_only"
    assert replay.discord_runtime_reason == "explicit_no_implementation"
    assert replay.discord_explicit_no_action_denial is True
    assert replay.participates_in_work_lifecycle is False
    assert replay.discord_drain_recovery is True


def test_read_only_drain_recovery_can_complete_internal_ledger_row(monkeypatch):
    from gateway.platforms.base import BasePlatformAdapter

    ledger = MagicMock()
    monkeypatch.setattr("gateway.work_ledger.GatewayWorkLedger", lambda: ledger)
    event = _discord_event(message_id="read-only-complete", text="audit only")
    event.work_item_id = "work-read-only"
    event.participates_in_work_lifecycle = False
    event.discord_drain_recovery = True

    BasePlatformAdapter._mark_work_item_completed(
        SimpleNamespace(name="test-adapter"),
        event,
        SimpleNamespace(message_id="reply-1"),
    )

    ledger.mark_completed.assert_called_once_with(
        "work-read-only",
        result_message_id="reply-1",
        confirmed_message_ids=("reply-1",),
    )


@pytest.mark.asyncio
async def test_startup_replays_recently_heartbeating_dead_process_work(tmp_path):
    now = [time.time()]
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(
        tmp_path / "work_ledger.json",
        now_fn=lambda: now[0],
    )
    adapter = SimpleNamespace(handle_message=AsyncMock())
    runner.adapters = {Platform.DISCORD: adapter}
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._session_run_generation = {}

    event = _discord_event(message_id=_discord_snowflake_at(now[0]))
    session_key = build_session_key(event.source)
    item = runner.work_ledger.accept_event(
        event,
        session_key=session_key,
        freshness_seconds=10,
    )
    assert item is not None
    assert runner.work_ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=4,
        owner_pid=99999999,
        process_epoch="dead-process",
    )
    now[0] += 3590
    assert runner.work_ledger.renew_active_run(
        item["id"],
        session_key=session_key,
        run_generation=4,
        owner_pid=99999999,
        process_epoch="dead-process",
    )
    now[0] += 20

    scheduled = runner._schedule_incomplete_discord_work_items()
    await asyncio.sleep(0)

    assert scheduled == 1
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_startup_reconciles_reaction_for_newly_expired_work(tmp_path):
    now = [100.0]
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(
        tmp_path / "work_ledger.json",
        now_fn=lambda: now[0],
    )
    adapter = SimpleNamespace(
        handle_message=AsyncMock(),
        reconcile_work_ledger_thread_reaction=AsyncMock(return_value="errored"),
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._background_tasks = set()

    event = _discord_event(message_id="startup-expired")
    item = runner.work_ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=10,
    )
    assert item is not None
    now[0] = 200.0

    scheduled = runner._schedule_incomplete_discord_work_items()
    await asyncio.sleep(0)

    assert scheduled == 0
    adapter.handle_message.assert_not_awaited()
    adapter.reconcile_work_ledger_thread_reaction.assert_awaited_once()
    assert runner.work_ledger.get(item["id"])["status"] == "expired"


@pytest.mark.asyncio
async def test_startup_auto_resume_reuses_original_discord_work_item(tmp_path):
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = GatewayWorkLedger(tmp_path / "work_ledger.json")
    runner._background_tasks = set()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._persist_active_agents = MagicMock()
    runner._update_runtime_status = MagicMock()
    runner._is_user_authorized = lambda _source: True
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
    runner._running_agents = {}
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
async def test_read_only_question_never_enters_or_mutates_action_ledger(tmp_path):
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
    event.discord_runtime_mode = "read_only"
    event.discord_action_request_intent = None
    session_key = build_session_key(event.source)
    item = runner._accept_discord_work_item(
        event,
        session_key,
    )
    assert item is None

    runner._register_discord_summary_post_delivery(
        event=event,
        source=event.source,
        session_key=session_key,
        run_generation=9,
        session_id="session-1",
        final_response="The parser and deployment verification are still outstanding.",
        agent_result={"completed": True},
    )

    assert callbacks == []
    adapter.update_feature_summary.assert_not_awaited()
    assert feature_summary == original_summary
    assert runner.work_ledger.incomplete_items() == []


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


def test_visual_completion_updates_latest_closeout_revision_without_clobbering_watcher_state(
    tmp_path,
):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(
        message_id="closeout-visual-latest",
        text="Build a responsive dashboard with a mobile sidebar.",
    )
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert item is not None
    head_sha = "a" * 40
    activated = _activated_visual_closeout(ledger, item, head_sha=head_sha)
    watcher_state = dict(activated)
    watcher_state["status"] = "waiting_for_ci"
    watcher_state["next_due_at"] = 130.0
    watcher_state["pr"] = {
        **watcher_state["pr"],
        "url": "https://github.com/acme/example/pull/9",
    }
    updated = ledger.update_closeout(
        item["id"],
        watcher_state,
        expected_revision=activated["revision"],
    )
    assert updated is not None

    applied = ledger.apply_closeout_visual_completion(
        item["id"],
        expected_head_sha=head_sha,
        receipts=[_visual_receipt(item["visual_qa_requirement"], order=4)],
        min_receipt_order=4,
    )

    assert applied is not None
    assert applied["revision"] == updated["revision"] + 1
    assert applied["status"] == "waiting_for_ci"
    assert applied["pr"]["url"].endswith("/9")
    assert applied["visual_qa"] == {"status": "passed", "head_sha": head_sha}
    assert applied["next_due_at"] == 100.0


def test_visual_completion_is_sanitized_and_late_h_receipt_is_rejected_after_h2(
    tmp_path,
):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(
        message_id="closeout-visual-stale",
        text="Build a responsive dashboard with a mobile sidebar.",
    )
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert item is not None
    head_sha = "b" * 40
    activated = _activated_visual_closeout(ledger, item, head_sha=head_sha)
    unsafe_receipt = {
        **_visual_receipt(item["visual_qa_requirement"], order=4),
        "evidence_ref": "https://example.test/?token=do-not-store",
    }

    sanitized = ledger.apply_closeout_visual_completion(
        item["id"],
        expected_head_sha=head_sha,
        receipts=[unsafe_receipt],
        min_receipt_order=4,
    )

    assert sanitized is not None
    assert sanitized["visual_qa"] == {"status": "missing", "head_sha": head_sha}
    assert "do-not-store" not in str(ledger.get(item["id"]))

    head_sha_2 = "c" * 40
    advanced = dict(sanitized)
    advanced["pr"] = {**advanced["pr"], "head_sha": head_sha_2}
    advanced["local_verification"] = {"status": "passed", "head_sha": head_sha_2}
    advanced["visual_qa"] = {"status": "stale"}
    advanced_state = ledger.update_closeout(
        item["id"],
        advanced,
        expected_revision=sanitized["revision"],
    )
    assert advanced_state is not None
    before = ledger.get(item["id"])

    assert ledger.apply_closeout_visual_completion(
        item["id"],
        expected_head_sha=head_sha,
        receipts=[_visual_receipt(item["visual_qa_requirement"], order=5)],
        min_receipt_order=5,
    ) is None
    assert ledger.get(item["id"]) == before


@pytest.mark.parametrize("source", ["direct", "fable", "opus"])
def test_verified_h2_publication_invalidates_h_gates_and_active_lease(
    tmp_path,
    source,
):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(
        message_id="closeout-publish-h2",
        text="Build a responsive dashboard with a mobile sidebar.",
    )
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert item is not None
    head_sha = "d" * 40
    activated = _activated_visual_closeout(
        ledger,
        item,
        head_sha=head_sha,
        source=source,
    )
    prior = dict(activated)
    prior["policy"] = {**prior["policy"], "require_review": True}
    prior["review"] = {"status": "approved", "head_sha": head_sha}
    prior["visual_qa"] = {"status": "passed", "head_sha": head_sha}
    prior["ci"] = {
        **prior["ci"],
        "head_sha": head_sha,
        "status": "passed",
        "wait_state": "complete",
    }
    prior["pr"] = {
        **prior["pr"],
        "head_sha": head_sha,
        "merge_sha": "f" * 40,
        "ready_at": 90.0,
        "merge_attempted_head_sha": head_sha,
    }
    prior["post_merge"] = {
        **prior["post_merge"],
        "target_sha": "f" * 40,
    }
    prior["mutation_uncertainty"] = {
        "operation": "git_push",
        "head_sha": head_sha,
        "started_at": 90.0,
    }
    updated = ledger.update_closeout(
        item["id"],
        prior,
        expected_revision=activated["revision"],
    )
    assert updated is not None
    leased = ledger.lease_closeout(
        item["id"],
        owner="watcher-h",
        lease_seconds=30,
        expected_revision=updated["revision"],
    )
    assert leased is not None
    head_sha_2 = "e" * 40

    published = ledger.publish_closeout_verified_head(
        item["id"],
        expected_head_sha=head_sha,
        verified_head_sha=head_sha_2,
    )

    assert published is not None
    assert published["revision"] == leased["closeout"]["revision"] + 1
    assert published["lease_generation"] == leased["closeout"]["lease_generation"] + 1
    assert published["lease"] == {"owner": "", "until": None}
    assert published["local_verification"] == {
        "status": "passed",
        "head_sha": head_sha_2,
    }
    assert published["review"] == {"status": "stale"}
    assert published["visual_qa"] == {"status": "pending", "head_sha": head_sha_2}
    assert published["ci"]["head_sha"] == head_sha_2
    assert published["ci"]["status"] == "not_checked"
    assert published["pr"]["merge_sha"] == ""
    assert published["pr"]["ready_at"] is None
    assert published["pr"]["pending_push_head_sha"] == head_sha_2
    assert published["post_merge"]["target_sha"] == ""
    assert published["mutation_uncertainty"] == {}
    assert ledger.publish_closeout_verified_head(
        item["id"],
        expected_head_sha=head_sha,
        verified_head_sha="f" * 40,
    ) is None
    assert ledger.apply_closeout_visual_completion(
        item["id"],
        expected_head_sha=head_sha,
        receipts=[_visual_receipt(item["visual_qa_requirement"], order=6)],
        min_receipt_order=6,
    ) is None


def test_leased_h_receipt_is_discarded_when_watcher_reconciles_h2(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(
        message_id="closeout-visual-h2-race",
        text="Build a responsive dashboard with a mobile sidebar.",
    )
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
        visual_qa_config={"mode": "enforce_explicit"},
    )
    assert item is not None
    head_sha = "d" * 40
    activated = _activated_visual_closeout(ledger, item, head_sha=head_sha)
    leased = ledger.lease_closeout(
        item["id"],
        owner="watcher-h2",
        lease_seconds=30,
        expected_revision=activated["revision"],
    )
    assert leased is not None
    assert ledger.apply_closeout_visual_completion(
        item["id"],
        expected_head_sha=head_sha,
        receipts=[_visual_receipt(item["visual_qa_requirement"], order=4)],
        min_receipt_order=4,
    ) is not None

    head_sha_2 = "e" * 40
    reconciled = dict(leased["closeout"])
    reconciled["pr"] = {**reconciled["pr"], "head_sha": head_sha_2}
    reconciled["local_verification"] = {"status": "stale"}
    reconciled["visual_qa"] = {"status": "stale"}
    released = ledger.release_closeout(
        item["id"],
        owner="watcher-h2",
        expected_revision=leased["closeout"]["revision"],
        expected_generation=leased["closeout"]["lease_generation"],
        closeout_state=reconciled,
    )

    assert released is not None
    assert released["pr"]["head_sha"] == head_sha_2
    assert released["visual_qa"] == {"status": "stale"}
    assert "closeout_visual_completion" not in ledger.get(item["id"])


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
    assert leased["closeout"]["lease_generation"] == 1
    assert leased["closeout"]["lease"] == {"owner": "watcher-1", "until": 130.0}
    assert ledger.pending_closeouts(due_at=now) == []
    now = 110.0
    assert ledger.renew_closeout_lease(
        item["id"],
        owner="watcher-2",
        lease_seconds=30,
        expected_revision=leased["closeout"]["revision"],
        expected_generation=leased["closeout"]["lease_generation"],
    ) is False
    assert ledger.renew_closeout_lease(
        item["id"],
        owner="watcher-1",
        lease_seconds=30,
        expected_revision=leased["closeout"]["revision"] - 1,
        expected_generation=leased["closeout"]["lease_generation"],
    ) is False
    assert ledger.renew_closeout_lease(
        item["id"],
        owner="watcher-1",
        lease_seconds=30,
        expected_revision=leased["closeout"]["revision"],
        expected_generation=leased["closeout"]["lease_generation"] + 1,
    ) is False
    assert ledger.renew_closeout_lease(
        item["id"],
        owner="watcher-1",
        lease_seconds=30,
        expected_revision=leased["closeout"]["revision"],
        expected_generation=leased["closeout"]["lease_generation"],
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
        expected_generation=leased["closeout"]["lease_generation"],
        closeout_state=updated,
    )
    assert released is not None
    assert released["revision"] == 4
    assert released["lease"] == {"owner": "", "until": None}
    assert ledger.pending_closeouts(due_at=150) == []
    assert ledger.pending_closeouts(due_at=160)[0]["closeout"]["status"] == "waiting_for_ci"


def test_expired_closeout_lease_cannot_persist_stale_authoritative_result(tmp_path):
    now = 100.0
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: now)
    event = _discord_event(message_id="expired-closeout-lease")
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
        owner="watcher-expired",
        lease_seconds=1,
        expected_revision=activated["revision"],
    )
    stale = dict(leased["closeout"])
    stale["status"] = "completed"
    before = ledger.get(item["id"])
    now = 102.0

    assert ledger.release_closeout(
        item["id"],
        owner="watcher-expired",
        expected_revision=leased["closeout"]["revision"],
        expected_generation=leased["closeout"]["lease_generation"],
        closeout_state=stale,
    ) is None
    assert ledger.finalize_blocked_closeout(
        item["id"],
        owner="watcher-expired",
        expected_revision=leased["closeout"]["revision"],
        expected_generation=leased["closeout"]["lease_generation"],
        closeout_state={**stale, "status": "repair_required"},
        final_response="stale result",
        reason="stale",
    ) is None
    assert ledger.get(item["id"]) == before


def test_remote_mutation_fence_survives_replacement_lease_generation(tmp_path):
    now = {"value": 100.0}
    ledger = GatewayWorkLedger(
        tmp_path / "work_ledger.json",
        now_fn=lambda: now["value"],
    )
    event = _discord_event(message_id="closeout-mutation-fence")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
    )
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        repository="acme/example",
        branch="feature/test",
        mode="enforce",
    )
    activated = ledger.activate_closeout(
        item["id"],
        attached,
        expected_revision=attached["revision"],
    )
    first = ledger.lease_closeout(
        item["id"],
        owner="watcher-old",
        lease_seconds=1,
        expected_revision=activated["revision"],
    )
    assert ledger.record_closeout_mutation_start(
        item["id"],
        owner="watcher-old",
        expected_revision=first["closeout"]["revision"],
        expected_generation=first["closeout"]["lease_generation"],
        operation="post_merge_restart",
        context={
            "at": 100.0,
            "head_sha": "a" * 40,
            "repository": "acme/example",
            "baseline_pid": 1234,
            "baseline_start_time": 777,
        },
    )

    now["value"] = 102.0
    replacement = ledger.lease_closeout(
        item["id"],
        owner="watcher-new",
        lease_seconds=30,
        expected_revision=first["closeout"]["revision"],
    )

    assert replacement is not None
    assert replacement["closeout"]["lease_generation"] == 2
    assert replacement["closeout"]["mutation_uncertainty"] == {
        "status": "uncertain",
        "operation": "post_merge_restart",
        "at": 100.0,
        "head_sha": "a" * 40,
        "repository": "acme/example",
        "baseline_pid": 1234,
        "baseline_start_time": 777,
    }
    assert "closeout_mutation_fence" not in ledger.get(item["id"])
    assert ledger.record_closeout_mutation_uncertainty(
        item["id"],
        owner="watcher-old",
        expected_revision=first["closeout"]["revision"],
        expected_generation=first["closeout"]["lease_generation"],
        uncertainty={"status": "uncertain", "operation": "post_merge_restart"},
    ) is False


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
        expected_generation=leased["closeout"]["lease_generation"],
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
        expected_generation=leased["closeout"]["lease_generation"],
        closeout_state=blocked_state,
        final_response="Trusted closeout blocked.",
        reason="trusted_closeout_repair_required",
    )
    loser = ledger.finalize_blocked_closeout(
        item["id"],
        owner="watcher-1",
        expected_revision=leased["closeout"]["revision"],
        expected_generation=leased["closeout"]["lease_generation"],
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


def test_successful_closeout_finalization_is_one_atomic_cas(tmp_path):
    ledger = GatewayWorkLedger(tmp_path / "work_ledger.json", now_fn=lambda: 100.0)
    event = _discord_event(message_id="successful-closeout-cas")
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
    completed_state = dict(leased["closeout"])
    completed_state["status"] = "completed"
    expected = ledger.run_state_snapshot(leased)

    winner = ledger.finalize_successful_closeout(
        item["id"],
        owner="watcher-1",
        expected_revision=leased["closeout"]["revision"],
        expected_generation=leased["closeout"]["lease_generation"],
        closeout_state=completed_state,
        final_response="Trusted closeout completed.",
        expected_run_state=expected,
    )
    loser = ledger.finalize_successful_closeout(
        item["id"],
        owner="watcher-1",
        expected_revision=leased["closeout"]["revision"],
        expected_generation=leased["closeout"]["lease_generation"],
        closeout_state=completed_state,
        final_response="duplicate",
        expected_run_state=expected,
    )

    assert winner is not None
    assert loser is None
    stored = ledger.get(item["id"])
    assert stored["status"] == "agent_done"
    assert stored["final_response"] == "Trusted closeout completed."
    assert stored["closeout"]["status"] == "completed"
    assert stored["closeout"]["lease"] == {"owner": "", "until": None}


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
        expected_generation=leased["closeout"]["lease_generation"],
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


def _required_async_ledger(tmp_path, *, now=100.0):
    ledger = GatewayWorkLedger(
        tmp_path / "required_async_ledger.json",
        now_fn=lambda: now,
    )
    event = _discord_event(message_id="required-async")
    session_key = build_session_key(event.source)
    item = ledger.accept_event(
        event,
        session_key=session_key,
        freshness_seconds=3600,
    )
    assert item is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=7,
        owner_pid=101,
        process_epoch="boot-a",
    )
    assert ledger.begin_required_async_attempt(
        item["id"],
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
    ) is not None
    return ledger, item["id"]


def _unstarted_required_async_ledger(tmp_path, *, name="atomic-required"):
    ledger = GatewayWorkLedger(tmp_path / f"{name}.json", now_fn=lambda: 100.0)
    event = _discord_event(message_id=name)
    session_key = build_session_key(event.source)
    item = ledger.accept_event(
        event,
        session_key=session_key,
        freshness_seconds=3600,
    )
    assert item is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=7,
        owner_pid=101,
        process_epoch="boot-a",
    )
    assert "required_async_completions" not in ledger.get(item["id"])
    return ledger, item["id"]


def _register_required_dispatch(ledger, work_id, delegation_id, *, owner_pid=101, epoch="boot-a"):
    return ledger.register_required_async_dispatch(
        work_id,
        delegation_id=delegation_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        owner_pid=owner_pid,
        process_epoch=epoch,
        scope_paths=[f"src/{delegation_id}"],
    )


def _complete_required_dispatch(ledger, work_id, delegation_id, *, success=True, **kwargs):
    return ledger.record_required_async_completion(
        work_id,
        delegation_id=delegation_id,
        success=success,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        status="completed" if success else "error",
        **kwargs,
    )


def test_required_async_first_registration_atomically_creates_attempt(tmp_path):
    ledger, work_id = _unstarted_required_async_ledger(tmp_path)

    state = ledger.register_required_async_dispatch(
        work_id,
        delegation_id="worker-first",
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        owner_pid=101,
        process_epoch="boot-a",
    )

    assert state is not None
    assert state["attempt_id"] == "boot-a:7"
    assert state["dispatches"]["worker-first"]["state"] == "registered"
    stored = ledger.get(work_id)["required_async_completions"]
    assert set(stored["dispatches"]) == {"worker-first"}


def test_required_async_registration_atomically_replaces_newer_attempt(tmp_path):
    ledger, work_id = _unstarted_required_async_ledger(tmp_path, name="atomic-newer")
    assert ledger.register_required_async_dispatch(
        work_id,
        delegation_id="worker-old",
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        owner_pid=101,
        process_epoch="boot-a",
    ) is not None

    newer = ledger.register_required_async_dispatch(
        work_id,
        delegation_id="worker-new",
        generation=8,
        attempt_id="boot-b:8",
        attempt_order=11,
        owner_pid=202,
        process_epoch="boot-b",
    )

    assert newer is not None
    assert newer["generation"] == 8
    assert newer["attempt_id"] == "boot-b:8"
    assert set(newer["dispatches"]) == {"worker-new"}
    assert newer["dispatches"]["worker-new"]["state"] == "registered"


def test_required_async_atomic_registration_rejects_stale_without_mutation(tmp_path):
    ledger, work_id = _unstarted_required_async_ledger(tmp_path, name="atomic-stale")
    assert ledger.register_required_async_dispatch(
        work_id,
        delegation_id="worker-current",
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        owner_pid=101,
        process_epoch="boot-a",
    ) is not None
    before = ledger.get(work_id)

    assert ledger.register_required_async_dispatch(
        work_id,
        delegation_id="worker-stale",
        generation=6,
        attempt_id="boot-old:6",
        attempt_order=9,
        owner_pid=99,
        process_epoch="boot-old",
    ) is None
    assert ledger.get(work_id) == before


def test_rejected_first_registration_does_not_create_empty_attempt(tmp_path):
    ledger, work_id = _unstarted_required_async_ledger(tmp_path, name="atomic-rejected")

    assert ledger.register_required_async_dispatch(
        work_id,
        delegation_id="worker-invalid",
        generation=None,
        attempt_id="",
        attempt_order=None,
    ) is None
    assert "required_async_completions" not in ledger.get(work_id)


def test_required_async_registration_sealing_and_two_worker_readiness(tmp_path):
    ledger, work_id = _required_async_ledger(tmp_path)
    first = _register_required_dispatch(ledger, work_id, "worker-a")
    assert first is not None
    assert first["pending_count"] == 1
    assert first["owns_recovery"] is True
    assert _register_required_dispatch(ledger, work_id, "worker-a") == first
    assert _register_required_dispatch(ledger, work_id, "worker-b") is not None
    assert ledger.mark_required_async_dispatch_running(
        work_id,
        delegation_id="worker-a",
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        owner_pid=101,
        process_epoch="boot-a",
    )["dispatches"]["worker-a"]["state"] == "running"

    sealed = ledger.seal_required_async_attempt(
        work_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
    )
    assert sealed["sealed"] is True
    assert sealed["pending_count"] == 2
    assert sealed["ready_to_reconcile"] is False
    assert _register_required_dispatch(ledger, work_id, "worker-c") is None

    first_done = _complete_required_dispatch(ledger, work_id, "worker-a")
    assert first_done["pending_count"] == 1
    assert first_done["all_terminal"] is False
    assert first_done["ready_to_reconcile"] is False
    second_done = _complete_required_dispatch(ledger, work_id, "worker-b")
    assert second_done["pending_count"] == 0
    assert second_done["all_terminal"] is True
    assert second_done["ready_to_reconcile"] is True
    assert second_done["succeeded"] == 2


def test_required_async_submit_failure_and_conflicting_replay_stay_failed(tmp_path):
    ledger, work_id = _required_async_ledger(tmp_path)
    assert _register_required_dispatch(ledger, work_id, "worker-a") is not None
    failed = ledger.record_required_async_submit_failure(
        work_id,
        delegation_id="worker-a",
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        error="executor refused submission",
    )
    assert failed["failed"] is True
    assert failed["dispatches"]["worker-a"]["status"] == "submit_failed"

    replay = _complete_required_dispatch(ledger, work_id, "worker-a", success=True)
    assert replay["failed"] is True
    assert replay["dispatches"]["worker-a"]["success"] is False
    assert replay["dispatches"]["worker-a"]["status"] == "conflicting_replay"
    assert ledger.get(work_id)["completion_gate"]["reason"] == "required_async_completion_failed"


def test_required_async_cancel_and_unknown_are_sticky(tmp_path):
    ledger, work_id = _required_async_ledger(tmp_path)
    assert _register_required_dispatch(ledger, work_id, "worker-cancel") is not None
    cancelled = ledger.cancel_required_async_dispatch(
        work_id,
        delegation_id="worker-cancel",
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        reason="user stopped worker",
    )
    assert cancelled["cancelled"] == 1
    assert cancelled["failed"] is True
    assert _complete_required_dispatch(
        ledger,
        work_id,
        "worker-cancel",
        success=True,
    )["dispatches"]["worker-cancel"]["state"] == "cancelled"

    assert _register_required_dispatch(
        ledger,
        work_id,
        "worker-old",
        owner_pid=202,
        epoch="boot-old",
    ) is not None
    changed = ledger.mark_orphaned_required_async_dispatches_unknown(
        current_process_epoch="boot-a",
        current_owner_pid=101,
    )
    assert changed == [work_id]
    state = ledger.required_async_completion_state(work_id)
    assert state["outcome_unknown"] == 1
    assert state["dispatches"]["worker-old"]["state"] == "outcome_unknown"
    assert state["failed"] is True


def test_required_async_attempt_cancel_preserves_terminal_dispatch_evidence(tmp_path):
    ledger, work_id = _required_async_ledger(tmp_path)
    assert _register_required_dispatch(ledger, work_id, "worker-terminal")
    terminal = _complete_required_dispatch(
        ledger,
        work_id,
        "worker-terminal",
        summary="completed before stop",
    )
    assert terminal["dispatches"]["worker-terminal"]["state"] == "terminal"

    cancelled = ledger.cancel_required_async_attempt(
        work_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        reason="session_stop",
    )

    assert cancelled["attempt_cancelled"] is True
    assert cancelled["failed"] is True
    assert cancelled["failure_reason"] == "required_async_attempt_cancelled"
    dispatch = cancelled["dispatches"]["worker-terminal"]
    assert dispatch["state"] == "terminal"
    assert dispatch["success"] is True
    assert dispatch["summary"] == "completed before stop"


def test_required_async_attempt_fencing_rejects_stale_or_conflicting_writes(tmp_path):
    ledger, work_id = _required_async_ledger(tmp_path)
    assert ledger.register_required_async_dispatch(
        work_id,
        delegation_id="stale",
        generation=6,
        attempt_id="boot-a:6",
        attempt_order=9,
    ) is None
    assert ledger.begin_required_async_attempt(
        work_id,
        generation=8,
        attempt_id="conflict",
        attempt_order=10,
    ) is None
    newer = ledger.begin_required_async_attempt(
        work_id,
        generation=8,
        attempt_id="boot-a:8",
        attempt_order=11,
    )
    assert newer is not None
    assert newer["generation"] == 8
    assert newer["dispatches"] == {}
    assert _complete_required_dispatch(ledger, work_id, "old-worker") is None


def test_required_async_terminal_evidence_is_allowlisted_and_bounded(tmp_path):
    ledger, work_id = _required_async_ledger(tmp_path)
    assert _register_required_dispatch(ledger, work_id, "worker-a") is not None
    huge = "x" * 5000
    state = _complete_required_dispatch(
        ledger,
        work_id,
        "worker-a",
        summary=huge,
        error=huge,
        closeout_id=huge,
        evidence={
            "worker_cwd": huge,
            "changed": True,
            "scope_paths": [f"src/{index}/{huge}" for index in range(100)],
            "scope_check": {
                "clean": False,
                "out_of_scope_files": [huge] * 100,
                "secret": huge,
            },
            "parallel_merge": {
                "merged": False,
                "recovery_required": True,
                "error": huge,
                "raw_result": {"secret": huge},
            },
            "worker_run": {
                "backend": "codex",
                "model": huge,
                "failed": False,
                "messages": [huge],
            },
            "test_refs": [f"test-{index}-{huge}" for index in range(100)],
            "head_sha": "a" * 40,
            "arbitrary_raw_payload": {"secret": huge},
        },
    )
    dispatch = state["dispatches"]["worker-a"]
    evidence = dispatch["evidence"]
    assert len(dispatch["summary"]) <= 1000
    assert len(dispatch["error"]) <= 1000
    assert len(dispatch["closeout_id"]) <= 240
    assert len(evidence["worker_cwd"]) <= 240
    assert len(evidence["scope_paths"]) == 32
    assert len(evidence["test_refs"]) == 16
    assert evidence["head_sha"] == "a" * 40
    assert "secret" not in evidence["scope_check"]
    assert "raw_result" not in evidence["parallel_merge"]
    assert "messages" not in evidence["worker_run"]
    assert "arbitrary_raw_payload" not in evidence


def test_required_async_legacy_outcomes_normalize_without_losing_compatibility(tmp_path):
    ledger, work_id = _required_async_ledger(tmp_path)
    data = ledger._read()
    data["items"][work_id]["required_async_completions"] = {
        "generation": 7,
        "attempt_id": "boot-a:7",
        "attempt_order": 10,
        "outcomes": {
            "legacy-worker": {
                "success": True,
                "status": "completed",
                "completed_at": 99.0,
                "closeout_id": "closeout-1",
            }
        },
    }
    ledger._write(data)

    state = ledger.required_async_completion_state(work_id)
    assert state["sealed"] is True
    assert state["ready_to_reconcile"] is True
    assert state["failed"] is False
    assert state["succeeded"] == 1
    assert state["dispatches"]["legacy-worker"]["state"] == "terminal"
    assert state["outcomes"]["legacy-worker"]["success"] is True


def test_required_async_v2_checkpoint_mutation_upgrades_schema(tmp_path):
    ledger, work_id = _required_async_ledger(tmp_path)
    assert _register_required_dispatch(ledger, work_id, "worker-v2")
    assert _complete_required_dispatch(ledger, work_id, "worker-v2")
    assert ledger.seal_required_async_attempt(
        work_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
    )
    data = ledger._read()
    data["items"][work_id]["required_async_completions"]["schema_version"] = 2
    ledger._write(data)

    state = ledger.record_required_async_checkpoint(
        work_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        parent_sha="a" * 40,
        tree_sha="b" * 40,
        message="Hermes async checkpoint work-v2",
        repository_root=str(tmp_path / "repo"),
        workspace_path=str(tmp_path / "repo" / "nested"),
    )

    assert state is not None
    assert state["schema_version"] == 6
    assert state["checkpoint"]["tree_sha"] == "b" * 40
    stored = ledger.get(work_id)["required_async_completions"]
    assert stored["schema_version"] == 6
    assert stored["checkpoint"]["parent_sha"] == "a" * 40


@pytest.mark.parametrize(
    "raw_state",
    [
        "not-a-mapping",
        {"schema_version": 999, "future_state": {"unknown": True}},
        {
            "schema_version": 999,
            "generation": 7,
            "attempt_id": "boot-a:7",
            "attempt_order": 10,
            "dispatches": {
                "future-worker": {
                    "state": "running",
                    "owner_pid": 101,
                    "process_epoch": "boot-a",
                }
            },
        },
        {"generation": 7, "attempt_id": "boot-a:7", "attempt_order": 10, "outcomes": {}},
    ],
)
def test_required_async_malformed_or_unknown_state_fails_closed(tmp_path, raw_state):
    ledger, work_id = _required_async_ledger(tmp_path)
    data = ledger._read()
    data["items"][work_id]["required_async_completions"] = raw_state
    ledger._write(data)

    state = ledger.required_async_completion_state(work_id)
    assert state["malformed"] is True
    assert state["owns_recovery"] is True
    assert state["sealed"] is True
    assert state["all_terminal"] is True
    assert state["ready_to_reconcile"] is True
    assert state["failed"] is True


def test_required_async_failure_finalization_uses_terminal_delivery_fence(tmp_path):
    ledger, work_id = _required_async_ledger(tmp_path)
    assert _register_required_dispatch(ledger, work_id, "worker-a") is not None
    assert ledger.seal_required_async_attempt(
        work_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
    ) is not None
    assert _complete_required_dispatch(ledger, work_id, "worker-a") is not None

    blocked = ledger.finalize_required_async_failure(
        work_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        final_response="Worker evidence could not be reconciled safely.",
        reason="required_async_reconciliation_failed",
        reconciliation_id="reconcile-1",
    )
    assert blocked is not None
    assert blocked["status"] == "blocked"
    assert blocked["completion_gate"]["reason"] == "required_async_reconciliation_failed"
    assert blocked["terminal_delivery"]["source"] == "required_async_completion"
    assert blocked["terminal_delivery"]["status"] == "pending"
    reconciled = ledger.required_async_completion_state(work_id)
    assert reconciled["reconciled_at"] == 100.0
    assert reconciled["owns_recovery"] is False
    assert ledger.finalize_required_async_failure(
        work_id,
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        final_response="duplicate",
        reason="different",
        reconciliation_id="reconcile-1",
    )["final_response"] == "Worker evidence could not be reconciled safely."
    assert ledger.finalize_required_async_failure(
        work_id,
        generation=8,
        attempt_id="boot-a:8",
        attempt_order=11,
        final_response="stale",
    ) is None


def test_required_async_ownership_outlives_intake_expiry_until_reconciled(tmp_path):
    now = [time.time()]
    ledger = GatewayWorkLedger(
        tmp_path / "required_async_expiry.json",
        now_fn=lambda: now[0],
    )
    old_message_id = _discord_snowflake_at(now[0] - (8 * 24 * 60 * 60))
    event = _discord_event(message_id=old_message_id)
    session_key = build_session_key(event.source)
    item = ledger.accept_event(
        event,
        session_key=session_key,
        freshness_seconds=1,
    )
    assert item is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_key=session_key,
        run_generation=7,
        owner_pid=101,
        process_epoch="boot-a",
    )
    assert ledger.begin_required_async_attempt(
        item["id"],
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
    ) is not None
    assert _register_required_dispatch(ledger, item["id"], "worker-long") is not None

    now[0] += 2 * 60 * 60
    pending = ledger.incomplete_items()
    assert [entry["id"] for entry in pending] == [item["id"]]
    assert ledger.get(item["id"])["status"] == "agent_running"

    assert _complete_required_dispatch(ledger, item["id"], "worker-long") is not None
    assert ledger.seal_required_async_attempt(
        item["id"],
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
    ) is not None
    assert ledger.mark_required_async_reconciled(
        item["id"],
        generation=7,
        attempt_id="boot-a:7",
        attempt_order=10,
        reconciliation_id="reconcile-long-worker",
    ) is not None

    assert ledger.incomplete_items() == []
    assert ledger.get(item["id"])["status"] == "expired"
