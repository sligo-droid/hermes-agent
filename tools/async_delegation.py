#!/usr/bin/env python3
"""
Async (background) delegation registry.

Backs ``delegate_task(background=true)`` and background coding workers: the
parent agent dispatches trusted work on a module-level daemon executor and
returns a handle immediately, so the user and the model can keep working.

When the child finishes, a completion event is pushed onto the SHARED
``process_registry.completion_queue`` with ``type="async_delegation"``. For a
Discord work-item attempt, both required coding and advisory read-only results
are first committed to the gateway work ledger; the queue is only a wakeup and
never becomes a model turn. Non-work-item CLI and gateway delegations retain
the legacy best-effort follow-up-turn behavior.

  - legacy completions surface as a NEW turn when the agent is idle, never spliced
    between a tool result and an assistant message. That keeps strict
    message-role alternation legal and the prompt cache intact (hard
    invariant: never mutate past context).
  - Discord attempt aggregation inherits the ledger's exact-attempt fencing,
    terminal delivery de-duplication, and crash recovery without model replay.

The completion payload carries a RICH, self-contained task-source block (the
original goal, the context the parent supplied, toolsets, model, dispatch
time, status, and the full result summary). When the result re-enters the
conversation the parent may be deep in unrelated context and won't remember
why the subagent existed; the block lets it either use the result or
re-dispatch if the world has moved on.

This module owns ONLY the async lifecycle. The actual child build + run is
delegated back to ``delegate_tool._run_single_child`` via an injected
runner, so all the credential leasing, heartbeat, timeout, and result-shaping
logic stays in one place.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
# A persistent daemon executor (NOT a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat the whole point of async). Workers are daemon
# threads so a hard process exit doesn't hang on an in-flight child.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> record dict. Kept for the lifetime of the run plus a short
# tail after completion so `list_async_delegations()` can show recent results.
_records: Dict[str, Dict[str, Any]] = {}

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# How many completed records to retain for status queries before pruning.
_MAX_RETAINED_COMPLETED = 50
_MAX_TERMINAL_EVIDENCE_TEXT = 2000
_MAX_TERMINAL_EVIDENCE_ITEMS = 32


def async_completion_succeeded(evt: Dict[str, Any]) -> bool:
    """Return deterministic success for single, coding, and batch events."""
    status = str(evt.get("status") or "").strip().lower()
    results = evt.get("results")
    if evt.get("is_batch") or isinstance(results, list):
        if status not in {"completed", "success"} or not isinstance(results, list):
            return False
        return bool(results) and all(
            isinstance(item, dict)
            and str(item.get("status") or "").strip().lower()
            in {"completed", "success"}
            for item in results
        )
    result = evt.get("result")
    if isinstance(result, dict) and "success" in result:
        return status in {"completed", "success"} and result.get("success") is True
    return status in {"completed", "success"}


_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000
_DB_LOCK = threading.Lock()


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT,
            delivery_claim TEXT,
            delivery_claimed_at REAL
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(async_delegations)")}
    for name, sql_type in (
        ("owner_pid", "INTEGER"),
        ("owner_started_at", "INTEGER"),
        ("task_json", "TEXT"),
        ("delivery_claim", "TEXT"),
        ("delivery_claimed_at", "REAL"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE async_delegations ADD COLUMN {name} {sql_type}")
    return conn


def _persist_dispatch(record: Dict[str, Any]) -> None:
    now = time.time()
    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(__import__("os").getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in (
            "goal",
            "goals",
            "context",
            "toolsets",
            "role",
            "model",
            "is_batch",
            "task_specs",
        )
        if key in record
    }
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json)
               VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?)""",
            (record["delegation_id"], record.get("session_key", ""),
             record.get("origin_ui_session_id", ""), record.get("parent_session_id"),
             record["dispatched_at"], now, __import__("os").getpid(),
             owner_started_at, json.dumps(task_payload)),
        )
    _prune_durable_records()


def _delete_durable_delegation(delegation_id: str) -> None:
    with _DB_LOCK, _connect() as conn:
        conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))


def _prune_durable_records() -> None:
    """Bound terminal history, preferring delivered records for deletion."""
    now = time.time()
    cutoff = now - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            "DELETE FROM async_delegations WHERE delivery_state='delivered' AND updated_at < ?",
            (cutoff,),
        )
        terminal_count = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE state NOT IN ('running','finalizing')"
        ).fetchone()[0]
        excess = max(0, terminal_count - _MAX_RETAINED_COMPLETED)
        if excess:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing')
                     ORDER BY CASE delivery_state WHEN 'delivered' THEN 0 ELSE 1 END,
                              updated_at ASC LIMIT ?
                   )""",
                (excess,),
            )
        pending_count = conn.execute(
            """SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'"""
        ).fetchone()[0]
        overflow = max(0, pending_count - _MAX_DURABLE_PENDING)
        if overflow:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing') AND delivery_state='pending'
                     ORDER BY updated_at ASC LIMIT ?
                   )""",
                (overflow,),
            )


def _persist_completion(event: Dict[str, Any], result: Dict[str, Any]) -> None:
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state='pending'
               WHERE delegation_id=?""",
            (event.get("status", "completed"), event.get("completed_at", now), now,
             json.dumps(event), json.dumps(result), event["delegation_id"]),
        )


def _note_delivery_attempt(delegation_id: str) -> None:
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            "UPDATE async_delegations SET delivery_attempts=delivery_attempts+1, updated_at=? WHERE delegation_id=?",
            (time.time(), delegation_id),
        )


def recover_abandoned_delegations() -> int:
    """Classify records whose owning process disappeared as outcome unknown."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return 0
    now = time.time()
    recovered = 0
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json
               FROM async_delegations WHERE state IN ('running','finalizing')"""
        ).fetchall()
        for row in rows:
            delegation_id, session_key, origin_ui, parent_id, dispatched_at, pid, started, task_json = row
            live = False
            if pid:
                live = _pid_exists(int(pid))
                if live and started is not None:
                    live = get_process_start_time(int(pid)) == int(started)
            if live:
                continue
            task = json.loads(task_json or "{}")
            event = {
                "type": "async_delegation", "delegation_id": delegation_id,
                "session_key": session_key, "origin_ui_session_id": origin_ui,
                "parent_session_id": parent_id, "goal": task.get("goal", ""),
                "goals": task.get("goals"), "context": task.get("context"),
                "toolsets": task.get("toolsets"), "role": task.get("role"),
                "model": task.get("model"), "is_batch": bool(task.get("is_batch")),
                "task_specs": task.get("task_specs") or [],
                "status": "unknown", "summary": None,
                "error": "Delegation owner exited before recording a terminal result; outcome unknown.",
                "dispatched_at": dispatched_at, "completed_at": now,
            }
            result = {"status": "unknown", "summary": None, "error": event["error"]}
            conn.execute(
                """UPDATE async_delegations SET state='unknown', completed_at=?,
                   updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                   WHERE delegation_id=?""",
                (now, now, json.dumps(event), json.dumps(result), delegation_id),
            )
            recovered += 1
    return recovered


def restore_undelivered_completions(target_queue) -> int:
    """Enqueue durable pending completions as fresh turns after process start.

    Every restored event is stamped ``restored=True`` (in-memory only — the
    stamp is added after the durable payload is deserialized and is never
    persisted). Restored events originate from a *previous* process, so no
    consumer in THIS process implicitly owns them: drain paths that run
    without an ownership filter (the legacy single-session behavior) must
    leave them queued for a consumer that can positively prove ownership,
    otherwise a brand-new session adopts a dead session's delegation
    results seconds after boot (#64484).
    """
    recover_abandoned_delegations()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT delegation_id, event_json FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id"""
        ).fetchall()
        for _delegation_id, payload in rows:
            evt = json.loads(payload)
            if isinstance(evt, dict):
                evt["restored"] = True
            target_queue.put(evt)
    return len(rows)


def _mark_durable_completion_delivered(delegation_id: str) -> bool:
    """Atomically acknowledge successful injection of a durable completion."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return False
        if row[0] == "delivered":
            return True
        if row[0] != "pending":
            return False
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'""",
            (now, now, delegation_id),
        )
        return cur.rowcount == 1


def _cancel_durable_completion_delivery(
    delegation_id: str,
    *,
    reason: str,
    cancelled_at: float,
) -> bool:
    """Fence any not-yet-delivered compatibility event for one delegation."""
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations
               SET state='cancelled', completed_at=COALESCE(completed_at, ?),
                   updated_at=?, delivery_state='cancelled', delivery_claim=NULL,
                   delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'""",
            (cancelled_at, cancelled_at, delegation_id),
        )
        return cur.rowcount == 1


def _cancel_durable_completions_for_identity(
    *,
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
    reason: str,
    cancelled_at: float,
) -> tuple[int, List[str]]:
    """Persist stop tombstones for generic completions even after restart.

    The generic compatibility store is the only owner available after process
    restart, when ``_records`` is intentionally empty. Match only explicit
    captured session identities and return producer IDs so the caller can
    de-duplicate volatile matches before bounding public diagnostics.
    """
    selectors: List[str] = []
    params: List[Any] = []
    for column, value in (
        ("origin_session", session_key),
        ("origin_ui_session_id", origin_ui_session_id),
        ("parent_session_id", parent_session_id),
    ):
        normalized = str(value or "").strip()
        if normalized:
            selectors.append(f"{column}=?")
            params.append(normalized)
    if not selectors:
        return 0, []
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            f"""SELECT delegation_id FROM async_delegations
                WHERE delivery_state='pending' AND ({' OR '.join(selectors)})
                ORDER BY dispatched_at, delegation_id""",
            params,
        ).fetchall()
        conn.execute(
            f"""UPDATE async_delegations
                SET state='cancelled', completed_at=COALESCE(completed_at, ?),
                    updated_at=?, delivery_state='cancelled', delivery_claim=NULL,
                    delivery_claimed_at=NULL
                WHERE delivery_state='pending' AND ({' OR '.join(selectors)})""",
            [cancelled_at, cancelled_at, *params],
        )
    if rows:
        logger.info(
            "Durably suppressed %d undelivered async completion(s) for session stop (%s)",
            len(rows),
            str(reason or "session_stop")[:120],
        )
    return len(rows), [str(row[0]) for row in rows]


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return True  # legacy event created before durable dispatch
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (claim_id, now, now, delegation_id, now - 300),
        )
        return cur.rowcount == 1


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; non-durable events need no token."""
    if evt.get("type") != "async_delegation":
        return ""
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{__import__('os').getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry."""
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=NULL,
                      delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (time.time(), delegation_id, claim_id),
        )
        return cur.rowcount == 1


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def is_completion_cancelled(delegation_id: str) -> bool:
    """Return whether explicit cancellation owns an undelivered completion."""
    normalized = str(delegation_id or "")
    if not normalized:
        return False
    with _records_lock:
        record = _records.get(normalized)
        if record is not None and record.get("cancel_requested"):
            return True
    try:
        with _DB_LOCK, _connect() as conn:
            row = conn.execute(
                "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
                (normalized,),
            ).fetchone()
        return bool(row and row[0] == "cancelled")
    except Exception:
        return False


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        complete_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        release_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT origin_session, state, dispatched_at, completed_at,
                      result_json, delivery_state, delivery_attempts
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "delegation_id": delegation_id, "origin_session": row[0], "state": row[1],
        "dispatched_at": row[2], "completed_at": row[3],
        "result": json.loads(row[4]) if row[4] else None,
        "delivery_state": row[5], "delivery_attempts": row[6],
    }


def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow) the shared daemon executor.

    We never shrink — ThreadPoolExecutor can't resize — but if the configured
    cap grows between calls we rebuild a larger pool. Existing in-flight
    futures keep running on the old pool until it's garbage collected.
    """
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            # Daemon threads: thread_name_prefix aids debugging in stack dumps.
            _executor = DaemonThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="async-delegate",
            )
            _executor_max_workers = max_workers
        return _executor


def active_count(*, kind: Optional[str] = None, session_key: Optional[str] = None) -> int:
    """Number of async delegations currently running, optionally filtered."""
    with _records_lock:
        return sum(
            1
            for record in _records.values()
            if record.get("status") in {"running", "finalizing"}
            and (kind is None or record.get("kind", "delegation") == kind)
            and (session_key is None or record.get("session_key", "") == session_key)
        )


def pending_count(
    *,
    kind: Optional[str] = None,
    session_key: Optional[str] = None,
    origin_work_item_id: Optional[str] = None,
    origin_run_generation: Optional[int] = None,
    origin_attempt_id: Optional[str] = None,
    exclude_delegation_id: Optional[str] = None,
) -> int:
    """Count running or completed-but-not-delivered background units."""
    work_item_id = str(origin_work_item_id or "").strip()
    generation = _normalized_generation(origin_run_generation)
    attempt_id = str(origin_attempt_id or "").strip()
    with _records_lock:
        return sum(
            1
            for delegation_id, record in _records.items()
            if delegation_id != exclude_delegation_id
            and (
                record.get("status") == "running"
                or bool(record.get("delivery_pending"))
            )
            and (kind is None or record.get("kind", "delegation") == kind)
            and (session_key is None or record.get("session_key", "") == session_key)
            and (
                not work_item_id
                or str(record.get("origin_work_item_id") or "") == work_item_id
            )
            and (
                generation is None
                or _normalized_generation(record.get("origin_run_generation"))
                == generation
            )
            and (
                not attempt_id
                or str(record.get("origin_attempt_id") or "") == attempt_id
            )
        )


def _normalized_generation(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        generation = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return generation if generation > 0 else None


def completion_state(
    *,
    session_key: Optional[str] = None,
    origin_work_item_id: Optional[str] = None,
    origin_run_generation: Optional[int] = None,
    origin_attempt_id: Optional[str] = None,
    exclude_delegation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Return the bounded live aggregate for one originating work generation.

    Coding workers are required lifecycle work. Ordinary ``delegate_task``
    children are advisory and are reported separately so they can never hold or
    complete the originating feature reaction.
    """
    work_item_id = str(origin_work_item_id or "").strip()
    generation = _normalized_generation(origin_run_generation)
    attempt_id = str(origin_attempt_id or "").strip()
    required_pending = 0
    advisory_pending = 0
    required_failed = False
    required_succeeded = 0
    with _records_lock:
        for delegation_id, record in _records.items():
            if session_key is not None and record.get("session_key", "") != session_key:
                continue
            if work_item_id and str(record.get("origin_work_item_id") or "") != work_item_id:
                continue
            if (
                generation is not None
                and _normalized_generation(record.get("origin_run_generation"))
                != generation
            ):
                continue
            if attempt_id and str(record.get("origin_attempt_id") or "") != attempt_id:
                continue
            pending = delegation_id != exclude_delegation_id and (
                record.get("status") == "running" or bool(record.get("delivery_pending"))
            )
            if record.get("kind") == "coding_worker":
                if pending:
                    required_pending += 1
                success = record.get("completion_success")
                if success is False:
                    required_failed = True
                elif success is True:
                    required_succeeded += 1
            elif pending:
                advisory_pending += 1
    return {
        "required_pending": required_pending,
        "required_failed": required_failed,
        "required_succeeded": required_succeeded,
        "advisory_pending": advisory_pending,
    }


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _capture_session_routing() -> Dict[str, str]:
    """Snapshot gateway routing metadata on the dispatching parent thread."""
    try:
        from gateway.session_context import get_session_env
    except Exception:
        return {}

    fields = {
        "platform": "HERMES_SESSION_PLATFORM",
        "chat_id": "HERMES_SESSION_CHAT_ID",
        "thread_id": "HERMES_SESSION_THREAD_ID",
        "user_id": "HERMES_SESSION_USER_ID",
        "user_name": "HERMES_SESSION_USER_NAME",
        "message_id": "HERMES_SESSION_MESSAGE_ID",
    }
    captured: Dict[str, str] = {}
    for field, env_name in fields.items():
        try:
            value = str(get_session_env(env_name, "") or "").strip()
        except Exception:
            value = ""
        if value:
            captured[field] = value
    return captured


def _is_durable_attempt_dispatch(record: Dict[str, Any]) -> bool:
    """Whether the Discord work ledger owns this dispatch's delivery."""
    return bool(
        str(record.get("origin_work_item_id") or "").strip()
        and _normalized_generation(record.get("origin_run_generation"))
        and str(record.get("origin_attempt_id") or "").strip()
        and _normalized_generation(record.get("origin_attempt_order"))
    )


def _is_required_coding_dispatch(record: Dict[str, Any]) -> bool:
    """Whether this durable dispatch must be running before coding starts."""

    return bool(
        _is_durable_attempt_dispatch(record)
        and record.get("kind") == "coding_worker"
    )


def _required_async_ledger():
    """Construct the profile-scoped durable owner lazily."""
    from gateway.work_ledger import GatewayWorkLedger

    return GatewayWorkLedger()


def _required_dispatch_state(
    state: Any,
    delegation_id: str,
) -> Optional[Dict[str, Any]]:
    """Return one normalized durable dispatch from a ledger mutation result."""
    if not isinstance(state, dict):
        return None
    dispatches = state.get("dispatches")
    if not isinstance(dispatches, dict):
        return None
    dispatch = dispatches.get(str(delegation_id))
    return dict(dispatch) if isinstance(dispatch, dict) else None


def _register_required_async_dispatch(record: Dict[str, Any]) -> Optional[str]:
    """Register one required coding dispatch before executor submission.

    Returns an error string when registration was rejected or unavailable.
    Non-attempt delegations intentionally never touch the durable work ledger.
    """
    if not _is_durable_attempt_dispatch(record):
        return None
    try:
        ledger = _required_async_ledger()
        registered = ledger.register_required_async_dispatch(
            str(record["origin_work_item_id"]),
            delegation_id=str(record["delegation_id"]),
            generation=record.get("origin_run_generation"),
            attempt_id=record.get("origin_attempt_id"),
            attempt_order=record.get("origin_attempt_order"),
            owner_pid=record.get("origin_owner_pid"),
            process_epoch=record.get("origin_process_epoch"),
            registered_at=record.get("dispatched_at"),
            closeout_id=record.get("closeout_id"),
            scope_paths=record.get("origin_scope_paths"),
            kind=(
                "coding_worker"
                if record.get("kind") == "coding_worker"
                else "advisory"
            ),
            required=record.get("kind") == "coding_worker",
            evidence=record.get("registration_evidence"),
        )
        registered_dispatch = _required_dispatch_state(
            registered,
            str(record["delegation_id"]),
        )
        if not registered_dispatch or registered_dispatch.get("state") != "registered":
            return "required async dispatch is absent, stale, or conflicts"
    except Exception as exc:  # noqa: BLE001 - dispatch must fail closed
        logger.exception(
            "Could not register required async dispatch %s",
            record.get("delegation_id"),
        )
        return f"required async dispatch registration failed: {exc}"
    record["required_async_registered"] = True
    record["required_async_state"] = "registered"
    return None


def _coding_terminal_fields(
    record: Dict[str, Any],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """Return bounded terminal evidence for one durable attempt dispatch."""
    if record.get("kind") != "coding_worker":
        raw_results = result.get("results") if isinstance(result.get("results"), list) else []
        task_specs = record.get("task_specs") if isinstance(record.get("task_specs"), list) else []
        advisory_results: List[Dict[str, str]] = []
        for index, raw_result in enumerate(raw_results[:_MAX_TERMINAL_EVIDENCE_ITEMS]):
            if not isinstance(raw_result, dict):
                continue
            task_spec = task_specs[index] if index < len(task_specs) and isinstance(task_specs[index], dict) else {}
            entry: Dict[str, str] = {}
            for key, value in (
                ("goal", task_spec.get("goal")),
                ("status", raw_result.get("status")),
                ("summary", raw_result.get("summary")),
                ("error", raw_result.get("error")),
            ):
                text = str(value or "")[:_MAX_TERMINAL_EVIDENCE_TEXT]
                if text:
                    entry[key] = text
            if entry:
                advisory_results.append(entry)
        summaries = [
            entry["summary"]
            for entry in advisory_results
            if entry.get("summary")
        ]
        errors = [
            entry["error"]
            for entry in advisory_results
            if entry.get("error")
        ]
        if not advisory_results and result.get("error"):
            advisory_results.append(
                {
                    "goal": str(record.get("goal") or "")[:_MAX_TERMINAL_EVIDENCE_TEXT],
                    "status": str(result.get("status") or "error")[:_MAX_TERMINAL_EVIDENCE_TEXT],
                    "error": str(result.get("error") or "")[:_MAX_TERMINAL_EVIDENCE_TEXT],
                }
            )
        return {
            "summary": "\n".join(summaries[:8]) or result.get("summary"),
            "error": "\n".join(errors[:8]) or result.get("error"),
            "closeout_id": "",
            "evidence": {"advisory_results": advisory_results},
        }

    deterministic = result.get("result")
    deterministic = deterministic if isinstance(deterministic, dict) else {}
    coding_event = result.get("_async_coding_worker")
    coding_event = coding_event if isinstance(coding_event, dict) else {}
    scope_check = deterministic.get("scope_check")
    scope_check = scope_check if isinstance(scope_check, dict) else {}
    parallel_merge = deterministic.get("parallel_merge")
    parallel_merge = parallel_merge if isinstance(parallel_merge, dict) else {}
    git_result = deterministic.get("fable_git_result")
    git_result = git_result if isinstance(git_result, dict) else {}

    def bounded_text(value: Any) -> str:
        return str(value or "")[:_MAX_TERMINAL_EVIDENCE_TEXT]

    def bounded_strings(value: Any) -> List[str]:
        if isinstance(value, (str, bytes)):
            value = [value]
        if not isinstance(value, (list, tuple, set)):
            return []
        return [
            bounded_text(item)
            for item in list(value)[:_MAX_TERMINAL_EVIDENCE_ITEMS]
            if bounded_text(item)
        ]

    evidence: Dict[str, Any] = {
        "scope_paths": bounded_strings(
            coding_event.get("scope_paths")
            or scope_check.get("scope_paths")
            or record.get("origin_scope_paths")
            or []
        ),
        "worker_cwd": bounded_text(coding_event.get("worker_cwd")),
    }
    initial_dirty_paths = bounded_strings(deterministic.get("initial_dirty_paths"))
    if initial_dirty_paths:
        evidence["initial_dirty_paths"] = initial_dirty_paths
    changed = deterministic.get("changed")
    if isinstance(changed, bool):
        evidence["changed"] = changed
    else:
        changed_paths = bounded_strings(changed)
        if changed_paths:
            evidence["changed_paths"] = changed_paths
    if isinstance(scope_check, dict):
        bounded_scope_check: Dict[str, Any] = {}
        if isinstance(scope_check.get("clean"), bool):
            bounded_scope_check["clean"] = scope_check["clean"]
        for key in ("scope_paths", "out_of_scope_files"):
            paths = bounded_strings(scope_check.get(key))
            if paths:
                bounded_scope_check[key] = paths
        if scope_check.get("inspection_error"):
            bounded_scope_check["inspection_error"] = bounded_text(
                scope_check["inspection_error"]
            )
        if bounded_scope_check:
            evidence["scope_check"] = bounded_scope_check
    if parallel_merge:
        bounded_parallel_merge: Dict[str, Any] = {}
        for key in (
            "success",
            "recovery_required",
            "merged",
            "merge_pending",
            "worktree_kept",
        ):
            if isinstance(parallel_merge.get(key), bool):
                bounded_parallel_merge[key] = parallel_merge[key]
        for key in ("group_id", "worker_cwd", "error", "next_action"):
            if parallel_merge.get(key):
                bounded_parallel_merge[key] = bounded_text(parallel_merge[key])
        conflicts = bounded_strings(parallel_merge.get("merge_conflicts"))
        if conflicts:
            bounded_parallel_merge["merge_conflicts"] = conflicts
        if bounded_parallel_merge:
            evidence["parallel_merge"] = bounded_parallel_merge
    worker_run = coding_event.get("worker_run")
    if isinstance(worker_run, dict):
        bounded_worker_run: Dict[str, Any] = {}
        for key in ("backend", "model", "reasoning", "model_tier"):
            if worker_run.get(key):
                bounded_worker_run[key] = bounded_text(worker_run[key])
        for key in ("failed", "background"):
            if isinstance(worker_run.get(key), bool):
                bounded_worker_run[key] = worker_run[key]
        if bounded_worker_run:
            evidence["worker_run"] = bounded_worker_run
    test_refs = bounded_strings(deterministic.get("test_refs"))
    if test_refs:
        evidence["test_refs"] = test_refs
    if isinstance(parallel_merge.get("merged"), bool):
        evidence["merged"] = parallel_merge["merged"]
    for source, key in (
        (parallel_merge, "merge_ref"),
        (deterministic, "commit_sha"),
        (deterministic, "head_sha"),
        (deterministic, "base_sha"),
        (git_result, "commit_sha"),
        (git_result, "head_sha"),
        (git_result, "base_sha"),
    ):
        if source.get(key):
            evidence[key] = bounded_text(source[key])
    summary = deterministic.get("summary")
    if summary is None:
        summary = result.get("summary")
    error = deterministic.get("error")
    if error is None:
        error = result.get("error")
    closeout_id = (
        git_result.get("closeout_id")
        or deterministic.get("closeout_id")
        or record.get("closeout_id")
        or ""
    )
    if closeout_id:
        evidence["closeout_id"] = bounded_text(closeout_id)
    return {
        "summary": summary,
        "error": error,
        "closeout_id": closeout_id,
        "evidence": evidence,
    }


def _persist_required_async_terminal(
    record: Dict[str, Any],
    result: Dict[str, Any],
    status: str,
    *,
    submit_failure: bool = False,
) -> bool:
    """Persist terminal required-work state before any volatile queue wakeup."""
    if not _is_durable_attempt_dispatch(record):
        return True
    if not record.get("required_async_registered"):
        logger.error(
            "Durable async dispatch %s reached terminal state without registration",
            record.get("delegation_id"),
        )
        return False
    terminal = _coding_terminal_fields(record, result)
    common = {
        "delegation_id": str(record.get("delegation_id") or ""),
        "generation": record.get("origin_run_generation"),
        "attempt_id": record.get("origin_attempt_id"),
        "attempt_order": record.get("origin_attempt_order"),
        "status": status,
        "completed_at": record.get("completed_at"),
        "closeout_id": terminal["closeout_id"],
        "evidence": terminal["evidence"],
    }
    try:
        ledger = _required_async_ledger()
        if submit_failure:
            persisted = ledger.record_required_async_submit_failure(
                str(record["origin_work_item_id"]),
                error=terminal["error"],
                **common,
            )
        else:
            persisted = ledger.record_required_async_completion(
                str(record["origin_work_item_id"]),
                success=bool(record.get("completion_success")),
                summary=terminal["summary"],
                error=terminal["error"],
                **common,
            )
    except Exception:  # noqa: BLE001 - detached worker must not crash
        logger.exception(
            "Could not persist durable async terminal state for %s",
            record.get("delegation_id"),
        )
        return False
    persisted_dispatch = _required_dispatch_state(
        persisted,
        str(record.get("delegation_id") or ""),
    )
    if (
        not persisted_dispatch
        or persisted_dispatch.get("state") != "terminal"
        or persisted_dispatch.get("conflicting_replay") is True
        or (persisted_dispatch.get("success") is True)
        != bool(record.get("completion_success"))
    ):
        logger.warning(
            "Required async terminal mutation for %s was rejected as stale or conflicting",
            record.get("delegation_id"),
        )
        return False
    return True


def _completion_requires_delivery_ack(record: Dict[str, Any]) -> bool:
    """Whether gateway-visible state must stay pending until a forged turn runs."""
    platform = str(record.get("platform") or "").strip().lower()
    if not platform:
        parts = str(record.get("session_key") or "").split(":")
        if len(parts) >= 3 and parts[:2] == ["agent", "main"]:
            platform = parts[2].strip().lower()
    return bool(platform and platform != "cli")


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the retention cap.

    Caller must hold ``_records_lock``.
    """
    completed = [
        (rid, r)
        for rid, r in _records.items()
        if r.get("status") != "running"
        and not r.get("delivery_pending")
    ]
    if len(completed) <= _MAX_RETAINED_COMPLETED:
        return
    # Oldest-first by completion time (fall back to dispatch time).
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: len(completed) - _MAX_RETAINED_COMPLETED]:
        _records.pop(rid, None)


def dispatch_async_delegation(
    *,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    kind: str = "delegation",
    origin_work_item_id: str = "",
    origin_run_generation: Optional[int] = None,
    origin_attempt_id: str = "",
    origin_attempt_order: Optional[int] = None,
    origin_owner_pid: Optional[int] = None,
    origin_process_epoch: str = "",
    origin_scope_paths: Optional[List[str]] = None,
    closeout_id: str = "",
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.

    Parameters
    ----------
    goal, context, toolsets, role, model
        The dispatch-time task spec, captured verbatim for the rich
        completion block.
    session_key
        The gateway session_key (from ``tools.approval.get_current_session_key``)
        captured on the parent thread BEFORE dispatch, because the daemon
        worker thread won't carry the contextvar. Used to route the
        completion back to the originating session.
    parent_session_id
        The durable ``state.db`` session id of the parent agent that spawned
        the delegation. Carried on the completion event so the gateway can
        pin routing to the spawning session instead of recovering the latest
        ``ended_at IS NULL`` row for the peer tuple (#57498).
    runner
        Zero-arg callable that builds + runs the child and returns the same
        result dict ``_run_single_child`` produces. Runs on the worker thread.
    interrupt_fn
        Optional callable to signal the child to stop (used on shutdown /
        explicit cancel).
    max_async_children
        Concurrency cap. When at capacity the dispatch is REJECTED (the caller
        should fall back to sync or tell the user) rather than queued, so a
        runaway model can't pile up unbounded background work.

    Returns
    -------
    dict
        ``{"status": "dispatched", "delegation_id": ...}`` on success, or
        ``{"status": "rejected", "error": ...}`` when at capacity.
    """
    delegation_id = _new_delegation_id()
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "parent_session_id": parent_session_id,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "kind": str(kind or "delegation"),
        "delivery_pending": False,
        "cancel_requested": False,
        "cancel_reason": "",
        "cancel_requested_at": None,
        "interrupt_invoked": False,
        "origin_work_item_id": str(origin_work_item_id or "")[:240],
        "origin_run_generation": _normalized_generation(origin_run_generation),
        "origin_attempt_id": str(origin_attempt_id or "")[:240],
        "origin_attempt_order": _normalized_generation(origin_attempt_order),
        "origin_owner_pid": int(origin_owner_pid or os.getpid()),
        "origin_process_epoch": str(origin_process_epoch or "")[:240],
        "origin_scope_paths": list(origin_scope_paths or []),
        "closeout_id": str(closeout_id or "")[:240],
    }
    record.update(_capture_session_routing())
    # Capacity check and record insert under ONE lock hold — checking
    # active_count() separately would let two concurrent dispatches (e.g.
    # from different gateway sessions) both pass the check and exceed the cap.
    with _records_lock:
        running = sum(
            1
            for r in _records.values()
            if r.get("status") in {"running", "finalizing"}
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or run this task synchronously "
                    f"(background=false). Raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background subagents."
                ),
            }
        _records[delegation_id] = record

    try:
        executor = _get_executor(max_async_children)
    except Exception as exc:  # pragma: no cover - executor construction is rare
        with _records_lock:
            _records.pop(delegation_id, None)
        return {
            "status": "rejected",
            "error": f"Failed to initialize async delegation executor: {exc}",
        }

    registration_error = _register_required_async_dispatch(record)
    if registration_error:
        with _records_lock:
            _records.pop(delegation_id, None)
        return {
            "status": "rejected",
            "error": f"Failed to register required async dispatch: {registration_error}",
        }
    if not _is_durable_attempt_dispatch(record):
        try:
            _persist_dispatch(record)
        except Exception as exc:
            with _records_lock:
                _records.pop(delegation_id, None)
            return {
                "status": "rejected",
                "error": f"Failed to persist async delegation dispatch: {exc}",
            }

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        try:
            result = runner() or {}
            status = result.get("status") or "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation %s crashed", delegation_id)
            result = {
                "status": "error",
                "summary": None,
                "error": f"{type(exc).__name__}: {exc}",
                "api_calls": 0,
                "duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize(delegation_id, result, status)

    try:
        # Propagate the dispatching profile so the detached child resolves
        # get_hermes_home() under the right profile.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        if _is_durable_attempt_dispatch(record):
            failed_result = {
                "status": "submit_failed",
                "summary": "",
                "error": f"Failed to schedule async delegation: {exc}",
                "result": {
                    "success": False,
                    "status": "submit_failed",
                    "summary": "",
                    "error": f"Failed to schedule async delegation: {exc}",
                },
            }
            _complete_record(
                delegation_id,
                failed_result,
                "submit_failed",
                enqueue=False,
                submit_failure=True,
            )
        else:
            with _records_lock:
                _records.pop(delegation_id, None)
            _delete_durable_delegation(delegation_id)
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation: {exc}",
        }

    logger.info(
        "Dispatched async delegation %s (session_key=%s): %s",
        delegation_id, session_key or "<cli>", (goal or "")[:80],
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize(delegation_id: str, result: Dict[str, Any], status: str) -> None:
    """Mark a record complete and push the completion event onto the queue."""
    _complete_record(delegation_id, result, status, enqueue=True)


def _persist_cancelled_generic_record(
    record: Dict[str, Any], result: Dict[str, Any]
) -> None:
    """Keep non-attempt cancellation terminal evidence without queue delivery."""
    if _is_durable_attempt_dispatch(record):
        return
    cancelled_at = float(
        record.get("cancel_requested_at")
        or record.get("completed_at")
        or time.time()
    )
    event = {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "status": "cancelled",
        "completed_at": cancelled_at,
        "cancel_reason": record.get("cancel_reason", "session_stop"),
    }
    try:
        _persist_completion(event, result)
        _cancel_durable_completion_delivery(
            str(record.get("delegation_id") or ""),
            reason=str(record.get("cancel_reason") or "session_stop"),
            cancelled_at=cancelled_at,
        )
    except Exception:
        logger.debug(
            "Could not persist cancelled delegation %s",
            record.get("delegation_id"),
            exc_info=True,
        )


def _complete_record(
    delegation_id: str,
    result: Dict[str, Any],
    status: str,
    *,
    enqueue: bool,
    submit_failure: bool = False,
) -> bool:
    """Terminalize one record exactly once, durably before queue delivery."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("_terminalizing") or record.get("_terminalized"):
            return False
        record["_terminalizing"] = True
        # Keep the unit active until both durable ownership layers and queue
        # publication have completed. A daemon worker must not disappear in
        # the gap between marking terminal and committing its evidence.
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None  # drop the closure; child is done
        cancelled = bool(record.get("cancel_requested"))
        if cancelled:
            status = "cancelled"
            enqueue = False
        deterministic_result = result.get("result")
        if (
            record.get("kind") == "coding_worker"
            and isinstance(deterministic_result, dict)
        ):
            record["completion_success"] = bool(
                status in {"completed", "success"}
                and deterministic_result.get("success") is True
            )
        else:
            record["completion_success"] = status in {"completed", "success"}
        if cancelled:
            record["completion_success"] = False
            record["delivery_pending"] = False
        terminal_record = dict(record)
        terminal_record["status"] = status

    if cancelled:
        if _is_durable_attempt_dispatch(terminal_record):
            durable_accepted = False
        else:
            _persist_cancelled_generic_record(terminal_record, result)
            durable_accepted = True
    else:
        durable_accepted = _persist_required_async_terminal(
            terminal_record,
            result,
            status,
            submit_failure=submit_failure,
        )

    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return durable_accepted
        cancelled = cancelled or bool(record.get("cancel_requested"))
        if cancelled:
            status = "cancelled"
            enqueue = False
            durable_accepted = False if _is_durable_attempt_dispatch(record) else True
            record["completion_success"] = False
        record["required_async_terminal_accepted"] = durable_accepted
        record["delivery_pending"] = bool(
            enqueue
            and durable_accepted
            and _completion_requires_delivery_ack(record)
        )
        event_record = dict(record)

    if enqueue and durable_accepted:
        _push_completion_event(event_record, result, status)
    with _records_lock:
        record = _records.get(delegation_id)
        if record is not None:
            if record.get("cancel_requested"):
                status = "cancelled"
                record["completion_success"] = False
                record["delivery_pending"] = False
            record.pop("_terminalizing", None)
            record["_terminalized"] = True
            record["status"] = status
        _prune_completed_locked()
    return durable_accepted


def mark_async_delegation_running(delegation_id: str) -> bool:
    """Mark a registered required coding dispatch running before release."""
    with _records_lock:
        record = _records.get(str(delegation_id))
        if record is None or record.get("_terminalized"):
            return False
        snapshot = dict(record)
    if not _is_required_coding_dispatch(snapshot):
        return True
    try:
        persisted = _required_async_ledger().mark_required_async_dispatch_running(
            str(snapshot["origin_work_item_id"]),
            delegation_id=str(snapshot["delegation_id"]),
            generation=snapshot.get("origin_run_generation"),
            attempt_id=snapshot.get("origin_attempt_id"),
            attempt_order=snapshot.get("origin_attempt_order"),
            owner_pid=snapshot.get("origin_owner_pid"),
            process_epoch=snapshot.get("origin_process_epoch"),
            started_at=time.time(),
        )
    except Exception:
        logger.exception(
            "Could not mark required async dispatch %s running",
            snapshot.get("delegation_id"),
        )
        return False
    persisted_dispatch = _required_dispatch_state(
        persisted,
        str(snapshot.get("delegation_id") or ""),
    )
    if not persisted_dispatch or persisted_dispatch.get("state") != "running":
        logger.warning(
            "Required async running mutation for %s was rejected",
            snapshot.get("delegation_id"),
        )
        return False
    with _records_lock:
        current = _records.get(str(delegation_id))
        if current is not None:
            current["required_async_state"] = "running"
    return True


def terminalize_async_delegation(
    delegation_id: str,
    result: Dict[str, Any],
    status: str = "error",
    *,
    enqueue: bool = False,
) -> bool:
    """Terminalize a dispatch rejected after submit but before worker release."""
    return _complete_record(
        str(delegation_id),
        result,
        str(status or "error"),
        enqueue=enqueue,
    )


def _push_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> None:
    """Push a type='async_delegation' event onto the shared completion queue.

    Best-effort: a failure here must not crash the worker. Required coding
    results remain durable; advisory delegation results would be lost, so both
    cases log loudly with the appropriate recovery semantics.
    """
    if is_completion_cancelled(str(record.get("delegation_id") or "")):
        return
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        if _is_durable_attempt_dispatch(record):
            logger.error(
                "Async delegation %s finished but process_registry import failed; "
                "durable result remains available for reconciliation: %s",
                record.get("delegation_id"),
                exc,
            )
        else:
            logger.error(
                "Async delegation %s finished but process_registry import failed; "
                "result lost: %s",
                record.get("delegation_id"),
                exc,
            )
        return

    coding_event = result.get("_async_coding_worker")
    deterministic_result = result.get("result")
    is_coding_worker = (
        record.get("kind") == "coding_worker"
        and isinstance(coding_event, dict)
        and isinstance(deterministic_result, dict)
    )
    summary = (
        deterministic_result.get("summary")
        if is_coding_worker
        else result.get("summary")
    )
    error = (
        deterministic_result.get("error")
        if is_coding_worker
        else result.get("error")
    )
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()

    evt = {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        # session_key routes the completion back to the originating gateway
        # session; empty string => CLI (single-session) path.
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "goal": record.get("goal", ""),
        "context": record.get("context"),
        "toolsets": record.get("toolsets"),
        "role": record.get("role"),
        "model": result.get("model") or record.get("model"),
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": result.get("api_calls", 0),
        "duration_seconds": result.get(
            "duration_seconds", round(completed_at - dispatched_at, 2)
        ),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "exit_reason": result.get("exit_reason"),
        "live_transcript": result.get("live_transcript"),
        "origin_work_item_id": record.get("origin_work_item_id", ""),
        "origin_run_generation": record.get("origin_run_generation"),
        "origin_attempt_id": record.get("origin_attempt_id", ""),
        "origin_attempt_order": record.get("origin_attempt_order"),
        "closeout_id": (
            (
                deterministic_result.get("fable_git_result", {}).get("closeout_id")
                if isinstance(deterministic_result, dict)
                and isinstance(deterministic_result.get("fable_git_result"), dict)
                else ""
            )
            or record.get("closeout_id", "")
        ),
    }
    for field in (
        "platform",
        "chat_id",
        "thread_id",
        "user_id",
        "user_name",
        "message_id",
    ):
        if record.get(field):
            evt[field] = record[field]
    if is_coding_worker:
        evt.update(
            {
                "kind": "coding_worker",
                "result": deterministic_result,
                "task": coding_event.get("task") or record.get("goal", ""),
                "context_pack": coding_event.get("context_pack") or {},
                "worker_cwd": coding_event.get("worker_cwd") or "",
                "model_tier": coding_event.get("model_tier") or "default",
                "scope_paths": list(coding_event.get("scope_paths") or []),
                "worker_run": coding_event.get("worker_run") or {},
                "parallel_group": coding_event.get("parallel_group"),
            }
        )
        evt.update(
            {
                # Compatibility with queue consumers whose formatter predates
                # async-delegation event types (notably the TUI gateway).
                "session_id": record.get("delegation_id"),
                "command": "delegate_coding_task(background=true)",
                "exit_code": 0 if deterministic_result.get("success") else 1,
                "output": (
                    "Review this completed coding worker as a fresh turn. Trusted "
                    "post-processing has already run; verify deterministic evidence "
                    "and report the outcome.\n"
                    f"Original task: {coding_event.get('task') or record.get('goal', '')}\n"
                    "Context pack:\n"
                    + json.dumps(
                        coding_event.get("context_pack") or {},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\nDeterministic result JSON:\n"
                    + json.dumps(
                        deterministic_result,
                        ensure_ascii=False,
                        indent=2,
                    )
                ),
            }
        )
    else:
        evt.update(
            {
                "session_id": record.get("delegation_id"),
                "command": "delegate_task(background=true)",
                "exit_code": 0 if status in {"completed", "success"} else 1,
                "output": (
                    f"Original goal: {record.get('goal', '')}\n"
                    + (f"Context: {record.get('context')}\n" if record.get("context") else "")
                    + f"Status: {status}\n"
                    + str(summary or error or "No result text.")
                ),
            }
        )
    if not _is_durable_attempt_dispatch(record):
        _persist_completion(evt, result)
    if is_completion_cancelled(str(record.get("delegation_id") or "")):
        _cancel_durable_completion_delivery(
            str(record.get("delegation_id") or ""),
            reason=str(record.get("cancel_reason") or "session_stop"),
            cancelled_at=float(record.get("cancel_requested_at") or time.time()),
        )
        return
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        if _is_required_coding_dispatch(record):
            logger.error(
                "Async delegation %s: failed to enqueue completion event; "
                "durable result remains available for reconciliation: %s",
                record.get("delegation_id"),
                exc,
            )
        else:
            logger.error(
                "Async delegation %s: failed to enqueue completion event; "
                "result lost: %s",
                record.get("delegation_id"),
                exc,
            )


def dispatch_async_delegation_batch(
    *,
    goals: List[str],
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    origin_work_item_id: str = "",
    origin_run_generation: Optional[int] = None,
    origin_attempt_id: str = "",
    origin_attempt_order: Optional[int] = None,
    origin_owner_pid: Optional[int] = None,
    origin_process_epoch: str = "",
    read_only: bool = False,
    task_specs: Optional[List[Dict[str, Any]]] = None,
    delegation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch a WHOLE fan-out batch as ONE background unit.

    Unlike ``dispatch_async_delegation`` (which backs a single subagent),
    ``runner`` here runs the entire batch — it builds and joins on every child
    in parallel and returns the combined ``{"results": [...],
    "total_duration_seconds": N}`` dict that the synchronous path would have
    returned. We occupy ONE async slot for the whole batch (the in-batch
    parallelism is bounded separately by ``max_concurrent_children``), so a
    single ``delegate_task`` fan-out never exhausts the async pool by itself.

    When the batch finishes, a SINGLE completion event is pushed onto the
    shared ``process_registry.completion_queue`` carrying the full per-task
    ``results`` list, so the consolidated summaries re-enter the conversation
    as one message once every child is done — the chat is never blocked while
    they run.

    Returns ``{"status": "dispatched", "delegation_id": ...}`` on success or
    ``{"status": "rejected", "error": ...}`` when the async pool is at
    capacity.
    """
    delegation_id = delegation_id or _new_delegation_id()
    dispatched_at = time.time()
    n = len(goals)
    # A combined goal label for status listings / the completion header.
    combined_goal = (
        goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in goals)
    )
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": combined_goal,
        "goals": list(goals),
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "parent_session_id": parent_session_id,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "is_batch": True,
        "kind": "delegation",
        "delivery_pending": False,
        "cancel_requested": False,
        "cancel_reason": "",
        "cancel_requested_at": None,
        "interrupt_invoked": False,
        "origin_work_item_id": str(origin_work_item_id or "")[:240],
        "origin_run_generation": _normalized_generation(origin_run_generation),
        "origin_attempt_id": str(origin_attempt_id or "")[:240],
        "origin_attempt_order": _normalized_generation(origin_attempt_order),
        "origin_owner_pid": int(origin_owner_pid or os.getpid()),
        "origin_process_epoch": str(origin_process_epoch or "")[:240],
        "origin_scope_paths": [],
        "read_only": bool(read_only),
        "task_specs": [dict(item) for item in (task_specs or [])],
        "registration_evidence": {
            "advisory_results": [
                {
                    "goal": str(item.get("goal") or ""),
                    "status": "registered",
                }
                for item in (task_specs or [])
                if isinstance(item, dict)
            ]
        },
    }
    record.update(_capture_session_routing())
    with _records_lock:
        running = sum(
            1
            for r in _records.values()
            if r.get("status") in {"running", "finalizing"}
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background units."
                ),
            }
        _records[delegation_id] = record

    try:
        executor = _get_executor(max_async_children)
    except Exception as exc:
        with _records_lock:
            _records.pop(delegation_id, None)
        return {
            "status": "rejected",
            "error": f"Failed to initialize async delegation executor: {exc}",
        }
    registration_error = _register_required_async_dispatch(record)
    if registration_error:
        with _records_lock:
            _records.pop(delegation_id, None)
        return {
            "status": "rejected",
            "error": f"Failed to register durable async dispatch: {registration_error}",
        }
    if not _is_durable_attempt_dispatch(record):
        try:
            _persist_dispatch(record)
        except Exception as exc:
            with _records_lock:
                _records.pop(delegation_id, None)
            return {
                "status": "rejected",
                "error": f"Failed to persist async delegation batch: {exc}",
            }

    def _worker() -> None:
        combined: Dict[str, Any] = {}
        status = "error"
        try:
            combined = runner() or {}
            child_results = combined.get("results") or []
            successful = sum(
                1
                for result in child_results
                if isinstance(result, dict)
                and str(result.get("status") or "").strip().lower()
                in {"completed", "success"}
            )
            if not child_results or successful == 0:
                status = "error"
            elif successful != len(child_results):
                status = "partial"
            else:
                status = "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception("Async delegation batch %s crashed", delegation_id)
            combined = {
                "results": [],
                "error": f"{type(exc).__name__}: {exc}",
                "total_duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize_batch(delegation_id, combined, status)

    try:
        # Propagate the dispatching profile to the detached batch children.
        executor.submit(propagate_context_to_thread(_worker))
    except Exception as exc:  # pragma: no cover
        if _is_durable_attempt_dispatch(record):
            _complete_record(
                delegation_id,
                {
                    "status": "submit_failed",
                    "results": [],
                    "error": f"Failed to schedule async delegation batch: {exc}",
                },
                "submit_failed",
                enqueue=False,
                submit_failure=True,
            )
        else:
            with _records_lock:
                _records.pop(delegation_id, None)
            _delete_durable_delegation(delegation_id)
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation batch: {exc}",
        }

    logger.info(
        "Dispatched async delegation batch %s (%d task(s), session_key=%s)",
        delegation_id, n, session_key or "<cli>",
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize_batch(
    delegation_id: str, combined: Dict[str, Any], status: str
) -> None:
    """Durably terminalize a batch, then push one compatibility wakeup."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("_terminalized") or record.get("_terminalizing"):
            return
        record["_terminalizing"] = True
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None
        cancelled = bool(record.get("cancel_requested"))
        if cancelled:
            status = "cancelled"
        record["completion_success"] = (
            not cancelled and status in {"completed", "success"}
        )
        if cancelled:
            record["delivery_pending"] = False
        terminal_record = dict(record)
        terminal_record["status"] = status

    if cancelled:
        if _is_durable_attempt_dispatch(terminal_record):
            durable_accepted = False
        else:
            _persist_cancelled_generic_record(terminal_record, combined)
            durable_accepted = True
    else:
        durable_accepted = _persist_required_async_terminal(
            terminal_record,
            combined,
            status,
        )

    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        cancelled = cancelled or bool(record.get("cancel_requested"))
        if cancelled:
            status = "cancelled"
            durable_accepted = False if _is_durable_attempt_dispatch(record) else True
            record["completion_success"] = False
        record["required_async_terminal_accepted"] = durable_accepted
        record["delivery_pending"] = bool(
            not cancelled
            and durable_accepted
            and _completion_requires_delivery_ack(record)
        )
        event_record = dict(record)

    if cancelled or not durable_accepted:
        with _records_lock:
            record = _records.get(delegation_id)
            if record is not None:
                record.pop("_terminalizing", None)
                record["_terminalized"] = True
                record["status"] = status
            _prune_completed_locked()
        return

    if is_completion_cancelled(delegation_id):
        with _records_lock:
            record = _records.get(delegation_id)
            if record is not None:
                record.pop("_terminalizing", None)
                record["_terminalized"] = True
                record["status"] = "cancelled"
                record["delivery_pending"] = False
            _prune_completed_locked()
        return

    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s finished but process_registry import "
            "failed; result lost: %s",
            delegation_id, exc,
        )
        with _records_lock:
            record = _records.get(delegation_id)
            if record is not None:
                record.pop("_terminalizing", None)
                record["_terminalized"] = True
                record["status"] = status
            _prune_completed_locked()
        return

    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    evt = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_id": delegation_id,
        "command": "delegate_task(background=true, batch=true)",
        "exit_code": 0 if status == "completed" else 1,
        "session_key": event_record.get("session_key", ""),
        "origin_ui_session_id": event_record.get("origin_ui_session_id", ""),
        "parent_session_id": event_record.get("parent_session_id"),
        "goal": event_record.get("goal", ""),
        "goals": event_record.get("goals"),
        "context": event_record.get("context"),
        "toolsets": event_record.get("toolsets"),
        "role": event_record.get("role"),
        "model": event_record.get("model"),
        "status": status,
        "is_batch": True,
        # The full per-task results list — the formatter renders a
        # consolidated multi-task block from this.
        "results": combined.get("results") or [],
        # Per-task live transcript log paths (cache/delegation/live/...).
        # They persist after completion and double as the full-fidelity
        # operational record of each child's run.
        "live_transcripts": combined.get("live_transcripts"),
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "origin_work_item_id": event_record.get("origin_work_item_id", ""),
        "origin_run_generation": event_record.get("origin_run_generation"),
        "origin_attempt_id": event_record.get("origin_attempt_id", ""),
        "origin_attempt_order": event_record.get("origin_attempt_order"),
        "kind": "advisory",
        "read_only": bool(event_record.get("read_only")),
        "task_specs": event_record.get("task_specs") or [],
        "accounting": combined.get("accounting"),
    }
    for field in (
        "platform",
        "chat_id",
        "thread_id",
        "user_id",
        "user_name",
        "message_id",
    ):
        if event_record.get(field):
            evt[field] = event_record[field]
    evt["output"] = (
        f"Original goals: {json.dumps(event_record.get('goals') or [], ensure_ascii=False)}\n"
        f"Status: {status}\nResults:\n"
        + json.dumps(evt["results"], ensure_ascii=False, indent=2)
    )
    if not _is_durable_attempt_dispatch(event_record):
        _persist_completion(evt, combined)
    if is_completion_cancelled(delegation_id):
        _cancel_durable_completion_delivery(
            delegation_id,
            reason=str(event_record.get("cancel_reason") or "session_stop"),
            cancelled_at=float(
                event_record.get("cancel_requested_at") or time.time()
            ),
        )
        with _records_lock:
            record = _records.get(delegation_id)
            if record is not None:
                record.pop("_terminalizing", None)
                record["_terminalized"] = True
                record["status"] = "cancelled"
                record["delivery_pending"] = False
                record["completion_success"] = False
            _prune_completed_locked()
        return
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        if _is_durable_attempt_dispatch(event_record):
            logger.error(
                "Async delegation batch %s: failed to enqueue completion event; "
                "durable result remains available for reconciliation: %s",
                delegation_id,
                exc,
            )
        else:
            logger.error(
                "Async delegation batch %s: failed to enqueue completion event; "
                "result lost: %s",
                delegation_id,
                exc,
            )
    finally:
        with _records_lock:
            record = _records.get(delegation_id)
            if record is not None:
                if record.get("cancel_requested"):
                    status = "cancelled"
                    record["completion_success"] = False
                    record["delivery_pending"] = False
                record.pop("_terminalizing", None)
                record["_terminalized"] = True
                record["status"] = status
            _prune_completed_locked()


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed).

    Safe to call from any thread. Excludes the non-serialisable interrupt_fn.
    """
    with _records_lock:
        return [
            {k: v for k, v in r.items() if k != "interrupt_fn"}
            for r in _records.values()
        ]


def discard_async_delegation(delegation_id: str) -> bool:
    """Drop a not-yet-started record without emitting a completion event."""
    with _records_lock:
        return _records.pop(str(delegation_id), None) is not None


def mark_completion_delivered(delegation_id: str) -> bool:
    """Acknowledge delivery in memory and in the generic durable store."""
    memory_updated = False
    with _records_lock:
        record = _records.get(str(delegation_id))
        if record is not None:
            record["delivery_pending"] = False
            _prune_completed_locked()
            memory_updated = True
    try:
        durable_updated = _mark_durable_completion_delivered(str(delegation_id))
    except Exception:
        durable_updated = False
    return memory_updated or durable_updated


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every running async delegation to stop. Returns how many.

    Used on ``/stop`` and gateway shutdown so a dangling background subagent
    can't keep burning tokens with no one listening. The child still emits a
    completion event (status='interrupted') via the normal finalize path.
    """
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values() if r.get("status") == "running"
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_all: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info("Interrupted %d async delegation(s) (%s)", count, reason)
    return count


def _routing_scope_matches(
    record: Dict[str, Any], routing_scope: Optional[Dict[str, str]]
) -> bool:
    if not routing_scope:
        return False
    for field in ("platform", "chat_id", "thread_id"):
        expected = str(routing_scope.get(field) or "").strip()
        if expected and str(record.get(field) or "").strip() != expected:
            return False
    return bool(
        str(routing_scope.get("platform") or "").strip()
        and str(routing_scope.get("chat_id") or "").strip()
    )


def session_keys_for_routing_scope(
    *,
    platform: str,
    chat_id: str,
    thread_id: str = "",
    limit: int = 32,
) -> List[str]:
    """Return bounded live/delivery-pending owners for one chat/thread scope."""
    scope = {
        "platform": str(platform or "").strip(),
        "chat_id": str(chat_id or "").strip(),
        "thread_id": str(thread_id or "").strip(),
    }
    if not scope["platform"] or not scope["chat_id"]:
        return []
    bounded = max(1, min(int(limit or 32), 64))
    with _records_lock:
        keys = {
            str(record.get("session_key") or "")
            for record in _records.values()
            if (record.get("status") in {"running", "finalizing"}
                or bool(record.get("delivery_pending")))
            and not record.get("cancel_requested")
            and _routing_scope_matches(record, scope)
            and str(record.get("session_key") or "")
        }
    return sorted(keys)[:bounded]


def cancel_session(
    session_key: str = "",
    *,
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
    routing_scope: Optional[Dict[str, str]] = None,
    kind: Optional[str] = None,
    origin_work_item_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    reason: str = "session_stop",
) -> Dict[str, Any]:
    """Idempotently cancel every matched async unit before signaling workers.

    Selectors are additive ownership proofs: a record may match a captured
    session identity or the bounded platform/chat/thread routing scope. Newly
    matched records are marked under ``_records_lock`` before callbacks are
    copied, so repeated callers cannot signal the same child twice.
    """
    normalized_session = str(session_key or "")
    normalized_ui = str(origin_ui_session_id or "")
    normalized_parent = str(parent_session_id or "")
    normalized_kind = str(kind or "").strip()
    normalized_work_id = str(origin_work_item_id or "").strip()
    normalized_attempt_id = str(attempt_id or "").strip()
    if not any((normalized_session, normalized_ui, normalized_parent, routing_scope)):
        return {
            "matched": 0,
            "newly_cancelled": 0,
            "callbacks_invoked": 0,
            "callbacks_failed": 0,
            "durable_cancelled": 0,
            "durable_completions_cancelled": 0,
            "durable_completion_ids": [],
            "by_kind": {},
            "failed_ids": [],
        }

    cancelled_at = time.time()
    durable_completions_cancelled = 0
    durable_completion_ids: List[str] = []
    if not any((normalized_kind, normalized_work_id, normalized_attempt_id, routing_scope)):
        try:
            (
                durable_completions_cancelled,
                durable_completion_ids,
            ) = _cancel_durable_completions_for_identity(
                session_key=normalized_session,
                origin_ui_session_id=normalized_ui,
                parent_session_id=normalized_parent,
                reason=str(reason or "session_stop")[:240],
                cancelled_at=cancelled_at,
            )
        except Exception:
            logger.exception("Could not durably suppress session completion delivery")
    targets: List[Dict[str, Any]] = []
    matched = 0
    by_kind: Dict[str, int] = {}
    with _records_lock:
        for record in _records.values():
            active_or_pending = (
                record.get("status") in {"running", "finalizing"}
                or bool(record.get("delivery_pending"))
            )
            if not active_or_pending:
                continue
            owns = bool(
                (normalized_session and str(record.get("session_key") or "") == normalized_session)
                or (normalized_ui and str(record.get("origin_ui_session_id") or "") == normalized_ui)
                or (normalized_parent and str(record.get("parent_session_id") or "") == normalized_parent)
                or _routing_scope_matches(record, routing_scope)
            )
            if not owns:
                continue
            if normalized_kind and record.get("kind", "delegation") != normalized_kind:
                continue
            if normalized_work_id and str(record.get("origin_work_item_id") or "") != normalized_work_id:
                continue
            if normalized_attempt_id and str(record.get("origin_attempt_id") or "") != normalized_attempt_id:
                continue
            matched += 1
            kind_name = str(record.get("kind") or "delegation")[:64]
            by_kind[kind_name] = by_kind.get(kind_name, 0) + 1
            if record.get("cancel_requested"):
                continue
            record["cancel_requested"] = True
            record["cancel_reason"] = str(reason or "session_stop")[:240]
            record["cancel_requested_at"] = cancelled_at
            record["delivery_pending"] = False
            callback = record.get("interrupt_fn")
            invoke_callback = callable(callback) and not record.get("interrupt_invoked")
            if invoke_callback:
                record["interrupt_invoked"] = True
            target = dict(record)
            target["_callback"] = callback if invoke_callback else None
            targets.append(target)

    durable_cancelled = 0
    callbacks_invoked = 0
    callbacks_failed = 0
    failed_ids: List[str] = []
    for target in targets:
        delegation_id = str(target.get("delegation_id") or "")
        try:
            _cancel_durable_completion_delivery(
                delegation_id,
                reason=str(reason or "session_stop")[:240],
                cancelled_at=cancelled_at,
            )
        except Exception:
            logger.debug(
                "Could not fence completion delivery for %s",
                delegation_id,
                exc_info=True,
            )
        if _is_durable_attempt_dispatch(target):
            try:
                cancelled = _required_async_ledger().cancel_required_async_dispatch(
                    str(target["origin_work_item_id"]),
                    delegation_id=delegation_id,
                    generation=target.get("origin_run_generation"),
                    attempt_id=target.get("origin_attempt_id"),
                    attempt_order=target.get("origin_attempt_order"),
                    reason=str(reason or "session_stop")[:240],
                    status="cancelled",
                    cancelled_at=cancelled_at,
                )
                cancelled_dispatch = _required_dispatch_state(cancelled, delegation_id)
                if cancelled_dispatch and cancelled_dispatch.get("state") == "cancelled":
                    durable_cancelled += 1
                    with _records_lock:
                        current = _records.get(delegation_id)
                        if current is not None:
                            current["required_async_state"] = "cancelled"
                elif len(failed_ids) < 10:
                    failed_ids.append(delegation_id)
            except Exception:
                logger.exception(
                    "Could not durably cancel required async dispatch %s",
                    delegation_id,
                )
                if len(failed_ids) < 10:
                    failed_ids.append(delegation_id)

        callback = target.get("_callback")
        if callable(callback):
            try:
                callback()
                callbacks_invoked += 1
            except Exception:
                callbacks_failed += 1
                logger.debug(
                    "cancel_session: %s interrupt failed",
                    delegation_id,
                    exc_info=True,
                )
                if len(failed_ids) < 10 and delegation_id not in failed_ids:
                    failed_ids.append(delegation_id)

    bounded_by_kind = dict(sorted(by_kind.items())[:12])
    newly_cancelled_ids = {
        str(target.get("delegation_id") or "")
        for target in targets
        if str(target.get("delegation_id") or "")
    }
    newly_cancelled_ids.update(durable_completion_ids)
    return {
        "matched": matched,
        "newly_cancelled": len(newly_cancelled_ids),
        "callbacks_invoked": callbacks_invoked,
        "callbacks_failed": callbacks_failed,
        "durable_cancelled": durable_cancelled,
        "durable_completions_cancelled": durable_completions_cancelled,
        "durable_completion_ids": durable_completion_ids[:10],
        "by_kind": bounded_by_kind,
        "failed_ids": failed_ids[:10],
    }


def interrupt_session(
    session_key: str,
    *,
    kind: Optional[str] = None,
    origin_work_item_id: Optional[str] = None,
    attempt_id: Optional[str] = None,
    reason: str = "session_stop",
) -> Dict[str, Any]:
    """Compatibility wrapper for the canonical session cancellation API."""
    result = cancel_session(
        session_key,
        kind=kind,
        origin_work_item_id=origin_work_item_id,
        attempt_id=attempt_id,
        reason=reason,
    )
    return {
        "matched": result["matched"],
        "durable_cancelled": result["durable_cancelled"],
        "interrupted": result["callbacks_invoked"],
        "failed_ids": result["failed_ids"],
    }


def interrupt_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
    reason: str = "session_end",
) -> int:
    """Signal running async delegations owned by ONE session to stop.

    A delegation's lifecycle is bound to the session that spawned it: when
    that session ends, its in-flight background subagents must end with it —
    a completed orphan would otherwise sit on the shared completion queue
    with no live owner, either leaking into another chat or burning tokens
    with no one listening (#55578).

    Selectors (any matching field claims the record):
    - ``origin_ui_session_id``: the live TUI tab/window that commissioned it.
    - ``session_key``: the durable routing key captured at dispatch.
    - ``parent_session_id``: the spawning agent's durable session-db id —
      the right selector for gateway chats, whose ``session_key`` (the
      platform conversation key) SURVIVES a ``/new`` reset while the
      session id rotates.

    Returns how many were interrupted.
    """
    result = cancel_session(
        session_key,
        origin_ui_session_id=origin_ui_session_id,
        parent_session_id=parent_session_id,
        reason=reason,
    )
    count = int(result.get("callbacks_invoked") or 0)
    if result.get("newly_cancelled"):
        logger.info(
            "Cancelled %d async delegation(s) for ending session (%s)",
            result["newly_cancelled"], reason,
        )
    return count


def _reset_for_tests() -> None:
    """Test-only: clear all state and tear down the executor."""
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    with _records_lock:
        _records.clear()
    try:
        from tools import coding_worker_tool

        with coding_worker_tool._MUTATION_RESERVATIONS_LOCK:
            coding_worker_tool._MUTATION_RESERVATIONS.clear()
        with coding_worker_tool._PARALLEL_WORKER_RESERVATIONS_LOCK:
            coding_worker_tool._PARALLEL_WORKER_RESERVATIONS.clear()
    except Exception:
        pass
