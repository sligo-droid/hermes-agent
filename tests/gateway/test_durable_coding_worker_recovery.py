from __future__ import annotations

import inspect
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from gateway.platforms.base import MessageEvent, SendResult
from gateway.run import GatewayRunner
from gateway.session import Platform, SessionSource, build_session_key
from gateway.work_ledger import GatewayWorkLedger
from tools import coding_worker_tool as cwt
from tools import async_delegation as ad


def _work_item(tmp_path: Path):
    ledger = GatewayWorkLedger(tmp_path / "work-ledger.json")
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
        MessageEvent(text="ship it", source=source, message_id="444"),
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
    worktree: Path,
    delegation_id: str,
    *,
    policy: str = "resume_or_relaunch",
    thread_id: str = "thread-old",
):
    probe = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        _init_repo(worktree)
    top_level, common_dir, base_sha = cwt._git_workspace_identity(str(worktree))
    assert ledger.register_required_async_dispatch(
        work_id,
        delegation_id=delegation_id,
        generation=7,
        attempt_id="old-epoch:7",
        attempt_order=10,
        owner_pid=111,
        process_epoch="old-epoch",
        scope_paths=["src"],
        recovery={
            "status": "running",
            "policy": policy,
            "side_effect_mode": (
                "workspace_only" if policy == "resume_or_relaunch" else "external"
            ),
            "task": f"implement {delegation_id}",
            "context": "continue from the current checkout",
            "worktree": str(worktree),
            "repository_root": str(worktree),
            "backend": "codex",
            "phase": "build",
            "thread_id": thread_id,
            "scope_paths": ["src"],
            "base_sha": base_sha,
            "git_top_level": top_level,
            "git_common_dir": common_dir,
            "owner_started_at": 100,
            "launch_generation": 1,
        },
    )
    assert ledger.mark_required_async_dispatch_running(
        work_id,
        delegation_id=delegation_id,
        generation=7,
        attempt_id="old-epoch:7",
        attempt_order=10,
        owner_pid=111,
        process_epoch="old-epoch",
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def test_restart_recovers_multiple_concurrent_children_with_stable_ids(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree_a = tmp_path / "worker-a"
    worktree_b = tmp_path / "worker-b"
    worktree_a.mkdir()
    worktree_b.mkdir()
    _register(ledger, work_id, worktree_a, "deleg-a")
    _register(ledger, work_id, worktree_b, "deleg-b")
    launched = []

    def launch_worker(**kwargs):
        launched.append(kwargs)
        return {"status": "dispatched", "delegation_id": kwargs["delegation_id"]}

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=222,
        max_async_children=4,
        process_alive=lambda _pid, _started: False,
        launch_worker=launch_worker,
    )

    assert report["enumerated"] == 2
    assert report["launched"] == 2
    assert {notice["kind"] for notice in report["notices"]} == {"resumed"}
    assert {notice["delegation_id"] for notice in report["notices"]} == {
        "deleg-a",
        "deleg-b",
    }
    assert {row["delegation_id"] for row in launched} == {"deleg-a", "deleg-b"}
    state = ledger.required_async_completion_state(work_id)
    assert state is not None
    assert set(state["dispatches"]) == {"deleg-a", "deleg-b"}
    for dispatch in state["dispatches"].values():
        assert dispatch["owner_pid"] == 222
        assert dispatch["process_epoch"] == "new-epoch"
        assert dispatch["recovery"]["launch_generation"] == 2


def test_one_child_launch_failure_does_not_skip_other_concurrent_children(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    for delegation_id in ("deleg-bad", "deleg-good"):
        worktree = tmp_path / delegation_id
        worktree.mkdir()
        _register(ledger, work_id, worktree, delegation_id)
    attempted = []

    def launch_worker(**kwargs):
        attempted.append(kwargs["delegation_id"])
        if kwargs["delegation_id"] == "deleg-bad":
            raise RuntimeError("simulated launch failure")
        return {"status": "dispatched", "delegation_id": kwargs["delegation_id"]}

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=222,
        max_async_children=4,
        process_alive=lambda _pid, _started: False,
        launch_worker=launch_worker,
    )

    assert attempted == ["deleg-bad", "deleg-good"]
    assert report["failed"] == 1
    assert report["launched"] == 1, report


def test_restart_defers_alive_owner_without_duplicate_launch(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-alive")
    launches = []

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=222,
        process_alive=lambda pid, _started: int(pid or 0) == 111,
        launch_worker=lambda **kwargs: launches.append(kwargs) or {"status": "dispatched"},
    )

    assert report["waiting_alive"] == 1
    assert launches == []
    dispatch = ledger.required_async_completion_state(work_id)["dispatches"]["deleg-alive"]
    assert dispatch["state"] == "running"
    assert dispatch["recovery"]["status"] == "waiting_for_owner"


def test_restart_defers_when_owner_liveness_is_unknown(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-unknown")
    launches = []

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=222,
        process_alive=lambda _pid, _started: None,
        launch_worker=lambda **kwargs: launches.append(kwargs) or {"status": "dispatched"},
    )

    assert report["waiting_alive"] == 1
    assert launches == []
    dispatch = ledger.required_async_completion_state(work_id)["dispatches"][
        "deleg-unknown"
    ]
    assert dispatch["recovery"]["status"] == "waiting_for_owner"
    assert "liveness is unknown" in dispatch["recovery"]["last_error"]


def test_dead_child_claim_preserves_codex_thread_for_resume(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-resume", thread_id="thread-resumable")
    observed = {}

    def launch_worker(**kwargs):
        observed.update(kwargs)
        return {"status": "dispatched", "delegation_id": kwargs["delegation_id"]}

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=222,
        process_alive=lambda _pid, _started: False,
        launch_worker=launch_worker,
    )

    assert report["launched"] == 1, report
    assert observed["delegation_id"] == "deleg-resume"
    assert observed["dispatch"]["recovery"]["thread_id"] == "thread-resumable"
    assert observed["dispatch"]["recovery"]["worktree"] == str(worktree)


def test_dead_child_actual_launch_resumes_thread_and_records_terminal_result(
    monkeypatch,
    tmp_path,
):
    ad._reset_for_tests()
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-real", thread_id="thread-resumable")
    captured = []

    class RecoverySession:
        def __init__(self, **kwargs):
            captured.append(kwargs)
            self._on_identity = kwargs.get("on_identity")

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return None

        def request_interrupt(self):
            return None

        def run_turn(self, **_kwargs):
            if callable(self._on_identity):
                self._on_identity(
                    {
                        "thread_id": "thread-resumable",
                        "worker_pid": 333,
                        "worker_started_at": 444,
                        "worker_scope_unit": "scope-1",
                        "recovery_mode": "thread_resume",
                    }
                )
            from agent.transports.codex_app_server_session import TurnResult

            return TurnResult(
                final_text="Recovered and verified the existing worktree.",
                thread_id="thread-resumable",
                turn_id="turn-recovered",
            )

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    (tmp_path / "codex-home").mkdir()
    monkeypatch.setattr(
        "agent.transports.codex_app_server_session.CodexAppServerSession",
        RecoverySession,
    )
    monkeypatch.setattr(
        "agent.opencode_worker.load_coding_worker_backend",
        lambda config=None: "codex",
    )
    monkeypatch.setattr(
        "agent.opencode_worker.looks_complex_or_risky",
        lambda *_args, **_kwargs: False,
    )
    monkeypatch.setattr(
        "hermes_cli.worker_autoreview.materialize_autoreview_helper",
        lambda _workdir: None,
    )
    monkeypatch.setattr(ad, "_required_async_ledger", lambda: ledger)
    monkeypatch.setattr("gateway.work_ledger.GatewayWorkLedger", lambda: ledger)

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=222,
        process_alive=lambda _pid, _started: False,
    )

    assert report["launched"] == 1, report
    deadline = time.time() + 5
    while not captured and time.time() < deadline:
        time.sleep(0.01)
    assert captured[0]["resume_thread_id"] == "thread-resumable"
    deadline = time.time() + 5
    state = ledger.required_async_completion_state(work_id)
    while state["dispatches"]["deleg-real"]["state"] != "terminal" and time.time() < deadline:
        time.sleep(0.01)
        state = ledger.required_async_completion_state(work_id)
    dispatch = state["dispatches"]["deleg-real"]
    assert dispatch["state"] == "terminal"
    assert dispatch["success"] is True
    assert dispatch["evidence"]["base_sha"] == dispatch["recovery"]["base_sha"]
    assert dispatch["recovery"]["thread_id"] == "thread-resumable"
    ad._reset_for_tests()


def test_parallel_recovery_without_exact_worktree_fails_closed(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-parallel")
    data = ledger._read()
    recovery = data["items"][work_id]["required_async_completions"]["dispatches"][
        "deleg-parallel"
    ]["recovery"]
    recovery.pop("worktree", None)
    recovery["requested_cwd"] = str(tmp_path)
    recovery["parallel_group"] = {
        "group_id": "parallel-group",
        "base_cwd": str(tmp_path),
        "base_sha": "a" * 40,
    }
    ledger._write(data)
    launches = []

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=222,
        process_alive=lambda _pid, _started: False,
        launch_worker=lambda **kwargs: launches.append(kwargs) or {"status": "dispatched"},
    )

    assert launches == []
    assert report["manual_fallback"] == 1
    dispatch = ledger.required_async_completion_state(work_id)["dispatches"][
        "deleg-parallel"
    ]
    assert dispatch["state"] == "outcome_unknown"
    assert "no exact durable isolated worktree" in dispatch["error"]


def test_external_authority_contradiction_normalizes_fail_closed(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-contradictory")
    data = ledger._read()
    recovery = data["items"][work_id]["required_async_completions"]["dispatches"][
        "deleg-contradictory"
    ]["recovery"]
    recovery["policy"] = "resume_or_relaunch"
    recovery["side_effect_mode"] = "external"
    recovery["allow_git_pr_lifecycle"] = True
    ledger._write(data)

    state = ledger.required_async_completion_state(work_id)

    assert state["malformed"] is True
    assert state["dispatches"]["deleg-contradictory"]["state"] == "outcome_unknown"
    assert state["dispatches"]["deleg-contradictory"]["recovery"]["policy"] == "manual"


def test_non_boolean_external_authority_is_malformed(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-bad-authority")
    data = ledger._read()
    data["items"][work_id]["required_async_completions"]["dispatches"][
        "deleg-bad-authority"
    ]["recovery"]["allow_git_pr_lifecycle"] = "true"
    ledger._write(data)

    state = ledger.required_async_completion_state(work_id)

    assert state["malformed"] is True
    assert state["dispatches"]["deleg-bad-authority"]["state"] == "outcome_unknown"


def test_durable_recovery_spec_fails_closed_without_redaction(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "agent.redact", None)

    with pytest.raises(RuntimeError, match="requires secret redaction"):
        cwt._durable_worker_recovery_spec(
            task="secret task",
            context_pack={"context": "token", "relevant_files": []},
            call_kwargs={"cwd": str(tmp_path), "parent_agent": None},
            parallel_group=None,
        )


def test_recorded_systemd_scope_keeps_worker_alive_when_pid_is_dead(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-scoped")
    data = ledger._read()
    recovery = data["items"][work_id]["required_async_completions"]["dispatches"][
        "deleg-scoped"
    ]["recovery"]
    recovery["worker_scope_unit"] = "hermes-gateway-child-coding-worker.scope"
    ledger._write(data)
    launches = []

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=222,
        process_alive=lambda _pid, _started: False,
        scope_alive=lambda _unit: True,
        launch_worker=lambda **kwargs: launches.append(kwargs) or {"status": "dispatched"},
    )

    assert launches == []
    assert report["waiting_alive"] == 1


def test_same_pid_with_new_epoch_does_not_wait_on_reused_pid(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-reused-pid")
    launches = []

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=111,
        process_alive=lambda pid, _started: int(pid or 0) == 111,
        launch_worker=lambda **kwargs: launches.append(kwargs) or {"status": "dispatched"},
    )

    assert report["launched"] == 1
    assert len(launches) == 1


def test_missing_backend_identity_fails_to_manual_recovery(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-no-backend")
    data = ledger._read()
    data["items"][work_id]["required_async_completions"]["dispatches"][
        "deleg-no-backend"
    ]["recovery"].pop("backend", None)
    ledger._write(data)
    launches = []

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=222,
        process_alive=lambda _pid, _started: False,
        launch_worker=lambda **kwargs: launches.append(kwargs) or {"status": "dispatched"},
    )

    assert launches == []
    assert report["manual_fallback"] == 1


def test_recreated_non_git_worktree_fails_closed(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-wrong-worktree")
    shutil.rmtree(worktree / ".git")
    launches = []

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=222,
        process_alive=lambda _pid, _started: False,
        launch_worker=lambda **kwargs: launches.append(kwargs) or {"status": "dispatched"},
    )

    assert launches == []
    assert report["manual_fallback"] == 1
    assert "exact durable Git worktree" in report["notices"][0]["message"]


def test_moved_worktree_head_fails_closed(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-moved-head")
    (worktree / "after.txt").write_text("new commit\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree), "add", "after.txt"], check=True)
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-qm", "move head"],
        check=True,
    )
    launches = []

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=222,
        process_alive=lambda _pid, _started: False,
        launch_worker=lambda **kwargs: launches.append(kwargs) or {"status": "dispatched"},
    )

    assert launches == []
    assert report["manual_fallback"] == 1


def test_same_recovery_launch_id_cannot_start_duplicate_child():
    ad._reset_for_tests()
    gate = threading.Event()

    def runner():
        assert gate.wait(5)
        return {
            "status": "completed",
            "result": {"success": True, "status": "completed", "summary": "done"},
            "_async_coding_worker": {
                "task": "task",
                "context_pack": {},
                "worker_cwd": "/tmp",
                "scope_paths": ["src"],
                "worker_run": {},
            },
        }

    kwargs = {
        "delegation_id": "deleg-stable",
        "goal": "task",
        "context": "context",
        "session_key": "agent:main:discord:thread:111:222",
        "runner": runner,
        "interrupt_fn": None,
        "max_async_children": 2,
        "origin_work_item_id": "work-1",
        "origin_run_generation": 7,
        "origin_attempt_id": "epoch:7",
        "origin_attempt_order": 10,
        "origin_owner_pid": 222,
        "origin_process_epoch": "epoch",
        "origin_scope_paths": ["src"],
        "recovery": {"launch_id": "epoch:deleg-stable:2"},
    }
    first = ad.recover_async_coding_delegation(**kwargs)
    second = ad.recover_async_coding_delegation(**kwargs)

    assert first["status"] == "dispatched"
    assert second == {"status": "already_running", "delegation_id": "deleg-stable"}
    assert ad.active_count(kind="coding_worker") == 1
    gate.set()
    deadline = time.time() + 5
    while ad.active_count(kind="coding_worker") and time.time() < deadline:
        time.sleep(0.01)
    ad._reset_for_tests()


def test_unsafe_external_side_effects_fail_to_structured_manual_fallback(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(
        ledger,
        work_id,
        worktree,
        "deleg-external",
        policy="manual",
    )

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=222,
        process_alive=lambda _pid, _started: False,
        launch_worker=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe child must not relaunch")
        ),
    )

    assert report["manual_fallback"] == 1
    state = ledger.required_async_completion_state(work_id)
    dispatch = state["dispatches"]["deleg-external"]
    assert dispatch["state"] == "outcome_unknown"
    assert dispatch["recovery"]["status"] == "manual_fallback"
    assert "external git/PR side effects" in dispatch["error"]
    assert state["sealed"] is True
    assert state["ready_to_reconcile"] is True


def test_completed_children_are_sealed_for_deterministic_reconciliation(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-complete")
    assert ledger.record_required_async_completion(
        work_id,
        delegation_id="deleg-complete",
        success=True,
        generation=7,
        attempt_id="old-epoch:7",
        attempt_order=10,
        status="completed",
        summary="done",
    )

    report = cwt.recover_durable_coding_workers(
        ledger=ledger,
        process_epoch="new-epoch",
        owner_pid=222,
        process_alive=lambda _pid, _started: False,
        launch_worker=lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal child must not relaunch")
        ),
    )

    assert report["completed"] == 1
    state = ledger.required_async_completion_state(work_id)
    assert state["sealed"] is True
    assert state["ready_to_reconcile"] is True


@pytest.mark.asyncio
async def test_noncritical_restart_drain_counts_detached_coding_workers():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._active_cron_job_count = lambda: 0
    runner._active_api_run_count = lambda: 0
    runner._active_coding_worker_count = lambda: 1
    runner._update_runtime_status = lambda *_args, **_kwargs: None

    _snapshot, timed_out = await runner._drain_active_agents(timeout=0)

    assert timed_out is True


@pytest.mark.asyncio
async def test_uncertain_recovery_sends_interim_discord_commentary():
    sent = []

    class Adapter:
        async def send(self, chat_id, content, metadata=None):
            sent.append((chat_id, content, metadata))
            return SendResult(success=True, message_id="notice-1")

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._adapter_for_source = lambda _source: Adapter()
    notice = {
        "kind": "waiting",
        "delegation_id": "deleg-wait",
        "source": {
            "platform": "discord",
            "chat_id": "111",
            "chat_type": "thread",
            "thread_id": "222",
            "user_id": "user-1",
        },
        "message": "Hermes is waiting to avoid duplicate side effects.",
    }

    count = await runner._send_durable_coding_worker_recovery_notices(
        [notice, notice]
    )
    repeated = await runner._send_durable_coding_worker_recovery_notices([notice])

    assert count == 1
    assert repeated == 0
    assert sent[0][0] == "111"
    assert sent[0][2] == {"thread_id": "222"}
    assert "Recovering coding work after restart" in sent[0][1]
    assert "avoid duplicate side effects" in sent[0][1]


@pytest.mark.asyncio
async def test_periodic_reconciler_sends_late_recovery_notices():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    sent = []

    def recover():
        runner._running = False
        return {"notices": [{"delegation_id": "deleg-late"}]}

    runner._recover_durable_coding_workers = recover

    async def send(notices):
        sent.extend(notices)
        return len(notices)

    runner._send_durable_coding_worker_recovery_notices = send

    await runner._durable_coding_worker_reconciler(interval=0.01)

    assert sent == [{"delegation_id": "deleg-late"}]


def test_stale_owner_terminal_result_cannot_overwrite_recovery_claim(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    _register(ledger, work_id, worktree, "deleg-fenced")
    claimed = ledger.claim_required_async_dispatch_recovery(
        work_id,
        delegation_id="deleg-fenced",
        generation=7,
        attempt_id="old-epoch:7",
        attempt_order=10,
        expected_owner_pid=111,
        expected_process_epoch="old-epoch",
        owner_pid=222,
        process_epoch="new-epoch",
        launch_id="new-epoch:deleg-fenced:2",
    )
    assert claimed is not None

    stale = ledger.record_required_async_completion(
        work_id,
        delegation_id="deleg-fenced",
        success=True,
        generation=7,
        attempt_id="old-epoch:7",
        attempt_order=10,
        owner_pid=111,
        process_epoch="old-epoch",
        status="completed",
        summary="stale result",
    )

    assert stale is None
    dispatch = ledger.required_async_completion_state(work_id)["dispatches"][
        "deleg-fenced"
    ]
    assert dispatch["owner_pid"] == 222
    assert dispatch["process_epoch"] == "new-epoch"
    assert dispatch["state"] == "registered"


def test_recovery_claim_exactly_matches_empty_prior_owner(tmp_path):
    ledger, work_id = _work_item(tmp_path)
    worktree = tmp_path / "worker"
    worktree.mkdir()
    assert ledger.register_required_async_dispatch(
        work_id,
        delegation_id="deleg-unowned",
        generation=7,
        attempt_id="old-epoch:7",
        attempt_order=10,
        owner_pid=0,
        process_epoch="",
        scope_paths=["src"],
        recovery={
            "policy": "resume_or_relaunch",
            "side_effect_mode": "workspace_only",
            "task": "recover exact empty owner",
            "worktree": str(worktree),
            "scope_paths": ["src"],
        },
    )
    data = ledger._read()
    dispatch = data["items"][work_id]["required_async_completions"]["dispatches"][
        "deleg-unowned"
    ]
    dispatch["owner_pid"] = 0
    dispatch["process_epoch"] = ""
    ledger._write(data)

    rejected = ledger.claim_required_async_dispatch_recovery(
        work_id,
        delegation_id="deleg-unowned",
        generation=7,
        attempt_id="old-epoch:7",
        attempt_order=10,
        expected_owner_pid=999,
        expected_process_epoch="",
        owner_pid=222,
        process_epoch="new-epoch",
        launch_id="new-epoch:deleg-unowned:1",
    )
    claimed = ledger.claim_required_async_dispatch_recovery(
        work_id,
        delegation_id="deleg-unowned",
        generation=7,
        attempt_id="old-epoch:7",
        attempt_order=10,
        expected_owner_pid=0,
        expected_process_epoch="",
        owner_pid=222,
        process_epoch="new-epoch",
        launch_id="new-epoch:deleg-unowned:1",
    )

    assert rejected is None
    assert claimed is not None


def test_startup_recovers_children_before_parent_resume_replay():
    source = inspect.getsource(GatewayRunner.start)
    assert source.index("self._recover_durable_coding_workers") < source.index(
        "self._schedule_resume_pending_sessions"
    )


def test_normal_repo_closeout_policy_does_not_require_gateway_restart():
    root = Path(__file__).resolve().parents[2]
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    decision = (
        root / "docs/decisions/0003-durable-discord-coding-worker-recovery.md"
    ).read_text(encoding="utf-8")

    assert "normal Hermes PR lifecycle ends" in agents
    assert "Do not restart the gateway merely because Hermes code was merged" in agents
    assert "routine Hermes PR ends after verified merge plus clean canonical fast-forward" in decision
    assert "Gateway restart is exceptional" in decision
