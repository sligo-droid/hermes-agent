#!/usr/bin/env python3
"""
Delegate Tool -- Subagent Architecture

Spawns child AIAgent instances with isolated context, inherited toolsets,
and their own terminal sessions. Supports single-task and batch (parallel)
modes. Top-level model calls run in the background; orchestrator children
wait for their own workers so they can synthesize the results.

Each child gets:
  - A fresh conversation (no parent history)
  - Its own task_id (own terminal session, file ops cache)
  - The parent's toolsets, with child-only blocked tools stripped
  - A focused system prompt built from the delegated goal + context

The parent's context only sees the delegation call and the summary result,
never the child's intermediate tool calls or reasoning.
"""

import enum
import copy
import json
import logging

logger = logging.getLogger(__name__)
import os
import sys
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
)
from typing import Any, Dict, List, Optional

from hermes_constants import VALID_REASONING_EFFORTS
from toolsets import TOOLSETS
from agent.runtime_capabilities import RuntimeMode, normalize_runtime_mode

# Sentinel value used by the runtime provider system for providers that are
# not natively known (named custom providers, third-party aggregators, etc.).
# Must match hermes_cli.runtime_provider.RUNTIME_PROVIDER_TYPE_CUSTOM.
_RUNTIME_PROVIDER_CUSTOM = "custom"
from tools import file_state
from tools.terminal_tool import set_approval_callback as _set_subagent_approval_cb
from agent.worker_runs import append_turn_worker_run
from utils import base_url_hostname, is_truthy_value


# Tools that children must never have access to
DELEGATE_BLOCKED_TOOLS = frozenset(
    [
        "delegate_task",  # no recursive delegation
        "clarify",  # no user interaction
        "memory",  # no writes to shared MEMORY.md
        "send_message",  # no cross-platform side effects
        "execute_code",  # children should reason step-by-step, not write scripts
        "cronjob",  # no scheduling more work in the parent's name
    ]
)

# Delegated workers must never be offered the post-merge canonical-checkout
# capability.  It remains part of the top-level core schemas, but is registered
# in this dedicated toolset so the normal enabled/disabled-toolset filtering can
# remove it even when a child inherits a composite ``hermes-*`` toolset.
_DELEGATE_DISABLED_TOOLSETS = frozenset(
    {
        "canonical_sync",
        # General delegates never receive the raw coding-worker tool. Trusted
        # nested mutation, when explicitly granted, goes through the dedicated
        # root-owned broker toolset instead.
        "coding_worker_raw",
        "delegated_coding_broker",
    }
)


# ---------------------------------------------------------------------------
# Subagent approval callbacks
# ---------------------------------------------------------------------------
# Subagents run inside a ThreadPoolExecutor worker. The CLI's interactive
# approval callback is stored in tools/terminal_tool.py's threading.local(),
# so worker threads do NOT inherit it. Without a callback,
# prompt_dangerous_approval() falls back to input() from the worker thread,
# which deadlocks against the parent's prompt_toolkit TUI that owns stdin.
#
# Fix: install a non-interactive callback into every subagent worker thread
# via ThreadPoolExecutor(initializer=_set_subagent_approval_cb, initargs=(cb,)).
# The callback is chosen by the `delegation.subagent_auto_approve` config:
#   false (default) → _subagent_auto_deny (safe; matches leaf tool blocklist)
#   true            → _subagent_auto_approve (opt-in YOLO for cron/batch)
# Both emit a logger.warning for audit; gateway sessions are unaffected
# because they resolve approvals via tools/approval.py's per-session queue,
# not through these TLS callbacks.
def _subagent_auto_deny(command: str, description: str, **kwargs) -> str:
    """Auto-deny dangerous commands in subagent threads (safe default).

    Returns 'deny' so the subagent sees a refusal it can recover from, and
    never calls input() (which would deadlock the parent TUI).
    """
    logger.warning(
        "Subagent auto-denied dangerous command: %s (%s). "
        "Set delegation.subagent_auto_approve: true to allow.",
        command, description,
    )
    return "deny"


def _subagent_auto_approve(command: str, description: str, **kwargs) -> str:
    """Auto-approve dangerous commands in subagent threads (opt-in YOLO).

    Only installed when delegation.subagent_auto_approve=true. Returns 'once'
    so the subagent proceeds without blocking the parent UI.
    """
    logger.warning(
        "Subagent auto-approved dangerous command: %s (%s)",
        command, description,
    )
    return "once"


def _get_subagent_approval_callback():
    """Return the callback to install into subagent worker threads.

    Config key: delegation.subagent_auto_approve (bool, default False).
    Reads via the same _load_config() path as the rest of delegate_task so
    priority is config.yaml > (no env override for this knob) > default.
    """
    cfg = _load_config()
    val = cfg.get("subagent_auto_approve", False)
    if is_truthy_value(val):
        return _subagent_auto_approve
    return _subagent_auto_deny

# Build a description fragment listing toolsets available for subagents.
# Excludes toolsets where ALL tools are blocked, composite/platform toolsets
# (hermes-* prefixed), and scenario toolsets.
#
# NOTE: "delegation" is in this exclusion set so the subagent-facing
# capability hint string (_TOOLSET_LIST_STR) doesn't advertise it as a
# toolset to request explicitly — the correct mechanism for nested
# delegation is role='orchestrator', which re-adds "delegation" in
# _build_child_agent regardless of this exclusion.
_EXCLUDED_TOOLSET_NAMES = frozenset({"debugging", "safe", "delegation", "moa", "rl"})
_SUBAGENT_TOOLSETS = sorted(
    name
    for name, defn in TOOLSETS.items()
    if name not in _EXCLUDED_TOOLSET_NAMES
    and not name.startswith("hermes-")
    and not all(t in DELEGATE_BLOCKED_TOOLS for t in defn.get("tools", []))
)
_TOOLSET_LIST_STR = ", ".join(f"'{n}'" for n in _SUBAGENT_TOOLSETS)

_DEFAULT_MAX_CONCURRENT_CHILDREN = 3
MAX_DEPTH = 1  # flat by default: parent (0) -> child (1); grandchild rejected unless max_spawn_depth raised.
# Configurable depth cap consulted by _get_max_spawn_depth; MAX_DEPTH
# stays as the default fallback and is still the symbol tests import.
_MIN_SPAWN_DEPTH = 1
_MAX_SPAWN_DEPTH_CAP = 3


def _interpreter_shutdown_in_progress() -> bool:
    if sys.is_finalizing():
        return True
    try:
        import concurrent.futures.thread as _futures_thread

        return bool(getattr(_futures_thread, "_shutdown", False))
    except Exception:
        return False


def _is_interpreter_shutdown_error(exc: BaseException) -> bool:
    return isinstance(exc, RuntimeError) and "interpreter shutdown" in str(exc).lower()


def _delegation_shutdown_message() -> str:
    return "Delegation is unavailable because the Python interpreter is shutting down."


# ---------------------------------------------------------------------------
# Runtime state: pause flag + active subagent registry
#
# Consumed by the TUI observability layer (overlay/control surface) and the
# gateway RPCs `delegation.pause`, `delegation.status`, `subagent.interrupt`.
# Kept module-level so they span every delegate_task invocation in the
# process, including nested orchestrator -> worker chains.
# ---------------------------------------------------------------------------

_spawn_pause_lock = threading.Lock()
_spawn_paused: bool = False

_active_subagents_lock = threading.Lock()
# subagent_id -> mutable record tracking the live child agent.  Stays only
# for the lifetime of the run; _run_single_child is the owner.
_active_subagents: Dict[str, Dict[str, Any]] = {}
_ASYNC_HANDOFF_MAX_AGE_SECONDS = 3600.0
_coding_broker_locks: Dict[str, threading.Lock] = {}
_coding_broker_locks_guard = threading.Lock()


def _coding_broker_lock(workspace: str) -> threading.Lock:
    key = os.path.abspath(os.path.expanduser(str(workspace or "")))
    with _coding_broker_locks_guard:
        return _coding_broker_locks.setdefault(key, threading.Lock())


def set_spawn_paused(paused: bool) -> bool:
    """Globally block/unblock new delegate_task spawns.

    Active children keep running; only NEW calls to delegate_task fail fast
    with a "spawning paused" error until unblocked.  Returns the new state.
    """
    global _spawn_paused
    with _spawn_pause_lock:
        _spawn_paused = bool(paused)
        return _spawn_paused


def is_spawn_paused() -> bool:
    with _spawn_pause_lock:
        return _spawn_paused


def _register_subagent(record: Dict[str, Any]) -> None:
    sid = record.get("subagent_id")
    if not sid:
        return
    with _active_subagents_lock:
        _active_subagents[sid] = record


def _unregister_subagent(subagent_id: str) -> None:
    with _active_subagents_lock:
        _active_subagents.pop(subagent_id, None)


def interrupt_subagent(subagent_id: str) -> bool:
    """Request that a single running subagent stop at its next iteration boundary.

    Does not hard-kill the worker thread (Python can't); sets the child's
    interrupt flag which propagates to in-flight tools and recurses into
    grandchildren via AIAgent.interrupt().  Returns True if a matching
    subagent was found.
    """
    with _active_subagents_lock:
        record = _active_subagents.get(subagent_id)
    if not record:
        return False
    agent = record.get("agent")
    if agent is None:
        return False
    try:
        agent.interrupt(f"Interrupted via TUI ({subagent_id})")
    except Exception as exc:
        logger.debug("interrupt_subagent(%s) failed: %s", subagent_id, exc)
        return False
    return True


def list_active_subagents() -> List[Dict[str, Any]]:
    """Snapshot of the currently running subagent tree.

    Each record: {subagent_id, parent_id, depth, goal, model, started_at,
    tool_count, status}.  Safe to call from any thread — returns a copy.
    """
    with _active_subagents_lock:
        return [
            {k: v for k, v in r.items() if k != "agent"}
            for r in _active_subagents.values()
        ]


def _extract_output_tail(
    result: Dict[str, Any],
    *,
    max_entries: int = 12,
    max_chars: int = 8000,
) -> List[Dict[str, Any]]:
    """Pull the last N tool-call results from a child's conversation.

    Powers the overlay's "Output" section — the cc-swarm-parity feature.
    We reuse the same messages list the trajectory saver walks, taking
    only the tail to keep event payloads small.  Each entry is
    ``{tool, preview, is_error}``.
    """
    messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(messages, list):
        return []

    # Walk in reverse to build a tail; stop when we have enough.
    tail: List[Dict[str, Any]] = []
    pending_call_by_id: Dict[str, str] = {}

    # First pass (forward): build tool_call_id -> tool_name map
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id")
                fn = tc.get("function") or {}
                if tc_id:
                    pending_call_by_id[tc_id] = str(fn.get("name") or "tool")

    # Second pass (reverse): pick tool results, newest first
    from agent.message_content import flatten_message_text

    for msg in reversed(messages):
        if len(tail) >= max_entries:
            break
        if not isinstance(msg, dict) or msg.get("role") != "tool":
            continue
        content = flatten_message_text(msg.get("content"))
        is_error = _looks_like_error_output(content)
        tool_name = pending_call_by_id.get(msg.get("tool_call_id") or "", "tool")
        # Preserve line structure so the overlay's wrapped scroll region can
        # show real output rather than a whitespace-collapsed blob. We still
        # cap the payload size to keep events bounded.
        preview = content[:max_chars]
        tail.append({"tool": tool_name, "preview": preview, "is_error": is_error})

    tail.reverse()  # restore chronological order for display
    return tail


def _looks_like_error_output(content: str) -> bool:
    """Conservative stderr/error detector for tool-result previews.

    The old heuristic flagged any preview containing the substring "error",
    which painted perfectly normal terminal/json output red.  We now only
    mark output as an error when there is stronger evidence:
      - structured JSON with an ``error`` key
      - structured JSON with ``status`` of error/failed
      - first line starts with a classic error marker
    """
    if not content:
        return False

    head = content.lstrip()
    if head.startswith("{") or head.startswith("["):
        try:
            parsed = json.loads(content)
            if isinstance(parsed, dict):
                if parsed.get("error"):
                    return True
                status = str(parsed.get("status") or "").strip().lower()
                if status in {"error", "failed", "failure", "timeout"}:
                    return True
        except Exception:
            pass

    first = content.splitlines()[0].strip().lower() if content.splitlines() else ""
    return (
        first.startswith("error:")
        or first.startswith("failed:")
        or first.startswith("traceback ")
        or first.startswith("exception:")
    )


def _looks_like_provider_failure_summary(summary: str) -> bool:
    """Detect framework/provider retry-exhaustion summaries, not model prose."""
    if not isinstance(summary, str):
        return False
    text = summary.strip().lower()
    return (
        text.startswith("api call failed after ")
        or text.startswith("api failed after ")
        or text.startswith("provider call failed after ")
    ) and " retr" in text


def _delegation_binding(root: Any, workspace_hint: Optional[str] = None) -> Dict[str, str]:
    """Capture the root identity that makes a structured handoff current."""
    workspace = str(workspace_hint or _resolve_workspace_hint(root) or "").strip()
    if workspace:
        workspace = os.path.realpath(os.path.abspath(os.path.expanduser(workspace)))
    repository_root = ""
    if workspace:
        try:
            from tools.coding_worker_tool import _reservation_root

            repository_root = str(_reservation_root(workspace) or "")
        except Exception:
            repository_root = workspace
    return {
        "session_key": str(
            getattr(root, "gateway_session_key", "")
            or getattr(root, "session_key", "")
            or ""
        ),
        "session_id": str(getattr(root, "session_id", "") or ""),
        "turn_id": str(
            getattr(root, "_current_turn_id", "")
            or getattr(root, "_current_task_id", "")
            or ""
        ),
        "work_item_id": str(
            getattr(root, "_origin_work_item_id", "")
            or getattr(root, "work_item_id", "")
            or ""
        ),
        "workspace": workspace,
        "repository_root": repository_root,
    }


_HANDOFF_SKIP = object()


def _copy_handoff_value(value: Any) -> Any:
    """Copy JSON-like handoff state without traversing live runtime objects.

    Delegation agents own locks, credential pools, clients, and other process
    handles that cannot be deep-copied. Structured handoffs are durable data,
    so retain only scalar/list/dict state that can safely cross that boundary.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        copied: Dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            durable_item = _copy_handoff_value(item)
            if durable_item is not _HANDOFF_SKIP:
                copied[key] = durable_item
        return copied
    if isinstance(value, (list, tuple)):
        copied_items = []
        for item in value:
            durable_item = _copy_handoff_value(item)
            if durable_item is not _HANDOFF_SKIP:
                copied_items.append(durable_item)
        return copied_items
    return _HANDOFF_SKIP


def _copy_handoff(handoff: Dict[str, Any]) -> Dict[str, Any]:
    copied = _copy_handoff_value(handoff)
    return copied if isinstance(copied, dict) else {}


def _register_structured_handoff(
    *,
    child: Any,
    parent_agent: Any,
    goal: str,
    entry: Dict[str, Any],
    files_read: list[str],
    files_written: list[str],
) -> Dict[str, Any]:
    """Store deterministic delegation evidence on the root orchestrator."""
    root = getattr(child, "_delegate_root_agent", None) or getattr(
        parent_agent, "_delegate_root_agent", parent_agent
    )
    handoff_id = f"handoff_{getattr(child, '_subagent_id', '') or os.urandom(4).hex()}"
    broker_results = []
    broker_registry = getattr(root, "_brokered_coding_results", None)
    if isinstance(broker_registry, dict):
        subagent_id = str(getattr(child, "_subagent_id", "") or "")
        for value in broker_registry.values():
            if (
                isinstance(value, dict)
                and value.get("requesting_subagent_id") == subagent_id
            ):
                durable_value = _copy_handoff_value(value)
                if isinstance(durable_value, dict):
                    broker_results.append(durable_value)
        broker_results = broker_results[-8:]
    raw_binding = getattr(child, "_delegation_root_binding", None)
    if not isinstance(raw_binding, dict):
        raw_binding = _delegation_binding(root)
    binding = _copy_handoff(raw_binding)
    handoff = {
        "version": 1,
        "handoff_id": handoff_id,
        "created_at": time.time(),
        "goal": str(goal or ""),
        "status": str(entry.get("status") or ""),
        "exit_reason": str(entry.get("exit_reason") or ""),
        "role": str(getattr(child, "_delegate_role", "leaf") or "leaf"),
        "read_only": bool(getattr(child, "_delegation_read_only", False)),
        "child_session_id": str(getattr(child, "session_id", "") or ""),
        "subagent_id": str(getattr(child, "_subagent_id", "") or ""),
        "binding": binding,
        "files_read": _copy_handoff_value(list(files_read)),
        "files_written": _copy_handoff_value(list(files_written)),
        "tool_trace": _copy_handoff_value(list(entry.get("tool_trace") or [])),
        "tokens": _copy_handoff_value(dict(entry.get("tokens") or {})),
        "broker_results": broker_results,
    }
    registry = getattr(root, "_delegation_handoffs", None)
    if not isinstance(registry, dict):
        registry = {}
        try:
            root._delegation_handoffs = registry
        except Exception:
            return _copy_handoff(handoff)
    lock = getattr(root, "_delegation_handoffs_lock", None)
    if lock is None:
        lock = threading.Lock()
        try:
            root._delegation_handoffs_lock = lock
        except Exception:
            lock = None
    if lock is not None:
        with lock:
            registry[handoff_id] = _copy_handoff(handoff)
            while len(registry) > 100:
                registry.pop(next(iter(registry)))
    else:
        registry[handoff_id] = _copy_handoff(handoff)
        while len(registry) > 100:
            registry.pop(next(iter(registry)))
    return _copy_handoff(handoff)


def _classify_child_result(result: Dict[str, Any]) -> Dict[str, str]:
    """Classify a delegated child using structured run_conversation metadata."""
    summary = result.get("final_response") or ""
    if not isinstance(summary, str):
        summary = str(summary)

    completed = result.get("completed", False) is True
    failed = result.get("failed", False) is True
    interrupted = result.get("interrupted", False) is True
    error = result.get("error")
    turn_exit_reason = str(result.get("turn_exit_reason") or "").strip()

    if interrupted:
        return {"status": "interrupted", "exit_reason": "interrupted"}
    if summary.strip().lower() in {"(empty)", "[empty]"}:
        return {"status": "failed", "exit_reason": "failed_no_final"}
    if failed and not completed:
        return {"status": "failed", "exit_reason": "provider_failure" if error or summary else "failed"}
    if not completed and _looks_like_provider_failure_summary(summary):
        return {"status": "failed", "exit_reason": "provider_failure"}
    if completed and summary:
        return {"status": "completed", "exit_reason": "completed"}
    if not summary:
        return {"status": "failed", "exit_reason": "failed_no_final"}
    return {"status": "failed", "exit_reason": turn_exit_reason or "failed_no_final"}


def _end_failed_child_session(child: Any, end_reason: Optional[str]) -> None:
    if not end_reason:
        return
    session_id = getattr(child, "session_id", None)
    session_db = getattr(child, "_session_db", None)
    if not session_id or session_db is None:
        return
    try:
        session_db.end_session(session_id, end_reason)
    except Exception:
        logger.debug("Failed to end child session after delegated failure", exc_info=True)


def _normalize_role(r: Optional[str]) -> str:
    """Normalise a caller-provided role to 'leaf' or 'orchestrator'.

    None/empty -> 'leaf'.  Unknown strings coerce to 'leaf' with a
    warning log (matches the silent-degrade pattern of
    _get_orchestrator_enabled).  _build_child_agent adds a second
    degrade layer for depth/kill-switch bounds.
    """
    if r is None or not r:
        return "leaf"
    r_norm = str(r).strip().lower()
    if r_norm in {"leaf", "orchestrator"}:
        return r_norm
    logger.warning("Unknown delegate_task role=%r, coercing to 'leaf'", r)
    return "leaf"


def _normalized_runtime_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _inherit_parent_base_url(parent_agent: Any, fallback: Optional[str]) -> Optional[str]:
    """Prefer the endpoint used by the active parent client over stale metadata."""
    surface_url = _normalized_runtime_url(fallback)
    client_kwargs = getattr(parent_agent, "_client_kwargs", None)
    if isinstance(client_kwargs, dict):
        kwargs_url = _normalized_runtime_url(client_kwargs.get("base_url"))
        if (
            kwargs_url
            and kwargs_url != surface_url
            and kwargs_url.startswith(("http://", "https://"))
        ):
            return kwargs_url

    client = getattr(parent_agent, "client", None)
    if client is not None:
        live_url = _normalized_runtime_url(getattr(client, "base_url", ""))
        if (
            live_url
            and live_url != surface_url
            and live_url.startswith(("http://", "https://"))
        ):
            return live_url

    return fallback or None


def _resolve_delegation_model_tier(
    cfg: Dict[str, Any], requested_tier: Any
):
    """Resolve an explicitly selected tier for one delegated task.

    Ordinary delegation never infers a tier from goal/context text. A blank
    value means inherit the parent runtime (subject to explicit operator-owned
    ``delegation`` provider/model settings); a non-blank unknown name is
    rejected by ``delegate_task`` before any child starts.
    """
    tier_name = str(requested_tier or "").strip()
    if not tier_name:
        return None
    from hermes_cli.model_tiers import resolve_model_tier

    tier_config = {"model_tiers": cfg.get("model_tiers") or {}}
    return resolve_model_tier(tier_config, tier_name)


def _deep_review_tier_error(parent_agent: Any, requested_tier: Any) -> Optional[str]:
    """Require an explicit human request on the root turn for deep_review."""
    if str(requested_tier or "").strip().lower() != "deep_review":
        return None
    if int(getattr(parent_agent, "_delegate_depth", 0) or 0) != 0:
        return "model_tier 'deep_review' may only be selected by a root human turn."
    if not bool(getattr(parent_agent, "_human_deep_review_requested", False)):
        return (
            "model_tier 'deep_review' requires the human's current message to "
            "explicitly request xhigh or a deep review."
        )
    return None


def _get_max_concurrent_children() -> int:
    """Read delegation.max_concurrent_children from config, falling back to
    DELEGATION_MAX_CONCURRENT_CHILDREN env var, then the default (3).

    Users can raise this as high as they want; only the floor (1) is enforced.

    Uses the same ``_load_config()`` path that the rest of ``delegate_task``
    uses, keeping config priority consistent (config.yaml > env > default).
    """
    cfg = _load_config()
    val = cfg.get("max_concurrent_children")
    if val is not None:
        try:
            result = max(1, int(val))
            if result > 10:
                logger.warning(
                    "delegation.max_concurrent_children=%d: each child consumes API tokens "
                    "independently. High values multiply cost linearly.",
                    result,
                )
            return result
        except (TypeError, ValueError):
            logger.warning(
                "delegation.max_concurrent_children=%r is not a valid integer; "
                "using default %d",
                val,
                _DEFAULT_MAX_CONCURRENT_CHILDREN,
            )
            return _DEFAULT_MAX_CONCURRENT_CHILDREN
    env_val = os.getenv("DELEGATION_MAX_CONCURRENT_CHILDREN")
    if env_val:
        try:
            return max(1, int(env_val))
        except (TypeError, ValueError):
            return _DEFAULT_MAX_CONCURRENT_CHILDREN
    return _DEFAULT_MAX_CONCURRENT_CHILDREN


def _get_child_timeout() -> float:
    """Read delegation.child_timeout_seconds from config.

    Returns the number of seconds a single child agent is allowed to run
    before being considered stuck.  Default: 600 s (10 minutes).
    """
    cfg = _load_config()
    val = cfg.get("child_timeout_seconds")
    if val is not None:
        try:
            return max(30.0, float(val))
        except (TypeError, ValueError):
            logger.warning(
                "delegation.child_timeout_seconds=%r is not a valid number; "
                "using default %d",
                val,
                DEFAULT_CHILD_TIMEOUT,
            )
    env_val = os.getenv("DELEGATION_CHILD_TIMEOUT_SECONDS")
    if env_val:
        try:
            return max(30.0, float(env_val))
        except (TypeError, ValueError):
            pass
    return float(DEFAULT_CHILD_TIMEOUT)


def _get_max_spawn_depth() -> int:
    """Read delegation.max_spawn_depth from config, clamped to [1, 3].

    depth 0 = parent agent.  max_spawn_depth = N means agents at depths
    0..N-1 can spawn; depth N is the leaf floor.  Default 1 is flat:
    parent spawns children (depth 1), depth-1 children cannot spawn
    (blocked by this guard AND, for leaf children, by the delegation
    toolset strip in _strip_blocked_tools).

    Raise to 2 or 3 to unlock nested orchestration. role="orchestrator"
    removes the toolset strip for depth-1 children when
    max_spawn_depth >= 2, enabling them to spawn their own workers.
    """
    cfg = _load_config()
    val = cfg.get("max_spawn_depth")
    if val is None:
        return MAX_DEPTH
    try:
        ival = int(val)
    except (TypeError, ValueError):
        logger.warning(
            "delegation.max_spawn_depth=%r is not a valid integer; " "using default %d",
            val,
            MAX_DEPTH,
        )
        return MAX_DEPTH
    clamped = max(_MIN_SPAWN_DEPTH, min(_MAX_SPAWN_DEPTH_CAP, ival))
    if clamped != ival:
        logger.warning(
            "delegation.max_spawn_depth=%d out of range [%d, %d]; clamping to %d",
            ival,
            _MIN_SPAWN_DEPTH,
            _MAX_SPAWN_DEPTH_CAP,
            clamped,
        )
    return clamped


def _get_max_async_children() -> int:
    """Use the normal delegation concurrency cap for detached work too."""
    return _get_max_concurrent_children()


def _get_orchestrator_enabled() -> bool:
    """Global kill switch for the orchestrator role.

    When False, role="orchestrator" is silently forced to "leaf" in
    _build_child_agent and the delegation toolset is stripped as before.
    Lets an operator disable the feature without a code revert.
    """
    cfg = _load_config()
    val = cfg.get("orchestrator_enabled", True)
    if isinstance(val, bool):
        return val
    # Accept "true"/"false" strings from YAML that doesn't auto-coerce.
    if isinstance(val, str):
        return val.strip().lower() in {"true", "1", "yes", "on"}
    return True


def _get_nested_coding_enabled() -> bool:
    """Whether root-owned coding brokerage may be granted to orchestrators."""
    cfg = _load_config()
    nested = cfg.get("nested_coding") or {}
    if not isinstance(nested, dict):
        return False
    return is_truthy_value(nested.get("enabled"), default=False)


def _nested_coding_grant_error(
    *,
    requested: bool,
    background: bool,
    read_only: bool,
    role: str,
    parent_agent: Any,
    max_spawn_depth: int,
) -> Optional[str]:
    """Validate an explicit root-broker grant before child construction."""
    if not requested:
        return None
    if background:
        return (
            "Brokered nested coding is foreground-only; retry with "
            "background=false so the root retains workspace ownership."
        )
    if read_only:
        return (
            "Read-only delegation cannot request coding mutation. Set "
            "read_only=false and use the explicit brokered orchestrator capability."
        )
    if role != "orchestrator":
        return "allow_nested_coding requires role='orchestrator'."
    parent_depth = int(getattr(parent_agent, "_delegate_depth", 0) or 0)
    if parent_depth != 0:
        return "Only the root orchestrator may grant brokered nested coding access."
    if not _get_nested_coding_enabled():
        return (
            "Brokered nested coding is disabled by "
            "delegation.nested_coding.enabled=false."
        )
    if not _get_orchestrator_enabled():
        return (
            "Brokered nested coding requires delegation.orchestrator_enabled=true; "
            "the requested orchestrator would otherwise degrade to a leaf."
        )
    child_depth = parent_depth + 1
    if child_depth >= max_spawn_depth:
        return (
            "Brokered nested coding requires delegation.max_spawn_depth>=2; "
            f"the requested orchestrator at depth {child_depth} would otherwise "
            "degrade to a leaf."
        )
    return None


def _get_inherit_mcp_toolsets() -> bool:
    """Whether narrowed child toolsets should keep the parent's MCP toolsets."""
    cfg = _load_config()
    return is_truthy_value(cfg.get("inherit_mcp_toolsets"), default=True)


def _get_denied_child_toolsets() -> List[str]:
    """Configured toolsets that every delegated child must lose.

    The default is empty to preserve legacy inheritance. Restrictions are
    applied through ``disabled_toolsets`` so they survive composite expansion
    and later registry/MCP refreshes.
    """
    raw = _load_config().get("denied_toolsets")
    if not isinstance(raw, (list, tuple, set)):
        return []
    return list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))


def _is_mcp_toolset_name(name: str) -> bool:
    """Return True for canonical MCP toolsets and their registered aliases."""
    if not name:
        return False
    if str(name).startswith("mcp-"):
        return True
    try:
        from tools.registry import registry

        target = registry.get_toolset_alias_target(str(name))
    except Exception:
        target = None
    return bool(target and str(target).startswith("mcp-"))


def _expand_parent_toolsets(parent_toolsets: set) -> set:
    """Expand composite toolsets so individual toolset names are recognized.

    When a parent uses a composite toolset like ``hermes-cli`` (which bundles
    all core tools), the child may request individual toolsets such as ``web``
    or ``terminal``.  A simple name-based intersection would reject them
    because ``"web" != "hermes-cli"``.

    This helper collects the tool names from each parent toolset, then adds
    the names of any individual toolsets whose tools are a *subset* of the
    parent's available tools.  The original parent toolset names are preserved.
    """
    parent_tool_names: set = set()
    for ts_name in parent_toolsets:
        ts_def = TOOLSETS.get(ts_name)
        if ts_def:
            parent_tool_names.update(ts_def.get("tools", []))

    if not parent_tool_names:
        return set(parent_toolsets)

    expanded = set(parent_toolsets)
    for ts_name, ts_def in TOOLSETS.items():
        if ts_name in expanded:
            continue
        ts_tools = ts_def.get("tools", [])
        if ts_tools and set(ts_tools).issubset(parent_tool_names):
            expanded.add(ts_name)
    return expanded


def _resolve_requested_child_toolsets(
    requested_toolsets: List[str], parent_toolsets: set[str]
) -> tuple[List[str], List[str]]:
    """Resolve requested toolsets without silently weakening posture bundles.

    Posture toolsets such as ``coding`` are broader aliases rather than
    independently configured platform capabilities. Expand them into the
    concrete toolsets the parent actually owns. Return any requests that could
    not grant a usable child capability so callers can fail before launch.
    """
    import model_tools

    expanded_parent = _expand_parent_toolsets(parent_toolsets)
    resolved: List[str] = []
    unavailable: List[str] = []

    for requested in requested_toolsets:
        name = str(requested or "").strip()
        if not name:
            continue
        if name in expanded_parent:
            resolved.append(name)
            continue

        definition = TOOLSETS.get(name)
        if not definition or not definition.get("posture"):
            unavailable.append(name)
            continue

        posture_matches = []
        for tool_name in definition.get("tools") or ():
            toolset_name = model_tools.get_toolset_for_tool(tool_name)
            if toolset_name in expanded_parent:
                posture_matches.append(toolset_name)
        posture_matches = _strip_blocked_tools(list(dict.fromkeys(posture_matches)))
        if posture_matches:
            resolved.extend(posture_matches)
        else:
            unavailable.append(name)

    resolved = _strip_blocked_tools(list(dict.fromkeys(resolved)))
    return resolved, unavailable


def _preserve_parent_mcp_toolsets(
    child_toolsets: List[str], parent_toolsets: set[str]
) -> List[str]:
    """Append any parent MCP toolsets that are missing from a narrowed child."""
    preserved = list(child_toolsets)
    for toolset_name in sorted(parent_toolsets):
        if _is_mcp_toolset_name(toolset_name) and toolset_name not in preserved:
            preserved.append(toolset_name)
    return preserved


DEFAULT_MAX_ITERATIONS = 50
# Hard per-summary character ceiling layered on top of the dynamic parent
# headroom budget. Zero disables this static ceiling.
DEFAULT_MAX_SUMMARY_CHARS = 24000
_SUMMARY_HEADROOM_FRACTION = 0.5
_MIN_SUMMARY_CHARS = 2000
DEFAULT_CHILD_TIMEOUT = 600  # seconds before a child agent is considered stuck
_HEARTBEAT_INTERVAL = 30  # seconds between parent activity heartbeats during delegation
# Stale-heartbeat thresholds. A child with no API-call progress is either:
#   - idle between turns (no current_tool) — probably stuck on a slow API call
#   - inside a tool (current_tool set) — probably running a legitimately long
#     operation (terminal command, web fetch, large file read)
# The idle ceiling stays tight so genuinely stuck children don't mask the gateway
# timeout. The in-tool ceiling is much higher so legit long-running tools get
# time to finish; child_timeout_seconds (default 600s) is still the hard cap.
_HEARTBEAT_STALE_CYCLES_IDLE = 15  # 15 * 30s = 450s idle between turns → stale
_HEARTBEAT_STALE_CYCLES_IN_TOOL = 40  # 40 * 30s = 1200s stuck on same tool → stale
DEFAULT_TOOLSETS = ["terminal", "file", "web"]


# ---------------------------------------------------------------------------
# Delegation progress event types
# ---------------------------------------------------------------------------


class DelegateEvent(str, enum.Enum):
    """Formal event types emitted during delegation progress.

    _build_child_progress_callback normalises incoming legacy strings
    (``tool.started``, ``_thinking``, …) to these enum values via
    ``_LEGACY_EVENT_MAP``.  External consumers (gateway SSE, ACP adapter,
    CLI) still receive the legacy strings during the deprecation window.

    TASK_SPAWNED / TASK_COMPLETED / TASK_FAILED are reserved for
    future orchestrator lifecycle events and are not currently emitted.
    """

    TASK_SPAWNED = "delegate.task_spawned"
    TASK_PROGRESS = "delegate.task_progress"
    TASK_COMPLETED = "delegate.task_completed"
    TASK_FAILED = "delegate.task_failed"
    TASK_THINKING = "delegate.task_thinking"
    TASK_TOOL_STARTED = "delegate.tool_started"
    TASK_TOOL_COMPLETED = "delegate.tool_completed"


# Legacy event strings → DelegateEvent mapping.
# Incoming child-agent events use the old names; the callback normalises them.
_LEGACY_EVENT_MAP: Dict[str, DelegateEvent] = {
    "_thinking": DelegateEvent.TASK_THINKING,
    "reasoning.available": DelegateEvent.TASK_THINKING,
    "tool.started": DelegateEvent.TASK_TOOL_STARTED,
    "tool.completed": DelegateEvent.TASK_TOOL_COMPLETED,
    "subagent_progress": DelegateEvent.TASK_PROGRESS,
}


def check_delegate_requirements() -> bool:
    """Delegation has no external requirements -- always available."""
    return True


def _build_child_system_prompt(
    goal: str,
    context: Optional[str] = None,
    *,
    workspace_path: Optional[str] = None,
    role: str = "leaf",
    max_spawn_depth: int = 2,
    child_depth: int = 1,
    read_only: bool = False,
    brokered_coding: bool = False,
) -> str:
    """Build a focused system prompt for a child agent.

    When role='orchestrator', appends a delegation-capability block
    modeled on OpenClaw's buildSubagentSystemPrompt (canSpawn branch at
    inspiration/openclaw/src/agents/subagent-system-prompt.ts:63-95).
    The depth note is literal truth (grounded in the passed config) so
    the LLM doesn't confabulate nesting capabilities that don't exist.
    """
    parts = [
        "You are a focused subagent working on a specific delegated task.",
        "",
        f"YOUR TASK:\n{goal}",
    ]
    if context and context.strip():
        parts.append(f"\nCONTEXT:\n{context}")
    if workspace_path and str(workspace_path).strip():
        parts.append(
            "\nWORKSPACE PATH:\n"
            f"{workspace_path}\n"
            "Use this exact path for local repository/workdir operations unless the task explicitly says otherwise."
        )
    parts.append(
        "\nComplete this task using the tools available to you. "
        "When finished, provide a clear, concise summary of:\n"
        "- What you did\n"
        "- What you found or accomplished\n"
        "- Any files you created or modified\n"
        "- Any issues encountered\n\n"
        "Important workspace rule: Never assume a repository lives at /workspace/... or any other container-style path unless the task/context explicitly gives that path. "
        "If no exact local path is provided, discover it first before issuing git/workdir-specific commands.\n\n"
        "Be thorough but concise -- your response is returned to the "
        "parent agent as a summary."
    )
    if read_only:
        parts.append(
            "\n## Enforced Read-Only Mode\n"
            "This delegation is runtime-enforced read-only. You may inspect the "
            "repository with explicit observation tools such as read_file and "
            "search_files, but terminal execution, write_file, patch, execute_code, "
            "raw coding-worker delegation, and other mutation "
            "paths are blocked. Do not claim that you changed files. Read-only "
            "mode propagates to every delegate_task child you create."
        )
        parts.append(
            "\nFor local codebase work, begin with read_file/search_files against "
            "the provided or discovered workspace path. Do not use browser or public "
            "dev-server URLs as a substitute for local source inspection."
        )
    elif brokered_coding:
        parts.append(
            "\n## Root-Owned Coding Broker\n"
            "You cannot edit repositories directly and you do not have the raw "
            "delegate_coding_task tool. For a bounded implementation step, use "
            "request_coding_task with non-overlapping scope_paths. Hermes' root "
            "orchestrator owns the authorized cwd, work-item/session identity, "
            "worker accounting, visual/closeout state, isolation, merge-back, and "
            "the deterministic result. Coding workers are leaves."
        )
    else:
        parts.append(
            "\n## Workspace Mutation Capability\n"
            "This delegation is not read-only. You may use the provided terminal "
            "and file tools for bounded exploration, setup, and in-scope changes. "
            "Preserve unrelated work, report every modified path, and return "
            "verifiable evidence for external side effects."
        )
    if role == "orchestrator":
        child_note = (
            "Your own children MUST be leaves (cannot delegate further) "
            "because they would be at the depth floor — you cannot pass "
            "role='orchestrator' to your own delegate_task calls."
            if child_depth + 1 >= max_spawn_depth
            else "Your own children can themselves be orchestrators or leaves, "
            "depending on the `role` you pass to delegate_task. Default is "
            "'leaf'; pass role='orchestrator' explicitly when a child "
            "needs to further decompose its work."
        )
        parts.append(
            "\n## Subagent Spawning (Orchestrator Role)\n"
            "You have access to the `delegate_task` tool and CAN spawn "
            "your own subagents to parallelize independent work.\n\n"
            "WHEN to delegate:\n"
            "- The goal decomposes into 2+ independent subtasks that can "
            "run in parallel (e.g. research A and B simultaneously).\n"
            "- A subtask is reasoning-heavy and would flood your context "
            "with intermediate data.\n\n"
            "WHEN NOT to delegate:\n"
            "- Single-step mechanical work — do it directly.\n"
            "- Trivial tasks you can execute in one or two tool calls.\n"
            "- Re-delegating your entire assigned goal to one worker "
            "(that's just pass-through with no value added).\n\n"
            "Coordinate your workers' results and synthesize them before "
            "reporting back to your parent. You are responsible for the "
            "final summary, not your workers.\n\n"
            f"NOTE: You are at depth {child_depth}. The delegation tree "
            f"is capped at max_spawn_depth={max_spawn_depth}. {child_note}"
        )
    return "\n".join(parts)


def _resolve_workspace_hint(parent_agent) -> Optional[str]:
    """Best-effort local workspace hint for child prompts.

    We only inject a path when we have a concrete absolute directory. This avoids
    teaching subagents a fake container path while still helping them avoid
    guessing `/workspace/...` for local repo tasks.
    """
    try:
        from gateway.session_context import get_session_env

        session_cwd = get_session_env("HERMES_SESSION_CWD", "")
    except Exception:
        session_cwd = ""

    candidates = [
        session_cwd,
        getattr(parent_agent, "session_cwd", None),
        os.getenv("TERMINAL_CWD"),
        getattr(
            getattr(parent_agent, "_subdirectory_hints", None), "working_dir", None
        ),
        getattr(parent_agent, "terminal_cwd", None),
        getattr(parent_agent, "cwd", None),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        try:
            text = os.path.abspath(os.path.expanduser(str(candidate)))
        except Exception:
            continue
        if os.path.isabs(text) and os.path.isdir(text):
            return text
    return None


def _strip_blocked_tools(toolsets: List[str]) -> List[str]:
    """Remove toolsets whose capability is wholly blocked for delegates."""
    composite_blocked = frozenset({"delegation", "code_execution"})
    blocked_toolset_names = {
        name
        for name, definition in TOOLSETS.items()
        if name in composite_blocked
        or all(
            tool_name in DELEGATE_BLOCKED_TOOLS
            for tool_name in definition.get("tools", [])
        )
    }
    blocked_toolset_names.update(_DELEGATE_DISABLED_TOOLSETS)
    return [t for t in toolsets if t not in blocked_toolset_names]


def _blocked_toolsets_for_role(role: str) -> List[str]:
    """Return one-tool deny toolsets for a delegated child role.

    ``_strip_blocked_tools`` can remove fully blocked toolsets, but it must keep
    mixed platform bundles such as ``hermes-cli`` because those also contain
    useful tools. Passing these exact deny toolsets to AIAgent lets
    ``model_tools`` subtract blocked names *after* composite expansion, and the
    restriction survives later registry/MCP refreshes through the agent's
    stored ``disabled_toolsets``.
    """
    blocked_names = set(DELEGATE_BLOCKED_TOOLS)
    if role == "orchestrator":
        blocked_names.discard("delegate_task")
    return sorted(
        name
        for name, defn in TOOLSETS.items()
        if defn.get("tools")
        and set(defn.get("tools", ())).issubset(blocked_names)
    )


def _emit_parent_console(parent_agent, line: str) -> None:
    """Emit a human-readable progress line to the parent's console.

    Routes through ``parent_agent._safe_print`` when available so headless
    stdio hosts (ACP, gateway API) can redirect non-protocol output to
    stderr via their configured ``_print_fn``. A bare ``print()`` would
    otherwise land on stdout and corrupt JSON-RPC framing.
    """
    printer = getattr(parent_agent, "_safe_print", None)
    if callable(printer):
        try:
            printer(line)
            return
        except Exception:
            pass
    print(line)


def _build_child_progress_callback(
    task_index: int,
    goal: str,
    parent_agent,
    task_count: int = 1,
    *,
    subagent_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    depth: Optional[int] = None,
    model: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
) -> Optional[callable]:
    """Build a callback that relays child agent tool calls to the parent display.

    Two display paths:
      CLI:     prints tree-view lines above the parent's delegation spinner
      Gateway: batches tool names and relays to parent's progress callback

    The identity kwargs (``subagent_id``, ``parent_id``, ``depth``, ``model``,
    ``toolsets``) are threaded into every relayed event so the TUI can
    reconstruct the live spawn tree and route per-branch controls (kill,
    pause) back by ``subagent_id``.  All are optional for backward compat —
    older callers that ignore them still produce a flat list on the TUI.

    Returns None if no display mechanism is available, in which case the
    child agent runs with no progress callback (identical to current behavior).
    """
    spinner = getattr(parent_agent, "_delegate_spinner", None)
    parent_cb = getattr(parent_agent, "tool_progress_callback", None)

    if not spinner and not parent_cb:
        return None  # No display → no callback → zero behavior change

    # Show 1-indexed prefix only in batch mode (multiple tasks)
    prefix = f"[{task_index + 1}] " if task_count > 1 else ""
    goal_label = (goal or "").strip()

    # Gateway: batch tool names, flush periodically
    _BATCH_SIZE = 5
    _batch: List[str] = []
    _tool_count = [0]  # per-subagent running counter (list for closure mutation)

    def _identity_kwargs() -> Dict[str, Any]:
        kw: Dict[str, Any] = {
            "task_index": task_index,
            "task_count": task_count,
            "goal": goal_label,
        }
        if subagent_id is not None:
            kw["subagent_id"] = subagent_id
        if parent_id is not None:
            kw["parent_id"] = parent_id
        if depth is not None:
            kw["depth"] = depth
        if model is not None:
            kw["model"] = model
        if toolsets is not None:
            kw["toolsets"] = list(toolsets)
        kw["tool_count"] = _tool_count[0]
        return kw

    def _relay(
        event_type: str, tool_name: str = None, preview: str = None, args=None, **kwargs
    ):
        if not parent_cb:
            return
        payload = _identity_kwargs()
        payload.update(kwargs)  # caller overrides (e.g. status, duration_seconds)
        try:
            parent_cb(event_type, tool_name, preview, args, **payload)
        except Exception as e:
            logger.debug("Parent callback failed: %s", e)

    def _callback(
        event_type, tool_name: str = None, preview: str = None, args=None, **kwargs
    ):
        # Lifecycle events emitted by the orchestrator itself — handled
        # before enum normalisation since they are not part of DelegateEvent.
        if event_type == "subagent.start":
            if spinner and goal_label:
                short = (
                    (goal_label[:55] + "...") if len(goal_label) > 55 else goal_label
                )
                try:
                    spinner.print_above(f" {prefix}├─ 🔀 {short}")
                except Exception as e:
                    logger.debug("Spinner print_above failed: %s", e)
            _relay("subagent.start", preview=preview or goal_label or "", **kwargs)
            return

        if event_type == "subagent.complete":
            _relay("subagent.complete", preview=preview, **kwargs)
            return

        # Normalise legacy strings, new-style "delegate.*" strings, and
        # DelegateEvent enum values all to a single DelegateEvent.  The
        # original implementation only accepted the five legacy strings;
        # enum-typed callers were silently dropped.
        if isinstance(event_type, DelegateEvent):
            event = event_type
        else:
            event = _LEGACY_EVENT_MAP.get(event_type)
            if event is None:
                try:
                    event = DelegateEvent(event_type)
                except (ValueError, TypeError):
                    return  # Unknown event — ignore

        if event == DelegateEvent.TASK_THINKING:
            text = preview or tool_name or ""
            if spinner:
                short = (text[:55] + "...") if len(text) > 55 else text
                try:
                    spinner.print_above(f' {prefix}├─ 💭 "{short}"')
                except Exception as e:
                    logger.debug("Spinner print_above failed: %s", e)
            _relay("subagent.thinking", preview=text)
            return

        if event == DelegateEvent.TASK_TOOL_COMPLETED:
            return

        if event == DelegateEvent.TASK_PROGRESS:
            # Pre-batched progress summary relayed from a nested
            # orchestrator's grandchild (upstream emits as
            # parent_cb("subagent_progress", summary_string) where the
            # summary lands in the tool_name positional slot).  Treat as
            # a pass-through: render distinctly (not via the tool-start
            # emoji lookup, which would mistake the summary string for a
            # tool name) and relay upward without re-batching.
            summary_text = tool_name or preview or ""
            if spinner and summary_text:
                try:
                    spinner.print_above(f" {prefix}├─ 🔀 {summary_text}")
                except Exception as e:
                    logger.debug("Spinner print_above failed: %s", e)
            if parent_cb:
                try:
                    parent_cb("subagent_progress", f"{prefix}{summary_text}")
                except Exception as e:
                    logger.debug("Parent callback relay failed: %s", e)
            return

        # TASK_TOOL_STARTED — display and batch for parent relay
        _tool_count[0] += 1
        if subagent_id is not None:
            with _active_subagents_lock:
                rec = _active_subagents.get(subagent_id)
                if rec is not None:
                    rec["tool_count"] = _tool_count[0]
                    rec["last_tool"] = tool_name or ""
        if spinner:
            short = (
                (preview[:35] + "...")
                if preview and len(preview) > 35
                else (preview or "")
            )
            from agent.display import get_tool_emoji

            emoji = get_tool_emoji(tool_name or "")
            line = f" {prefix}├─ {emoji} {tool_name}"
            if short:
                line += f'  "{short}"'
            try:
                spinner.print_above(line)
            except Exception as e:
                logger.debug("Spinner print_above failed: %s", e)

        if parent_cb:
            _relay("subagent.tool", tool_name, preview, args)
            _batch.append(tool_name or "")
            if len(_batch) >= _BATCH_SIZE:
                summary = ", ".join(_batch)
                _relay("subagent.progress", preview=f"🔀 {prefix}{summary}")
                _batch.clear()

    def _flush():
        """Flush remaining batched tool names to gateway on completion."""
        if parent_cb and _batch:
            summary = ", ".join(_batch)
            _relay("subagent.progress", preview=f"🔀 {prefix}{summary}")
            _batch.clear()

    _callback._flush = _flush
    return _callback


def _emit_subagent_start(
    *,
    child: Any,
    parent_agent: Any,
    root_agent: Any,
    parent_subagent_id: Any,
    subagent_id: str,
    role: str,
    goal: str,
) -> None:
    """Emit the public child-start observer payload without affecting launch."""
    try:
        from hermes_cli.plugins import invoke_hook

        invoke_hook(
            "subagent_start",
            root_session_id=str(getattr(root_agent, "session_id", "") or "") or None,
            parent_session_id=str(getattr(parent_agent, "session_id", "") or "") or None,
            parent_turn_id=str(
                getattr(parent_agent, "_current_turn_id", "")
                or getattr(parent_agent, "_current_task_id", "")
                or ""
            ),
            parent_subagent_id=str(parent_subagent_id or "") or None,
            child_session_id=str(getattr(child, "session_id", "") or "") or None,
            child_subagent_id=str(subagent_id or "") or None,
            child_role=role,
            child_goal=goal,
            platform=getattr(parent_agent, "platform", None) or "",
        )
    except Exception:
        logger.debug("subagent_start hook invocation failed", exc_info=True)


def _build_child_agent(
    task_index: int,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    model: Optional[str],
    max_iterations: int,
    task_count: int,
    parent_agent,
    # Credential overrides from delegation config (provider:model resolution)
    override_provider: Optional[str] = None,
    override_base_url: Optional[str] = None,
    override_api_key: Optional[str] = None,
    override_api_mode: Optional[str] = None,
    override_request_overrides: Optional[Dict[str, Any]] = None,
    override_max_tokens: Optional[int] = None,
    # ACP transport overrides — lets a non-ACP parent spawn ACP child agents.
    override_acp_command: Optional[str] = None,
    override_acp_args: Optional[List[str]] = None,
    override_reasoning_config: Optional[Dict[str, Any]] = None,
    # Per-call role controlling whether the child can further delegate.
    # 'leaf' (default) cannot; 'orchestrator' retains the delegation
    # toolset subject to depth/kill-switch bounds applied below.
    role: str = "leaf",
    read_only: bool = False,
    allow_nested_coding: bool = False,
    runtime_audit_context: Optional[Dict[str, Any]] = None,
    inherit_fallback: bool = True,
):
    """
    Build a child AIAgent on the main thread (thread-safe construction).
    Returns the constructed child agent without running it.

    When override_* params are set (from delegation config), the child uses
    those credentials instead of inheriting from the parent.  This enables
    routing subagents to a different provider:model pair (e.g. cheap/fast
    model on OpenRouter while the parent runs on Nous Portal).
    """
    from run_agent import AIAgent
    import uuid as _uuid

    # ── Role resolution ─────────────────────────────────────────────────
    # Honor the caller's role only when BOTH the kill switch and the
    # child's depth allow it.  This is the single point where role
    # degrades to 'leaf' — keeps the rule predictable.  Callers pass
    # the normalised role (_normalize_role ran in delegate_task) so
    # we only deal with 'leaf' or 'orchestrator' here.
    child_depth = getattr(parent_agent, "_delegate_depth", 0) + 1
    max_spawn = _get_max_spawn_depth()
    orchestrator_ok = _get_orchestrator_enabled() and child_depth < max_spawn
    effective_role = role if (role == "orchestrator" and orchestrator_ok) else "leaf"
    root_agent = getattr(parent_agent, "_delegate_root_agent", parent_agent)
    brokered_coding = bool(
        allow_nested_coding
        and effective_role == "orchestrator"
        and child_depth == 1
        and _get_nested_coding_enabled()
        and not read_only
    )

    # ── Subagent identity (stable across events, 0-indexed for TUI) ─────
    # subagent_id is generated here so the progress callback, the
    # spawn_requested event, and the _active_subagents registry all share
    # one key.  parent_id is non-None when THIS parent is itself a subagent
    # (nested orchestrator -> worker chain).
    subagent_id = f"sa-{task_index}-{_uuid.uuid4().hex[:8]}"
    parent_subagent_id = getattr(parent_agent, "_subagent_id", None)
    tui_depth = max(0, child_depth - 1)  # 0 = first-level child for the UI

    delegation_cfg = _load_config()

    # When no explicit toolsets given, inherit from parent's enabled toolsets
    # so disabled tools (e.g. web) don't leak to subagents.
    # Note: enabled_toolsets=None means "all tools enabled" (the default),
    # so we must derive effective toolsets from the parent's loaded tools.
    parent_enabled = getattr(parent_agent, "enabled_toolsets", None)
    if parent_enabled is not None:
        parent_toolsets = set(parent_enabled)
    elif parent_agent and hasattr(parent_agent, "valid_tool_names"):
        # enabled_toolsets is None (all tools) — derive from loaded tool names
        import model_tools

        parent_toolsets = {
            ts
            for name in parent_agent.valid_tool_names
            if (ts := model_tools.get_toolset_for_tool(name)) is not None
        }
    else:
        parent_toolsets = set(DEFAULT_TOOLSETS)

    if toolsets:
        # Intersect with parent — subagent must not gain tools the parent lacks.
        # Posture aliases such as ``coding`` expand to the concrete capabilities
        # already owned by the parent rather than disappearing at intersection.
        child_toolsets, _unavailable = _resolve_requested_child_toolsets(
            toolsets, parent_toolsets
        )
        if _get_inherit_mcp_toolsets():
            child_toolsets = _preserve_parent_mcp_toolsets(
                child_toolsets, parent_toolsets
            )
    elif parent_agent and parent_enabled is not None:
        child_toolsets = _strip_blocked_tools(parent_enabled)
    elif parent_toolsets:
        child_toolsets = _strip_blocked_tools(sorted(parent_toolsets))
    else:
        child_toolsets = _strip_blocked_tools(DEFAULT_TOOLSETS)

    # Blocked tools also live inside mixed platform bundles (hermes-cli,
    # hermes-telegram, etc.) that _strip_blocked_tools must keep because they
    # carry useful tools too. Pass exact one-tool deny toolsets through to the
    # child so model_tools subtracts the blocked names AFTER composite
    # expansion, and the restriction survives later registry/MCP refreshes.
    raw_parent_disabled = getattr(parent_agent, "disabled_toolsets", None)
    if isinstance(raw_parent_disabled, (list, tuple, set)):
        inherited_disabled = [str(name) for name in raw_parent_disabled]
    else:
        inherited_disabled = []
    if effective_role == "orchestrator":
        # Role grants delegate_task explicitly, matching the unconditional
        # delegation toolset re-add below.
        inherited_disabled = [
            name for name in inherited_disabled if name != "delegation"
        ]
    # Orchestrators retain the 'delegation' toolset that _strip_blocked_tools
    # removed.  The re-add is unconditional on parent-toolset membership because
    # orchestrator capability is granted by role, not inherited — see the
    # test_intersection_preserves_delegation_bound test for the design rationale.
    if effective_role == "orchestrator" and "delegation" not in child_toolsets:
        child_toolsets.append("delegation")
    if brokered_coding and "delegated_coding_broker" not in child_toolsets:
        child_toolsets.append("delegated_coding_broker")

    # Preserve any restrictions already applied to the parent and add the
    # worker-only canonical-sync restriction.  ``disabled_toolsets`` is applied
    # after composite toolsets resolve, so this also covers inherited
    # ``hermes-cli``/platform toolsets that contain the core sync tool.
    disabled_for_child = set(_DELEGATE_DISABLED_TOOLSETS)
    if brokered_coding:
        disabled_for_child.discard("delegated_coding_broker")
    child_disabled_toolsets = list(
        dict.fromkeys(
            inherited_disabled
            + _blocked_toolsets_for_role(effective_role)
            + sorted(disabled_for_child)
            + _get_denied_child_toolsets()
        )
    )

    workspace_hint = _resolve_workspace_hint(parent_agent)
    child_prompt = _build_child_system_prompt(
        goal,
        context,
        workspace_path=workspace_hint,
        role=effective_role,
        max_spawn_depth=max_spawn,
        child_depth=child_depth,
        read_only=read_only,
        brokered_coding=brokered_coding,
    )
    # Extract parent's API key so subagents inherit auth (e.g. Nous Portal).
    parent_api_key = getattr(parent_agent, "api_key", None)
    if (not parent_api_key) and hasattr(parent_agent, "_client_kwargs"):
        parent_api_key = parent_agent._client_kwargs.get("api_key")

    # Resolve the child's effective model early so it can ride on every event.
    effective_model_for_cb = model or getattr(parent_agent, "model", None)

    # Build progress callback to relay tool calls to parent display.
    # Identity kwargs thread the subagent_id through every emitted event so the
    # TUI can reconstruct the spawn tree and route per-branch controls.
    child_progress_cb = _build_child_progress_callback(
        task_index,
        goal,
        parent_agent,
        task_count,
        subagent_id=subagent_id,
        parent_id=parent_subagent_id,
        depth=tui_depth,
        model=effective_model_for_cb,
        toolsets=child_toolsets,
    )

    # Each subagent gets its own iteration budget capped at max_iterations
    # (configurable via delegation.max_iterations, default 50).  This means
    # total iterations across parent + subagents can exceed the parent's
    # max_iterations.  The user controls the per-subagent cap in config.yaml.

    child_thinking_cb = None
    if child_progress_cb:

        def _child_thinking(text: str) -> None:
            if not text:
                return
            try:
                child_progress_cb("_thinking", text)
            except Exception as e:
                logger.debug("Child thinking callback relay failed: %s", e)

        child_thinking_cb = _child_thinking

    # Resolve effective credentials: config override > parent inherit
    effective_model = model or parent_agent.model
    effective_provider = override_provider or getattr(parent_agent, "provider", None)
    effective_base_url = override_base_url or _inherit_parent_base_url(
        parent_agent, parent_agent.base_url
    )
    effective_api_key = override_api_key or parent_api_key
    # Bug #20558 / PR #20563: api_mode must NOT be inherited when the child uses a
    # different provider than the parent — each provider has its own API surface
    # (e.g. MiniMax uses anthropic_messages, DeepSeek uses chat_completions).
    # Inheriting the parent's mode causes 404 errors when the child routes to the
    # wrong endpoint.  Derive the mode from the target provider when it differs.
    _parent_provider = getattr(parent_agent, "provider", None) or ""
    if override_api_mode is not None:
        effective_api_mode = override_api_mode
    elif effective_provider != _parent_provider:
        effective_api_mode = None  # force re-derivation from provider's defaults
    else:
        effective_api_mode = getattr(parent_agent, "api_mode", None)
    effective_acp_command = override_acp_command or getattr(
        parent_agent, "acp_command", None
    )
    effective_acp_args = list(
        override_acp_args
        if override_acp_args is not None
        else (getattr(parent_agent, "acp_args", []) or [])
    )

    usable_acp_override = override_acp_command
    if usable_acp_override:
        import shutil

        if shutil.which(str(usable_acp_override)) is None:
            logger.warning(
                "Ignoring delegation ACP command %r because it is not available on PATH",
                usable_acp_override,
            )
            usable_acp_override = None
            effective_acp_command = None
            effective_acp_args = []

    # When override_provider is set (e.g. delegation.provider: minimax-cn),
    # the subagent must use direct API calls — not the parent's ACP transport.
    # Inheriting acp_command unconditionally causes run_agent.py to initialize
    # CopilotACPClient, bypassing override credentials entirely (issue #16816).
    if override_provider and not usable_acp_override:
        effective_acp_command = None
        effective_acp_args = []

    if usable_acp_override:
        # If explicitly forcing an ACP transport override, the provider MUST be copilot-acp
        # so run_agent.py initializes the CopilotACPClient.
        effective_provider = "copilot-acp"
        effective_api_mode = "chat_completions"

    # Resolve reasoning config: explicit per-call tier > operator delegation
    # override > parent. Goal/context keywords never rewrite the selected or
    # inherited reasoning level.
    parent_reasoning = getattr(parent_agent, "reasoning_config", None)
    child_reasoning = override_reasoning_config or parent_reasoning
    if override_reasoning_config is None:
        try:
            delegation_effort = str(delegation_cfg.get("reasoning_effort") or "").strip()
            if delegation_effort:
                from hermes_constants import parse_reasoning_effort

                parsed = parse_reasoning_effort(delegation_effort)
                if parsed is not None:
                    child_reasoning = parsed
                else:
                    logger.warning(
                        "Unknown delegation.reasoning_effort '%s', inheriting parent level",
                        delegation_effort,
                    )
        except Exception as exc:
            logger.debug("Could not load delegation reasoning_effort: %s", exc)

    # Inherit the parent's fallback provider chain so subagents can recover
    # from rate-limits and credential exhaustion exactly like the top-level
    # agent does.  _fallback_chain is a list accepted by AIAgent's
    # fallback_model parameter (which handles both list and dict forms).
    parent_fallback = (
        getattr(parent_agent, "_fallback_chain", None) or None
        if inherit_fallback
        else None
    )

    # Inherit the parent's OpenRouter provider-preference filters by default
    # (so subagents routed to the same provider honour the same routing
    # constraints).  BUT: when `delegation.provider` is set the user is
    # explicitly asking the child to run on a different provider, and
    # parent-level OpenRouter filters (e.g. `only=["Anthropic"]`) would
    # silently force the child back onto the parent's provider. Clear the
    # filters in that case so the delegated provider is honoured.
    child_providers_allowed = getattr(parent_agent, "providers_allowed", None)
    child_providers_ignored = getattr(parent_agent, "providers_ignored", None)
    child_providers_order = getattr(parent_agent, "providers_order", None)
    child_provider_sort = getattr(parent_agent, "provider_sort", None)
    child_provider_require_parameters = getattr(
        parent_agent, "provider_require_parameters", False
    )
    child_provider_data_collection = getattr(
        parent_agent, "provider_data_collection", None
    ) or ""
    child_openrouter_min_coding_score = getattr(parent_agent, "openrouter_min_coding_score", None)
    if override_provider:
        child_providers_allowed = None
        child_providers_ignored = None
        child_providers_order = None
        child_provider_sort = None
        child_provider_require_parameters = False
        child_provider_data_collection = ""
        # Note: openrouter_min_coding_score is model-gated (only emitted on
        # openrouter/pareto-code), so we keep it inherited even when the
        # provider is overridden — it's a no-op on any other model.

    child_max_tokens = (
        override_max_tokens
        if override_max_tokens is not None
        else getattr(parent_agent, "max_tokens", None)
    )
    child_optional_kwargs: Dict[str, Any] = {}
    if isinstance(child_max_tokens, int):
        child_optional_kwargs["max_tokens"] = child_max_tokens

    child = AIAgent(
        base_url=effective_base_url,
        api_key=effective_api_key,
        model=effective_model,
        provider=effective_provider,
        api_mode=effective_api_mode,
        acp_command=effective_acp_command,
        acp_args=effective_acp_args,
        max_iterations=max_iterations,

        reasoning_config=child_reasoning,
        prefill_messages=getattr(parent_agent, "prefill_messages", None),
        fallback_model=parent_fallback,
        enabled_toolsets=child_toolsets,
        disabled_toolsets=child_disabled_toolsets,
        quiet_mode=True,
        ephemeral_system_prompt=child_prompt,
        log_prefix=f"[subagent-{task_index}]",
        platform=parent_agent.platform,
        skip_context_files=True,
        skip_memory=True,
        session_role="worker",
        runtime_mode=("read_only" if read_only else "action"),
        clarify_callback=None,
        thinking_callback=child_thinking_cb,
        session_db=getattr(parent_agent, "_session_db", None),
        parent_session_id=getattr(parent_agent, "session_id", None),
        providers_allowed=child_providers_allowed,
        providers_ignored=child_providers_ignored,
        providers_order=child_providers_order,
        provider_sort=child_provider_sort,
        provider_require_parameters=child_provider_require_parameters,
        provider_data_collection=child_provider_data_collection,
        request_overrides=(
            dict(override_request_overrides or {})
            if override_provider
            else dict(getattr(parent_agent, "request_overrides", {}) or {})
        ),
        openrouter_min_coding_score=child_openrouter_min_coding_score,
        tool_progress_callback=child_progress_cb,
        iteration_budget=None,  # fresh budget per subagent
        **child_optional_kwargs,
    )
    from agent.runtime_audit import set_runtime_audit_context

    audit_context = dict(runtime_audit_context or {})
    audit_context.update(
        runtime_route="delegation",
        runtime_role=effective_role,
    )
    set_runtime_audit_context(child, **audit_context)
    child._print_fn = getattr(parent_agent, "_print_fn", None)
    # Read-only delegation may query the parent's session store through the
    # bounded session_search handler, but the child itself must never create a
    # session, append messages, persist token accounting, or compact history.
    child._persist_disabled = bool(read_only)
    # Set delegation depth so children can't spawn grandchildren
    child._delegate_depth = child_depth
    # Stash the post-degrade role for introspection (leaf if the
    # kill switch or depth bounded the caller's requested role).
    child._delegate_role = effective_role
    child._delegation_read_only = bool(read_only)
    child._delegation_broker_only_mutation = bool(brokered_coding)
    child._delegate_root_agent = root_agent
    child._delegation_root_binding = copy.deepcopy(
        _delegation_binding(root_agent, workspace_hint)
    )
    child._delegation_broker_context = (
        {
            "enabled": True,
            "root_agent": root_agent,
            "authorized_cwd": _resolve_workspace_hint(root_agent) or "",
            "gateway_session_key": str(
                getattr(root_agent, "_gateway_session_key", "")
                or getattr(root_agent, "gateway_session_key", "")
                or getattr(root_agent, "session_key", "")
                or ""
            ),
            "session_id": str(getattr(root_agent, "session_id", "") or ""),
            "origin_work_item_id": str(
                getattr(root_agent, "_origin_work_item_id", "")
                or getattr(root_agent, "work_item_id", "")
                or ""
            ),
            "turn_id": child._delegation_root_binding.get("turn_id", ""),
            "visual_qa_requirement": copy.deepcopy(
                getattr(root_agent, "visual_qa_requirement", None)
            ),
            "project_inspection_candidates": copy.deepcopy(
                getattr(root_agent, "project_inspection_candidates", None)
            ),
        }
        if brokered_coding
        else None
    )
    # Stash subagent identity for nested-delegation event propagation and
    # for _run_single_child / interrupt_subagent to look up by id.
    child._subagent_id = subagent_id
    child._parent_subagent_id = parent_subagent_id
    child._subagent_goal = goal

    # Share a credential pool with the child when possible so subagents can
    # rotate credentials on rate limits instead of getting pinned to one key.
    child_pool = _resolve_child_credential_pool(
        effective_provider,
        parent_agent,
        effective_base_url,
    )
    if child_pool is not None:
        child._credential_pool = child_pool

    # Register child for interrupt propagation
    if hasattr(parent_agent, "_active_children"):
        lock = getattr(parent_agent, "_active_children_lock", None)
        if lock:
            with lock:
                parent_agent._active_children.append(child)
        else:
            parent_agent._active_children.append(child)

    # Announce the spawn immediately — the child may sit in a queue
    # for seconds if max_concurrent_children is saturated, so the TUI
    # wants a node in the tree before run starts.
    if child_progress_cb:
        try:
            child_progress_cb("subagent.spawn_requested", preview=goal)
        except Exception as exc:
            logger.debug("spawn_requested relay failed: %s", exc)

    _emit_subagent_start(
        child=child,
        parent_agent=parent_agent,
        root_agent=root_agent,
        parent_subagent_id=parent_subagent_id,
        subagent_id=subagent_id,
        role=effective_role,
        goal=goal,
    )

    return child


def _dump_subagent_timeout_diagnostic(
    *,
    child: Any,
    task_index: int,
    timeout_seconds: float,
    duration_seconds: float,
    worker_thread: Optional[threading.Thread],
    goal: str,
) -> Optional[str]:
    """Write a structured diagnostic dump for a subagent that timed out
    before making any API call.

    See issue #14726: users hit "subagent timed out after 300s with no response"
    with zero API calls and no way to inspect what happened. This helper
    writes a dedicated log under ``~/.hermes/logs/subagent-<sid>-<ts>.log``
    capturing the child's config, system-prompt / tool-schema sizes, activity
    tracker snapshot, and the worker thread's Python stack at timeout.

    Returns the absolute path to the diagnostic file, or None on failure.
    """
    try:
        from hermes_constants import get_hermes_home
        import datetime as _dt
        import sys as _sys
        import traceback as _traceback

        hermes_home = get_hermes_home()
        logs_dir = hermes_home / "logs"
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None

        subagent_id = getattr(child, "_subagent_id", None) or f"idx{task_index}"
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        dump_path = logs_dir / f"subagent-timeout-{subagent_id}-{ts}.log"

        lines: List[str] = []
        def _w(line: str = "") -> None:
            lines.append(line)

        _w("# Subagent timeout diagnostic — issue #14726")
        _w(f"# Generated: {_dt.datetime.now().isoformat()}")
        _w("")
        _w("## Timeout")
        _w(f"  task_index:        {task_index}")
        _w(f"  subagent_id:       {subagent_id}")
        _w(f"  configured_timeout: {timeout_seconds}s")
        _w(f"  actual_duration:   {duration_seconds:.2f}s")
        _w("")

        _w("## Goal")
        _goal_preview = (goal or "").strip()
        if len(_goal_preview) > 1000:
            _goal_preview = _goal_preview[:1000] + " ...[truncated]"
        _w(_goal_preview or "(empty)")
        _w("")

        _w("## Child config")
        for attr in (
            "model", "provider", "api_mode", "base_url", "max_iterations",
            "quiet_mode", "skip_memory", "skip_context_files", "platform",
            "_delegate_role", "_delegate_depth",
        ):
            try:
                val = getattr(child, attr, None)
                # Redact api_key-shaped values defensively
                if isinstance(val, str) and attr == "base_url":
                    pass
                _w(f"  {attr}: {val!r}")
            except Exception:
                _w(f"  {attr}: <unreadable>")
        _w("")

        _w("## Toolsets")
        enabled = getattr(child, "enabled_toolsets", None)
        _w(f"  enabled_toolsets:  {enabled!r}")
        tool_names = getattr(child, "valid_tool_names", None)
        if tool_names:
            _w(f"  loaded tool count: {len(tool_names)}")
            try:
                _w(f"  loaded tools:      {sorted(tool_names)}")
            except Exception:
                pass
        _w("")

        _w("## Prompt / schema sizes")
        try:
            sys_prompt = getattr(child, "ephemeral_system_prompt", None) \
                or getattr(child, "system_prompt", None) \
                or ""
            _w(f"  system_prompt_bytes: {len(sys_prompt.encode('utf-8')) if isinstance(sys_prompt, str) else 'n/a'}")
            _w(f"  system_prompt_chars: {len(sys_prompt) if isinstance(sys_prompt, str) else 'n/a'}")
        except Exception as exc:
            _w(f"  system_prompt: <error: {exc}>")
        try:
            tools_schema = getattr(child, "tools", None)
            if tools_schema is not None:
                _schema_json = json.dumps(tools_schema, default=str)
                _w(f"  tool_schema_count: {len(tools_schema)}")
                _w(f"  tool_schema_bytes: {len(_schema_json.encode('utf-8'))}")
        except Exception as exc:
            _w(f"  tool_schema: <error: {exc}>")
        _w("")

        _w("## Activity summary")
        try:
            summary = child.get_activity_summary()
            for k, v in summary.items():
                _w(f"  {k}: {v!r}")
        except Exception as exc:
            _w(f"  <get_activity_summary failed: {exc}>")
        _w("")

        _w("## Worker thread stack at timeout")
        if worker_thread is not None and worker_thread.is_alive():
            frames = _sys._current_frames()
            worker_frame = frames.get(worker_thread.ident)
            if worker_frame is not None:
                stack = _traceback.format_stack(worker_frame)
                for frame_line in stack:
                    for sub in frame_line.rstrip().split("\n"):
                        _w(f"  {sub}")
            else:
                _w("  <worker frame not available>")
        elif worker_thread is None:
            _w("  <no worker thread handle>")
        else:
            _w("  <worker thread already exited>")
        _w("")

        _w("## Notes")
        _w("  This file is written ONLY when a subagent times out with 0 API calls.")
        _w("  0-API-call timeouts mean the child never reached its first LLM request.")
        _w("  Common causes: oversized prompt rejected by provider, transport hang,")
        _w("  credential resolution stuck. See issue #14726 for context.")

        dump_path.write_text("\n".join(lines), encoding="utf-8")
        return str(dump_path)
    except Exception as exc:
        logger.warning("Subagent timeout diagnostic dump failed: %s", exc)
        return None


def _spill_summary_to_file(task_index: int, summary: str) -> Optional[str]:
    """Persist a complete subagent summary and return its absolute path."""
    try:
        import datetime as _dt

        from hermes_constants import get_hermes_dir

        cache_dir = get_hermes_dir("cache/delegation", "delegation_cache")
        cache_dir.mkdir(parents=True, exist_ok=True)
        timestamp = _dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = cache_dir / f"subagent-summary-{task_index}-{timestamp}.txt"
        path.write_text(summary, encoding="utf-8")
        return str(path)
    except Exception as exc:
        logger.debug("Failed to spill subagent summary to file: %s", exc)
        return None


def _trim_summary_with_footer(
    summary: str, cap: int, task_index: int
) -> tuple[str, Optional[str]]:
    """Keep a bounded head/tail view while preserving the full summary."""
    original_len = len(summary)
    head_budget = int(cap * 0.75)
    tail_budget = cap - head_budget

    head = summary[:head_budget]
    tail = summary[-tail_budget:]
    newline = head.rfind("\n")
    if newline > head_budget * 0.5:
        head = head[:newline]
    newline = tail.find("\n")
    if 0 <= newline < tail_budget * 0.5:
        tail = tail[newline + 1:]

    spill_path = _spill_summary_to_file(task_index, summary)
    footer_lines = [
        "",
        "─" * 8 + " [SUMMARY TRUNCATED] " + "─" * 8,
        f"Showing {len(head):,} chars (head) + {len(tail):,} chars (tail) "
        f"of {original_len:,} total — trimmed to protect the parent's context window.",
    ]
    if spill_path:
        middle_start_line = head.count("\n") + 2
        footer_lines.append(f"Full subagent output saved to: {spill_path}")
        footer_lines.append(
            f'To read the omitted middle: read_file path="{spill_path}" '
            f"offset={middle_start_line} limit=200  (the file is the complete "
            "summary; raise/lower offset to page through it)."
        )
    else:
        footer_lines.append(
            "Full output could not be stored to disk; the head+tail above is "
            "all that was preserved."
        )
    footer_lines.append("─" * 37)

    model_text = (
        head
        + "\n\n[... middle omitted — see footer ...]\n\n"
        + tail
        + "\n".join(footer_lines)
    )
    return model_text, spill_path


def _parent_summary_char_budget(
    parent_agent: Any, n_summaries: int
) -> Optional[int]:
    """Size each summary against the parent's remaining context headroom."""
    try:
        compressor = getattr(parent_agent, "context_compressor", None)
        context_length = getattr(compressor, "context_length", None)
        if not isinstance(context_length, int) or context_length <= 0:
            return None

        used_tokens = getattr(parent_agent, "session_prompt_tokens", 0)
        if not isinstance(used_tokens, (int, float)) or used_tokens < 0:
            used_tokens = 0
        reserved = getattr(compressor, "max_tokens", 0) or 0
        headroom_tokens = context_length - int(used_tokens) - int(reserved)
        if headroom_tokens <= 0:
            return _MIN_SUMMARY_CHARS

        batch_token_budget = int(headroom_tokens * _SUMMARY_HEADROOM_FRACTION)
        per_summary_tokens = batch_token_budget // max(1, n_summaries)
        return max(_MIN_SUMMARY_CHARS, per_summary_tokens * 4)
    except Exception:
        logger.debug("Summary budget computation failed", exc_info=True)
        return None


def _apply_summary_budget(
    results: List[Dict[str, Any]], parent_agent: Any
) -> None:
    """Bound summaries in-place and spill complete overflow text to disk."""
    summaries = [
        entry
        for entry in results
        if isinstance(entry, dict)
        and isinstance(entry.get("summary"), str)
        and entry["summary"]
    ]
    if not summaries:
        return

    cfg = _load_config()
    try:
        static_ceiling = int(
            cfg.get("max_summary_chars", DEFAULT_MAX_SUMMARY_CHARS)
        )
    except (TypeError, ValueError):
        static_ceiling = DEFAULT_MAX_SUMMARY_CHARS

    dynamic_budget = _parent_summary_char_budget(parent_agent, len(summaries))
    candidates = [
        candidate
        for candidate in (static_ceiling, dynamic_budget)
        if candidate and candidate > 0
    ]
    if not candidates:
        return
    cap = min(candidates)

    for entry in summaries:
        summary = entry["summary"]
        if len(summary) <= cap:
            continue
        original_len = len(summary)
        model_text, spill_path = _trim_summary_with_footer(
            summary, cap, entry.get("task_index", -1)
        )
        entry["summary"] = model_text
        entry["summary_truncated"] = True
        if spill_path:
            entry["summary_full_path"] = spill_path
        logger.debug(
            "[subagent-%s] summary trimmed %d → ~%d chars (spill=%s)",
            entry.get("task_index", "?"),
            original_len,
            cap,
            spill_path or "none",
        )


def _run_single_child(
    task_index: int,
    goal: str,
    child=None,
    parent_agent=None,
    **_kwargs,
) -> Dict[str, Any]:
    """
    Run a pre-built child agent. Called from within a thread.
    Returns a structured result dict.
    """
    child_start = time.monotonic()
    child_end_reason: Optional[str] = None

    # Get the progress callback from the child agent
    child_progress_cb = getattr(child, "tool_progress_callback", None)
    child_stream_cb = vars(child).get("_live_transcript_stream_callback")
    if not callable(child_stream_cb):
        child_stream_cb = None

    # Restore parent tool names using the value saved before child construction
    # mutated the global. This is the correct parent toolset, not the child's.
    import model_tools

    _saved_tool_names = getattr(
        child, "_delegate_saved_tool_names", list(model_tools._last_resolved_tool_names)
    )

    child_pool = getattr(child, "_credential_pool", None)
    leased_cred_id = None
    if child_pool is not None:
        preferred_cred_id = None
        try:
            current_entry = child_pool.current()
            current_status = str(getattr(current_entry, "last_status", "") or "").lower()
            if current_entry is not None and current_status != "exhausted":
                preferred_cred_id = getattr(current_entry, "id", None)
        except Exception:
            preferred_cred_id = None
        leased_cred_id = child_pool.acquire_lease(preferred_cred_id)
        if leased_cred_id is not None:
            try:
                leased_entry = child_pool.current()
                if leased_entry is not None and hasattr(child, "_swap_credential"):
                    child._swap_credential(leased_entry)
            except Exception as exc:
                logger.debug("Failed to bind child to leased credential: %s", exc)

    # Heartbeat: periodically propagate child activity to the parent so the
    # gateway inactivity timeout doesn't fire while the subagent is working.
    # Without this, the parent's _last_activity_ts freezes when delegate_task
    # starts and the gateway eventually kills the agent for "no activity".
    _heartbeat_stop = threading.Event()
    # Stale detection: track the child's (tool, iteration) pair across
    # heartbeat cycles. If neither advances, count the cycle as stale.
    # Different thresholds for idle vs in-tool (see _HEARTBEAT_STALE_CYCLES_*).
    _last_seen_iter = [0]
    _last_seen_tool = [None]  # type: list
    _stale_count = [0]

    def _heartbeat_loop():
        while not _heartbeat_stop.wait(_HEARTBEAT_INTERVAL):
            if parent_agent is None:
                continue
            touch = getattr(parent_agent, "_touch_activity", None)
            if not touch:
                continue
            # Pull detail from the child's own activity tracker
            desc = f"delegate_task: subagent {task_index} working"
            try:
                child_summary = child.get_activity_summary()
                child_tool = child_summary.get("current_tool")
                child_iter = child_summary.get("api_call_count", 0)
                child_max = child_summary.get("max_iterations", 0)

                # Stale detection: count cycles where neither the iteration
                # count nor the current_tool advances. A child running a
                # legitimately long-running tool (terminal command, web
                # fetch) keeps current_tool set but doesn't advance
                # api_call_count — we don't want that to look stale at the
                # idle threshold.
                iter_advanced = child_iter > _last_seen_iter[0]
                tool_changed = child_tool != _last_seen_tool[0]
                if iter_advanced or tool_changed:
                    _last_seen_iter[0] = child_iter
                    _last_seen_tool[0] = child_tool
                    _stale_count[0] = 0
                else:
                    _stale_count[0] += 1

                # Pick threshold based on whether the child is currently
                # inside a tool call. In-tool threshold is high enough to
                # cover legitimately slow tools; idle threshold stays
                # tight so the gateway timeout can fire on a truly wedged
                # child.
                stale_limit = (
                    _HEARTBEAT_STALE_CYCLES_IN_TOOL
                    if child_tool
                    else _HEARTBEAT_STALE_CYCLES_IDLE
                )
                if _stale_count[0] >= stale_limit:
                    logger.warning(
                        "Subagent %d appears stale (no progress for %d "
                        "heartbeat cycles, tool=%s) — stopping heartbeat",
                        task_index,
                        _stale_count[0],
                        child_tool or "<none>",
                    )
                    break  # stop touching parent, let gateway timeout fire

                if child_tool:
                    desc = (
                        f"delegate_task: subagent running {child_tool} "
                        f"(iteration {child_iter}/{child_max})"
                    )
                else:
                    child_desc = child_summary.get("last_activity_desc", "")
                    if child_desc:
                        desc = (
                            f"delegate_task: subagent {child_desc} "
                            f"(iteration {child_iter}/{child_max})"
                        )
            except Exception:
                pass
            try:
                touch(desc)
            except Exception:
                pass

    _heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)

    # Register the live agent in the module-level registry so the TUI can
    # target it by subagent_id (kill, pause, status queries).  Unregistered
    # in the finally block, even when the child raises.  Test doubles that
    # hand us a MagicMock don't carry stable ids; skip registration then.
    _raw_sid = getattr(child, "_subagent_id", None)
    _subagent_id = _raw_sid if isinstance(_raw_sid, str) else None
    if _subagent_id:
        _raw_depth = getattr(child, "_delegate_depth", 1)
        _tui_depth = max(0, _raw_depth - 1) if isinstance(_raw_depth, int) else 0
        _parent_sid = getattr(child, "_parent_subagent_id", None)
        _register_subagent(
            {
                "subagent_id": _subagent_id,
                "parent_id": _parent_sid if isinstance(_parent_sid, str) else None,
                "depth": _tui_depth,
                "goal": goal,
                "model": (
                    getattr(child, "model", None)
                    if isinstance(getattr(child, "model", None), str)
                    else None
                ),
                "started_at": time.time(),
                "status": "running",
                "tool_count": 0,
                "agent": child,
            }
        )

    try:
        _heartbeat_thread.start()
        if child_progress_cb:
            try:
                child_progress_cb("subagent.start", preview=goal)
            except Exception as e:
                logger.debug("Progress callback start failed: %s", e)

        # File-state coordination: reuse the stable subagent_id as the child's
        # task_id so file_state writes, active-subagents registry, and TUI
        # events all share one key.  Falls back to a fresh uuid only if the
        # pre-built id is somehow missing.
        import uuid as _uuid

        child_task_id = _subagent_id or f"subagent-{task_index}-{_uuid.uuid4().hex[:8]}"
        parent_task_id = _kwargs.get("parent_task_id") or getattr(
            parent_agent, "_current_task_id", None
        )
        # Seed the child's session-cwd record from the parent's (cwd rearch):
        # children share the parent's container, and today they inherit the
        # parent's live env.cwd implicitly. Seeding at spawn preserves that
        # starting directory while keeping the child's subsequent `cd`s
        # isolated in its own record (a child's cd no longer bleeds back into
        # the parent once readers flip to the record store).
        try:
            from tools.terminal_tool import get_session_cwd, record_session_cwd

            record_session_cwd(child_task_id, get_session_cwd(parent_task_id))
        except Exception as e:
            logger.debug("Child cwd seed failed: %s", e)
        wall_start = time.time()
        parent_reads_snapshot = (
            list(file_state.known_reads(parent_task_id)) if parent_task_id else []
        )

        # Run child with a hard timeout to prevent indefinite blocking
        # when the child's API call or tool-level HTTP request hangs.
        from agent.worker_budget import remaining_nested_worker_budget

        requested_timeout = _get_child_timeout()
        child_timeout = (
            requested_timeout
            if _kwargs.get("ignore_parent_deadline")
            else remaining_nested_worker_budget(parent_agent, requested_timeout)
        )
        if child_timeout <= 0:
            duration = round(time.monotonic() - child_start, 2)
            child_end_reason = "timeout"
            return {
                "task_index": task_index,
                "status": "timeout",
                "summary": None,
                "error": "Parent turn nested-worker deadline was exhausted before launch.",
                "exit_reason": "timeout",
                "api_calls": 0,
                "duration_seconds": duration,
                "_child_role": getattr(child, "_delegate_role", None),
            }
        try:
            _timeout_executor = ThreadPoolExecutor(
                max_workers=1,
                # Install a non-interactive approval callback in the worker thread
                # so dangerous-command prompts from the subagent don't fall back to
                # input() and deadlock the parent's prompt_toolkit TUI.
                # Callback (deny vs approve) is governed by delegation.subagent_auto_approve.
                initializer=_set_subagent_approval_cb,
                initargs=(_get_subagent_approval_callback(),),
            )
        except RuntimeError as exc:
            if not _is_interpreter_shutdown_error(exc):
                raise
            duration = round(time.monotonic() - child_start, 2)
            child_end_reason = "error"
            return {
                "task_index": task_index,
                "status": "error",
                "summary": None,
                "error": _delegation_shutdown_message(),
                "exit_reason": "error",
                "api_calls": 0,
                "duration_seconds": duration,
                "_child_role": getattr(child, "_delegate_role", None),
            }
        # Capture the worker thread so the timeout diagnostic can dump its
        # Python stack (see #14726 — 0-API-call hangs are opaque without it).
        _worker_thread_holder: Dict[str, Optional[threading.Thread]] = {"t": None}

        def _run_with_thread_capture():
            _worker_thread_holder["t"] = threading.current_thread()
            run_kwargs = dict(
                user_message=goal,
                task_id=child_task_id,
            )
            if child_stream_cb is not None:
                run_kwargs["stream_callback"] = child_stream_cb
            return child.run_conversation(**run_kwargs)

        try:
            _child_future = _timeout_executor.submit(_run_with_thread_capture)
        except RuntimeError as exc:
            if not _is_interpreter_shutdown_error(exc):
                raise
            _timeout_executor.shutdown(wait=False)
            duration = round(time.monotonic() - child_start, 2)
            child_end_reason = "error"
            return {
                "task_index": task_index,
                "status": "error",
                "summary": None,
                "error": _delegation_shutdown_message(),
                "exit_reason": "error",
                "api_calls": 0,
                "duration_seconds": duration,
                "_child_role": getattr(child, "_delegate_role", None),
            }
        try:
            result = _child_future.result(timeout=child_timeout)
        except Exception as _timeout_exc:
            # Signal the child to stop so its thread can exit cleanly.
            try:
                if hasattr(child, "interrupt"):
                    child.interrupt()
                elif hasattr(child, "_interrupt_requested"):
                    child._interrupt_requested = True
            except Exception:
                pass

            is_timeout = isinstance(_timeout_exc, (FuturesTimeoutError, TimeoutError))
            duration = round(time.monotonic() - child_start, 2)
            logger.warning(
                "Subagent %d %s after %.1fs",
                task_index,
                "timed out" if is_timeout else f"raised {type(_timeout_exc).__name__}",
                duration,
            )

            # When a subagent times out BEFORE making any API call, dump a
            # diagnostic to help users (and us) see what the child was doing.
            # See #14726 — without this, 0-API-call hangs are black boxes.
            diagnostic_path: Optional[str] = None
            child_api_calls = 0
            try:
                _summary = child.get_activity_summary()
                child_api_calls = int(_summary.get("api_call_count", 0) or 0)
            except Exception:
                pass
            if is_timeout and child_api_calls == 0:
                diagnostic_path = _dump_subagent_timeout_diagnostic(
                    child=child,
                    task_index=task_index,
                    timeout_seconds=float(child_timeout),
                    duration_seconds=float(duration),
                    worker_thread=_worker_thread_holder.get("t"),
                    goal=goal,
                )
                if diagnostic_path:
                    logger.warning(
                        "Subagent %d 0-API-call timeout — diagnostic written to %s",
                        task_index,
                        diagnostic_path,
                    )

            if child_progress_cb:
                try:
                    child_progress_cb(
                        "subagent.complete",
                        preview=(
                            f"Timed out after {duration}s"
                            if is_timeout
                            else str(_timeout_exc)
                        ),
                        status="timeout" if is_timeout else "error",
                        duration_seconds=duration,
                        summary="",
                    )
                except Exception:
                    pass

            if is_timeout:
                if child_api_calls == 0:
                    _err = (
                        f"Subagent timed out after {child_timeout}s without "
                        f"making any API call — the child never reached its "
                        f"first LLM request (prompt construction, credential "
                        f"resolution, or transport may be stuck)."
                    )
                    if diagnostic_path:
                        _err += f" Diagnostic: {diagnostic_path}"
                else:
                    _err = (
                        f"Subagent timed out after {child_timeout}s with "
                        f"{child_api_calls} API call(s) completed — likely "
                        f"stuck on a slow API call or unresponsive network request."
                    )
            else:
                _err = str(_timeout_exc)

            child_end_reason = "timeout" if is_timeout else "error"
            return {
                "task_index": task_index,
                "status": "timeout" if is_timeout else "error",
                "summary": None,
                "error": _err,
                "exit_reason": child_end_reason,
                "api_calls": child_api_calls,
                "duration_seconds": duration,
                "_child_role": getattr(child, "_delegate_role", None),
                "diagnostic_path": diagnostic_path,
            }
        finally:
            # Shut down executor without waiting — if the child thread
            # is stuck on blocking I/O, wait=True would hang forever.
            _timeout_executor.shutdown(wait=False)

        # Flush any remaining batched progress to gateway
        if child_progress_cb and hasattr(child_progress_cb, "_flush"):
            try:
                child_progress_cb._flush()
            except Exception as e:
                logger.debug("Progress callback flush failed: %s", e)

        duration = round(time.monotonic() - child_start, 2)

        summary = result.get("final_response") or ""
        if not isinstance(summary, str):
            summary = str(summary)
        classification = _classify_child_result(result)
        status = classification["status"]
        exit_reason = classification["exit_reason"]
        api_calls = result.get("api_calls", 0)

        # Build tool trace from conversation messages (already in memory).
        # Uses tool_call_id to correctly pair parallel tool calls with results.
        tool_trace: list[Dict[str, Any]] = []
        trace_by_id: Dict[str, Dict[str, Any]] = {}
        messages = result.get("messages") or []
        if isinstance(messages, list):
            from agent.message_content import flatten_message_text

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        fn = tc.get("function", {})
                        entry_t = {
                            "tool": fn.get("name", "unknown"),
                            "args_bytes": len(fn.get("arguments", "")),
                        }
                        tool_trace.append(entry_t)
                        tc_id = tc.get("id")
                        if tc_id:
                            trace_by_id[tc_id] = entry_t
                elif msg.get("role") == "tool":
                    content = flatten_message_text(msg.get("content"))
                    is_error = _looks_like_error_output(content)
                    result_meta = {
                        "result_bytes": len(content),
                        "status": "error" if is_error else "ok",
                    }
                    # Match by tool_call_id for parallel calls
                    tc_id = msg.get("tool_call_id")
                    target = trace_by_id.get(tc_id) if tc_id else None
                    if target is not None:
                        target.update(result_meta)
                    elif tool_trace:
                        # Fallback for messages without tool_call_id
                        tool_trace[-1].update(result_meta)

        # Extract token counts (safe for mock objects)
        _input_tokens = getattr(child, "session_prompt_tokens", 0)
        _output_tokens = getattr(child, "session_completion_tokens", 0)
        _model = getattr(child, "model", None)

        entry: Dict[str, Any] = {
            "task_index": task_index,
            "status": status,
            "summary": summary,
            "api_calls": api_calls,
            "duration_seconds": duration,
            "model": _model if isinstance(_model, str) else None,
            "exit_reason": exit_reason,
            "tokens": {
                "input": (
                    _input_tokens if isinstance(_input_tokens, (int, float)) else 0
                ),
                "output": (
                    _output_tokens if isinstance(_output_tokens, (int, float)) else 0
                ),
            },
            "tool_trace": tool_trace,
            # Captured before the finally block calls child.close() so the
            # parent thread can fire subagent_stop with the correct role.
            # Stripped before the dict is serialised back to the model.
            "_child_role": getattr(child, "_delegate_role", None),
            # Captured before child.close() so the parent aggregator can fold
            # the child's total spend into the parent's session cost.  Port of
            # Kilo-Org/kilocode#9448 — previously the footer only reflected the
            # parent's direct API calls and under-counted subagent-heavy runs.
            # Stripped before the dict is serialised back to the model.
            "_child_cost_usd": (
                float(getattr(child, "session_estimated_cost_usd", 0.0) or 0.0)
                if isinstance(
                    getattr(child, "session_estimated_cost_usd", 0.0),
                    (int, float),
                )
                else 0.0
            ),
        }
        try:
            from agent.runtime_audit import runtime_audit_fields

            audit = runtime_audit_fields(child)
            entry["_worker_run"] = {
                "backend": "delegate",
                "model": _model if isinstance(_model, str) else "",
                "reasoning": str(audit.get("reasoning_effort") or "").strip(),
                "model_tier": str(audit.get("model_tier") or "").strip() or None,
                "failed": status != "completed",
            }
        except Exception:
            entry["_worker_run"] = {
                "backend": "delegate",
                "model": _model if isinstance(_model, str) else "",
                "reasoning": "",
                "model_tier": None,
                "failed": status != "completed",
            }
        if status == "failed":
            entry["error"] = result.get("error") or summary or "Subagent did not produce a response."

        # Cross-agent file-state reminder.  If this subagent wrote any
        # files the parent had already read, surface it so the parent
        # knows to re-read before editing — the scenario that motivated
        # the registry.  We check writes by ANY non-parent task_id (not
        # just this child's), which also covers transitive writes from
        # nested orchestrator→worker chains.
        try:
            if parent_task_id and parent_reads_snapshot:
                sibling_writes = file_state.writes_since(
                    parent_task_id, wall_start, parent_reads_snapshot
                )
                if sibling_writes:
                    mod_paths = sorted(
                        {p for paths in sibling_writes.values() for p in paths}
                    )
                    if mod_paths:
                        reminder = (
                            "\n\n[NOTE: subagent modified files the parent "
                            "previously read — re-read before editing: "
                            + ", ".join(mod_paths[:8])
                            + (
                                f" (+{len(mod_paths) - 8} more)"
                                if len(mod_paths) > 8
                                else ""
                            )
                            + "]"
                        )
                        if entry.get("summary"):
                            entry["summary"] = entry["summary"] + reminder
                        else:
                            entry["stale_paths"] = mod_paths
        except Exception:
            logger.debug("file_state sibling-write check failed", exc_info=True)

        # Per-branch observability payload: tokens, cost, files touched, and
        # a tail of tool-call results.  Fed into the TUI's overlay detail
        # pane + accordion rollups (features 1, 2, 4).  All fields are
        # optional — missing data degrades gracefully on the client.
        _cost_usd = getattr(child, "session_estimated_cost_usd", None)
        _reasoning_tokens = getattr(child, "session_reasoning_tokens", 0)
        try:
            _files_read = list(file_state.known_reads(child_task_id))[:40]
        except Exception:
            _files_read = []
        try:
            _files_written_map = file_state.writes_since(
                "", wall_start, []
            )  # all writes since wall_start
        except Exception:
            _files_written_map = {}
        _files_written = sorted(
            {
                p
                for tid, paths in _files_written_map.items()
                if tid == child_task_id
                for p in paths
            }
        )[:40]

        entry["handoff"] = _register_structured_handoff(
            child=child,
            parent_agent=parent_agent,
            goal=goal,
            entry=entry,
            files_read=_files_read,
            files_written=_files_written,
        )

        _output_tail = _extract_output_tail(result, max_entries=8, max_chars=600)
        if status == "failed" and _output_tail:
            entry["output_tail"] = _output_tail

        complete_kwargs: Dict[str, Any] = {
            "preview": summary[:160] if summary else entry.get("error", ""),
            "status": status,
            "duration_seconds": duration,
            "summary": summary[:500] if summary else entry.get("error", ""),
            "input_tokens": (
                int(_input_tokens) if isinstance(_input_tokens, (int, float)) else 0
            ),
            "output_tokens": (
                int(_output_tokens) if isinstance(_output_tokens, (int, float)) else 0
            ),
            "reasoning_tokens": (
                int(_reasoning_tokens)
                if isinstance(_reasoning_tokens, (int, float))
                else 0
            ),
            "api_calls": int(api_calls) if isinstance(api_calls, (int, float)) else 0,
            "files_read": _files_read,
            "files_written": _files_written,
            "output_tail": _output_tail,
        }
        if _cost_usd is not None:
            try:
                complete_kwargs["cost_usd"] = float(_cost_usd)
            except (TypeError, ValueError):
                pass

        if child_progress_cb:
            try:
                child_progress_cb("subagent.complete", **complete_kwargs)
            except Exception as e:
                logger.debug("Progress callback completion failed: %s", e)

        if status != "completed":
            child_end_reason = exit_reason
        return entry

    except Exception as exc:
        duration = round(time.monotonic() - child_start, 2)
        logging.exception(f"[subagent-{task_index}] failed")
        child_end_reason = "error"
        if child_progress_cb:
            try:
                child_progress_cb(
                    "subagent.complete",
                    preview=str(exc),
                    status="failed",
                    duration_seconds=duration,
                    summary=str(exc),
                )
            except Exception as e:
                logger.debug("Progress callback failure relay failed: %s", e)
        return {
            "task_index": task_index,
            "status": "error",
            "summary": None,
            "error": str(exc),
            "api_calls": 0,
            "duration_seconds": duration,
            "_child_role": getattr(child, "_delegate_role", None),
        }

    finally:
        # Stop the heartbeat thread so it doesn't keep touching parent activity
        # after the child has finished (or failed).  Guard the join: .start()
        # now lives inside the try block, so if it raised (OS thread
        # exhaustion) the thread was never started and Thread.join() would
        # raise RuntimeError.  ident is None until start() succeeds.
        _heartbeat_stop.set()
        if _heartbeat_thread.ident is not None:
            _heartbeat_thread.join(timeout=5)

        # Drop the TUI-facing registry entry.  Safe to call even if the
        # child was never registered (e.g. ID missing on test doubles).
        if _subagent_id:
            _unregister_subagent(_subagent_id)

        if child_pool is not None and leased_cred_id is not None:
            try:
                child_pool.release_lease(leased_cred_id)
            except Exception as exc:
                logger.debug("Failed to release credential lease: %s", exc)

        # Restore the parent's tool names so the process-global is correct
        # for any subsequent execute_code calls or other consumers.
        import model_tools

        saved_tool_names = getattr(child, "_delegate_saved_tool_names", None)
        if isinstance(saved_tool_names, list):
            model_tools._last_resolved_tool_names = list(saved_tool_names)

        # Remove child from active tracking

        # Unregister child from interrupt propagation
        if hasattr(parent_agent, "_active_children"):
            try:
                lock = getattr(parent_agent, "_active_children_lock", None)
                if lock:
                    with lock:
                        parent_agent._active_children.remove(child)
                else:
                    parent_agent._active_children.remove(child)
            except (ValueError, UnboundLocalError) as e:
                logger.debug("Could not remove child from active_children: %s", e)

        # Close tool resources (terminal sandboxes, browser daemons,
        # background processes, httpx clients) so subagent subprocesses
        # don't outlive the delegation.
        try:
            _end_failed_child_session(child, child_end_reason)
            if hasattr(child, "close"):
                child.close()
        except Exception:
            logger.debug("Failed to close child agent after delegation")


def _recover_tasks_from_json_string(
    tasks: Any,
) -> tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    if not isinstance(tasks, str):
        return None, None
    raw = tasks.strip()
    if not raw:
        return None, "Provide either 'goal' (single task) or 'tasks' (batch)."
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, (
            "tasks must be a JSON array of task objects; received a string "
            f"that could not be parsed as JSON ({exc.msg})."
        )
    if not isinstance(parsed, list):
        return None, (
            f"tasks must be a JSON array of task objects; parsed "
            f"{type(parsed).__name__} instead."
        )
    return parsed, None


def _detach_background_children(parent_agent: Any, children: list[tuple[int, dict, Any]]) -> None:
    """Transfer interrupt ownership from the parent turn to async delegation."""
    active = getattr(parent_agent, "_active_children", None)
    if not isinstance(active, list):
        return
    lock = getattr(parent_agent, "_active_children_lock", None)
    for _index, _task, child in children:
        try:
            if lock:
                with lock:
                    active.remove(child)
            else:
                active.remove(child)
        except ValueError:
            pass


def _discard_unstarted_background_children(
    children: list[tuple[int, dict, Any]],
    reason: str,
) -> None:
    """Close children whose detached batch could not be scheduled."""
    for _index, _task, child in children:
        progress = getattr(child, "tool_progress_callback", None)
        if progress is not None:
            try:
                progress(
                    "subagent.complete",
                    preview=reason,
                    status="rejected",
                    duration_seconds=0,
                    summary=reason,
                )
            except Exception:
                pass
        try:
            _end_failed_child_session(child, "error")
            if hasattr(child, "close"):
                child.close()
        except Exception:
            logger.debug("Failed to close unscheduled background child", exc_info=True)


def _execute_background_children(
    children: list[tuple[int, dict, Any]],
    parent_agent: Any,
    max_children: int,
    accounting_context: Dict[str, Any],
) -> Dict[str, Any]:
    """Run a pre-built fan-out as one detached unit and consolidate results."""
    started = time.monotonic()
    if len(children) == 1:
        index, task, child = children[0]
        results = [
            _run_single_child(
                index,
                task["goal"],
                child,
                parent_agent,
                parent_task_id=accounting_context.get("parent_task_id"),
                ignore_parent_deadline=True,
            )
        ]
    else:
        results = []
        from tools.thread_context import propagate_context_to_thread

        with ThreadPoolExecutor(max_workers=max_children) as executor:
            futures = {
                executor.submit(
                    propagate_context_to_thread(_run_single_child),
                    task_index=index,
                    goal=task["goal"],
                    child=child,
                    parent_agent=parent_agent,
                    parent_task_id=accounting_context.get("parent_task_id"),
                    ignore_parent_deadline=True,
                ): index
                for index, task, child in children
            }
            for future, index in list(futures.items()):
                try:
                    results.append(future.result())
                except Exception as exc:
                    results.append(
                        {
                            "task_index": index,
                            "status": "error",
                            "summary": None,
                            "error": str(exc),
                            "api_calls": 0,
                            "duration_seconds": 0,
                        }
                    )
        results.sort(key=lambda item: item.get("task_index", 0))
    accounting = _finalize_detached_results(
        results,
        children,
        accounting_context,
    )
    return {
        "results": results,
        "total_duration_seconds": round(time.monotonic() - started, 2),
        "accounting": accounting,
    }


def _capture_detached_accounting_context(parent_agent: Any) -> Dict[str, Any]:
    """Snapshot parent identity before a background delegation leaves its turn."""
    root = getattr(parent_agent, "_delegate_root_agent", parent_agent)
    binding = _delegation_binding(root)
    return {
        "version": 1,
        "accounting_id": f"delegation_accounting_{os.urandom(8).hex()}",
        "binding": copy.deepcopy(binding),
        "parent_session_id": str(getattr(parent_agent, "session_id", "") or ""),
        "parent_turn_id": str(
            getattr(parent_agent, "_current_turn_id", "")
            or getattr(parent_agent, "_current_task_id", "")
            or ""
        ),
        "parent_task_id": str(getattr(parent_agent, "_current_task_id", "") or ""),
    }


def _finalize_detached_results(
    results: list[Dict[str, Any]],
    children: list[tuple[int, dict, Any]],
    accounting_context: Dict[str, Any],
) -> Dict[str, Any]:
    """Build immutable accounting for the next serialized parent turn."""
    child_by_index = {index: (task, child) for index, task, child in children}
    cost_total = 0.0
    children_accounting: list[Dict[str, Any]] = []
    for entry in results:
        child_role = entry.pop("_child_role", None)
        try:
            cost_total += float(entry.pop("_child_cost_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
        task, child = child_by_index.get(entry.get("task_index"), ({}, None))
        children_accounting.append(
            {
                "task": str(task.get("goal") or ""),
                "summary": str(entry.get("summary") or ""),
                "child_session_id": str(getattr(child, "session_id", "") or ""),
                "child_subagent_id": str(getattr(child, "_subagent_id", "") or ""),
                "child_role": child_role,
                "child_status": str(entry.get("status") or ""),
                "duration_ms": int((entry.get("duration_seconds") or 0) * 1000),
                "worker_run": dict(entry.pop("_worker_run", {}) or {}),
            }
        )
    return {
        **copy.deepcopy(accounting_context),
        "children": children_accounting,
        "cost_total_usd": cost_total,
    }


def apply_detached_delegation_accounting(
    parent_agent: Any,
    accounting: Any,
) -> bool:
    """Apply detached callbacks/cost on the next serialized parent turn."""
    if parent_agent is None or not isinstance(accounting, dict) or accounting.get("version") != 1:
        return False
    accounting_id = str(accounting.get("accounting_id") or "")
    if not accounting_id:
        return False
    applied = getattr(parent_agent, "_applied_delegation_accounting_ids", None)
    if not isinstance(applied, set):
        applied = set()
        parent_agent._applied_delegation_accounting_ids = applied
    if accounting_id in applied:
        return False
    expected_binding = accounting.get("binding")
    current_binding = _delegation_binding(parent_agent)
    if isinstance(expected_binding, dict):
        for field in ("session_key", "workspace", "repository_root"):
            expected = str(expected_binding.get(field) or "")
            current = str(current_binding.get(field) or "")
            if expected and current and expected != current:
                return False
    applied.add(accounting_id)

    memory_manager = getattr(parent_agent, "_memory_manager", None)
    try:
        from hermes_cli.plugins import invoke_hook
    except Exception:
        invoke_hook = None
    for child in accounting.get("children") or []:
        if not isinstance(child, dict):
            continue
        if memory_manager is not None:
            try:
                memory_manager.on_delegation(
                    task=str(child.get("task") or ""),
                    result=str(child.get("summary") or ""),
                    child_session_id=str(child.get("child_session_id") or ""),
                )
            except Exception:
                logger.debug("detached delegation memory callback failed", exc_info=True)
        if invoke_hook is not None:
            try:
                invoke_hook(
                    "subagent_stop",
                    root_session_id=str(
                        (accounting.get("binding") or {}).get("session_id") or ""
                    )
                    or None,
                    parent_session_id=str(accounting.get("parent_session_id") or "") or None,
                    parent_turn_id=str(accounting.get("parent_turn_id") or ""),
                    child_session_id=str(child.get("child_session_id") or "") or None,
                    child_subagent_id=str(child.get("child_subagent_id") or "") or None,
                    child_role=child.get("child_role"),
                    child_summary=child.get("summary"),
                    child_status=child.get("child_status"),
                    duration_ms=int(child.get("duration_ms") or 0),
                )
            except Exception:
                logger.debug("detached subagent_stop hook failed", exc_info=True)
        worker_run = child.get("worker_run")
        if isinstance(worker_run, dict) and worker_run.get("model"):
            append_turn_worker_run(parent_agent, dict(worker_run))

    try:
        cost_total = float(accounting.get("cost_total_usd") or 0.0)
    except (TypeError, ValueError):
        cost_total = 0.0
    if cost_total > 0:
        parent_agent.session_estimated_cost_usd = float(
            getattr(parent_agent, "session_estimated_cost_usd", 0.0) or 0.0
        ) + cost_total
        if getattr(parent_agent, "session_cost_source", "none") in {None, "", "none"}:
            parent_agent.session_cost_source = "subagent"
        if getattr(parent_agent, "session_cost_status", "unknown") in {None, "", "unknown"}:
            parent_agent.session_cost_status = "estimated"
    return True


def _background_context_error(parent_agent: Any) -> str:
    platform = str(getattr(parent_agent, "platform", "") or "").strip().lower()
    try:
        from gateway.session_context import async_delivery_supported, is_cron_execution

        if platform == "cron" or is_cron_execution():
            return "Background delegation is unavailable in cron sessions."
        if not async_delivery_supported():
            return (
                "Background delegation is unavailable because this session "
                "cannot receive a later completion turn."
            )
    except Exception:
        if platform == "cron" or os.environ.get("HERMES_CRON_SESSION"):
            return "Background delegation is unavailable in cron sessions."
    if os.environ.get("HERMES_KANBAN_TASK"):
        return "Background delegation is unavailable in Kanban worker sessions."
    return ""


def _model_background_value(args: dict, parent_agent: Any = None) -> bool:
    """Return the caller's optional detached-delegation request."""
    return is_truthy_value((args or {}).get("background"), default=False)


def delegate_task(
    goal: Optional[str] = None,
    context: Optional[str] = None,
    toolsets: Optional[List[str]] = None,
    tasks: Optional[List[Dict[str, Any]]] = None,
    max_iterations: Optional[int] = None,
    acp_command: Optional[str] = None,
    acp_args: Optional[List[str]] = None,
    role: Optional[str] = None,
    model_tier: Optional[str] = None,
    purpose: Optional[str] = None,
    read_only: bool = False,
    background: bool = False,
    allow_nested_coding: bool = False,
    parent_agent=None,
) -> str:
    """
    Spawn one or more child agents to handle delegated tasks.

    Supports two modes:
      - Single: provide goal (+ optional context, role, model_tier, and purpose)
      - Batch:  provide tasks array [{goal, context, role, model_tier, purpose}, ...]

    The 'role' parameter controls whether a child can further delegate:
    'leaf' (default) cannot; 'orchestrator' retains the delegation
    toolset and can spawn its own workers, bounded by
    delegation.max_spawn_depth.  Per-task role beats the top-level one.
    Per-task model_tier likewise beats the top-level value. When omitted, the
    child inherits the parent runtime model and reasoning unless an explicit
    operator-owned delegation runtime override is configured.

    Returns JSON with results array, one entry per task.
    """
    if parent_agent is None:
        return tool_error("delegate_task requires a parent agent context.")

    from agent.worker_budget import remaining_nested_worker_budget

    if remaining_nested_worker_budget(parent_agent, _get_child_timeout()) <= 0:
        return tool_error(
            "Parent turn nested-worker deadline was exhausted before delegation launch."
        )

    if _interpreter_shutdown_in_progress():
        return tool_error(_delegation_shutdown_message())

    # Operator-controlled kill switch — lets the TUI freeze new fan-out
    # when a runaway tree is detected, without interrupting already-running
    # children.  Cleared via the matching `delegation.pause` RPC.
    if is_spawn_paused():
        return tool_error(
            "Delegation spawning is paused. Clear the pause via the TUI "
            "(`p` in /agents) or the `delegation.pause` RPC before retrying."
        )

    # Read-only is monotonic down the delegation tree. A child cannot turn off
    # a restriction imposed by its parent.
    inherited_read_only = bool(
        getattr(parent_agent, "_delegation_read_only", False)
        or normalize_runtime_mode(
            getattr(parent_agent, "_runtime_mode", None),
            default=RuntimeMode.ACTION,
        )
        is RuntimeMode.READ_ONLY
    )
    top_read_only = inherited_read_only or is_truthy_value(read_only, default=False)
    if top_read_only and (acp_command is not None or acp_args is not None):
        return tool_error(
            "Read-only delegation does not accept model-supplied ACP transport overrides. "
            "Configure the delegation transport outside the tool call instead."
        )
    background_requested = is_truthy_value(background, default=False)
    background = background_requested

    # Normalise the top-level role once; per-task overrides re-normalise.
    top_role = _normalize_role(role)
    requested_nested_coding = is_truthy_value(allow_nested_coding, default=False)
    if background:
        context_error = _background_context_error(parent_agent)
        if context_error:
            logger.info(
                "delegate_task background delivery unavailable (%s); running synchronously",
                context_error,
            )
            background = False

    # Depth limit — configurable via delegation.max_spawn_depth,
    # default 2 for parity with the original MAX_DEPTH constant.
    depth = getattr(parent_agent, "_delegate_depth", 0)
    max_spawn = _get_max_spawn_depth()
    if depth >= max_spawn:
        return json.dumps(
            {
                "error": (
                    f"Delegation depth limit reached (depth={depth}, "
                    f"max_spawn_depth={max_spawn}). Raise "
                    f"delegation.max_spawn_depth in config.yaml if deeper "
                    f"nesting is required (cap: {_MAX_SPAWN_DEPTH_CAP})."
                )
            }
        )

    # Load config
    cfg = _load_config()
    default_max_iter = cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS)
    # Model-supplied max_iterations is ignored — the config value is authoritative
    # so users get predictable budgets. The kwarg is retained for internal callers
    # and tests; a model-emitted value here would only shrink the budget and
    # surprise the user mid-run. Log and drop it if one slips through from a
    # cached tool schema or a stale provider.
    if max_iterations is not None and max_iterations != default_max_iter:
        logger.debug(
            "delegate_task: ignoring caller-supplied max_iterations=%s; "
            "using delegation.max_iterations=%s from config",
            max_iterations, default_max_iter,
        )
    effective_max_iter = default_max_iter

    # Normalize to task list
    max_children = _get_max_concurrent_children()
    recovered_tasks, tasks_error = _recover_tasks_from_json_string(tasks)
    if tasks_error:
        return tool_error(tasks_error)
    if recovered_tasks is not None:
        tasks = recovered_tasks

    if tasks and isinstance(tasks, list):
        if len(tasks) > max_children:
            return tool_error(
                f"Too many tasks: {len(tasks)} provided, but "
                f"max_concurrent_children is {max_children}. "
                f"Either reduce the task count, split into multiple "
                f"delegate_task calls, or increase "
                f"delegation.max_concurrent_children in config.yaml."
            )
        task_list = tasks
    elif goal and isinstance(goal, str) and goal.strip():
        task_list = [
            {
                "goal": goal,
                "context": context,
                "toolsets": toolsets,
                "role": top_role,
                "model_tier": model_tier,
                "purpose": purpose,
                "read_only": top_read_only,
                "allow_nested_coding": requested_nested_coding,
            }
        ]
    else:
        return tool_error("Provide either 'goal' (single task) or 'tasks' (batch).")

    if not task_list:
        return tool_error("No tasks provided.")

    if top_read_only and any(
        isinstance(task, dict)
        and any(field in task for field in _MODEL_HIDDEN_TASK_FIELDS)
        for task in task_list
    ):
        return tool_error(
            "Read-only delegation does not accept per-task ACP transport overrides."
        )

    # Validate every task, including per-task broker grants, before constructing
    # any child. A batch must fail atomically rather than partially building a
    # silently degraded leaf for an invalid explicit grant.
    for i, task in enumerate(task_list):
        if not isinstance(task, dict):
            return tool_error(
                f"Task {i} must be an object, got {type(task).__name__}."
            )
        if not task.get("goal", "").strip():
            return tool_error(f"Task {i} is missing a 'goal'.")
        task["read_only"] = top_read_only or is_truthy_value(
            task.get("read_only"), default=False
        )
        task["allow_nested_coding"] = is_truthy_value(
            task.get("allow_nested_coding", requested_nested_coding), default=False
        )
        task["role"] = _normalize_role(task.get("role") or top_role)
        task["model_tier"] = (
            task.get("model_tier") if "model_tier" in task else model_tier
        )
        task["purpose"] = task.get("purpose") if "purpose" in task else purpose
        requested_task_toolsets = task.get("toolsets") or toolsets
        if requested_task_toolsets:
            if not isinstance(requested_task_toolsets, list):
                return tool_error(f"Task {i}: toolsets must be an array of names.")
            parent_enabled = getattr(parent_agent, "enabled_toolsets", None)
            if parent_enabled is not None:
                parent_toolsets = set(parent_enabled)
            elif hasattr(parent_agent, "valid_tool_names"):
                import model_tools

                parent_toolsets = {
                    toolset_name
                    for tool_name in parent_agent.valid_tool_names
                    if (
                        toolset_name := model_tools.get_toolset_for_tool(tool_name)
                    )
                    is not None
                }
            else:
                parent_toolsets = set(DEFAULT_TOOLSETS)
            resolved_toolsets, unavailable_toolsets = _resolve_requested_child_toolsets(
                requested_task_toolsets, parent_toolsets
            )
            if unavailable_toolsets:
                return tool_error(
                    f"Task {i}: requested toolsets are unavailable to the parent: "
                    f"{', '.join(unavailable_toolsets)}. No child was launched."
                )
            if not resolved_toolsets:
                return tool_error(
                    f"Task {i}: requested toolsets grant no usable child tools. "
                    "No child was launched."
                )
            task["toolsets"] = resolved_toolsets
        grant_error = _nested_coding_grant_error(
            requested=bool(task["allow_nested_coding"]),
            background=background_requested,
            read_only=bool(task["read_only"]),
            role=task["role"],
            parent_agent=parent_agent,
            max_spawn_depth=max_spawn,
        )
        if grant_error:
            return tool_error(f"Task {i}: {grant_error}")

    # A reserved deep review commonly has a larger configured worker budget
    # than a gateway turn can synchronously hold open. Detach it when async
    # delivery is available rather than guaranteeing a parent-deadline failure.
    # Explicit background=false remains synchronous for ordinary delegations.
    if not background and all(bool(task.get("read_only")) for task in task_list):
        has_deep_review = any(
            str(task.get("model_tier") or "").strip().lower() == "deep_review"
            for task in task_list
        )
        if has_deep_review:
            requested_timeout = _get_child_timeout()
            remaining_timeout = remaining_nested_worker_budget(
                parent_agent, requested_timeout
            )
            if (
                remaining_timeout < requested_timeout
                and not _background_context_error(parent_agent)
            ):
                logger.info(
                    "Auto-detaching read-only deep_review delegation: child timeout "
                    "%.1fs exceeds remaining parent-turn budget %.1fs",
                    requested_timeout,
                    remaining_timeout,
                )
                background = True

    if background and not all(bool(task.get("read_only")) for task in task_list):
        return tool_error(
            "delegate_task background=true requires read_only=true for every task. "
            "Detached general delegates do not own mutation reservations; use "
            "delegate_coding_task(background=true) for repository changes."
        )

    # Resolve every task's explicit tier and provider credentials before any
    # child/runtime setup so a bad batch fails atomically. Tier selection is
    # per-task because a batch may deliberately mix difficulty levels.
    task_runtimes: List[tuple[Any, Dict[str, Any]]] = []
    credential_cache: Dict[tuple[Optional[str], Optional[str]], Dict[str, Any]] = {}
    for i, task in enumerate(task_list):
        from hermes_cli.model_tiers import (
            DEFAULT_MODEL_TIERS,
            VISUAL_DELEGATION_PURPOSE_TIERS,
            restrict_model_tier_for_task,
        )

        requested_purpose = str(task.get("purpose") or "").strip().lower()
        purpose_tier = VISUAL_DELEGATION_PURPOSE_TIERS.get(requested_purpose)
        if requested_purpose and purpose_tier is None:
            supported = ", ".join(VISUAL_DELEGATION_PURPOSE_TIERS)
            return tool_error(
                f"Task {i}: unknown delegation purpose {requested_purpose!r}. "
                f"Supported purposes: {supported}."
            )
        requested_tier = str(task.get("model_tier") or "").strip()
        deep_review_error = _deep_review_tier_error(parent_agent, requested_tier)
        if deep_review_error:
            return tool_error(f"Task {i}: {deep_review_error}")
        if purpose_tier and requested_tier and requested_tier.lower() != purpose_tier:
            return tool_error(
                f"Task {i}: purpose {requested_purpose!r} requires model_tier "
                f"{purpose_tier!r}, not {requested_tier!r}. Omit model_tier to "
                "select the purpose tier automatically."
            )
        if purpose_tier:
            requested_tier = purpose_tier
            task["model_tier"] = purpose_tier
        resolved_tier = _resolve_delegation_model_tier(cfg, requested_tier)
        if requested_tier and resolved_tier is None:
            built_ins = ", ".join(DEFAULT_MODEL_TIERS)
            return tool_error(
                f"Task {i}: unknown model_tier {requested_tier!r}. Configure a "
                f"custom tier under model_tiers or use a built-in tier: {built_ins}."
            )
        if resolved_tier is not None and purpose_tier:
            resolved_tier = restrict_model_tier_for_task(
                cfg,
                resolved_tier,
                task.get("goal"),
                task.get("context"),
                purpose="review",
            )
        target_model = resolved_tier.model if resolved_tier else None
        target_provider = resolved_tier.provider if resolved_tier else None
        credential_key = (target_provider, target_model)
        task_creds = credential_cache.get(credential_key)
        if task_creds is None:
            try:
                task_creds = _resolve_delegation_credentials(
                    cfg,
                    parent_agent,
                    target_model=target_model,
                    target_provider=target_provider,
                )
            except ValueError as exc:
                return tool_error(str(exc))
            credential_cache[credential_key] = task_creds
        task_runtimes.append((resolved_tier, task_creds))

    overall_start = time.monotonic()
    results = []

    n_tasks = len(task_list)
    # Track goal labels for progress display (truncated for readability)
    task_labels = [t["goal"][:40] for t in task_list]

    # Live transcripts: one pre-headered append-only log per task under
    # cache/delegation/live/<delegation_id>/task-<n>.log so the caller can
    # tail each child's operations while it runs (side-channel only — zero
    # effect on message content or prompt caching). Best-effort: on failure
    # live_paths is empty and delegation proceeds exactly as before.
    from tools.delegation_live_log import (
        create_live_transcripts,
        update_manifest_statuses,
        wrap_progress_callback,
    )

    live_deleg_id, live_writers, live_paths = create_live_transcripts(
        task_list, context
    )

    def _finalize_live_results(result_items: List[Dict[str, Any]]) -> None:
        """Close transcript writers and attach retained paths to each result."""
        for entry in result_items:
            index = entry.get("task_index", -1)
            writer = (
                live_writers[index]
                if isinstance(index, int) and 0 <= index < len(live_writers)
                else None
            )
            if writer is not None:
                try:
                    writer.finalize(entry)
                except Exception:
                    logger.debug("Live transcript finalize failed", exc_info=True)
                if index < len(live_paths):
                    entry["live_transcript"] = live_paths[index]
        update_manifest_statuses(live_deleg_id, result_items)

    # Save parent tool names BEFORE any child construction mutates the global.
    # _build_child_agent() calls AIAgent() which calls get_tool_definitions(),
    # which overwrites model_tools._last_resolved_tool_names with child's toolset.
    import model_tools as _model_tools

    _parent_tool_names = list(_model_tools._last_resolved_tool_names)

    # Build all child agents on the main thread (thread-safe construction)
    # Wrapped in try/finally so the global is always restored even if a
    # child build raises (otherwise _last_resolved_tool_names stays corrupted).
    children = []
    try:
        for i, t in enumerate(task_list):
            task_acp_args = t.get("acp_args") if "acp_args" in t else None
            # Per-task role beats top-level; normalise again so unknown
            # per-task values warn and degrade to leaf uniformly.
            effective_role = t["role"]
            resolved_tier, creds = task_runtimes[i]
            explicit_runtime = any(
                str(cfg.get(key) or "").strip()
                for key in ("provider", "base_url", "model", "reasoning_effort")
            )
            raw_reasoning_effort = str(cfg.get("reasoning_effort") or "").strip()
            if resolved_tier is not None:
                model_tier_source = "explicit"
                reasoning_source = "model_tier"
            else:
                model_tier_source = (
                    "explicit_override"
                    if explicit_runtime
                    else "parent"
                )
                if raw_reasoning_effort:
                    from hermes_constants import parse_reasoning_effort

                    reasoning_source = (
                        "delegation_config"
                        if parse_reasoning_effort(raw_reasoning_effort) is not None
                        else "parent"
                    )
                else:
                    reasoning_source = "parent"
            child = _build_child_agent(
                task_index=i,
                goal=t["goal"],
                context=t.get("context"),
                toolsets=t.get("toolsets") or toolsets,
                model=(resolved_tier.model if resolved_tier else creds["model"]),
                max_iterations=effective_max_iter,
                task_count=n_tasks,
                parent_agent=parent_agent,
                override_provider=creds["provider"],
                override_base_url=creds["base_url"],
                override_api_key=creds["api_key"],
                override_api_mode=creds["api_mode"],
                override_request_overrides=creds.get("request_overrides"),
                override_max_tokens=creds.get("max_output_tokens"),
                override_acp_command=t.get("acp_command")
                or acp_command
                or creds.get("command"),
                override_acp_args=(
                    task_acp_args
                    if task_acp_args is not None
                    else (acp_args if acp_args is not None else creds.get("args"))
                ),
                override_reasoning_config=(
                    resolved_tier.reasoning_config() if resolved_tier else None
                ),
                role=effective_role,
                read_only=bool(t.get("read_only")),
                allow_nested_coding=bool(t.get("allow_nested_coding")),
                runtime_audit_context={
                    "model_tier": resolved_tier.name if resolved_tier is not None else "",
                    "model_tier_source": model_tier_source,
                    "runtime_pass": "selected" if resolved_tier is not None else "inherited",
                    "reasoning_source": reasoning_source,
                },
                # Explicit visual purposes designate an exact runtime. Do not
                # silently turn an Opus/Luna/Sonnet visual stage into a parent
                # fallback model while retaining the visual-purpose label.
                inherit_fallback=not bool(t.get("purpose")),
            )
            # Override with correct parent tool names (before child construction mutated global)
            child._delegate_saved_tool_names = _parent_tool_names
            # Tee the child's progress events into its live transcript log.
            # wrap_progress_callback preserves the inner callback contract
            # (including the _flush attribute) and never lets writer failures
            # reach the agent loop. When no parent display exists the inner
            # callback is None and the wrapper still records events.
            _writer = live_writers[i] if i < len(live_writers) else None
            if _writer is not None:
                child.tool_progress_callback = wrap_progress_callback(
                    getattr(child, "tool_progress_callback", None), _writer
                )
                child._live_transcript_stream_callback = _writer.add_stream_delta
                child._live_transcript_path = str(_writer.path)
            children.append((i, t, child))
    finally:
        # Authoritative restore: reset global to parent's tool names after all children built
        _model_tools._last_resolved_tool_names = _parent_tool_names

    if background:
        accounting_context = _capture_detached_accounting_context(parent_agent)
        raw_work_item_id = (
            getattr(parent_agent, "_origin_work_item_id", "")
            or getattr(parent_agent, "work_item_id", "")
            or ""
        )
        origin_work_item_id = (
            raw_work_item_id if isinstance(raw_work_item_id, str) else ""
        )
        raw_attempt_id = getattr(
            parent_agent,
            "_origin_work_item_attempt_id",
            "",
        )
        origin_attempt_id = raw_attempt_id if isinstance(raw_attempt_id, str) else ""
        raw_process_epoch = getattr(
            parent_agent,
            "_origin_work_item_process_epoch",
            "",
        )
        origin_process_epoch = (
            raw_process_epoch if isinstance(raw_process_epoch, str) else ""
        )
        try:
            from tools.approval import get_current_session_key

            session_key = get_current_session_key(default="")
        except Exception:
            session_key = ""
        origin_ui_session_id = ""
        try:
            from gateway.session_context import get_session_env

            source = str(get_session_env("HERMES_SESSION_SOURCE", "") or "")
            origin_ui_session_id = str(
                get_session_env("HERMES_UI_SESSION_ID", "") or ""
            )
            if source == "tui":
                session_key = str(getattr(parent_agent, "session_id", "") or session_key)
        except Exception:
            pass
        session_key = str(
            session_key
            or getattr(parent_agent, "gateway_session_key", "")
            or getattr(parent_agent, "session_key", "")
            or getattr(parent_agent, "session_id", "")
            or ""
        )
        _detach_background_children(parent_agent, children)

        def _batch_runner() -> Dict[str, Any]:
            combined = _execute_background_children(
                children,
                parent_agent,
                max_children,
                accounting_context,
            )
            _finalize_live_results(combined.get("results") or [])
            if live_paths:
                combined["live_transcripts"] = list(live_paths)
            return combined

        def _batch_interrupt() -> None:
            for _index, _task, child in children:
                try:
                    child.interrupt("Async delegation cancelled")
                except Exception:
                    try:
                        child._interrupt_requested = True
                    except Exception:
                        pass

        from tools.async_delegation import dispatch_async_delegation_batch

        goals = [str(task["goal"]) for task in task_list]
        background_task_specs: List[Dict[str, Any]] = []
        for i, (_index, task, child) in enumerate(children):
            resolved_tier, task_creds = task_runtimes[i]
            child_model = getattr(child, "model", None)
            if not isinstance(child_model, str) or not child_model.strip():
                child_model = (
                    resolved_tier.model
                    if resolved_tier is not None
                    else task_creds.get("model")
                    or getattr(parent_agent, "model", None)
                )
            normalized_child_model = str(child_model or "").strip() or None
            background_task_specs.append(
                {
                    "goal": str(task.get("goal") or ""),
                    "context": str(task.get("context") or ""),
                    "role": str(task.get("role") or top_role),
                    "read_only": bool(task.get("read_only")),
                    "model_tier": (
                        resolved_tier.name if resolved_tier is not None else None
                    ),
                    "model": normalized_child_model,
                }
            )
        batch_models = [
            str(spec.get("model") or "").strip() or None
            for spec in background_task_specs
        ]
        shared_batch_model = (
            batch_models[0]
            if batch_models
            and batch_models[0] is not None
            and all(model == batch_models[0] for model in batch_models)
            else None
        )
        dispatch = dispatch_async_delegation_batch(
            goals=goals,
            context=context,
            toolsets=toolsets,
            role=top_role,
            model=shared_batch_model,
            session_key=session_key,
            origin_ui_session_id=origin_ui_session_id,
            parent_session_id=getattr(parent_agent, "session_id", None),
            runner=_batch_runner,
            interrupt_fn=_batch_interrupt,
            max_async_children=_get_max_async_children(),
            origin_work_item_id=origin_work_item_id,
            origin_run_generation=getattr(
                parent_agent,
                "_origin_work_item_generation",
                None,
            ),
            origin_attempt_id=origin_attempt_id,
            origin_attempt_order=getattr(
                parent_agent,
                "_origin_work_item_attempt_order",
                None,
            ),
            origin_owner_pid=getattr(
                parent_agent,
                "_origin_work_item_owner_pid",
                None,
            ),
            origin_process_epoch=origin_process_epoch,
            read_only=all(bool(task.get("read_only")) for task in task_list),
            task_specs=background_task_specs,
            delegation_id=live_deleg_id,
        )
        if dispatch.get("status") == "dispatched":
            return json.dumps(
                {
                    "status": "dispatched",
                    "mode": "background",
                    "count": len(goals),
                    "delegation_id": dispatch["delegation_id"],
                    "goals": goals,
                    "read_only": all(bool(task.get("read_only")) for task in task_list),
                    "note": (
                        "Delegation is running in the background. Its result is attached "
                        "to this originating attempt and will be included in the single "
                        "terminal response when ready; do not poll this dispatch handle."
                    ),
                    "live_transcripts": list(live_paths),
                    "live_transcripts_hint": (
                        "Each path is an append-only, redacted child transcript "
                        "that can be inspected while the delegation runs."
                    ),
                },
                ensure_ascii=False,
            )
        scheduling_error = str(
            dispatch.get("error") or "Background delegation could not be scheduled."
        )
        _discard_unstarted_background_children(children, scheduling_error)
        return tool_error(scheduling_error)

    if n_tasks == 1:
        # Single task -- run directly (no thread pool overhead)
        _i, _t, child = children[0]
        result = _run_single_child(0, _t["goal"], child, parent_agent)
        results.append(result)
    else:
        # Batch -- run in parallel with per-task progress lines
        completed_count = 0
        spinner_ref = getattr(parent_agent, "_delegate_spinner", None)

        try:
            executor_cm = ThreadPoolExecutor(max_workers=max_children)
        except RuntimeError as exc:
            if _is_interpreter_shutdown_error(exc):
                return tool_error(_delegation_shutdown_message())
            raise
        with executor_cm as executor:
            futures = {}
            for i, t, child in children:
                try:
                    future = executor.submit(
                        _run_single_child,
                        task_index=i,
                        goal=t["goal"],
                        child=child,
                        parent_agent=parent_agent,
                    )
                except RuntimeError as exc:
                    if _is_interpreter_shutdown_error(exc):
                        return tool_error(_delegation_shutdown_message())
                    raise
                futures[future] = i

            # Poll futures with interrupt checking.  as_completed() blocks
            # until ALL futures finish — if a child agent gets stuck,
            # the parent blocks forever even after interrupt propagation.
            # Instead, use wait() with a short timeout so we can bail
            # when the parent is interrupted.
            # Map task_index -> child agent, so fabricated entries for
            # still-pending futures can carry the correct _delegate_role.
            _child_by_index = {i: child for (i, _, child) in children}

            pending = set(futures.keys())
            while pending:
                if getattr(parent_agent, "_interrupt_requested", False) is True:
                    # Parent interrupted — collect whatever finished and
                    # abandon the rest.  Children already received the
                    # interrupt signal; we just can't wait forever.
                    for f in pending:
                        idx = futures[f]
                        if f.done():
                            try:
                                entry = f.result()
                            except Exception as exc:
                                entry = {
                                    "task_index": idx,
                                    "status": "error",
                                    "summary": None,
                                    "error": str(exc),
                                    "api_calls": 0,
                                    "duration_seconds": 0,
                                    "_child_role": getattr(
                                        _child_by_index.get(idx), "_delegate_role", None
                                    ),
                                }
                        else:
                            entry = {
                                "task_index": idx,
                                "status": "interrupted",
                                "summary": None,
                                "error": "Parent agent interrupted — child did not finish in time",
                                "api_calls": 0,
                                "duration_seconds": 0,
                                "_child_role": getattr(
                                    _child_by_index.get(idx), "_delegate_role", None
                                ),
                            }
                        results.append(entry)
                        completed_count += 1
                    break

                from concurrent.futures import wait as _cf_wait, FIRST_COMPLETED

                done, pending = _cf_wait(
                    pending, timeout=0.5, return_when=FIRST_COMPLETED
                )
                for future in done:
                    try:
                        entry = future.result()
                    except Exception as exc:
                        idx = futures[future]
                        entry = {
                            "task_index": idx,
                            "status": "error",
                            "summary": None,
                            "error": str(exc),
                            "api_calls": 0,
                            "duration_seconds": 0,
                            "_child_role": getattr(
                                _child_by_index.get(idx), "_delegate_role", None
                            ),
                        }
                    results.append(entry)
                    completed_count += 1

                    # Print per-task completion line above the spinner
                    idx = entry["task_index"]
                    label = (
                        task_labels[idx] if idx < len(task_labels) else f"Task {idx}"
                    )
                    dur = entry.get("duration_seconds", 0)
                    status = entry.get("status", "?")
                    icon = "✓" if status == "completed" else "✗"
                    remaining = n_tasks - completed_count
                    completion_line = f"{icon} [{idx+1}/{n_tasks}] {label}  ({dur}s)"
                    if spinner_ref:
                        try:
                            spinner_ref.print_above(completion_line)
                        except Exception:
                            print(f"  {completion_line}")
                    else:
                        print(f"  {completion_line}")

                    # Update spinner text to show remaining count
                    if spinner_ref and remaining > 0:
                        try:
                            spinner_ref.update_text(
                                f"🔀 {remaining} task{'s' if remaining != 1 else ''} remaining"
                            )
                        except Exception as e:
                            logger.debug("Spinner update_text failed: %s", e)

        # Sort by task_index so results match input order
        results.sort(key=lambda r: r["task_index"])

    # Bound the aggregate child output before it re-enters the parent's
    # context. Complete summaries are spilled to disk when truncation occurs.
    _apply_summary_budget(results, parent_agent)

    # Notify parent's memory provider of delegation outcomes
    if (
        parent_agent
        and hasattr(parent_agent, "_memory_manager")
        and parent_agent._memory_manager
    ):
        for entry in results:
            try:
                _task_goal = (
                    task_list[entry["task_index"]]["goal"]
                    if entry["task_index"] < len(task_list)
                    else ""
                )
                parent_agent._memory_manager.on_delegation(
                    task=_task_goal,
                    result=entry.get("summary", "") or "",
                    child_session_id=(
                        getattr(children[entry["task_index"]][2], "session_id", "")
                        if entry["task_index"] < len(children)
                        else ""
                    ),
                )
            except Exception:
                pass

    # Fire subagent_stop hooks once per child, serialised on the parent thread.
    # This keeps Python-plugin and shell-hook callbacks off of the worker threads
    # that ran the children, so hook authors don't need to reason about
    # concurrent invocation.  Role was captured into the entry dict in
    # _run_single_child (or the fabricated-entry branches above) before the
    # child was closed.
    _parent_session_id = getattr(parent_agent, "session_id", None)
    _root_session_id = getattr(
        getattr(parent_agent, "_delegate_root_agent", parent_agent),
        "session_id",
        _parent_session_id,
    )
    _parent_turn_id = str(
        getattr(parent_agent, "_current_turn_id", "")
        or getattr(parent_agent, "_current_task_id", "")
        or ""
    )
    try:
        from hermes_cli.plugins import invoke_hook as _invoke_hook
    except Exception:
        _invoke_hook = None
    # Aggregate child spend here so the parent's footer/UI reflect the true
    # cost of a subagent-heavy turn.  Port of Kilo-Org/kilocode#9448.  Each
    # child's cost was captured in _run_single_child before its AIAgent was
    # closed; we fold them into the parent in one pass alongside the
    # subagent_stop hook loop so we don't walk `results` twice.
    _children_cost_total = 0.0
    _observer_child_by_index = {
        index: child for index, _task, child in children
    }
    for entry in results:
        child_role = entry.pop("_child_role", None)
        observer_child = _observer_child_by_index.get(entry.get("task_index"))
        worker_run = entry.pop("_worker_run", None)
        if isinstance(worker_run, dict) and worker_run.get("model"):
            append_turn_worker_run(parent_agent, dict(worker_run))
        child_cost = entry.pop("_child_cost_usd", 0.0)
        try:
            if child_cost:
                _children_cost_total += float(child_cost)
        except (TypeError, ValueError):
            pass
        if _invoke_hook is None:
            continue
        try:
            _invoke_hook(
                "subagent_stop",
                root_session_id=str(_root_session_id or "") or None,
                parent_session_id=_parent_session_id,
                parent_turn_id=_parent_turn_id,
                child_session_id=(
                    str(getattr(observer_child, "session_id", "") or "") or None
                ),
                child_subagent_id=(
                    str(getattr(observer_child, "_subagent_id", "") or "") or None
                ),
                child_role=child_role,
                child_summary=entry.get("summary"),
                child_status=entry.get("status"),
                duration_ms=int((entry.get("duration_seconds") or 0) * 1000),
            )
        except Exception:
            logger.debug("subagent_stop hook invocation failed", exc_info=True)

    # Fold the aggregated child cost into the parent's session total.  This is
    # additive — each delegate_task call contributes its own children — so
    # nested orchestrator→worker trees roll up naturally: each layer's own
    # delegate_task() folds its direct children in, and when the orchestrator
    # itself finishes, its parent folds the orchestrator's now-inflated total
    # on top.  Degrades silently if the parent lacks the counter (older test
    # fixtures, etc.).
    if _children_cost_total > 0.0:
        try:
            current = float(getattr(parent_agent, "session_estimated_cost_usd", 0.0) or 0.0)
            parent_agent.session_estimated_cost_usd = current + _children_cost_total
            # Upgrade the cost_source so the UI doesn't label a partially-real
            # total as "none" when the parent itself hadn't billed any calls
            # yet (rare but possible when the parent's only action this turn
            # was delegate_task).
            if getattr(parent_agent, "session_cost_source", "none") in {None, "", "none"}:
                parent_agent.session_cost_source = "subagent"
            if getattr(parent_agent, "session_cost_status", "unknown") in {None, "", "unknown"}:
                parent_agent.session_cost_status = "estimated"
        except Exception:
            logger.debug("Subagent cost rollup failed", exc_info=True)

    total_duration = round(time.monotonic() - overall_start, 2)
    _finalize_live_results(results)
    payload: Dict[str, Any] = {
        "results": results,
        "total_duration_seconds": total_duration,
    }
    if live_paths:
        payload["live_transcripts"] = list(live_paths)
    return json.dumps(payload, ensure_ascii=False)


def _resolve_child_credential_pool(
    effective_provider: Optional[str],
    parent_agent,
    effective_base_url: Optional[str] = None,
):
    """Resolve a credential pool for the child agent.

    Rules:
    1. Same provider as the parent -> share the parent's pool so cooldown state
       and rotation stay synchronized.
    2. Different provider -> try to load that provider's own pool.
    3. No pool available -> return None and let the child keep the inherited
       fixed credential behavior.
    """
    if not effective_provider:
        return getattr(parent_agent, "_credential_pool", None)

    parent_provider = getattr(parent_agent, "provider", None) or ""
    parent_pool = getattr(parent_agent, "_credential_pool", None)
    if effective_provider == "custom":
        try:
            from agent.credential_pool import (
                get_custom_provider_pool_key,
                load_pool,
            )

            child_key = get_custom_provider_pool_key(effective_base_url or "")
            if not child_key:
                return None
            parent_key = get_custom_provider_pool_key(
                getattr(parent_agent, "base_url", "") or ""
            )
            if parent_pool is not None and child_key == parent_key:
                return parent_pool
            pool = load_pool(child_key)
            if pool is not None and pool.has_credentials():
                return pool
        except Exception as exc:
            logger.debug(
                "Could not load credential pool for custom child endpoint %r: %s",
                effective_base_url,
                exc,
            )
        return None
    if parent_pool is not None and effective_provider == parent_provider:
        return parent_pool

    try:
        from agent.credential_pool import load_pool

        pool = load_pool(effective_provider)
        if pool is not None and pool.has_credentials():
            return pool
    except Exception as exc:
        logger.debug(
            "Could not load credential pool for child provider '%s': %s",
            effective_provider,
            exc,
        )
    return None


def _resolve_delegation_credentials(
    cfg: dict,
    parent_agent,
    *,
    target_model: Optional[str] = None,
    target_provider: Optional[str] = None,
) -> dict:
    """Resolve credentials for subagent delegation.

    ``target_model`` and ``target_provider`` are the task's explicit named-tier
    route. They override legacy delegation runtime values and are passed through
    provider resolution so compatibility/auth validation runs before launch.

    If ``delegation.base_url`` is configured, subagents use that direct
    OpenAI-compatible endpoint. ``delegation.api_key`` overrides the key; when
    omitted, ``api_key`` is returned as ``None`` so ``_build_child_agent``
    inherits the parent agent's key (``effective_api_key = override_api_key or
    parent_api_key``). This lets providers that store their key outside
    ``OPENAI_API_KEY`` (e.g. ``MINIMAX_API_KEY``, ``DASHSCOPE_API_KEY``) work
    without a duplicate config entry.

    Otherwise, if ``delegation.provider`` is configured, the full credential
    bundle (base_url, api_key, api_mode, provider) is resolved via the runtime
    provider system — the same path used by CLI/gateway startup. This lets
    subagents run on a completely different provider:model pair.

    If neither base_url nor provider is configured, returns None values so the
    child inherits everything from the parent agent.

    Raises ValueError with a user-friendly message on credential failure.
    """
    configured_model = str(target_model or cfg.get("model") or "").strip() or None
    configured_provider = str(target_provider or cfg.get("provider") or "").strip() or None
    configured_base_url = str(cfg.get("base_url") or "").strip() or None
    configured_api_key = str(cfg.get("api_key") or "").strip() or None
    configured_api_mode = str(cfg.get("api_mode") or "").strip().lower() or None

    bedrock_aliases = {"bedrock", "aws", "aws-bedrock", "amazon-bedrock", "amazon"}
    if configured_base_url and configured_provider not in bedrock_aliases:
        # When delegation.api_key is not set, return None so _build_child_agent
        # falls back to the parent agent's API key via the credential inheritance
        # path (effective_api_key = override_api_key or parent_api_key). This
        # lets providers that store their key in a non-OPENAI_API_KEY env var
        # (e.g. MINIMAX_API_KEY, DASHSCOPE_API_KEY) work without requiring
        # callers to duplicate the key under delegation.api_key.
        api_key = configured_api_key  # None → inherited from parent in _build_child_agent

        # Use the shared URL-based api_mode detector (same path the main agent's
        # runtime resolver uses) so Anthropic-compatible direct endpoints with a
        # /anthropic suffix — Azure AI Foundry, MiniMax, Zhipu GLM, LiteLLM
        # proxies — pick the right transport automatically. Without this,
        # subagents would default to chat_completions and hit 404s on endpoints
        # that only speak the Anthropic Messages protocol. Fixes #10213.
        from hermes_cli.runtime_provider import _detect_api_mode_for_url

        base_lower = configured_base_url.lower()
        provider = "custom"
        api_mode = _detect_api_mode_for_url(configured_base_url) or "chat_completions"
        if (
            base_url_hostname(configured_base_url) == "chatgpt.com"
            and "/backend-api/codex" in base_lower
        ):
            provider = "openai-codex"
            api_mode = "codex_responses"
        elif base_url_hostname(configured_base_url) == "api.anthropic.com":
            provider = "anthropic"
            api_mode = "anthropic_messages"
        elif "api.kimi.com/coding" in base_lower:
            provider = "custom"
            api_mode = "anthropic_messages"

        # Explicit delegation.api_mode in config always wins. Lets users force
        # a transport for non-standard endpoints the URL heuristic can't detect.
        if configured_api_mode in {"chat_completions", "codex_responses", "anthropic_messages"}:
            api_mode = configured_api_mode

        return {
            "model": configured_model,
            "provider": provider,
            "base_url": configured_base_url,
            "api_key": api_key,
            "api_mode": api_mode,
        }

    if not configured_provider:
        # No provider override — child inherits everything from parent
        return {
            "model": configured_model,
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
            "request_overrides": None,
            "max_output_tokens": None,
        }

    # Provider is configured — resolve full credentials
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        runtime = resolve_runtime_provider(requested=configured_provider, target_model=configured_model)
    except Exception as exc:
        raise ValueError(
            f"Cannot resolve delegation provider '{configured_provider}': {exc}. "
            f"Check that the provider is configured (API key set, valid provider name), "
            f"or set delegation.base_url/delegation.api_key for a direct endpoint. "
            f"Available providers: openrouter, nous, zai, kimi-coding, minimax."
        ) from exc

    api_key = runtime.get("api_key", "")
    if not api_key:
        raise ValueError(
            f"Delegation provider '{configured_provider}' resolved but has no API key. "
            f"Set the appropriate environment variable or run 'hermes auth'."
        )

    return {
        "model": configured_model or runtime.get("model") or None,
        "provider": configured_provider if runtime.get("provider") == _RUNTIME_PROVIDER_CUSTOM else runtime.get("provider"),
        "base_url": runtime.get("base_url"),
        "api_key": api_key,
        "api_mode": runtime.get("api_mode"),
        "request_overrides": dict(runtime.get("request_overrides") or {}),
        "max_output_tokens": runtime.get("max_output_tokens"),
        "command": runtime.get("command"),
        "args": list(runtime.get("args") or []),
    }


def _load_config() -> dict:
    """Load delegation config from the active Hermes config.

    Prefer the shared persistent loader because it follows the active
    HERMES_HOME/profile. ``cli.CLI_CONFIG`` is a legacy fallback for entry
    points that cannot import the shared loader; importing it first can return
    an old default ``delegation`` block and hide user-set keys such as
    ``max_concurrent_children``.

    Uses ``load_config_readonly()``: every consumer of this dict is read-only
    (``.get()`` lookups), and this runs on each ``get_definitions()`` schema
    rebuild via ``_get_max_concurrent_children``, so skipping the defensive
    deepcopy matters. Do NOT mutate the returned dict.

    ``HERMES_IGNORE_USER_CONFIG=1`` (``hermes chat --ignore-user-config``) is
    only honored by the legacy ``cli`` loader, not the shared one, so when the
    flag is set we keep ``cli.CLI_CONFIG`` authoritative to preserve the
    flag's contract of suppressing user config.yaml settings.
    """
    prefer_legacy = os.environ.get("HERMES_IGNORE_USER_CONFIG") == "1"
    if not prefer_legacy:
        try:
            from hermes_cli.config import load_config_readonly

            full = load_config_readonly()
            cfg = full.get("delegation") or {}
            if isinstance(cfg, dict):
                result = dict(cfg)
                result["model_tiers"] = full.get("model_tiers") or {}
                return result
        except Exception:
            pass
    try:
        from cli import CLI_CONFIG

        cfg = CLI_CONFIG.get("delegation") or {}
        if isinstance(cfg, dict):
            result = dict(cfg)
            result["model_tiers"] = CLI_CONFIG.get("model_tiers") or {}
            return result
    except Exception:
        pass
    try:
        from hermes_cli.config import load_config

        full = load_config()
        result = dict(full.get("delegation") or {})
        result["model_tiers"] = full.get("model_tiers") or {}
        return result
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# OpenAI Function-Calling Schema
# ---------------------------------------------------------------------------


def _build_top_level_description() -> str:
    """Compose the delegate_task tool description with current runtime limits.

    The model needs to know its actual ceilings (not the framework defaults),
    otherwise it self-caps at "default 3" / "default 2" even when the user has
    raised delegation.max_concurrent_children / max_spawn_depth. Called both
    at module import (to seed DELEGATE_TASK_SCHEMA) and on every
    get_definitions() call via dynamic_schema_overrides.
    """
    try:
        max_children = _get_max_concurrent_children()
    except Exception:
        max_children = _DEFAULT_MAX_CONCURRENT_CHILDREN
    try:
        max_depth = _get_max_spawn_depth()
    except Exception:
        max_depth = MAX_DEPTH
    try:
        orchestrator_on = _get_orchestrator_enabled()
    except Exception:
        orchestrator_on = True

    if max_depth >= 2 and orchestrator_on:
        nesting_clause = (
            f"Nested delegation IS enabled for this user "
            f"(max_spawn_depth={max_depth}): pass role='orchestrator' on a "
            f"child to let it spawn its own workers, up to {max_depth - 1} "
            f"additional level(s) deep."
        )
    elif max_depth >= 2 and not orchestrator_on:
        nesting_clause = (
            f"Nested delegation is DISABLED on this install "
            f"(delegation.orchestrator_enabled=false), even though "
            f"max_spawn_depth={max_depth}. role='orchestrator' is silently "
            f"forced to 'leaf'."
        )
    else:
        nesting_clause = (
            f"Nested delegation is OFF for this user "
            f"(max_spawn_depth={max_depth}): every child is a leaf and "
            f"cannot delegate further. Raise delegation.max_spawn_depth in "
            f"config.yaml to enable nesting."
        )

    return (
        "Spawn one or more subagents to work on tasks in isolated contexts. "
        "Each subagent gets its own conversation, terminal session, and toolset. "
        "Only the final summary is returned -- intermediate tool results "
        "never enter your context window.\n\n"
        "TWO MODES (one of 'goal' or 'tasks' is required):\n"
        "1. Single task: provide 'goal' (+ optional context, toolsets)\n"
        f"2. Batch (parallel): provide 'tasks' array with up to {max_children} "
        f"items concurrently for this user (configured via "
        f"delegation.max_concurrent_children in config.yaml). "
        f"All run in parallel and results are returned together. {nesting_clause}\n\n"
        "Set background=true only for independent work whose result is not on "
        "the current critical path. Background completion returns as a later "
        "turn; never claim completion from the dispatch handle. Independent "
        "synchronous read-only analysis can still run concurrently with other "
        "delegation calls when emitted together in one assistant batch. "
        "Background single tasks and batches share the same global concurrency "
        "cap and persist completed results as Hermes-internal runtime state for "
        "restart-safe delivery; this is not user/project mutation.\n\n"
        "LIVE TRANSCRIPTS: the dispatch response includes 'live_transcripts' — "
        "one append-only human-readable log file per task (under "
        "cache/delegation/live/<delegation_id>/). Each child streams its "
        "assistant text, tool calls, and tool results there while it runs. "
        "These redacted logs/manifests are Hermes-internal cache state, not "
        "user/project mutation. "
        "Read (or `tail -f` in a terminal) those paths any time you or the "
        "user want to see what a subagent is actually doing instead of "
        "waiting for the final summary.\n\n"
        "WHEN TO USE delegate_task:\n"
        "- Reasoning-heavy subtasks (debugging, code review, research synthesis)\n"
        "- Tasks that would flood your context with intermediate data\n"
        "- Parallel independent workstreams (research A and B simultaneously)\n\n"
        "WHEN NOT TO USE (use these instead):\n"
        "- Mechanical multi-step work with no reasoning needed -> use execute_code\n"
        "- Single tool call -> just call the tool directly\n"
        "- Tasks needing user interaction -> subagents cannot use clarify\n"
        "- Running work that itself must survive process/session exit -> "
        "use cronjob (action='create') or terminal(background=True, "
        "notify_on_complete=True) instead. Detached delegation persists terminal "
        "results for delivery, but /stop or process shutdown still cancels live children.\n\n"
        "IMPORTANT:\n"
        "- Subagents have NO memory of your conversation. Pass all relevant "
        "info (file paths, error messages, constraints) via the 'context' field.\n"
        "- Choose model_tier from actual task difficulty, not incidental words "
        "in the goal/context: trivial for obvious tiny work, basic for "
        "straightforward bounded work, intermediate for ordinary multi-step "
        "work, and advanced only for the hardest cross-cutting or high-risk "
        "work. Omit model_tier to inherit your runtime model and reasoning.\n"
        "- deep_review is a reserved Sol/xhigh tier. Use it only when the human's "
        "current root-turn message explicitly requests xhigh or a deep review; it "
        "is rejected for ordinary selection and nested delegation.\n"
        "- For visual work, use the explicit purpose field instead of guessing a "
        "tier from task wording: visual_advisor for one read-only pre-implementation "
        "design consultation, visual_sweep for one browser/navigation evidence "
        "pass, visual_inspector for bounded screenshot judgement over collected "
        "evidence, and visual_critique for at most one final aesthetic critique. "
        "Run the sweep before judgement passes; do not fan out duplicate browser "
        "setup. Each purpose selects its designated model tier automatically.\n"
        "- If the user is writing in a non-English language, or asked for "
        "output in a specific language / tone / style, say so in 'context' "
        "(e.g. \"respond in Chinese\", \"return output in Japanese\"). "
        "Otherwise subagents default to English and their summaries will "
        "contaminate your final reply with the wrong language.\n"
        "- Subagent summaries are SELF-REPORTS, not verified facts. A subagent "
        "that claims \"uploaded successfully\" or \"file written\" may be wrong. "
        "For operations with external side-effects (HTTP POST/PUT, remote "
        "writes, file creation at shared paths, publishing), require the "
        "subagent to return a verifiable handle (URL, ID, absolute path, HTTP "
        "status) and verify it yourself — fetch the URL, stat the file, read "
        "back the content — before telling the user the operation succeeded.\n"
        "- Leaf subagents (role='leaf', the default) CANNOT call: "
        "delegate_task, clarify, memory, send_message, execute_code.\n"
        "- read_only=false is the default in ACTION runtime. Use it when a worker may need bounded "
        "workspace exploration, setup, or in-scope mutation; terminal and file "
        "tools remain available.\n"
        "- read_only=true is inherited automatically from a READ_ONLY parent (you may omit "
        "the argument there), enforced at dispatch time, and propagates to nested "
        "delegate_task calls. It blocks file writes, mutable terminal/process actions, "
        "execute_code, and coding-worker mutation while retaining bounded observation.\n"
        "- background=true requires read_only=true for every task. Detached "
        "repository mutation belongs on delegate_coding_task.\n"
        "- Orchestrator subagents (role='orchestrator') retain "
        "delegate_task so they can spawn their own workers, but still "
        "cannot use clarify, memory, send_message, or execute_code. "
        f"Orchestrators are bounded by max_spawn_depth={max_depth} for this "
        f"user and can be disabled globally via "
        "delegation.orchestrator_enabled=false.\n"
        "- Each subagent gets its own terminal session (separate working directory and state).\n"
        "- Results are always returned as an array, one entry per task."
    )


def _build_tasks_param_description() -> str:
    """Compose the 'tasks' parameter description with current concurrency limit."""
    try:
        max_children = _get_max_concurrent_children()
    except Exception:
        max_children = _DEFAULT_MAX_CONCURRENT_CHILDREN
    return (
        f"Batch mode: tasks to run in parallel (up to {max_children} for this "
        f"user, set via delegation.max_concurrent_children). Each gets "
        "its own subagent with isolated context and terminal session. "
        "When provided, top-level goal/context/role are ignored. Top-level "
        "model_tier and purpose remain batch defaults; per-task values override them."
    )


def _build_role_param_description() -> str:
    """Compose the 'role' parameter description with current spawn-depth limit."""
    try:
        max_depth = _get_max_spawn_depth()
    except Exception:
        max_depth = MAX_DEPTH
    try:
        orchestrator_on = _get_orchestrator_enabled()
    except Exception:
        orchestrator_on = True

    if max_depth >= 2 and orchestrator_on:
        nesting_note = (
            f"Nesting IS enabled for this user (max_spawn_depth={max_depth}): "
            f"orchestrator children can themselves delegate up to {max_depth - 1} "
            "more level(s) deep."
        )
    elif max_depth >= 2 and not orchestrator_on:
        nesting_note = (
            "Nesting is currently disabled "
            "(delegation.orchestrator_enabled=false); 'orchestrator' is "
            "silently forced to 'leaf'."
        )
    else:
        nesting_note = (
            f"Nesting is OFF for this user (max_spawn_depth={max_depth}); "
            "'orchestrator' is silently forced to 'leaf'. Raise "
            "delegation.max_spawn_depth in config.yaml to enable."
        )

    return (
        "Role of the child agent. 'leaf' (default) = focused "
        "worker, cannot delegate further. 'orchestrator' = can "
        f"use delegate_task to spawn its own workers. {nesting_note}"
    )


def _build_dynamic_schema_overrides() -> dict:
    """Return per-call schema overrides reflecting current config.

    Plugged into ToolEntry.dynamic_schema_overrides so every
    get_definitions() pass rewrites the description fields to the user's
    actual limits.
    """
    overrides_params = {
        **DELEGATE_TASK_SCHEMA["parameters"],
    }
    # Deep-copy properties so we don't mutate the static schema dict.
    overrides_params["properties"] = {
        k: dict(v) for k, v in DELEGATE_TASK_SCHEMA["parameters"]["properties"].items()
    }
    overrides_params["properties"]["tasks"]["description"] = _build_tasks_param_description()
    overrides_params["properties"]["role"]["description"] = _build_role_param_description()
    return {
        "description": _build_top_level_description(),
        "parameters": overrides_params,
    }


DELEGATE_TASK_SCHEMA = {
    "name": "delegate_task",
    # NOTE: description / tasks.description / role.description are placeholder
    # values. The real text is generated per get_definitions() call by
    # _build_dynamic_schema_overrides() (registered via
    # dynamic_schema_overrides below) so the model sees the user's actual
    # delegation.max_concurrent_children / max_spawn_depth, not the framework
    # defaults. Building these lazily (instead of at module import) also
    # avoids forcing cli.CLI_CONFIG to load before the test conftest can
    # redirect HERMES_HOME.
    "description": (
        "Spawn one or more subagents in isolated contexts. "
        "Description is rebuilt at every get_definitions() call to reflect "
        "the user's current delegation limits."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "What the subagent should accomplish. Be specific and "
                    "self-contained -- the subagent knows nothing about your "
                    "conversation history."
                ),
            },
            "context": {
                "type": "string",
                "description": (
                    "Background information the subagent needs: file paths, "
                    "error messages, project structure, constraints. The more "
                    "specific you are, the better the subagent performs."
                ),
            },
            "toolsets": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Toolsets to enable for this subagent. Default: inherits "
                    "your enabled toolsets. "
                    f"Available toolsets: {_TOOLSET_LIST_STR}."
                ),
            },
            "tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "goal": {"type": "string", "description": "Task goal"},
                        "context": {
                            "type": "string",
                            "description": "Task-specific context",
                        },
                        "toolsets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": f"Toolsets for this task. Available: {_TOOLSET_LIST_STR}.",
                        },
                        "acp_command": {
                            "type": "string",
                            "description": "Per-task ACP command override for trusted/operator-controlled callers.",
                        },
                        "acp_args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Per-task ACP args override.",
                        },
                        "role": {
                            "type": "string",
                            "enum": ["leaf", "orchestrator"],
                            "description": "Per-task role override. See top-level 'role' for semantics.",
                        },
                        "model_tier": {
                            "type": "string",
                            "description": (
                                "Per-task difficulty tier overriding the top-level value. "
                                "Choose from actual task difficulty: trivial for obvious tiny "
                                "work, basic for straightforward bounded work, intermediate for "
                                "ordinary multi-step work, or advanced only for the hardest "
                                "cross-cutting/high-risk work. deep_review is reserved for an "
                                "explicit human xhigh/deep-review request on the root turn. "
                                "Custom configured names are valid."
                            ),
                        },
                        "purpose": {
                            "type": "string",
                            "enum": ["visual_advisor", "visual_sweep", "visual_inspector", "visual_critique"],
                            "description": (
                                "Explicit visual-workflow purpose. Selects the matching "
                                "visual model tier automatically without task-text inference."
                            ),
                        },
                        "read_only": {
                            "type": "boolean",
                            "description": (
                                "Enforce read-only execution for this task and descendants. "
                                "Inherited automatically when the parent runtime is read-only."
                            ),
                        },
                        "allow_nested_coding": {
                            "type": "boolean",
                            "default": False,
                            "description": "Root-only coding-broker grant; requires role=orchestrator and read_only=false.",
                        },
                    },
                    "required": ["goal"],
                },
                # No maxItems — the runtime limit is configurable via
                # delegation.max_concurrent_children (default 3) and
                # enforced with a clear error in delegate_task().
                "description": "(rebuilt at get_definitions() time)",
            },
            "role": {
                "type": "string",
                "enum": ["leaf", "orchestrator"],
                "description": "(rebuilt at get_definitions() time)",
            },
            "model_tier": {
                "type": "string",
                "description": (
                    "Optional difficulty tier for a single task or the default for every "
                    "batch item. Choose based on actual task difficulty: trivial for obvious "
                    "tiny work, basic for straightforward bounded work, intermediate for "
                    "ordinary multi-step work, and advanced only for the hardest cross-cutting "
                    "or high-risk work. deep_review is reserved for an explicit human "
                    "xhigh/deep-review request on the root turn. Custom configured names are "
                    "valid. Omit to inherit "
                    "the parent runtime model and reasoning."
                ),
            },
            "purpose": {
                "type": "string",
                "enum": ["visual_advisor", "visual_sweep", "visual_inspector", "visual_critique"],
                "description": (
                    "Explicit visual-workflow purpose for a single task or batch default. "
                    "Selects the matching visual tier automatically. Use visual_advisor "
                    "once before explicit visual implementation, one visual_sweep "
                    "before evidence-only inspector passes and at most one visual_critique."
                ),
            },
            "read_only": {
                "type": "boolean",
                "description": (
                    "Runtime-enforced repository read-only mode. In a READ_ONLY parent this "
                    "is inherited automatically and may be omitted. In ACTION runtime, false: "
                    "workers may use terminal/file tools for bounded exploration, setup, "
                    "and in-scope mutation. True propagates to descendants."
                ),
            },
            "background": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Run as a detached background unit and deliver consolidated "
                    "completion in a later turn. Requires read_only=true for every "
                    "task; detached repository mutation uses delegate_coding_task. "
                    "Completed results are persisted as Hermes-internal runtime state "
                    "for restart-safe delivery, and the response includes redacted live "
                    "transcript paths. This does not grant user/project mutation."
                ),
            },
            "allow_nested_coding": {
                "type": "boolean",
                "default": False,
                "description": (
                    "Root-only explicit capability grant. Requires role=orchestrator, "
                    "read_only=false, and delegation.nested_coding.enabled=true. "
                    "Exposes only the root-owned coding broker, never the raw tool."
                ),
            },
            "acp_command": {
                "type": "string",
                "description": "Override the ACP command for trusted/operator-controlled callers.",
            },
            "acp_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Arguments for the configured ACP command.",
            },
        },
        "required": [],
    },
}


REQUEST_CODING_TASK_SCHEMA = {
    "name": "request_coding_task",
    "description": (
        "Request one synchronous coding worker through Hermes' trusted root "
        "broker. Available only to explicitly authorized orchestrator children. "
        "The broker, not this child, owns cwd, work-item/session identity, "
        "scope reservations, isolation, worker accounting, visual state, and "
        "trusted closeout."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "Concrete bounded implementation task."},
            "context": {"type": "string", "description": "Analysis and constraints for the root-owned worker."},
            "model_tier": {
                "type": "string",
                "description": (
                    "Optional canonical model tier chosen from the actual difficulty of the "
                    "root-owned coding task, not incidental keywords in its text. "
                    "Custom names configured under model_tiers are valid. Omit to use "
                    "the configured coding-worker pass profiles."
                ),
            },
            "reasoning_effort": {
                "type": "string",
                "enum": list(VALID_REASONING_EFFORTS),
                "description": (
                    "Rare effort-only override; does not change the model selected "
                    "by model_tier or configured pass routing."
                ),
            },
            "relevant_files": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["path", "note"],
                },
            },
            "approach": {"type": "string"},
            "constraints": {"type": "string"},
            "verification": {"type": "string"},
            "scope_paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "Required non-overlapping workdir-relative mutation scopes.",
            },
            "analysis_handoff_ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Root-registered structured delegate handoff IDs to attach.",
            },
        },
        "required": ["task", "scope_paths"],
    },
}


def request_coding_task(
    *,
    task: Optional[str],
    context: Optional[str],
    model_tier: Optional[str],
    reasoning_effort: Optional[str],
    relevant_files: Optional[list[dict[str, str]]],
    approach: Optional[str],
    constraints: Optional[str],
    verification: Optional[str],
    scope_paths: Optional[list[str]],
    analysis_handoff_ids: Optional[list[str]],
    parent_agent: Any,
) -> str:
    """Broker nested coding through state owned by the original root agent."""
    broker = getattr(parent_agent, "_delegation_broker_context", None)
    if not isinstance(broker, dict) or broker.get("enabled") is not True:
        return tool_error("request_coding_task requires an explicit root broker grant.")
    if getattr(parent_agent, "_delegate_role", "leaf") != "orchestrator":
        return tool_error("request_coding_task is available only to orchestrator children.")
    if getattr(parent_agent, "_delegation_read_only", False):
        return tool_error("Read-only delegation cannot request coding mutation.")
    if not isinstance(scope_paths, list) or not scope_paths:
        return tool_error("request_coding_task requires non-empty scope_paths.")
    root_agent = broker.get("root_agent")
    if root_agent is None:
        return tool_error("The root coding broker context is unavailable.")
    authorized_cwd = str(broker.get("authorized_cwd") or "").strip()
    if not authorized_cwd:
        return tool_error("The root coding broker has no authorized cwd for this turn.")

    from tools.coding_worker_tool import delegate_coding_task

    with _coding_broker_lock(authorized_cwd):
        raw = delegate_coding_task(
            task=task,
            context=context,
            cwd=authorized_cwd,
            model_tier=model_tier,
            reasoning_effort=reasoning_effort,
            relevant_files=relevant_files,
            approach=approach,
            constraints=constraints,
            verification=verification,
            scope_paths=scope_paths,
            analysis_handoff_ids=analysis_handoff_ids,
            background=False,
            allow_git_pr_lifecycle=False,
            trusted_allow_git_pr_lifecycle=False,
            visual_qa_requirement=broker.get("visual_qa_requirement"),
            project_inspection_candidates=broker.get("project_inspection_candidates"),
            parent_agent=root_agent,
            parent_messages=None,
        )
    if isinstance(raw, str):
        root_agent._coding_worker_used_this_turn = True
    try:
        payload = json.loads(raw)
    except Exception:
        return raw
    if isinstance(payload, dict):
        result_id = f"broker_result_{os.urandom(6).hex()}"
        bounded_result = {
            key: copy.deepcopy(payload.get(key))
            for key in (
                "success",
                "status",
                "error",
                "cwd",
                "backend",
                "scope_check",
                "parallel",
                "parallel_merge",
                "analysis_handoff_ids",
            )
            if key in payload
        }
        root_results = getattr(root_agent, "_brokered_coding_results", None)
        if not isinstance(root_results, dict):
            root_results = {}
            root_agent._brokered_coding_results = root_results
        root_results[result_id] = {
            "result_id": result_id,
            "requesting_subagent_id": str(
                getattr(parent_agent, "_subagent_id", "") or ""
            ),
            "task": str(task or "")[:1000],
            "result": bounded_result,
        }
        while len(root_results) > 100:
            root_results.pop(next(iter(root_results)))
        payload["broker"] = {
            "root_owned": True,
            "authorized_cwd": authorized_cwd,
            "requesting_subagent_id": getattr(parent_agent, "_subagent_id", ""),
            "background": False,
            "gateway_session_key": broker.get("gateway_session_key", ""),
            "session_id": broker.get("session_id", ""),
            "origin_work_item_id": broker.get("origin_work_item_id", ""),
            "result_id": result_id,
        }
    return json.dumps(payload, ensure_ascii=False)


# --- Registry ---
from tools.registry import registry, tool_error

_MODEL_HIDDEN_TASK_FIELDS = {"acp_command", "acp_args"}


def _strip_model_hidden_task_fields(tasks: Any) -> Any:
    if not isinstance(tasks, list):
        return tasks
    stripped_tasks = []
    changed = False
    for task in tasks:
        if not isinstance(task, dict):
            stripped_tasks.append(task)
            continue
        stripped = {
            key: value
            for key, value in task.items()
            if key not in _MODEL_HIDDEN_TASK_FIELDS
        }
        changed = changed or len(stripped) != len(task)
        stripped_tasks.append(stripped)
    return stripped_tasks if changed else tasks


registry.register(
    name="delegate_task",
    toolset="delegation",
    schema=DELEGATE_TASK_SCHEMA,
    handler=lambda args, **kw: delegate_task(
        goal=args.get("goal"),
        context=args.get("context"),
        toolsets=args.get("toolsets"),
        tasks=_strip_model_hidden_task_fields(args.get("tasks")),
        max_iterations=args.get("max_iterations"),
        acp_command=args.get("acp_command"),
        acp_args=args.get("acp_args"),
        role=args.get("role"),
        model_tier=args.get("model_tier"),
        purpose=args.get("purpose"),
        read_only=bool(args.get("read_only", False)),
        background=_model_background_value(args, kw.get("parent_agent")),
        allow_nested_coding=bool(args.get("allow_nested_coding", False)),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_delegate_requirements,
    emoji="🔀",
    dynamic_schema_overrides=_build_dynamic_schema_overrides,
    effect="conditional",
    read_only_check=lambda args: (
        True
        if (
            args.get("allow_nested_coding") is not True
            and (
                (
                    isinstance(args.get("tasks"), list)
                    and bool(args.get("tasks"))
                    and all(
                        isinstance(task, dict)
                        and task.get("allow_nested_coding") is not True
                        for task in args.get("tasks")
                    )
                )
                or not args.get("tasks")
            )
        )
        else "read-only delegation requires valid tasks and nested coding disabled"
    ),
)

registry.register(
    name="request_coding_task",
    toolset="delegated_coding_broker",
    schema=REQUEST_CODING_TASK_SCHEMA,
    handler=lambda args, **kw: request_coding_task(
        task=args.get("task"),
        context=args.get("context"),
        model_tier=args.get("model_tier"),
        reasoning_effort=args.get("reasoning_effort"),
        relevant_files=args.get("relevant_files"),
        approach=args.get("approach"),
        constraints=args.get("constraints"),
        verification=args.get("verification"),
        scope_paths=args.get("scope_paths"),
        analysis_handoff_ids=args.get("analysis_handoff_ids"),
        parent_agent=kw.get("parent_agent"),
    ),
    check_fn=check_delegate_requirements,
    emoji="code",
    effect="mutating",
)
