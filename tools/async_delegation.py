#!/usr/bin/env python3
"""
Async (background) delegation registry.

Backs ``delegate_task(background=true)`` and background coding workers: the
parent agent dispatches trusted work on a module-level daemon executor and
returns a handle immediately, so the user and the model can keep working.

When the child finishes, a completion event is pushed onto the SHARED
``process_registry.completion_queue`` with ``type="async_delegation"``. The
CLI (``cli.py`` process_loop) and gateway (``_run_process_watcher`` /
``completion_queue`` drain) already poll that queue while the agent is idle
and forge a fresh user/internal turn from each event. We deliberately reuse
that rail rather than reaching into a running agent loop:

  - completions surface as a NEW turn when the agent is idle, never spliced
    between a tool result and an assistant message. That keeps strict
    message-role alternation legal and the prompt cache intact (hard
    invariant: never mutate past context).
  - we inherit the queue's de-dup, crash-recovery checkpoint, and the
    existing CLI + gateway drain wiring for free — no new drain loops in the
    two largest files in the repo.

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
import threading
import time
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures.thread import _worker
from typing import Any, Callable, Dict, List, Optional

from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)


class _DaemonThreadPoolExecutor(DaemonThreadPoolExecutor):
    """Python-version-compatible daemon executor for detached delegations.

    ``tools.daemon_pool`` mirrors the CPython 3.8-3.13 private worker API.  On
    Python 3.14 the worker signature moved to a worker-context object, so keep
    this rail compatible locally without changing the shared executor used by
    unrelated subsystems.
    """

    def _adjust_thread_count(self) -> None:
        if not hasattr(self, "_create_worker_context"):
            return super()._adjust_thread_count()
        if self._idle_semaphore.acquire(timeout=0):
            return

        def weakref_cb(_, q=self._work_queue):
            q.put(None)

        num_threads = len(self._threads)
        if num_threads >= self._max_workers:
            return
        thread_name = "%s_%d" % (self._thread_name_prefix or self, num_threads)
        thread = threading.Thread(
            name=thread_name,
            target=_worker,
            args=(
                weakref.ref(self, weakref_cb),
                self._create_worker_context(),
                self._work_queue,
            ),
            daemon=True,
        )
        thread.start()
        self._threads.add(thread)


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
            _executor = _DaemonThreadPoolExecutor(
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
            if record.get("status") == "running"
            and (kind is None or record.get("kind", "delegation") == kind)
            and (session_key is None or record.get("session_key", "") == session_key)
        )


def pending_count(
    *,
    kind: Optional[str] = None,
    session_key: Optional[str] = None,
    exclude_delegation_id: Optional[str] = None,
) -> int:
    """Count running or completed-but-not-delivered background units."""
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
        )


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
    runner: Callable[[], Dict[str, Any]],
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    kind: str = "delegation",
    origin_work_item_id: str = "",
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
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "kind": str(kind or "delegation"),
        "delivery_pending": False,
        "origin_work_item_id": str(origin_work_item_id or "")[:240],
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
            if r.get("status") == "running"
            and r.get("kind", "delegation") == record["kind"]
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

    executor = _get_executor(max_async_children)

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
        with _records_lock:
            _records.pop(delegation_id, None)
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
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = status
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None  # drop the closure; child is done
        record["delivery_pending"] = _completion_requires_delivery_ack(record)
        # Snapshot fields needed for the event while holding the lock.
        event_record = dict(record)
        _prune_completed_locked()

    _push_completion_event(event_record, result, status)


def _push_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> None:
    """Push a type='async_delegation' event onto the shared completion queue.

    Best-effort: a failure here must not crash the worker, but it WOULD mean a
    silently-lost result, so we log loudly.
    """
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s finished but process_registry import failed; "
            "result lost: %s",
            record.get("delegation_id"), exc,
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
        "origin_work_item_id": record.get("origin_work_item_id", ""),
        "closeout_id": record.get("closeout_id", ""),
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
                "worker_tier": coding_event.get("worker_tier") or "default",
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
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s: failed to enqueue completion event; "
            "result lost: %s",
            record.get("delegation_id"), exc,
        )


def dispatch_async_delegation_batch(
    *,
    goals: List[str],
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    runner: Callable[[], Dict[str, Any]],
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    origin_work_item_id: str = "",
    read_only: bool = False,
    task_specs: Optional[List[Dict[str, Any]]] = None,
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
    delegation_id = _new_delegation_id()
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
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "is_batch": True,
        "kind": "delegation",
        "delivery_pending": False,
        "origin_work_item_id": str(origin_work_item_id or "")[:240],
        "read_only": bool(read_only),
        "task_specs": [dict(item) for item in (task_specs or [])],
    }
    record.update(_capture_session_routing())
    with _records_lock:
        running = sum(
            1
            for r in _records.values()
            if r.get("status") == "running"
            and r.get("kind", "delegation") == record["kind"]
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

    executor = _get_executor(max_async_children)

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
        with _records_lock:
            _records.pop(delegation_id, None)
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
    """Mark a batch record complete and push ONE combined completion event."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = status
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None
        record["delivery_pending"] = _completion_requires_delivery_ack(record)
        event_record = dict(record)
        _prune_completed_locked()

    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s finished but process_registry import "
            "failed; result lost: %s",
            delegation_id, exc,
        )
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
        "error": combined.get("error"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "origin_work_item_id": event_record.get("origin_work_item_id", ""),
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
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s: failed to enqueue completion event; "
            "result lost: %s",
            delegation_id, exc,
        )


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
    """Clear the delivery-pending marker after the forged turn is accepted."""
    with _records_lock:
        record = _records.get(str(delegation_id))
        if record is None:
            return False
        record["delivery_pending"] = False
        _prune_completed_locked()
        return True


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
