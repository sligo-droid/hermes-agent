from __future__ import annotations

from pathlib import Path

from gateway.platforms.base import MessageEvent
from gateway.session import Platform, SessionSource, build_session_key
from gateway.work_ledger import GatewayWorkLedger
from tools import async_delegation as ad


def _attempt(
    tmp_path: Path,
    *,
    default_ledger: bool = False,
) -> tuple[GatewayWorkLedger, str]:
    ledger = (
        GatewayWorkLedger()
        if default_ledger
        else GatewayWorkLedger(tmp_path / "work-ledger.json")
    )
    source = SessionSource(
        platform=Platform.DISCORD,
        chat_id="111",
        chat_type="thread",
        thread_id="222",
        user_id="user-1",
        message_id="444",
    )
    session_key = build_session_key(source)
    item = ledger.accept_event(
        MessageEvent(text="review and implement", source=source, message_id="444"),
        session_key=session_key,
        freshness_seconds=60,
    )
    assert item is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_id="parent-session",
        session_key=session_key,
        run_generation=7,
        process_epoch="old-epoch",
    )
    assert ledger.begin_required_async_attempt(
        item["id"],
        generation=7,
        attempt_id="old-epoch:7",
        attempt_order=10,
    )
    return ledger, item["id"]


def _register(
    ledger: GatewayWorkLedger,
    work_id: str,
    delegation_id: str,
    *,
    kind: str,
) -> None:
    advisory = kind == "advisory"
    assert ledger.register_required_async_dispatch(
        work_id,
        delegation_id=delegation_id,
        generation=7,
        attempt_id="old-epoch:7",
        attempt_order=10,
        owner_pid=111,
        process_epoch="old-epoch",
        kind=kind,
        required=not advisory,
        evidence=(
            {
                "advisory_results": [
                    {"goal": "review the implementation", "status": "registered"}
                ]
            }
            if advisory
            else None
        ),
    )


def test_orphaned_advisory_terminalizes_and_seals_without_sticky_failure(tmp_path):
    ledger, work_id = _attempt(tmp_path)
    _register(ledger, work_id, "advisor-a", kind="advisory")

    state = ledger.mark_orphaned_advisory_async_dispatch_terminal(
        work_id,
        delegation_id="advisor-a",
        generation=7,
        attempt_id="old-epoch:7",
        attempt_order=10,
        expected_owner_pid=111,
        expected_process_epoch="old-epoch",
    )

    assert state is not None
    dispatch = state["dispatches"]["advisor-a"]
    assert dispatch["state"] == "terminal"
    assert dispatch["success"] is False
    assert dispatch["status"] == "producer_process_lost"
    assert dispatch["evidence"]["advisory_results"] == [
        {
            "goal": "review the implementation",
            "status": "error",
            "error": "advisory producer exited before recording a durable terminal outcome",
        }
    ]
    assert state["sealed"] is True
    assert state["ready_to_reconcile"] is True
    assert state["advisory_failed"] == 1
    assert state["failed"] is False
    assert state["sticky_failure"] is False
    assert ledger.get(work_id).get("completion_gate", {}).get("reason") != (
        "required_async_completion_failed"
    )


def test_restart_recovers_only_advisory_and_mixed_attempt_reconciles(tmp_path):
    ledger, work_id = _attempt(tmp_path)
    _register(ledger, work_id, "advisor-a", kind="advisory")
    _register(ledger, work_id, "worker-a", kind="coding_worker")

    recovered = ad.recover_abandoned_advisory_dispatches(
        ledger=ledger,
        current_process_epoch="new-epoch",
        current_owner_pid=111,
        process_alive=lambda _pid, _started_at: True,
    )

    assert recovered == 1
    state = ledger.required_async_completion_state(work_id)
    assert state is not None
    assert state["sealed"] is True
    assert state["dispatches"]["advisor-a"]["state"] == "terminal"
    assert state["dispatches"]["worker-a"]["state"] == "registered"
    assert state["required_pending_count"] == 1
    assert state["advisory_failed"] == 1
    assert state["failed"] is False
    assert state["ready_to_reconcile"] is False

    completed = ledger.record_required_async_completion(
        work_id,
        delegation_id="worker-a",
        success=True,
        generation=7,
        attempt_id="old-epoch:7",
        attempt_order=10,
        owner_pid=111,
        process_epoch="old-epoch",
        status="completed",
        summary="implementation complete",
    )
    assert completed is not None
    assert completed["required_succeeded"] == 1
    assert completed["advisory_failed"] == 1
    assert completed["failed"] is False
    assert completed["ready_to_reconcile"] is True


def test_generic_startup_recovery_invokes_advisory_ledger_scan(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_loaded_gateway_process_epoch", lambda: "new-epoch")
    from gateway import status as gateway_status

    monkeypatch.setattr(gateway_status, "_pid_exists", lambda _pid: False)
    ledger, work_id = _attempt(tmp_path, default_ledger=True)
    _register(ledger, work_id, "advisor-startup", kind="advisory")

    assert ad.recover_abandoned_delegations() == 1
    state = ledger.required_async_completion_state(work_id)
    assert state is not None
    assert state["dispatches"]["advisor-startup"]["state"] == "terminal"
    assert state["ready_to_reconcile"] is True
    assert state["failed"] is False


def test_unknown_advisory_owner_liveness_defers_recovery(tmp_path):
    ledger, work_id = _attempt(tmp_path)
    _register(ledger, work_id, "advisor-unknown", kind="advisory")

    recovered = ad.recover_abandoned_advisory_dispatches(
        ledger=ledger,
        current_process_epoch="new-epoch",
        current_owner_pid=222,
        process_alive=lambda _pid, _started_at: None,
    )

    assert recovered == 0
    state = ledger.required_async_completion_state(work_id)
    assert state["dispatches"]["advisor-unknown"]["state"] == "registered"
    assert state["sealed"] is False
