import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import hermes_time
from cron import discord_execution_audit as audit
from hermes_cli.discord_time import DISCORD_EPOCH_SECONDS


def _snowflake(at: datetime) -> str:
    milliseconds = int((at.timestamp() - DISCORD_EPOCH_SECONDS) * 1000)
    return str(milliseconds << 22)


def _item(
    at: datetime,
    *,
    status: str = "completed",
    gate_reason: str = "no_self_declared_delivery_gap",
    terminal_status: str | None = None,
    runtime: dict | None = None,
    provider: dict | None = None,
    board: str | None = None,
) -> dict:
    feature_summary = {"kanban_board": {"slug": board}} if board else {}
    return {
        "id": f"discord-work-{at.timestamp()}-{status}",
        "platform": "discord",
        "message_id": _snowflake(at),
        "source": {"platform": "discord", "message_id": _snowflake(at)},
        "status": status,
        "summary_status": "Blocked" if status == "blocked" else "Complete",
        "completion_gate": {
            "allowed_to_complete": status != "blocked",
            "terminal_status": terminal_status or status,
            "reason": gate_reason,
        },
        "runtime_breakdown": runtime
        or {
            "wall_s": 30,
            "model_s": 20,
            "tools_s": 5,
            "overhead_s": 5,
            "tool_calls": 1,
            "tool_errors": 0,
            "tool_blocked": 0,
            "top_tools": [],
        },
        "provider_no_progress": provider or {},
        "feature_summary": feature_summary,
    }


def _write_ledger(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "items": {item["id"]: item for item in items}}),
        encoding="utf-8",
    )


def _window(monkeypatch, timezone: str, now: datetime) -> audit.AuditWindow:
    monkeypatch.setenv("HERMES_TIMEZONE", timezone)
    hermes_time.reset_cache()
    return audit.previous_local_day_window(now)


def test_previous_local_day_window_uses_adjacent_midnights_across_dst(monkeypatch):
    tz = ZoneInfo("America/New_York")

    spring = _window(monkeypatch, "America/New_York", datetime(2026, 3, 9, 8, tzinfo=tz))
    fall = _window(monkeypatch, "America/New_York", datetime(2026, 11, 2, 8, tzinfo=tz))

    assert spring.local_date == "2026-03-08"
    assert spring.to_dict()["duration_hours"] == 23.0
    assert fall.local_date == "2026-11-01"
    assert fall.to_dict()["duration_hours"] == 25.0


def test_daily_facts_include_direct_requests_and_use_source_snowflake(monkeypatch, tmp_path):
    tz = ZoneInfo("America/New_York")
    window = _window(monkeypatch, "America/New_York", datetime(2026, 7, 18, 9, tzinfo=tz))
    inside = _item(datetime(2026, 7, 17, 12, tzinfo=tz))
    outside = _item(datetime(2026, 7, 16, 23, 59, tzinfo=tz))
    ledger = tmp_path / "work_ledger.json"
    _write_ledger(ledger, [inside, outside])

    facts, diagnostics = audit.load_daily_discord_facts(ledger_path=ledger, window=window)

    assert len(facts) == 1
    assert facts[0].attached_board is False
    assert diagnostics["accepted_requests"] == 1
    assert diagnostics["discord_entries"] == 2


def test_attached_board_evidence_is_read_only_for_explicit_board(monkeypatch, tmp_path):
    tz = ZoneInfo("UTC")
    window = _window(monkeypatch, "UTC", datetime(2026, 7, 18, 9, tzinfo=tz))
    ledger = tmp_path / "work_ledger.json"
    _write_ledger(
        ledger,
        [
            _item(datetime(2026, 7, 17, hour, tzinfo=tz), board="discord-feature-1")
            for hour in (12, 13)
        ],
    )
    seen = []

    def fake_summary(board):
        seen.append(board)
        return {
            "schema_version": 3,
            "board": board,
            "thread_state": "blocked",
            "task_counts": {"blocked": 1},
            "run_counts": {"by_status": {"failed": 1}, "by_outcome": {"crashed": 1}},
            "duration_seconds": 700,
            "root_goal": "secret raw request must not be projected",
            "final_response": {"text": "secret final"},
        }

    monkeypatch.setattr("hermes_cli.discord_worker_boards.read_board_run_summary", fake_summary)

    facts, diagnostics = audit.load_daily_discord_facts(ledger_path=ledger, window=window)
    candidates = audit.build_candidates(facts, total_requests=len(facts))

    assert seen == ["discord-feature-1"]
    assert diagnostics["attached_board_summaries"] == 2
    assert candidates[0].category == "coding_worker"
    assert candidates[0].attributable_s == 700
    assert candidates[0].error_count == 2
    assert "secret" not in json.dumps(candidates[0].to_dict())


def test_smooth_day_selects_no_candidate(monkeypatch, tmp_path):
    tz = ZoneInfo("UTC")
    now = datetime(2026, 7, 18, 9, tzinfo=tz)
    _window(monkeypatch, "UTC", now)
    home = tmp_path / "home"
    _write_ledger(home / "gateway" / "work_ledger.json", [_item(datetime(2026, 7, 17, 12, tzinfo=tz))])

    report = audit.build_audit_report(hermes_home=home, now=now)

    assert report["smooth"] is True
    assert report["selected_candidate"] is None
    assert report["selection"]["reason"] == "no_qualified_candidates"


def test_valid_day_without_discord_requests_is_insufficient_not_smooth(monkeypatch, tmp_path):
    tz = ZoneInfo("UTC")
    now = datetime(2026, 7, 18, 9, tzinfo=tz)
    _window(monkeypatch, "UTC", now)
    home = tmp_path / "home"
    _write_ledger(home / "gateway" / "work_ledger.json", [])

    report = audit.build_audit_report(hermes_home=home, now=now)

    assert report["source"]["ledger_status"] == "ok"
    assert report["source"]["accepted_requests"] == 0
    assert report["selected_candidate"] is None
    assert report["smooth"] is False


def test_repeated_terminal_failure_becomes_one_candidate(monkeypatch, tmp_path):
    tz = ZoneInfo("UTC")
    window = _window(monkeypatch, "UTC", datetime(2026, 7, 18, 9, tzinfo=tz))
    items = [
        _item(
            datetime(2026, 7, 17, hour, tzinfo=tz),
            status="blocked",
            gate_reason="self_declared_delivery_incomplete",
        )
        for hour in (10, 11)
    ]
    ledger = tmp_path / "work_ledger.json"
    _write_ledger(ledger, items)

    facts, _ = audit.load_daily_discord_facts(ledger_path=ledger, window=window)
    candidates = audit.build_candidates(facts, total_requests=len(facts))

    assert len(candidates) == 1
    assert candidates[0].idempotency_key == (
        "hermes-discord-execution:terminal:self_declared_delivery_incomplete"
    )
    assert candidates[0].affected_requests == 2


def test_exact_runtime_handoff_failure_is_severe_even_once(monkeypatch, tmp_path):
    tz = ZoneInfo("UTC")
    window = _window(monkeypatch, "UTC", datetime(2026, 7, 18, 9, tzinfo=tz))
    ledger = tmp_path / "work_ledger.json"
    _write_ledger(
        ledger,
        [
            _item(
                datetime(2026, 7, 17, 10, tzinfo=tz),
                status="blocked",
                gate_reason="runtime_handoff_unverified",
            )
        ],
    )

    facts, _ = audit.load_daily_discord_facts(ledger_path=ledger, window=window)
    candidates = audit.build_candidates(facts, total_requests=1)

    assert [candidate.subtype for candidate in candidates] == ["runtime_handoff_unverified"]


def test_repeated_tool_errors_and_runtime_dominance_are_aggregated(monkeypatch, tmp_path):
    tz = ZoneInfo("UTC")
    window = _window(monkeypatch, "UTC", datetime(2026, 7, 18, 9, tzinfo=tz))
    runtime = {
        "wall_s": 240,
        "model_s": 180,
        "tools_s": 40,
        "overhead_s": 20,
        "tool_calls": 4,
        "tool_errors": 2,
        "tool_blocked": 0,
        "top_tools": [
            {"name": "discord", "duration_s": 40, "count": 4, "errors": 2, "blocked": 0},
            {"name": "web_acme_payroll", "duration_s": 1, "count": 1, "errors": 9, "blocked": 0},
        ],
    }
    ledger = tmp_path / "work_ledger.json"
    _write_ledger(
        ledger,
        [_item(datetime(2026, 7, 17, hour, tzinfo=tz), runtime=runtime) for hour in (10, 11)],
    )

    facts, _ = audit.load_daily_discord_facts(ledger_path=ledger, window=window)
    candidates = audit.build_candidates(facts, total_requests=2)

    keys = {candidate.idempotency_key for candidate in candidates}
    assert "hermes-discord-execution:tool_failure:discord_errors" in keys
    assert "hermes-discord-execution:runtime:model_latency" in keys
    candidate_text = json.dumps([candidate.to_dict() for candidate in candidates])
    assert "acme" not in candidate_text
    assert "payroll" not in candidate_text


def test_provider_stall_requires_breadth_or_high_share(monkeypatch, tmp_path):
    tz = ZoneInfo("UTC")
    window = _window(monkeypatch, "UTC", datetime(2026, 7, 18, 9, tzinfo=tz))
    provider = {
        "failure_class": "invalid_response_backoff",
        "action": "degraded_partial",
        "delay_class": "very_long",
        "no_progress_elapsed_s": 901,
        "retry_count": 2,
    }
    ledger = tmp_path / "work_ledger.json"
    _write_ledger(
        ledger,
        [_item(datetime(2026, 7, 17, 10, tzinfo=tz), provider=provider)],
    )

    facts, _ = audit.load_daily_discord_facts(ledger_path=ledger, window=window)
    candidates = audit.build_candidates(facts, total_requests=1)

    assert candidates[0].category == "provider_stall"


def test_selection_is_deterministic_and_suppresses_active_duplicate(monkeypatch, tmp_path):
    tz = ZoneInfo("UTC")
    window = _window(monkeypatch, "UTC", datetime(2026, 7, 18, 9, tzinfo=tz))
    items = [
        _item(
            datetime(2026, 7, 17, hour, tzinfo=tz),
            status="blocked",
            gate_reason="runtime_handoff_unverified",
        )
        for hour in (10, 11)
    ]
    ledger = tmp_path / "work_ledger.json"
    _write_ledger(ledger, items)
    facts, _ = audit.load_daily_discord_facts(ledger_path=ledger, window=window)
    candidates = audit.build_candidates(facts, total_requests=2)

    selected, detail = audit.select_candidate(
        list(reversed(candidates)),
        active_idempotency_keys={candidates[0].idempotency_key},
        total_requests=2,
    )

    assert selected is None
    assert detail["reason"] == "all_candidates_active_duplicates"
    assert detail["active_duplicates_suppressed"] == 1


def test_active_duplicate_failure_day_is_not_labeled_smooth(monkeypatch, tmp_path):
    tz = ZoneInfo("UTC")
    now = datetime(2026, 7, 18, 9, tzinfo=tz)
    _window(monkeypatch, "UTC", now)
    home = tmp_path / "home"
    _write_ledger(
        home / "gateway" / "work_ledger.json",
        [
            _item(
                datetime(2026, 7, 17, 10, tzinfo=tz),
                status="blocked",
                gate_reason="runtime_handoff_unverified",
            )
        ],
    )
    monkeypatch.setattr(
        audit.proposal_storage,
        "list_active_idempotency_keys",
        lambda **_kwargs: {
            "hermes-discord-execution:terminal:runtime_handoff_unverified"
        },
    )

    report = audit.build_audit_report(hermes_home=home, now=now)

    assert report["selected_candidate"] is None
    assert report["selection"]["reason"] == "all_candidates_active_duplicates"
    assert report["smooth"] is False


def test_missing_and_malformed_ledgers_produce_bounded_empty_reports(monkeypatch, tmp_path):
    tz = ZoneInfo("UTC")
    now = datetime(2026, 7, 18, 9, tzinfo=tz)
    _window(monkeypatch, "UTC", now)
    missing_home = tmp_path / "missing-home"
    malformed_home = tmp_path / "malformed-home"
    malformed_path = malformed_home / "gateway" / "work_ledger.json"
    malformed_path.parent.mkdir(parents=True)
    malformed_path.write_text("not json", encoding="utf-8")

    missing = audit.build_audit_report(hermes_home=missing_home, now=now)
    malformed = audit.build_audit_report(hermes_home=malformed_home, now=now)

    assert missing["source"]["ledger_status"] == "missing"
    assert malformed["source"]["ledger_status"] == "malformed"
    assert missing["selected_candidate"] is None
    assert malformed["selected_candidate"] is None
    assert missing["smooth"] is False
    assert malformed["smooth"] is False
    assert len(json.dumps(missing).encode()) < 12_000


def test_report_never_leaks_raw_request_response_metadata_or_paths(monkeypatch, tmp_path):
    tz = ZoneInfo("UTC")
    now = datetime(2026, 7, 18, 9, tzinfo=tz)
    _window(monkeypatch, "UTC", now)
    home = tmp_path / "home"
    item = _item(
        datetime(2026, 7, 17, 10, tzinfo=tz),
        status="blocked",
        gate_reason="runtime_handoff_unverified",
    )
    item.update(
        {
            "text": "RAW_REQUEST_SECRET",
            "final_response": "RAW_RESPONSE_SECRET",
            "blocked_reason": "PRIVATE_BLOCKER_SECRET",
            "project_summary": {"path": "/private/local/path", "url": "https://secret.invalid"},
            "metadata": {"token": "credential-secret"},
        }
    )
    _write_ledger(home / "gateway" / "work_ledger.json", [item])

    report_text = json.dumps(audit.build_audit_report(hermes_home=home, now=now), sort_keys=True)

    for secret in (
        "RAW_REQUEST_SECRET",
        "RAW_RESPONSE_SECRET",
        "PRIVATE_BLOCKER_SECRET",
        "/private/local/path",
        "secret.invalid",
        "credential-secret",
    ):
        assert secret not in report_text
