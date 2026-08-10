from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent.visual_qa import (
    normalize_visual_requirement,
    visual_requirement_id,
    visual_requirement_uses_orchestrator_contract,
)
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import Platform, SessionSource, build_session_key
from gateway.trusted_closeout_watcher import (
    TrustedCloseoutWatcher,
    closeout_dirty_marker_path,
    mark_closeout_dirty,
)
from gateway.work_ledger import GatewayWorkLedger
from hermes_cli.closeout_execution import run_closeout_command


def _event(message_id="m1", text="ship the change"):
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


def _visual_receipt(requirement, *, order=3, status="passed"):
    normalized = normalize_visual_requirement(requirement)
    coverage_ids = [item["id"] for item in normalized["assertions"]]
    receipt = {
        "requirement_id": visual_requirement_id(normalized),
        "contract_id": "vac_" + ("a" * 24),
        "assertion_ids": coverage_ids,
        "status": status,
        "attempts": 1,
        "vision_calls": 0,
        "duration_ms": 25,
        "diagnostic_codes": ["no_horizontal_overflow_satisfied"],
        "order": order,
    }
    if visual_requirement_uses_orchestrator_contract(normalized):
        receipt["coverage_ids"] = coverage_ids
        receipt["assertion_ids"] = ["vassert_" + ("c" * 24)]
        receipt["diagnostic_codes"] = ["appearance_satisfied"]
    return receipt


def _install_blocking_git(monkeypatch, tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pid_path = tmp_path / "watcher-git-tree.pids"
    script = bin_dir / "git"
    script.write_text(
        """#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time

child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
def terminate(_signum, _frame):
    try:
        child.wait(timeout=1)
    except Exception:
        pass
    raise SystemExit(143)
signal.signal(signal.SIGTERM, terminate)
with open(os.environ["HERMES_TEST_WATCHER_PID_FILE"], "w", encoding="utf-8") as handle:
    handle.write(f"{os.getpid()} {child.pid}\\n")
    handle.flush()
time.sleep(60)
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("HERMES_TEST_WATCHER_PID_FILE", str(pid_path))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return pid_path


async def _wait_for_pids(pid_path: Path) -> tuple[int, int]:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            values = [int(value) for value in pid_path.read_text().split()]
        except (FileNotFoundError, ValueError):
            values = []
        if len(values) == 2:
            return values[0], values[1]
        await asyncio.sleep(0.01)
    raise AssertionError("fake Git process tree did not start")


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _pending_item(
    ledger: GatewayWorkLedger,
    *,
    mode="enforce",
    message_id="m1",
    freshness_seconds=3600,
):
    event = _event(message_id=message_id)
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=freshness_seconds,
    )
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        mode=mode,
    )
    assert attached is not None
    state = ledger.activate_closeout(
        item["id"],
        attached,
        expected_revision=attached["revision"],
    )
    assert state is not None
    return item, state


def _blocked_item(
    ledger: GatewayWorkLedger,
    *,
    message_id="m1",
    freshness_seconds=3600,
):
    item, state = _pending_item(
        ledger,
        message_id=message_id,
        freshness_seconds=freshness_seconds,
    )
    leased = ledger.lease_closeout(
        item["id"],
        owner="watcher-1",
        lease_seconds=30,
        expected_revision=state["revision"],
    )
    assert leased is not None
    blocked_state = dict(leased["closeout"])
    blocked_state["status"] = "repair_required"
    blocked = ledger.finalize_blocked_closeout(
        item["id"],
        owner="watcher-1",
        expected_revision=leased["closeout"]["revision"],
        expected_generation=leased["closeout"]["lease_generation"],
        closeout_state=blocked_state,
        final_response="Trusted closeout blocked: repair required.",
        reason="trusted_closeout_repair_required",
    )
    assert blocked is not None
    return blocked


def _pending_visual_item(ledger: GatewayWorkLedger):
    event = _event(
        message_id="visual-pending",
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
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        repository="acme/example",
        branch="feature/visual",
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
    state = ledger.activate_closeout(
        item["id"],
        attached,
        expected_revision=attached["revision"],
    )
    assert state is not None
    return item, state, head_sha


def test_dirty_marker_is_bounded_identifier_only(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    for index in range(75):
        mark_closeout_dirty(f"work-{index}")

    payload = json.loads(closeout_dirty_marker_path().read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert len(payload["work_item_ids"]) == 50
    assert payload["work_item_ids"][0] == "work-25"
    assert set(payload) == {"version", "dirty_at", "work_item_ids"}


@pytest.mark.asyncio
async def test_watcher_leases_reconciles_and_releases_revision(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, state = _pending_item(ledger)
    seen = []

    def reconcile(value, **kwargs):
        seen.append((value["revision"], kwargs["poll_seconds"]))
        updated = dict(value)
        updated["status"] = "waiting_for_ci"
        updated["next_due_at"] = 130.0
        return SimpleNamespace(state=updated)

    watcher = TrustedCloseoutWatcher(
        ledger,
        config={"poll_seconds": 30, "lease_seconds": 20, "max_concurrency": 2},
        reconcile=reconcile,
        owner="watcher-1",
    )

    assert await watcher.run_once() == 1

    stored = ledger.get(item["id"])
    assert seen == [(state["revision"] + 1, 30.0)]
    assert stored["closeout"]["status"] == "waiting_for_ci"
    assert stored["closeout"]["next_due_at"] == 130.0
    assert stored["closeout"]["lease"] == {"owner": "", "until": None}
    assert stored["closeout"]["revision"] == state["revision"] + 2


@pytest.mark.asyncio
async def test_visual_receipt_during_watcher_lease_merges_without_losing_revision(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, state, head_sha = _pending_visual_item(ledger)
    reconcile_started = threading.Event()
    allow_reconcile = threading.Event()

    def reconcile(value, **_kwargs):
        reconcile_started.set()
        assert allow_reconcile.wait(timeout=2)
        updated = dict(value)
        updated["status"] = "waiting_for_ci"
        updated["next_due_at"] = 130.0
        updated["pr"] = {
            **updated["pr"],
            "url": "https://github.com/acme/example/pull/11",
        }
        return SimpleNamespace(state=updated)

    watcher = TrustedCloseoutWatcher(
        ledger,
        config={"poll_seconds": 30, "lease_seconds": 20},
        reconcile=reconcile,
        owner="watcher-visual",
    )
    task = asyncio.create_task(watcher.run_once())
    assert await asyncio.to_thread(reconcile_started.wait, 1)

    queued = await asyncio.to_thread(
        ledger.apply_closeout_visual_completion,
        item["id"],
        expected_head_sha=head_sha,
        receipts=[_visual_receipt(item["visual_qa_requirement"], order=4)],
        min_receipt_order=4,
    )
    assert queued is not None
    during = ledger.get(item["id"])
    assert during["closeout"]["revision"] == state["revision"] + 1
    assert during["closeout_visual_completion"] == {
        "status": "passed",
        "head_sha": head_sha,
    }

    allow_reconcile.set()
    assert await task == 1

    stored = ledger.get(item["id"])
    assert stored["closeout"]["revision"] == state["revision"] + 2
    assert stored["closeout"]["status"] == "waiting_for_ci"
    assert stored["closeout"]["pr"]["url"].endswith("/11")
    assert stored["closeout"]["visual_qa"] == {
        "status": "passed",
        "head_sha": head_sha,
    }
    assert "closeout_visual_completion" not in stored


@pytest.mark.asyncio
async def test_long_reconciliation_renews_single_owner_lease(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=time.time)
    _item, _state = _pending_item(ledger)
    started = threading.Event()
    reconciler_calls = []

    def reconcile(value, **_kwargs):
        reconciler_calls.append(threading.get_ident())
        started.set()
        time.sleep(1.5)
        updated = dict(value)
        updated["status"] = "waiting_for_ci"
        updated["next_due_at"] = time.time() + 30
        return SimpleNamespace(state=updated)

    first = TrustedCloseoutWatcher(
        ledger,
        config={"lease_seconds": 1, "poll_seconds": 30},
        reconcile=reconcile,
        owner="watcher-1",
    )
    second = TrustedCloseoutWatcher(
        ledger,
        config={"lease_seconds": 1, "poll_seconds": 30},
        reconcile=reconcile,
        owner="watcher-2",
    )

    first_task = asyncio.create_task(first.run_once())
    assert await asyncio.to_thread(started.wait, 1.0)
    await asyncio.sleep(1.1)
    assert await second.run_once() == 0
    assert await first_task == 1
    assert len(reconciler_calls) == 1


@pytest.mark.asyncio
async def test_lost_closeout_renewal_stops_later_cooperative_mutation(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=time.time)
    item, state = _pending_item(ledger)
    mutations = []

    def reconcile(value, *, mutation_allowed, **_kwargs):
        time.sleep(0.45)
        if mutation_allowed():
            mutations.append("external-mutation")
        updated = dict(value)
        updated["status"] = "waiting_for_ci"
        updated["next_due_at"] = time.time() + 30
        return SimpleNamespace(state=updated)

    monkeypatch.setattr(ledger, "renew_closeout_lease", lambda *_args, **_kwargs: False)
    watcher = TrustedCloseoutWatcher(
        ledger,
        config={"lease_seconds": 1, "poll_seconds": 30},
        reconcile=reconcile,
        owner="watcher-lost",
    )

    assert await watcher.run_once() == 0
    assert mutations == []
    stored = ledger.get(item["id"])
    assert stored["closeout"]["revision"] == state["revision"] + 1
    assert stored["closeout"]["status"] == state["status"]


@pytest.mark.asyncio
async def test_renewal_failure_kills_active_mutation_tree_before_watcher_returns(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    pid_path = _install_blocking_git(monkeypatch, tmp_path)
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=time.time)
    item, state = _pending_item(ledger)

    def reconcile(value, *, control, **_kwargs):
        try:
            run_closeout_command(
                ["git", "fetch", "origin", "main"],
                cwd=tmp_path,
                timeout=10,
                control=control,
            )
        except Exception:
            pass
        updated = dict(value)
        updated["status"] = "completed"
        return SimpleNamespace(state=updated)

    monkeypatch.setattr(ledger, "renew_closeout_lease", lambda *_args, **_kwargs: False)
    watcher = TrustedCloseoutWatcher(
        ledger,
        config={"lease_seconds": 1, "poll_seconds": 30},
        reconcile=reconcile,
        owner="watcher-renewal-failed",
    )

    run_task = asyncio.create_task(watcher.run_once())
    parent_pid, child_pid = await _wait_for_pids(pid_path)
    assert await run_task == 0

    assert not _pid_exists(parent_pid)
    assert not _pid_exists(child_pid)
    stored = ledger.get(item["id"])
    assert stored["closeout"]["revision"] == state["revision"] + 1
    assert stored["closeout"]["status"] == state["status"]


@pytest.mark.asyncio
async def test_watcher_cancellation_kills_mutation_tree_before_completion(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    pid_path = _install_blocking_git(monkeypatch, tmp_path)
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=time.time)
    item, state = _pending_item(ledger)

    def reconcile(value, *, control, **_kwargs):
        run_closeout_command(
            ["git", "fetch", "origin", "main"],
            cwd=tmp_path,
            timeout=10,
            control=control,
        )
        return SimpleNamespace(state=value)

    watcher = TrustedCloseoutWatcher(
        ledger,
        config={"lease_seconds": 10, "poll_seconds": 30},
        reconcile=reconcile,
        owner="watcher-cancelled",
    )
    run_task = asyncio.create_task(watcher.run_once())
    parent_pid, child_pid = await _wait_for_pids(pid_path)
    run_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert not _pid_exists(parent_pid)
    assert not _pid_exists(child_pid)
    stored = ledger.get(item["id"])
    assert stored["closeout"]["revision"] == state["revision"] + 1
    assert stored["closeout"]["status"] == state["status"]


@pytest.mark.asyncio
async def test_cancelled_remote_mutation_uncertainty_survives_for_next_lease(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    now = 100.0
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: now)
    item, state = _pending_item(ledger)
    started = threading.Event()

    def reconcile(value, *, control, **_kwargs):
        started.set()
        while control.mutation_allowed():
            time.sleep(0.01)
        updated = dict(value)
        updated["mutation_uncertainty"] = {
            "status": "uncertain",
            "operation": "github_pr_create",
            "at": now,
            "head_sha": "a" * 40,
        }
        return SimpleNamespace(state=updated)

    watcher = TrustedCloseoutWatcher(
        ledger,
        config={"lease_seconds": 1, "poll_seconds": 30},
        reconcile=reconcile,
        owner="watcher-remote-cancelled",
    )
    run_task = asyncio.create_task(watcher.run_once())
    assert await asyncio.to_thread(started.wait, 1)
    run_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await run_task

    stored = ledger.get(item["id"])
    assert stored["closeout"]["revision"] == state["revision"] + 1
    assert stored["closeout"]["status"] == state["status"]
    assert stored["closeout_mutation_uncertainty"] == {
        "status": "uncertain",
        "operation": "github_pr_create",
        "at": 100.0,
        "head_sha": "a" * 40,
    }

    now = 102.0
    leased = ledger.lease_closeout(
        item["id"],
        owner="watcher-next",
        lease_seconds=30,
        expected_revision=state["revision"] + 1,
    )
    assert leased["closeout"]["mutation_uncertainty"] == stored[
        "closeout_mutation_uncertainty"
    ]
    assert "closeout_mutation_uncertainty" not in ledger.get(item["id"])


@pytest.mark.asyncio
async def test_immediate_transition_rearms_same_process_wakeup(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    _item, _state = _pending_item(ledger)

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "waiting_for_preview"
        updated["next_due_at"] = 100.0
        return SimpleNamespace(state=updated, wake_immediately=True)

    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
    )
    watcher.wakeup.clear()

    assert await watcher.run_once() == 1
    assert watcher.wakeup.is_set()


@pytest.mark.asyncio
async def test_terminal_closeout_completes_delivered_summary_without_model_replay(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, _state = _pending_item(ledger)
    ledger.mark_agent_done(item["id"], final_response="Model work already completed.")
    ledger.mark_response_delivered(item["id"], result_message_id="result-1")
    ledger.mark_summary_updated(item["id"])
    model_replays = []

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "completed"
        updated["next_due_at"] = None
        return SimpleNamespace(state=updated)

    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
    )

    assert await watcher.run_once() == 1
    assert model_replays == []
    stored = ledger.get(item["id"])
    assert stored["status"] == "completed"
    assert stored["closeout"]["status"] == "completed"


@pytest.mark.asyncio
async def test_terminal_closeout_synthesizes_result_for_interrupted_agent_without_replay(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, _state = _pending_item(ledger)
    ledger.claim(item["id"])
    ledger.mark_agent_running(item["id"], session_id="session-1")
    callbacks = []

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "completed"
        updated["next_due_at"] = None
        return SimpleNamespace(state=updated)

    async def on_terminal(stored):
        callbacks.append(stored)

    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
        is_agent_active=lambda _stored: False,
        on_terminal=on_terminal,
    )

    assert await watcher.run_once() == 1
    stored = ledger.get(item["id"])
    assert stored["status"] == "agent_done"
    assert stored["final_response"].startswith("PR preview QA completed")
    assert callbacks[0]["id"] == item["id"]


@pytest.mark.asyncio
async def test_preview_ready_callback_runs_before_terminal_closeout(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, _state = _pending_item(ledger)
    callbacks = []
    head_sha = "a" * 40

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "waiting_for_gates"
        updated["next_due_at"] = 130.0
        updated["policy"] = {**updated["policy"], "require_preview": True}
        updated["pr"] = {
            **updated["pr"],
            "url": "https://github.com/acme/example/pull/7",
            "head_sha": head_sha,
        }
        updated["preview"] = {
            "provider": "vercel",
            "status": "ready",
            "observed_sha": head_sha,
            "url": "https://example-git-thread.vercel.app",
            "deployment_id": "42",
        }
        return SimpleNamespace(state=updated)

    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
        on_preview=lambda stored: callbacks.append(stored),
    )

    assert await watcher.run_once() == 1
    assert callbacks[0]["preview_delivery"]["status"] == "pending"
    assert callbacks[0]["preview_delivery"]["preview_url"] == (
        "https://example-git-thread.vercel.app"
    )
    assert ledger.get(item["id"])["closeout"]["status"] == "waiting_for_gates"


@pytest.mark.asyncio
async def test_ready_preview_is_notified_before_blocked_terminal_callback(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, _state = _pending_item(ledger)
    callbacks = []
    head_sha = "b" * 40

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "repair_required"
        updated["next_due_at"] = None
        updated["policy"] = {**updated["policy"], "require_preview": True}
        updated["pr"] = {
            **updated["pr"],
            "url": "https://github.com/acme/example/pull/8",
            "head_sha": head_sha,
        }
        updated["preview"] = {
            "provider": "vercel",
            "status": "ready",
            "observed_sha": head_sha,
            "url": "https://example-git-blocked.vercel.app",
            "deployment_id": "43",
        }
        return SimpleNamespace(state=updated)

    def on_preview(stored):
        callbacks.append("preview")
        owner = "blocked-preview"
        assert ledger.claim_preview_delivery(stored["id"], owner=owner) is not None
        assert ledger.begin_preview_send_attempt(stored["id"], owner=owner) is True
        assert ledger.complete_preview_delivery(
            stored["id"],
            owner=owner,
            result_message_id="blocked-preview-message",
        ) is True

    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
        on_preview=on_preview,
        on_terminal=lambda _stored: callbacks.append("terminal"),
    )

    assert await watcher.run_once() == 1
    assert callbacks == ["preview", "terminal"]
    assert ledger.get(item["id"])["status"] == "blocked"


@pytest.mark.asyncio
async def test_ready_preview_is_notified_before_success_terminal_callback(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, _state = _pending_item(ledger)
    callbacks = []
    head_sha = "c" * 40

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "pr_published"
        updated["next_due_at"] = None
        updated["policy"] = {
            **updated["policy"],
            "require_preview": True,
            "require_local_verification": False,
            "require_review": False,
            "require_visual_qa": False,
        }
        updated["pr"] = {
            **updated["pr"],
            "url": "https://github.com/acme/example/pull/9",
            "state": "OPEN",
            "head_sha": head_sha,
        }
        updated["ci"] = {
            **updated["ci"],
            "head_sha": head_sha,
            "status": "passed",
        }
        updated["preview"] = {
            "provider": "vercel",
            "status": "ready",
            "observed_sha": head_sha,
            "url": "https://example-git-success.vercel.app",
            "deployment_id": "44",
        }
        return SimpleNamespace(state=updated)

    def on_preview(stored):
        callbacks.append("preview")
        owner = "success-preview"
        assert ledger.claim_preview_delivery(stored["id"], owner=owner) is not None
        assert ledger.begin_preview_send_attempt(stored["id"], owner=owner) is True
        assert ledger.complete_preview_delivery(
            stored["id"],
            owner=owner,
            result_message_id="success-preview-message",
        ) is True

    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
        on_preview=on_preview,
        on_terminal=lambda _stored: callbacks.append("terminal"),
    )

    assert await watcher.run_once() == 1
    assert callbacks == ["preview", "terminal"]
    assert ledger.get(item["id"])["status"] == "agent_done"


@pytest.mark.asyncio
async def test_terminal_callback_waits_for_preview_retry_completion(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    now = [100.0]
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: now[0])
    item, _state = _pending_item(ledger)
    callbacks = []
    head_sha = "9" * 40

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "pr_published"
        updated["next_due_at"] = None
        updated["policy"] = {
            **updated["policy"],
            "require_preview": True,
            "require_local_verification": False,
            "require_review": False,
            "require_visual_qa": False,
        }
        updated["pr"] = {
            **updated["pr"],
            "url": "https://github.com/acme/example/pull/12",
            "state": "OPEN",
            "head_sha": head_sha,
        }
        updated["ci"] = {**updated["ci"], "head_sha": head_sha, "status": "passed"}
        updated["preview"] = {
            "provider": "vercel",
            "status": "ready",
            "observed_sha": head_sha,
            "url": "https://example-git-ordered.vercel.app",
            "deployment_id": "47",
        }
        return SimpleNamespace(state=updated)

    attempts = [0]

    def on_preview(stored):
        attempts[0] += 1
        owner = f"ordered-preview-{attempts[0]}"
        assert ledger.claim_preview_delivery(stored["id"], owner=owner) is not None
        if attempts[0] == 1:
            callbacks.append("preview_failed")
            assert ledger.fail_preview_delivery(
                stored["id"], owner=owner, uncertain=False
            ) is True
            return
        callbacks.append("preview")
        assert ledger.begin_preview_send_attempt(stored["id"], owner=owner) is True
        assert ledger.complete_preview_delivery(
            stored["id"],
            owner=owner,
            result_message_id="ordered-preview-message",
        ) is True

    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
        on_preview=on_preview,
        on_terminal=lambda _stored: callbacks.append("terminal"),
    )

    assert await watcher.run_once() == 1
    assert callbacks == ["preview_failed"]
    assert ledger.get(item["id"])["status"] == "agent_done"

    now[0] = 102.0
    assert await watcher.run_once() == 1
    assert callbacks == ["preview_failed", "preview", "terminal"]


@pytest.mark.asyncio
async def test_terminal_preview_retry_is_scanned_without_reopening_closeout(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, state = _pending_item(ledger)
    head_sha = "d" * 40
    terminal = dict(state)
    terminal["status"] = "pr_published"
    terminal["next_due_at"] = None
    terminal["policy"] = {**terminal["policy"], "require_preview": True}
    terminal["pr"] = {
        **terminal["pr"],
        "url": "https://github.com/acme/example/pull/10",
        "state": "OPEN",
        "head_sha": head_sha,
    }
    terminal["preview"] = {
        "provider": "vercel",
        "status": "ready",
        "observed_sha": head_sha,
        "url": "https://example-git-retry.vercel.app",
        "deployment_id": "45",
    }
    persisted = ledger.update_closeout(
        item["id"],
        terminal,
        expected_revision=state["revision"],
    )
    assert persisted is not None
    assert ledger.pending_closeouts(due_at=100.0) == []
    delivered = []

    def on_preview(stored):
        owner = "retry-sender"
        assert ledger.claim_preview_delivery(stored["id"], owner=owner) is not None
        assert ledger.begin_preview_send_attempt(stored["id"], owner=owner) is True
        assert ledger.complete_preview_delivery(
            stored["id"],
            owner=owner,
            result_message_id="preview-10",
        ) is True
        delivered.append(stored["id"])

    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("terminal closeout must not be reconciled again")
        ),
        owner="watcher-1",
        on_preview=on_preview,
    )

    assert await watcher.run_once() == 1
    assert delivered == [item["id"]]
    assert ledger.get(item["id"])["preview_delivery"]["status"] == "completed"


@pytest.mark.asyncio
async def test_preview_send_names_exact_head_when_branch_advances_in_flight(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, state = _pending_item(ledger)
    old_head = "e" * 40
    ready = dict(state)
    ready["pr"] = {
        **ready["pr"],
        "url": "https://github.com/acme/example/pull/11",
        "state": "OPEN",
        "head_sha": old_head,
    }
    ready["preview"] = {
        "provider": "vercel",
        "status": "ready",
        "observed_sha": old_head,
        "url": "https://example-git-old.vercel.app",
        "deployment_id": "46",
    }
    persisted = ledger.update_closeout(
        item["id"],
        ready,
        expected_revision=state["revision"],
    )
    assert persisted is not None
    sent = []

    class Adapter:
        async def _send_with_retry(self, **kwargs):
            sent.append(kwargs["content"])
            current = ledger.get(item["id"])["closeout"]
            advanced = dict(current)
            new_head = "f" * 40
            advanced["pr"] = {**advanced["pr"], "head_sha": new_head}
            advanced["preview"] = {
                "provider": "vercel",
                "status": "pending",
                "observed_sha": new_head,
                "url": "",
            }
            assert ledger.update_closeout(
                item["id"],
                advanced,
                expected_revision=current["revision"],
            ) is not None
            return SimpleNamespace(
                success=True,
                message_id="stale-preview-message",
                confirmed_message_ids=("stale-preview-message",),
            )

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.work_ledger = ledger
    runner._adapter_for_source = lambda _source: Adapter()

    await runner._on_trusted_closeout_preview(ledger.get(item["id"]))

    assert f"exact commit `{old_head}`" in sent[0]
    assert "newer commit will get a separate preview message" in sent[0]
    delivery = ledger.get(item["id"])["preview_delivery"]
    assert delivery["status"] == "cancelled"
    assert delivery["cancelled_reason"] == "pr_head_advanced"


@pytest.mark.asyncio
async def test_terminal_closeout_never_overwrites_live_agent(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, _state = _pending_item(ledger)
    ledger.claim(item["id"])
    ledger.mark_agent_running(item["id"], session_id="session-live")
    callbacks = []

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "completed"
        updated["next_due_at"] = None
        return SimpleNamespace(state=updated)

    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
        is_agent_active=lambda _stored: True,
        on_terminal=lambda stored: callbacks.append(stored),
    )

    assert await watcher.run_once() == 1
    stored = ledger.get(item["id"])
    assert stored["status"] == "agent_running"
    assert stored["session_id"] == "session-live"
    assert stored["closeout"]["status"] == "completed"
    assert callbacks == []


@pytest.mark.asyncio
async def test_live_terminal_closeout_release_cas_rejects_replacement_run(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, _state = _pending_item(ledger)
    ledger.claim(item["id"])
    ledger.mark_agent_running(
        item["id"],
        session_id="session-live",
        run_generation=1,
        owner_pid=1111,
        process_epoch="original-process",
    )
    session_key = str(item["session_key"])

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "completed"
        updated["next_due_at"] = None
        return SimpleNamespace(state=updated)

    original_release = ledger.release_closeout
    raced = False

    def race_then_release(work_id, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            assert ledger.mark_agent_running(
                work_id,
                session_key=session_key,
                run_generation=2,
                owner_pid=2222,
                process_epoch="replacement-process",
            )
        return original_release(work_id, **kwargs)

    monkeypatch.setattr(ledger, "release_closeout", race_then_release)
    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
        is_agent_active=lambda _stored: True,
    )

    assert await watcher.run_once() == 0
    stored = ledger.get(item["id"])
    assert stored["status"] == "agent_running"
    assert stored["active_run"]["generation"] == 2
    assert stored["closeout"]["status"] == "pending"


@pytest.mark.asyncio
async def test_terminal_closeout_run_state_cas_rejects_run_started_before_agent_done(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, _state = _pending_item(ledger)
    session_key = str(item["session_key"])

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "completed"
        updated["next_due_at"] = None
        return SimpleNamespace(state=updated)

    original_finalize = ledger.finalize_successful_closeout
    raced = False

    def race_then_finalize(work_id, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            assert ledger.mark_agent_running(
                work_id,
                session_key=session_key,
                run_generation=2,
                owner_pid=4242,
                process_epoch="replacement-process",
            )
        return original_finalize(work_id, **kwargs)

    monkeypatch.setattr(ledger, "finalize_successful_closeout", race_then_finalize)
    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
        is_agent_active=lambda _stored: False,
    )

    assert await watcher.run_once() == 0
    stored = ledger.get(item["id"])
    assert stored["status"] == "agent_running"
    assert stored["active_run"]["generation"] == 2
    assert stored["closeout"]["status"] == "pending"
    assert "final_response" not in stored
    assert "summary_status" not in stored


@pytest.mark.asyncio
async def test_terminal_closeout_run_state_cas_rejects_run_started_before_completed(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, _state = _pending_item(ledger)
    assert ledger.mark_agent_done(item["id"], final_response="done")
    assert ledger.mark_response_delivered(item["id"], result_message_id="result-1")
    assert ledger.mark_summary_updated(item["id"])
    session_key = str(item["session_key"])

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "completed"
        updated["next_due_at"] = None
        return SimpleNamespace(state=updated)

    original_finalize = ledger.finalize_successful_closeout
    raced = False

    def race_then_finalize(work_id, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            assert ledger.mark_agent_running(
                work_id,
                session_key=session_key,
                run_generation=3,
                owner_pid=4242,
                process_epoch="replacement-process",
            )
        return original_finalize(work_id, **kwargs)

    monkeypatch.setattr(ledger, "finalize_successful_closeout", race_then_finalize)
    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
        is_agent_active=lambda _stored: False,
    )

    assert await watcher.run_once() == 0
    stored = ledger.get(item["id"])
    assert stored["status"] == "agent_running"
    assert stored["active_run"]["generation"] == 3
    assert stored["closeout"]["status"] == "pending"
    assert "result_message_id" not in stored


@pytest.mark.asyncio
async def test_blocked_closeout_run_state_cas_rejects_run_started_before_finalization(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, _state = _pending_item(ledger)
    session_key = str(item["session_key"])

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "repair_required"
        updated["next_due_at"] = None
        return SimpleNamespace(state=updated)

    original_finalize = ledger.finalize_blocked_closeout
    raced = False

    def race_then_finalize(work_id, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            assert ledger.mark_agent_running(
                work_id,
                session_key=session_key,
                run_generation=4,
                owner_pid=4242,
                process_epoch="replacement-process",
            )
        return original_finalize(work_id, **kwargs)

    monkeypatch.setattr(ledger, "finalize_blocked_closeout", race_then_finalize)
    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
        is_agent_active=lambda _stored: False,
    )

    assert await watcher.run_once() == 0
    stored = ledger.get(item["id"])
    assert stored["status"] == "agent_running"
    assert stored["active_run"]["generation"] == 4
    assert stored["closeout"]["status"] == "pending"
    assert "terminal_delivery" not in stored
    assert "blocked_reason" not in stored
    assert "final_response" not in stored


@pytest.mark.asyncio
async def test_repair_required_closeout_blocks_once_without_poll_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, _state = _pending_item(ledger)
    callbacks = []

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "repair_required"
        updated["next_due_at"] = None
        return SimpleNamespace(state=updated)

    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
        on_terminal=lambda stored: callbacks.append(stored),
    )

    assert await watcher.run_once() == 1
    stored = ledger.get(item["id"])
    assert stored["status"] == "blocked"
    assert stored["blocked_reason"] == "trusted_closeout_repair_required"
    assert stored["closeout"]["status"] == "repair_required"
    assert callbacks[0]["summary_status"] == "Blocked"
    assert ledger.pending_closeouts(due_at=100.0) == []
    assert await watcher.run_once() == 0


@pytest.mark.asyncio
async def test_shadow_result_never_blocks_or_completes_owning_work_item(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, _state = _pending_item(ledger, mode="shadow")
    callbacks = []

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "repair_required"
        updated["next_due_at"] = 130.0
        return SimpleNamespace(state=updated)

    watcher = TrustedCloseoutWatcher(
        ledger,
        reconcile=reconcile,
        owner="watcher-1",
        on_terminal=lambda stored: callbacks.append(stored),
    )

    assert ledger.get(item["id"])["closeout_authoritative"] is False
    assert await watcher.run_once() == 1
    stored = ledger.get(item["id"])
    assert stored["status"] == "accepted"
    assert stored["closeout"]["status"] == "repair_required"
    assert stored["closeout_authoritative"] is False
    assert callbacks == []


def test_notify_uses_captured_loop_threadsafe(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    watcher = TrustedCloseoutWatcher(GatewayWorkLedger(tmp_path / "ledger.json"))
    callbacks = []

    class Loop:
        @staticmethod
        def is_running():
            return True

        @staticmethod
        def call_soon_threadsafe(callback):
            callbacks.append(callback)

    watcher._loop = Loop()
    watcher.wakeup.clear()
    watcher.notify()

    assert len(callbacks) == 1
    assert watcher.wakeup.is_set() is False
    callbacks[0]()
    assert watcher.wakeup.is_set() is True


@pytest.mark.asyncio
async def test_same_process_notify_wakes_forever_loop(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json")
    watcher = TrustedCloseoutWatcher(ledger, config={"poll_seconds": 60})
    stop = asyncio.Event()
    calls = []

    async def run_once():
        calls.append(True)
        if len(calls) >= 2:
            stop.set()
        return 0

    monkeypatch.setattr(watcher, "run_once", run_once)
    task = asyncio.create_task(watcher.run_forever(stop))
    for _ in range(20):
        if calls:
            break
        await asyncio.sleep(0)
    watcher.notify()
    await asyncio.wait_for(task, timeout=1)

    assert len(calls) >= 2


@pytest.mark.parametrize(
    ("key", "value", "attribute", "expected"),
    [
        ("poll_seconds", None, "poll_seconds", 30.0),
        ("poll_seconds", True, "poll_seconds", 30.0),
        ("poll_seconds", "bad", "poll_seconds", 30.0),
        ("poll_seconds", float("nan"), "poll_seconds", 30.0),
        ("poll_seconds", float("inf"), "poll_seconds", 30.0),
        ("poll_seconds", -5, "poll_seconds", 1.0),
        ("poll_seconds", 5000, "poll_seconds", 3600.0),
        ("lease_seconds", False, "lease_seconds", 120.0),
        ("lease_seconds", "2.5", "lease_seconds", 2.5),
        ("max_concurrency", None, "max_concurrency", 2),
        ("max_concurrency", True, "max_concurrency", 2),
        ("max_concurrency", "bad", "max_concurrency", 2),
        ("max_concurrency", float("nan"), "max_concurrency", 2),
        ("max_concurrency", 0, "max_concurrency", 1),
        ("max_concurrency", 99, "max_concurrency", 16),
        ("max_concurrency", "3", "max_concurrency", 3),
    ],
)
def test_watcher_config_is_fail_safe_and_clamped(
    tmp_path,
    key,
    value,
    attribute,
    expected,
):
    watcher = TrustedCloseoutWatcher(
        GatewayWorkLedger(tmp_path / "ledger.json"),
        config={key: value},
    )

    assert getattr(watcher, attribute) == expected


@pytest.mark.asyncio
async def test_notify_between_marker_consume_and_reconcile_is_not_cleared(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    watcher = TrustedCloseoutWatcher(
        GatewayWorkLedger(tmp_path / "ledger.json"),
        config={"poll_seconds": 60},
    )
    stop = asyncio.Event()
    calls = []
    consumed = False

    def consume_marker():
        nonlocal consumed
        if not consumed:
            consumed = True
            watcher.notify()
            return True
        return False

    async def run_once():
        calls.append(True)
        if len(calls) >= 2:
            stop.set()
        return 0

    monkeypatch.setattr(watcher, "_consume_marker", consume_marker)
    monkeypatch.setattr(watcher, "run_once", run_once)

    await asyncio.wait_for(watcher.run_forever(stop), timeout=1)

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cross_process_marker_written_during_reconcile_runs_immediate_next_pass(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    watcher = TrustedCloseoutWatcher(
        GatewayWorkLedger(tmp_path / "ledger.json"),
        config={"poll_seconds": 60},
    )
    stop = asyncio.Event()
    calls = []

    async def run_once():
        calls.append(True)
        if len(calls) == 1:
            mark_closeout_dirty("work-during-reconcile")
        else:
            stop.set()
        return 0

    monkeypatch.setattr(watcher, "run_once", run_once)

    await asyncio.wait_for(watcher.run_forever(stop), timeout=1)

    assert len(calls) == 2


@pytest.mark.asyncio
async def test_cross_process_marker_after_post_pass_check_does_not_wait_full_poll(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    watcher = TrustedCloseoutWatcher(
        GatewayWorkLedger(tmp_path / "ledger.json"),
        config={"poll_seconds": 60},
    )
    stop = asyncio.Event()
    calls = []
    marker_checks = 0

    def consume_marker():
        nonlocal marker_checks
        marker_checks += 1
        return marker_checks == 3

    async def run_once():
        calls.append(True)
        if len(calls) >= 2:
            stop.set()
        return 0

    monkeypatch.setattr(watcher, "_consume_marker", consume_marker)
    monkeypatch.setattr(watcher, "run_once", run_once)

    await asyncio.wait_for(watcher.run_forever(stop), timeout=1)

    assert len(calls) == 2
    assert marker_checks >= 3


@pytest.mark.asyncio
async def test_blocked_closeout_race_has_one_cas_winner_and_callback(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, _state = _pending_item(ledger)
    callbacks = []

    def reconcile(value, **_kwargs):
        updated = dict(value)
        updated["status"] = "repair_required"
        updated["next_due_at"] = None
        return SimpleNamespace(state=updated)

    watchers = [
        TrustedCloseoutWatcher(
            ledger,
            reconcile=reconcile,
            owner=f"watcher-{index}",
            on_terminal=lambda stored: callbacks.append(stored["id"]),
        )
        for index in range(2)
    ]

    results = await asyncio.gather(*(watcher.run_once() for watcher in watchers))

    assert sum(results) == 1
    assert callbacks == [item["id"]]
    stored = ledger.get(item["id"])
    assert stored["status"] == "blocked"
    assert stored["terminal_delivery"]["status"] == "pending"
    assert stored["final_response"].startswith("Trusted closeout blocked")


@pytest.mark.asyncio
async def test_blocked_terminal_callback_delivers_exactly_once(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, state = _pending_item(ledger)
    leased = ledger.lease_closeout(
        item["id"],
        owner="watcher-1",
        lease_seconds=30,
        expected_revision=state["revision"],
    )
    assert leased is not None
    blocked_state = dict(leased["closeout"])
    blocked_state["status"] = "repair_required"
    blocked = ledger.finalize_blocked_closeout(
        item["id"],
        owner="watcher-1",
        expected_revision=leased["closeout"]["revision"],
        expected_generation=leased["closeout"]["lease_generation"],
        closeout_state=blocked_state,
        final_response="Trusted closeout blocked: repair required.",
        reason="trusted_closeout_repair_required",
    )
    assert blocked is not None

    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    adapter = SimpleNamespace(
        _send_with_retry=AsyncMock(
            return_value=SimpleNamespace(
                success=True,
                message_id="blocked-result",
                confirmed_message_ids=("blocked-result", "blocked-result-2"),
                retry_safe=False,
            )
        )
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)

    await asyncio.gather(
        runner._on_trusted_closeout_terminal(blocked),
        runner._on_trusted_closeout_terminal(blocked),
    )

    adapter._send_with_retry.assert_awaited_once()
    runner._update_discord_summaries.assert_awaited_once()
    stored = ledger.get(item["id"])
    assert stored["status"] == "blocked"
    assert stored["result_message_id"] == "blocked-result"
    assert stored["confirmed_message_ids"] == ["blocked-result", "blocked-result-2"]
    assert stored["terminal_delivery"]["confirmed_message_ids"] == [
        "blocked-result",
        "blocked-result-2",
    ]
    assert stored["terminal_delivery"]["status"] == "completed"
    assert ledger.incomplete_items() == []


@pytest.mark.asyncio
async def test_blocked_terminal_delivery_retries_in_same_process(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("gateway.run._TERMINAL_DELIVERY_RETRY_BASE_SECONDS", 0.01)
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=time.time)
    blocked = _blocked_item(ledger)
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._background_tasks = set()
    runner._terminal_delivery_retry_tasks = {}
    adapter = SimpleNamespace(
        _send_with_retry=AsyncMock(
            side_effect=[
                SimpleNamespace(success=False, error="transient"),
                SimpleNamespace(success=True, message_id="blocked-result"),
            ]
        )
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)

    await runner._resume_finished_discord_work_item(blocked)
    for _ in range(100):
        stored = ledger.get(blocked["id"])
        if (
            stored["terminal_delivery"]["status"] == "completed"
            and not runner._terminal_delivery_retry_tasks
        ):
            break
        await asyncio.sleep(0.01)

    assert adapter._send_with_retry.await_count == 2
    runner._update_discord_summaries.assert_awaited_once()
    stored = ledger.get(blocked["id"])
    assert stored["terminal_delivery"]["status"] == "completed"
    assert stored["terminal_delivery"]["retry_count"] == 1
    assert runner._terminal_delivery_retry_tasks == {}


@pytest.mark.asyncio
async def test_startup_terminal_delivery_retires_deleted_discord_thread(
    monkeypatch,
    tmp_path,
):
    """Match the live startup trace without suppressing transient retries."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=time.time)
    blocked = _blocked_item(
        ledger,
        message_id="1528874646290698260",
        freshness_seconds=1,
    )
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._background_tasks = set()
    runner._terminal_delivery_retry_tasks = {}
    adapter = SimpleNamespace(
        _send_with_retry=AsyncMock(
            return_value=SimpleNamespace(
                success=False,
                error="404 Not Found (error code: 10003): Unknown Channel",
                error_kind="not_found",
                confirmed_message_ids=(),
                retry_safe=True,
            )
        )
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)

    await runner._resume_finished_discord_work_item(blocked)

    adapter._send_with_retry.assert_awaited_once()
    runner._update_discord_summaries.assert_not_awaited()
    stored = ledger.get(blocked["id"])
    assert stored["status"] == "blocked"
    assert stored["delivery_outcome"] == "unreachable"
    assert stored["terminal_delivery"]["status"] == "completed"
    assert stored["terminal_delivery"]["unreachable_reason"] == "discord_unknown_channel"
    assert stored["terminal_delivery"]["retry_count"] == 0
    assert ledger.incomplete_items() == []
    assert runner._schedule_incomplete_discord_work_items() == 0
    assert runner._terminal_delivery_retry_tasks == {}


@pytest.mark.asyncio
async def test_terminal_delivery_retry_is_deduplicated_and_stops_on_shutdown(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setattr("gateway.run._TERMINAL_DELIVERY_RETRY_BASE_SECONDS", 60.0)
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=time.time)
    blocked = _blocked_item(ledger)
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._background_tasks = set()
    runner._terminal_delivery_retry_tasks = {}

    assert runner._schedule_terminal_delivery_retry(blocked["id"], attempt=1) is True
    assert runner._schedule_terminal_delivery_retry(blocked["id"], attempt=1) is False
    task = runner._terminal_delivery_retry_tasks[blocked["id"]]
    runner._shutdown_event.set()
    await task

    assert runner._terminal_delivery_retry_tasks == {}
    assert task.done()


@pytest.mark.asyncio
async def test_ambiguous_terminal_send_timeout_is_not_retried(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=time.time)
    blocked = _blocked_item(ledger)
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._background_tasks = set()
    runner._terminal_delivery_retry_tasks = {}
    adapter = SimpleNamespace(
        _is_timeout_error=lambda _error: True,
        _send_with_retry=AsyncMock(
            return_value=SimpleNamespace(success=False, error="ReadTimeout")
        ),
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)

    await runner._resume_finished_discord_work_item(blocked)

    stored = ledger.get(blocked["id"])
    adapter._send_with_retry.assert_awaited_once()
    assert runner._terminal_delivery_retry_tasks == {}
    assert stored["terminal_delivery"]["status"] == "uncertain"
    assert stored["terminal_delivery"]["uncertain_reason"] == "send_timeout_outcome_unknown"
    assert stored["terminal_delivery"]["summary_updated_at"] is not None


@pytest.mark.asyncio
async def test_partial_terminal_delivery_persists_prefix_and_restart_does_not_replay(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=time.time)
    blocked = _blocked_item(ledger)
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    runner._running = True
    runner._shutdown_event = asyncio.Event()
    runner._background_tasks = set()
    runner._terminal_delivery_retry_tasks = {}
    adapter = SimpleNamespace(
        _is_timeout_error=lambda _error: False,
        _send_with_retry=AsyncMock(
            return_value=SimpleNamespace(
                success=False,
                message_id="chunk-1",
                error="connection dropped after chunk 1",
                confirmed_message_ids=("chunk-1",),
                retry_safe=False,
            )
        ),
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)

    await runner._resume_finished_discord_work_item(blocked)
    first = ledger.get(blocked["id"])
    await runner._resume_finished_discord_work_item(first)

    stored = ledger.get(blocked["id"])
    adapter._send_with_retry.assert_awaited_once()
    assert runner._terminal_delivery_retry_tasks == {}
    assert stored["delivery_outcome"] == "uncertain"
    assert stored["confirmed_message_ids"] == ["chunk-1"]
    assert stored["terminal_delivery"]["confirmed_message_ids"] == ["chunk-1"]
    assert stored["terminal_delivery"]["uncertain_reason"] == "partial_send_confirmed"
    assert stored["terminal_delivery"]["status"] == "uncertain"


@pytest.mark.asyncio
async def test_restart_terminal_delivery_waits_for_preview_completion(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    item, state = _pending_item(ledger)
    head_sha = "8" * 40
    terminal = dict(state)
    terminal["status"] = "pr_published"
    terminal["next_due_at"] = None
    terminal["policy"] = {**terminal["policy"], "require_preview": True}
    terminal["pr"] = {
        **terminal["pr"],
        "url": "https://github.com/acme/example/pull/13",
        "state": "OPEN",
        "head_sha": head_sha,
    }
    terminal["preview"] = {
        "provider": "vercel",
        "status": "ready",
        "observed_sha": head_sha,
        "url": "https://example-git-restart.vercel.app",
        "deployment_id": "48",
    }
    persisted = ledger.update_closeout(
        item["id"],
        terminal,
        expected_revision=state["revision"],
    )
    assert persisted is not None
    assert ledger.mark_agent_done(
        item["id"],
        final_response="Visual QA passed for the draft PR.",
    )
    agent_done = ledger.get(item["id"])

    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    adapter = SimpleNamespace(
        _send_with_retry=AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="terminal-after-preview")
        )
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)

    await runner._resume_finished_discord_work_item(agent_done)
    adapter._send_with_retry.assert_not_awaited()

    owner = "restart-preview"
    assert ledger.claim_preview_delivery(item["id"], owner=owner) is not None
    assert ledger.begin_preview_send_attempt(item["id"], owner=owner) is True
    assert ledger.complete_preview_delivery(
        item["id"],
        owner=owner,
        result_message_id="restart-preview-message",
    ) is True

    await runner._resume_finished_discord_work_item(ledger.get(item["id"]))
    adapter._send_with_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_pre_closeout_failure_delivers_without_impossible_preview(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    event = _event(message_id="failure-before-closeout")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
    )
    attached = ledger.attach_closeout_workspace(
        item["id"],
        workspace_path="/mutable/worktree",
        mode="enforce",
        policy={"require_preview": True},
    )
    assert attached is not None
    assert ledger.mark_agent_done(
        item["id"],
        final_response="Implementation failed before a pull request was created.",
        summary_status="Blocked",
    )
    failed = ledger.get(item["id"])
    assert "preview_delivery" not in failed
    assert ledger.preview_delivery_satisfied(item["id"]) is True

    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    adapter = SimpleNamespace(
        _send_with_retry=AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="failure-message")
        )
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)

    await runner._resume_finished_discord_work_item(failed)

    adapter._send_with_retry.assert_awaited_once()


@pytest.mark.asyncio
async def test_partial_normal_delivery_is_blocked_and_restart_does_not_replay(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=time.time)
    event = _event(message_id="partial-normal")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
    )
    assert ledger.claim(item["id"]) is not None
    assert ledger.mark_agent_running(item["id"], session_id="session-1")
    expected = ledger.run_state_snapshot(ledger.get(item["id"]))
    assert ledger.mark_agent_done(
        item["id"],
        final_response="a long final response",
        session_id="session-1",
        expected_run_state=expected,
    )
    agent_done = ledger.get(item["id"])

    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    adapter = SimpleNamespace(
        _is_timeout_error=lambda _error: False,
        _send_with_retry=AsyncMock(
            return_value=SimpleNamespace(
                success=False,
                message_id="chunk-1",
                error="later chunk failed",
                confirmed_message_ids=("chunk-1",),
                retry_safe=False,
            )
        ),
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)

    await runner._resume_finished_discord_work_item(agent_done)
    first = ledger.get(item["id"])
    await runner._resume_finished_discord_work_item(first)

    stored = ledger.get(item["id"])
    adapter._send_with_retry.assert_awaited_once()
    assert stored["status"] == "blocked"
    assert stored["delivery_outcome"] == "uncertain"
    assert stored["confirmed_message_ids"] == ["chunk-1"]


@pytest.mark.asyncio
async def test_normal_delivery_receipt_cas_loss_stops_before_summary_update(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    event = _event(message_id="normal-receipt-cas-race")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
    )
    assert ledger.claim(item["id"]) is not None
    assert ledger.mark_agent_running(
        item["id"],
        session_id="session-1",
        run_generation=1,
        owner_pid=1111,
        process_epoch="first-process",
    )
    expected = ledger.run_state_snapshot(ledger.get(item["id"]))
    assert ledger.mark_agent_done(
        item["id"],
        final_response="final response",
        session_id="session-1",
        feature_summary={"message_id": "summary-1"},
        expected_run_state=expected,
    )
    agent_done = ledger.get(item["id"])

    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    adapter = SimpleNamespace(
        _send_with_retry=AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="sent-once")
        )
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)
    original_mark_delivered = ledger.mark_response_delivered
    raced = False

    def race_then_mark_delivered(work_id, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            assert ledger.mark_agent_running(
                work_id,
                session_key=str(item["session_key"]),
                run_generation=2,
                owner_pid=2222,
                process_epoch="replacement-process",
            )
        return original_mark_delivered(work_id, **kwargs)

    monkeypatch.setattr(
        ledger,
        "mark_response_delivered",
        race_then_mark_delivered,
    )

    await runner._resume_finished_discord_work_item(agent_done)

    stored = ledger.get(item["id"])
    assert stored["status"] == "agent_running"
    assert stored["active_run"]["generation"] == 2
    assert "summary_updated_at" not in stored
    runner._update_discord_summaries.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_delivery_sending_fence_recovers_uncertain_without_replay(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: 100.0)
    event = _event(message_id="normal-send-fence")
    item = ledger.accept_event(
        event,
        session_key=build_session_key(event.source),
        freshness_seconds=3600,
    )
    assert ledger.mark_agent_done(item["id"], final_response="final response")
    agent_done = ledger.get(item["id"])
    expected = ledger.run_state_snapshot(agent_done)
    assert ledger.mark_response_delivery_started(
        item["id"],
        expected_run_state=expected,
    )

    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    adapter = SimpleNamespace(_send_with_retry=AsyncMock())
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)

    await runner._resume_finished_discord_work_item(ledger.get(item["id"]))

    stored = ledger.get(item["id"])
    adapter._send_with_retry.assert_not_awaited()
    assert stored["delivery_outcome"] == "uncertain"
    assert stored["status"] == "blocked"
    assert stored["delivery_attempt"]["status"] == "uncertain"


@pytest.mark.asyncio
async def test_crash_after_send_recovers_as_uncertain_without_duplicate(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    now = {"value": 100.0}
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: now["value"])
    blocked = _blocked_item(ledger)
    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    adapter = SimpleNamespace(
        _send_with_retry=AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="sent-once")
        )
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)
    persist_receipt = ledger.mark_terminal_response_delivered

    def crash_before_receipt(*_args, **_kwargs):
        raise RuntimeError("injected crash window")

    monkeypatch.setattr(ledger, "mark_terminal_response_delivered", crash_before_receipt)
    await runner._resume_finished_discord_work_item(blocked)
    first = ledger.get(blocked["id"])
    assert first["terminal_delivery"]["status"] == "sending"
    assert adapter._send_with_retry.await_count == 1

    now["value"] = 221.0
    monkeypatch.setattr(ledger, "mark_terminal_response_delivered", persist_receipt)
    await runner._resume_finished_discord_work_item(first)

    stored = ledger.get(blocked["id"])
    assert adapter._send_with_retry.await_count == 1
    runner._update_discord_summaries.assert_awaited_once()
    assert stored["terminal_delivery"]["status"] == "uncertain"
    assert stored["terminal_delivery"]["uncertain_reason"] == "send_attempt_outcome_unknown"
    assert stored["terminal_delivery"]["operator_repair_required"] is True
    assert stored["terminal_delivery"]["summary_updated_at"] == 221.0
    assert stored["delivery_outcome"] == "uncertain"
    assert ledger.incomplete_items() == []


@pytest.mark.asyncio
async def test_expired_blocked_item_recovers_terminal_delivery(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    now = {"value": time.time()}
    old_timestamp = now["value"] - (8 * 24 * 60 * 60)
    old_snowflake = str(int((old_timestamp * 1000 - 1420070400000)) << 22)
    ledger = GatewayWorkLedger(tmp_path / "ledger.json", now_fn=lambda: now["value"])
    blocked = _blocked_item(
        ledger,
        message_id=old_snowflake,
        freshness_seconds=1,
    )
    now["value"] += 2

    pending = ledger.incomplete_items()
    assert [item["id"] for item in pending] == [blocked["id"]]
    assert ledger.mark_expired(blocked["id"]) is False
    assert ledger.get(blocked["id"])["status"] == "blocked"

    runner = object.__new__(GatewayRunner)
    runner.work_ledger = ledger
    adapter = SimpleNamespace(
        _send_with_retry=AsyncMock(
            return_value=SimpleNamespace(success=True, message_id="late-result")
        )
    )
    runner.adapters = {Platform.DISCORD: adapter}
    runner._update_discord_summaries = AsyncMock(return_value=True)

    await runner._resume_finished_discord_work_item(pending[0])

    stored = ledger.get(blocked["id"])
    assert stored["status"] == "blocked"
    assert stored["result_message_id"] == "late-result"
    assert stored["terminal_delivery"]["status"] == "completed"
    assert ledger.incomplete_items() == []
