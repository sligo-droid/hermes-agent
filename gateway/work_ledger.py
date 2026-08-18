"""Durable gateway work ledger for idempotent Discord recovery."""

from __future__ import annotations

import contextlib
import functools
import hashlib
import os
import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from hermes_cli.discord_time import discord_message_exceeds_age_limit
from gateway.session import Platform, SessionSource
from gateway.work_ledger_budget import (
    DEFAULT_LEDGER_HARD_BYTES,
    DEFAULT_LEDGER_TARGET_BYTES,
    enforce_ledger_budget,
)
from hermes_constants import get_hermes_home
from utils import atomic_json_write
from agent.verification_evidence import (
    claim_constraints_for_text,
    downgrade_verified_metadata,
    evidence_from_runtime_breakdown,
)
from agent.request_text import merge_request_fragments
from agent.visual_qa import (
    classify_visual_requirement,
    normalize_visual_qa_config,
    normalize_visual_requirement,
    sanitize_visual_receipt,
    visual_receipt_completion,
)


INCOMPLETE_STATUSES = frozenset(
    {
        "accepted",
        "claimed",
        "agent_running",
        "agent_done",
        "response_delivered",
        "summary_updated",
    }
)
TERMINAL_STATUSES = frozenset({"completed", "failed", "blocked", "cancelled", "expired"})
LEASE_SECONDS = 3600.0
_CLOSEOUT_LOCK_TIMEOUT_SECONDS = 5.0
_CLOSEOUT_LOCK_POLL_SECONDS = 0.05
_PROCESS_LEDGER_LOCK = threading.RLock()
_DROP = object()
_RUN_STATE_UNSET = object()
_RUN_OWNED_STATUSES = frozenset({"claimed", "agent_running"})
_RUN_STATE_TEXT_LIMIT = 240
_RUN_STATE_EPOCH_LIMIT = 160
_RUN_STATE_INT_LIMIT = (1 << 63) - 1
_RUN_STATE_LEASE_LIMIT = 1_000_000_000_000_000.0
_MAX_CONFIRMED_MESSAGE_IDS = 128
_MAX_DELIVERY_MESSAGE_ID_LENGTH = 160
_MAX_REQUIRED_ASYNC_COMPLETIONS = 64
_REQUIRED_ASYNC_SCHEMA_VERSION = 6
_SUPPORTED_REQUIRED_ASYNC_SCHEMA_VERSIONS = frozenset({2, 3, 4, 5, 6})
_REQUIRED_ASYNC_DISPATCH_STATES = frozenset(
    {"registered", "running", "terminal", "cancelled", "outcome_unknown"}
)
_REQUIRED_ASYNC_PENDING_STATES = frozenset({"registered", "running"})
_REQUIRED_ASYNC_EVIDENCE_TEXT_LIMIT = 1000
_MAX_REQUIRED_ASYNC_SCOPE_PATHS = 32
_MAX_REQUIRED_ASYNC_TEST_REFS = 16
_MAX_ASYNC_ADVISORY_RESULTS = 16
_REQUIRED_ASYNC_CHECKPOINT_PATH_LIMIT = 1000
_REQUIRED_ASYNC_RECOVERY_TEXT_LIMIT = 20_000
_REQUIRED_ASYNC_RECOVERY_CONTEXT_LIMIT = 40_000
_MAX_REQUIRED_ASYNC_RECOVERY_FILES = 32
_FULL_RECORD_RETENTION_SECONDS = 7 * 24 * 60 * 60
_TOMBSTONE_RETENTION_SECONDS = 30 * 24 * 60 * 60
_COMPACTION_INTERVAL_SECONDS = 60 * 60
_MAX_DISCORD_PR_LIFECYCLES = 4096
_QUIESCENT_CLOSEOUT_STATUSES = frozenset(
    {
        "completed",
        "not_required",
        "pr_open",
        "pr_published",
        "post_merge_complete",
        "blocked",
        "repair_required",
    }
)
_REQUIRED_ASYNC_RECOVERY_STATUSES = frozenset(
    {
        "registered",
        "running",
        "waiting_for_owner",
        "waiting_for_worker",
        "claimed",
        "resuming_thread",
        "relaunching",
        "recovered",
        "manual_fallback",
        "failed",
    }
)
_CLOSEOUT_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CLOSEOUT_VISUAL_STATUSES = frozenset(
    {"passed", "failed", "blocked", "uncertain", "missing"}
)
_ENFORCED_VISUAL_QA_BLOCK_NOTICE = (
    "⚠️ **Completion blocked.** Enforced visual QA is active, and the work "
    "ledger did not authorize completion."
)
_GENERAL_COMPLETION_BLOCK_NOTICE = (
    "⚠️ **Completion blocked.** The work ledger did not authorize completion."
)


def enforced_visual_qa_block_notice(item: Any) -> str:
    """Return the user-visible notice for an enforced blocked completion gate."""

    if not isinstance(item, dict):
        return ""
    config = normalize_visual_qa_config(item.get("visual_qa_config"))
    gate = item.get("completion_gate")
    if (
        config.get("mode") != "enforce_explicit"
        or not isinstance(gate, dict)
        or gate.get("allowed_to_complete") is not False
    ):
        return ""

    reason = str(gate.get("reason") or item.get("blocked_reason") or "").strip()
    visual = gate.get("visual_qa") if isinstance(gate.get("visual_qa"), dict) else {}
    notice = (
        _ENFORCED_VISUAL_QA_BLOCK_NOTICE
        if reason.startswith("visual_qa_")
        or (
            visual.get("enforced") is True
            and str(visual.get("status") or "").lower() != "passed"
        )
        else _GENERAL_COMPLETION_BLOCK_NOTICE
    )
    if reason:
        readable_reason = re.sub(r"\s+", " ", reason.replace("_", " ")).strip()
        if readable_reason:
            notice = f"{notice} Gate reason: {readable_reason[:160]}."
    return notice


def annotate_enforced_visual_qa_blocked_response(
    item: Any,
    final_response: str,
    *,
    already_delivered: bool = False,
) -> str:
    """Keep successful-path bytes unchanged and visibly qualify enforced blocks."""

    notice = enforced_visual_qa_block_notice(item)
    if not notice or notice in final_response:
        return final_response
    if not final_response:
        return notice
    if already_delivered:
        return f"{final_response}\n\n{notice}"
    return f"{notice}\n\n{final_response}"


def _bounded_delivery_message_ids(
    values: Any = None,
    *,
    primary: Any = None,
) -> tuple[str, ...]:
    """Sanitize and bound durable platform message IDs in confirmation order."""

    if values is None:
        values = ()
    elif isinstance(values, (str, bytes)):
        values = (values,)
    try:
        candidates = iter(values)
    except TypeError:
        candidates = iter((values,))

    def _sanitize(value: Any) -> str:
        text = str(value or "").strip()
        return "".join(
            char for char in text if char.isalnum() or char in {"-", "_", ".", ":"}
        )[:_MAX_DELIVERY_MESSAGE_ID_LENGTH]

    normalized: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        text = _sanitize(value)
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
        if len(normalized) >= _MAX_CONFIRMED_MESSAGE_IDS:
            break
    primary_text = _sanitize(primary)
    if (
        primary_text
        and primary_text not in seen
        and len(normalized) < _MAX_CONFIRMED_MESSAGE_IDS
    ):
        normalized.append(primary_text)
    return tuple(normalized)


def _sync_closeout_preview_delivery(
    item: dict[str, Any],
    closeout: Mapping[str, Any],
    *,
    now: float,
) -> None:
    """Create one durable delivery obligation for an exact-head preview URL."""

    preview = closeout.get("preview") if isinstance(closeout.get("preview"), Mapping) else {}
    pr = closeout.get("pr") if isinstance(closeout.get("pr"), Mapping) else {}
    current_head = str(pr.get("head_sha") or "")[:64]
    current = item.get("preview_delivery") if isinstance(item.get("preview_delivery"), dict) else {}
    if (
        current
        and str(current.get("head_sha") or "") != current_head
        and str(current.get("status") or "") not in {"completed", "cancelled"}
    ):
        item["preview_delivery"] = {
            **current,
            "status": "cancelled",
            "owner": "",
            "lease_until": None,
            "cancelled_at": now,
            "cancelled_reason": "pr_head_advanced",
        }
    if (
        str(preview.get("status") or "") != "ready"
        or not str(preview.get("url") or "").startswith("https://")
        or not str(pr.get("url") or "").startswith("https://")
        or str(preview.get("observed_sha") or "") != str(pr.get("head_sha") or "")
    ):
        return
    identity = {
        "preview_url": str(preview.get("url") or "")[:1200],
        "pr_url": str(pr.get("url") or "")[:1200],
        "head_sha": str(preview.get("observed_sha") or "")[:64],
    }
    current = item.get("preview_delivery") if isinstance(item.get("preview_delivery"), dict) else {}
    if all(str(current.get(key) or "") == value for key, value in identity.items()):
        return
    item["preview_delivery"] = {
        **identity,
        "status": "pending",
        "revision": 1,
        "owner": "",
        "lease_until": None,
        "attempt_count": 0,
        "retry_count": 0,
        "next_attempt_at": now,
        "send_started_at": None,
        "send_confirmed_at": None,
        "result_message_id": None,
        "confirmed_message_ids": [],
        "created_at": now,
    }


def _preview_delivery_matches_closeout(
    item: Mapping[str, Any],
    delivery: Mapping[str, Any],
) -> bool:
    closeout = item.get("closeout") if isinstance(item.get("closeout"), Mapping) else {}
    preview = closeout.get("preview") if isinstance(closeout.get("preview"), Mapping) else {}
    pr = closeout.get("pr") if isinstance(closeout.get("pr"), Mapping) else {}
    return (
        str(delivery.get("head_sha") or "") == str(pr.get("head_sha") or "")
        and str(delivery.get("head_sha") or "") == str(preview.get("observed_sha") or "")
        and str(delivery.get("preview_url") or "") == str(preview.get("url") or "")
        and str(delivery.get("pr_url") or "") == str(pr.get("url") or "")
        and str(preview.get("status") or "") == "ready"
    )


def _discord_thread_key(item: Any) -> tuple[str, str, str, str] | None:
    """Return the durable Discord thread identity for a work item."""

    if not isinstance(item, dict) or str(item.get("platform") or "") != "discord":
        return None
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    thread_id = str(source.get("thread_id") or "").strip()
    if not thread_id and str(source.get("chat_type") or "").strip() == "thread":
        thread_id = str(source.get("chat_id") or "").strip()
    if not thread_id:
        return None
    return (
        str(source.get("profile") or "default").strip(),
        str(source.get("guild_id") or "").strip(),
        str(source.get("parent_chat_id") or "").strip(),
        thread_id,
    )


def _discord_item_order(item: dict[str, Any]) -> tuple[float, int, float, str]:
    message_id = str(item.get("message_id") or "").strip()
    try:
        snowflake = int(message_id)
    except (TypeError, ValueError):
        snowflake = 0
    return (
        float(item.get("created_at") or 0),
        snowflake,
        float(item.get("updated_at") or 0),
        str(item.get("id") or ""),
    )


_PROJECT_SOURCE_KEYS = frozenset(
    {
        "project_path",
        "project_github_url",
        "project_channel_id",
        "project_mapping_resolved",
    }
)
_PROJECT_SUMMARY_KEYS = frozenset(
    {
        "project_path",
        "github_url",
        "project_github_url",
        "channel_id",
        "project_channel_id",
    }
)
_EXPLICIT_NARROW_SCOPE_PATTERNS = (
    r"\bopen\s+(?:a\s+)?(?:pr|pull\s+request)\b[^\n.]{0,80}\b(?:do\s*not|don't)\s+merge\b",
    r"\bopen\s+(?:a\s+)?(?:pr|pull\s+request)\b[^\n.]{0,80}\bfor\s+review\b",
    r"\b(?:do\s*not|don't)\s+merge\b",
    r"\bleave\s+(?:it\s+)?unmerged\b",
    r"\bclose\s+(?:the\s+)?(?:pr|pull\s+request)\b",
    r"\bpr\s+only\b",
    r"\breview\s+only\b",
    r"\bdraft\s+pr\b",
)
_PR_ONLY_FINAL_PATTERNS = (
    r"\bpr\s+(?:opened|created|is\s+open)\b",
    r"\bopened\s+(?:a\s+)?pr\b",
    r"\bpull\s+request\s+(?:opened|created|is\s+open)\b",
)
_INTENTIONAL_UNMERGED_PATTERNS = (
    r"\bintentionally\s+left\s+unmerged\b",
    r"\bleft\s+unmerged\s+per\s+(?:instruction|request)\b",
    r"\bnot\s+merged\s+per\s+(?:instruction|request)\b",
    r"\bclosed\b[^\n.]{0,80}\bnot\s+merged\b",
    r"\bfor\s+review\b",
    r"\bdraft\s+pr\b",
)
_REVIEW_ONLY_FINAL_PATTERNS = (
    r"\breview(?:ed|\s+complete|\s+done)\b",
    r"\breview-only\b",
    r"\bfindings\b",
    r"\bno\s+changes\s+(?:made|required)\b",
)
_REVIEW_ONLY_REQUEST_PATTERNS = (
    r"\breview\b",
    r"\baudit\b",
    r"\binspect\b",
    r"\bassess\b",
    r"\bevaluate\b",
)
_REVIEW_REQUEST_ACTION_PATTERNS = (
    r"\b(?:fix|patch|implement|add|build|ship|change|update|edit|refactor|rewrite|remove|delete)\b",
    r"\b(?:open|create)\s+(?:a\s+)?(?:pr|pull\s+request)\b",
    r"\bmerge\b",
    r"\bdeploy\b",
)
_READ_ONLY_REQUEST_PATTERNS = (
    r"^\s*(?:why|what|when|where|who|how)\b",
)
_INCOMPLETE_FINAL_PATTERNS = (
    ("not_done_yet", r"\bnot\s+done\s+yet\b"),
    ("no_commit", r"\bno\s+commit\b"),
    (
        "not_committed",
        r"\bnot\s+committed\b|\buncommitted\s+(?:changes?|work|files?|edits?|diff|working\s+tree)\b|"
        r"\bworking\s+tree\b[^\n.]{0,120}\bnot\s+committed\b",
    ),
    ("not_pushed", r"\bnot\s+pushed\b"),
    ("no_pr", r"\bno\s+pr\b|\bpr\s+not\s+opened\b|\bno\s+pull\s+request\b|\bpull\s+request\s+not\s+opened\b"),
    ("no_deploy", r"\bno\s+deploy\b|\bnot\s+deployed\b"),
    ("not_merged", r"\bnot\s+merged\b|\bleft\s+unmerged\b"),
    (
        "checks_not_green",
        r"\b(?:ci|checks?|status\s+checks?)\b[^\n.]{0,80}\b"
        r"(?:pending|still\s+running|not\s+green|not\s+passed|has\s+not\s+passed|"
        r"haven['’]?t\s+passed|not\s+completed|failing|failed)\b",
    ),
    (
        "runtime_not_synced",
        r"\b(?:canonical|runtime|checkout|worktree|deployed\s+runtime|private\s+runtime|runtime\s+sync)\b"
        r"[^\n.]{0,120}\b(?:not\s+synced|not\s+updated|not\s+pulled|not\s+verified|"
        r"pending|behind|dirty|drift|lag)\b",
    ),
    (
        "live_pickup_unverified",
        r"\b(?:live\s+pickup|runtime\s+pickup|process\s+pickup|live\s+runtime\s+pickup)\b[^\n.]{0,120}\b"
        r"(?:not\s+verified|unverified|needs?\s+verification|not\s+confirmed|unknown|pending|not\s+picked\s+up)\b|"
        r"\b(?:not|hasn['’]?t|has\s+not)\s+picked\s+up\b",
    ),
)
_DEFERRED_RUNTIME_WATCH_PATTERNS = (
    r"\b(?:first\s+scheduled\s+)?(?:airflow\s+)?(?:dag|run|runtime\s+verification|data-writing\s+run)\b"
    r"[^\n.]{0,140}\b(?:still\s+running|running|pending|queued|watch(?:er|ing)|background\s+watch)\b",
    r"\bbackground\s+watch(?:er|ing)?\b[^\n.]{0,140}\b(?:airflow|dag|run|runtime\s+verification|data-writing)\b",
)
_RUNTIME_SYNC_VERIFIED_PATTERNS = (
    r"\b(?:canonical|runtime|checkout|worktree|deployed\s+runtime|private\s+runtime)\b[^\n.]{0,120}\b"
    r"(?:synced|updated|pulled|fast-forwarded|clean)\b",
    r"\bcanonical/runtime\s+checkouts?\s+synced\b",
)
_LIVE_PICKUP_VERIFIED_PATTERNS = (
    r"\b(?:live\s+pickup|runtime\s+pickup|process\s+pickup|live\s+runtime\s+pickup)\b[^\n.]{0,120}\b"
    r"(?:verified|confirmed|picked\s+up)\b",
    r"\bpicked\s+up\s+(?:the\s+)?(?:merged\s+)?(?:code|change|version|commit)\b",
)
_SAFE_GATEWAY_RELOAD_HANDOFF_PATTERNS = (
    r"\b(?:safe\s+)?(?:gateway\s+)?reload\s+watcher\b[^\n.]{0,160}\b(?:started|queued|running|waiting)",
    r"\bhermes-safe-gateway-reload-[\w-]+\.service\b",
    r"\bwill\s+send\s+SIGUSR1\b[^\n.]{0,160}\b(?:active_agents|idle|gateway)",
)


def default_path() -> Path:
    return get_hermes_home() / "gateway" / "work_ledger.json"


def _record_provider_progress(item: dict[str, Any], reason: str, *, status: str | None = None) -> None:
    try:
        from agent.provider_progress import record_provider_progress_signal

        metadata: dict[str, Any] = {"work_id": str(item.get("id") or "")}
        if status:
            metadata["status"] = str(status)
        record_provider_progress_signal(
            str(item.get("session_key") or ""),
            reason,
            phase="work_ledger",
            source="work_ledger",
            metadata=metadata,
        )
    except Exception:
        pass


def _now() -> float:
    return time.time()


def _platform_value(value: Any) -> str:
    return getattr(value, "value", value) or ""


def _durable_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        safe: dict[str, Any] = {}
        for key, item in value.items():
            key_str = str(key)
            if key_str.startswith("_"):
                continue
            safe_item = _durable_json_value(item)
            if safe_item is not _DROP:
                safe[key_str] = safe_item
        return safe
    if isinstance(value, (list, tuple, set)):
        safe_items = []
        for item in value:
            safe_item = _durable_json_value(item)
            if safe_item is not _DROP:
                safe_items.append(safe_item)
        return safe_items
    return _DROP


def _durable_metadata(value: Any) -> Any:
    safe = _durable_json_value(value)
    return None if safe is _DROP else safe


def _visual_qa_worker_route_for_event(event: Any) -> Any:
    """Return advisory route metadata without making it durable request data."""

    fable_metadata = getattr(event, "fable_plan_metadata", None)
    opus_metadata = getattr(event, "opus_plan_metadata", None)
    return {
        "worker_route": getattr(event, "worker_route", None),
        "fable_route": (
            fable_metadata.get("route") if isinstance(fable_metadata, dict) else None
        ),
        "opus_route": (
            opus_metadata.get("route") if isinstance(opus_metadata, dict) else None
        ),
        "runtime_mode": getattr(event, "discord_runtime_mode", None),
    }


def _visual_qa_requirement_for_event(event: Any) -> dict[str, Any]:
    """Classify only the accepted request text into a bounded public shape."""

    feature_summary = getattr(event, "feature_summary", None)
    request_text = merge_request_fragments(
        getattr(event, "text", ""),
        feature_summary.get("initial_request") if isinstance(feature_summary, dict) else "",
        getattr(event, "reply_to_text", ""),
        getattr(event, "goal_thread_context", ""),
    )
    return classify_visual_requirement(
        request_text,
        worker_route=_visual_qa_worker_route_for_event(event),
    )


def _visual_qa_receipts(
    receipts: Any,
    requirement: Any,
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Keep only explicit, requirement-matching, secret-safe receipts."""

    safe: list[dict[str, Any]] = []
    if not isinstance(receipts, list):
        return safe
    max_receipts = max(0, int(limit or 0))
    if max_receipts <= 0:
        return safe
    for raw in receipts:
        receipt = sanitize_visual_receipt(raw, requirement=requirement)
        if receipt is not None:
            safe.append(receipt)
    safe.sort(key=lambda item: int(item.get("order") or 0))
    return safe[-max_receipts:]


def _visual_qa_state_for_item(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the normalized, persisted visual-QA contract for an item."""

    config = normalize_visual_qa_config(item.get("visual_qa_config"))
    requirement = normalize_visual_requirement(item.get("visual_qa_requirement"))
    return config, requirement


def _promote_visual_qa_requirement_in_item(
    item: dict[str, Any],
    requirement: Any,
) -> bool:
    """Promote only a prior ``none`` requirement and tighten closeout policy."""

    current = normalize_visual_requirement(item.get("visual_qa_requirement"))
    promoted = normalize_visual_requirement(requirement)
    if current["level"] != "none" or promoted["level"] == "none":
        return False
    item["visual_qa_requirement"] = promoted
    item["visual_qa_receipts"] = []
    config = normalize_visual_qa_config(item.get("visual_qa_config"))
    closeout = item.get("closeout")
    if isinstance(closeout, dict) and config["mode"] == "enforce_explicit":
        policy = closeout.get("policy")
        if not isinstance(policy, dict):
            policy = {}
            closeout["policy"] = policy
        policy["require_visual_qa"] = True
        closeout["visual_qa"] = {"status": "pending"}
        closeout["revision"] = max(0, _positive_int(closeout.get("revision"))) + 1
    return True


def _durable_runtime_breakdown(
    runtime_breakdown: Any,
    requirement: Any,
    *,
    receipt_limit: int,
) -> Any:
    """Persist runtime metrics without retaining unsafe raw visual receipt text."""

    durable = _durable_metadata(runtime_breakdown)
    if not isinstance(durable, dict):
        return durable
    durable["visual_qa_receipts"] = _visual_qa_receipts(
        runtime_breakdown.get("visual_qa_receipts") if isinstance(runtime_breakdown, dict) else None,
        requirement,
        limit=receipt_limit,
    )
    try:
        from agent.terminal_outcomes import sanitize_closeout_receipt

        durable["closeout_receipt"] = sanitize_closeout_receipt(
            runtime_breakdown.get("closeout_receipt")
            if isinstance(runtime_breakdown, dict)
            else None
        )
    except Exception:
        durable["closeout_receipt"] = None
    try:
        from agent.preview_readiness import summarize_preview_events

        preview = (
            runtime_breakdown.get("preview_readiness")
            if isinstance(runtime_breakdown, dict)
            else None
        )
        durable["preview_readiness"] = summarize_preview_events(
            preview.get("events") if isinstance(preview, dict) else None
        )
    except Exception:
        durable["preview_readiness"] = None
    return durable


def _adopt_repo_native_closeout_receipt(
    item: dict[str, Any],
    receipt: Any,
    *,
    now: float,
) -> None:
    """Adopt one sanitized exact-head receipt into an existing closeout contract."""

    if not isinstance(receipt, dict):
        return
    item["closeout_receipt"] = dict(receipt)
    if not isinstance(item.get("closeout"), dict):
        return

    from hermes_cli.trusted_closeout import normalize_closeout_state

    current = normalize_closeout_state(item["closeout"])
    state = normalize_closeout_state(
        {
            **current,
            "source": "repo_native",
            "mode": "enforce",
            "status": "completed",
            "local_verification": {
                "status": "passed",
                "head_sha": receipt["head_sha"],
            },
            "lease": {"owner": "", "until": None},
            "next_due_at": None,
        }
    )
    state["revision"] = int(current.get("revision") or 0) + 1
    item["closeout"] = state
    item["closeout_authoritative"] = True
    item["closeout_activated_at"] = now


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _closeout_expects_head(state: dict[str, Any], expected_head_sha: str) -> bool:
    """Return whether every active exact-head closeout receipt still targets H."""

    if not _CLOSEOUT_SHA_RE.fullmatch(expected_head_sha):
        return False
    if state.get("policy", {}).get("require_visual_qa") is not True:
        return False
    return (
        str(state.get("pr", {}).get("head_sha") or "").strip().lower()
        == expected_head_sha
        and str(state.get("local_verification", {}).get("head_sha") or "")
        .strip()
        .lower()
        == expected_head_sha
    )


def _pending_closeout_visual_completion(item: dict[str, Any]) -> dict[str, str] | None:
    raw = item.get("closeout_visual_completion")
    if not isinstance(raw, dict):
        return None
    status = str(raw.get("status") or "").strip().lower()
    head_sha = str(raw.get("head_sha") or "").strip().lower()
    if status not in _CLOSEOUT_VISUAL_STATUSES or not _CLOSEOUT_SHA_RE.fullmatch(head_sha):
        return None
    return {"status": status, "head_sha": head_sha}


def _merge_pending_closeout_visual_completion(
    item: dict[str, Any],
    state: dict[str, Any],
) -> bool:
    """Merge one leased visual result only if reconciliation still expects H."""

    pending = _pending_closeout_visual_completion(item)
    item.pop("closeout_visual_completion", None)
    if pending is None or not _closeout_expects_head(state, pending["head_sha"]):
        return False
    if state.get("visual_qa") == pending:
        return False
    state["visual_qa"] = pending
    return True


def _bounded_run_state_text(value: Any, *, limit: int = _RUN_STATE_TEXT_LIMIT) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]
    prefix_limit = max(0, limit - len(digest) - 1)
    return f"{text[:prefix_limit]}:{digest}"


def _bounded_run_state_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return 0
    return min(_RUN_STATE_INT_LIMIT, max(0, number))


def _bounded_run_state_lease(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        lease = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(lease):
        return None
    return max(-_RUN_STATE_LEASE_LIMIT, min(_RUN_STATE_LEASE_LIMIT, lease))


def _bounded_required_async_text(value: Any) -> str:
    return _bounded_run_state_text(value, limit=_REQUIRED_ASYNC_EVIDENCE_TEXT_LIMIT)


def _bounded_required_async_strings(
    values: Any,
    *,
    limit: int,
) -> list[str]:
    if isinstance(values, (str, bytes)):
        values = (values,)
    try:
        candidates = iter(values or ())
    except TypeError:
        candidates = iter((values,))
    normalized: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        text = _bounded_run_state_text(value)
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
        if len(normalized) >= limit:
            break
    return normalized


def _bounded_required_async_evidence(value: Any) -> dict[str, Any]:
    """Keep only deterministic, bounded coding closeout references."""

    raw = value if isinstance(value, Mapping) else {}
    evidence: dict[str, Any] = {}
    for key in ("changed", "merged"):
        if isinstance(raw.get(key), bool):
            evidence[key] = raw[key]
    for key in ("scope_paths", "changed_paths", "initial_dirty_paths"):
        paths = _bounded_required_async_strings(
            raw.get(key),
            limit=_MAX_REQUIRED_ASYNC_SCOPE_PATHS,
        )
        if paths:
            evidence[key] = paths
    test_refs = _bounded_required_async_strings(
        raw.get("test_refs"),
        limit=_MAX_REQUIRED_ASYNC_TEST_REFS,
    )
    if test_refs:
        evidence["test_refs"] = test_refs
    for key in ("worker_cwd", "merge_ref", "result_ref", "closeout_id"):
        text = _bounded_run_state_text(raw.get(key))
        if text:
            evidence[key] = text
    for key in ("commit_sha", "head_sha", "base_sha"):
        sha = str(raw.get(key) or "").strip().lower()
        if _CLOSEOUT_SHA_RE.fullmatch(sha):
            evidence[key] = sha
    raw_scope_check = raw.get("scope_check")
    if isinstance(raw_scope_check, Mapping):
        scope_check: dict[str, Any] = {}
        if isinstance(raw_scope_check.get("clean"), bool):
            scope_check["clean"] = raw_scope_check["clean"]
        for key in ("scope_paths", "out_of_scope_files"):
            paths = _bounded_required_async_strings(
                raw_scope_check.get(key),
                limit=_MAX_REQUIRED_ASYNC_SCOPE_PATHS,
            )
            if paths:
                scope_check[key] = paths
        inspection_error = _bounded_required_async_text(
            raw_scope_check.get("inspection_error")
        )
        if inspection_error:
            scope_check["inspection_error"] = inspection_error
        if scope_check:
            evidence["scope_check"] = scope_check
    raw_parallel_merge = raw.get("parallel_merge")
    if isinstance(raw_parallel_merge, Mapping):
        parallel_merge: dict[str, Any] = {}
        for key in (
            "success",
            "recovery_required",
            "merged",
            "merge_pending",
            "worktree_kept",
        ):
            if isinstance(raw_parallel_merge.get(key), bool):
                parallel_merge[key] = raw_parallel_merge[key]
        for key in ("group_id", "worker_cwd", "error", "next_action"):
            text = _bounded_required_async_text(raw_parallel_merge.get(key))
            if text:
                parallel_merge[key] = text
        conflicts = _bounded_required_async_strings(
            raw_parallel_merge.get("merge_conflicts"),
            limit=_MAX_REQUIRED_ASYNC_SCOPE_PATHS,
        )
        if conflicts:
            parallel_merge["merge_conflicts"] = conflicts
        if parallel_merge:
            evidence["parallel_merge"] = parallel_merge
    raw_worker_run = raw.get("worker_run")
    if isinstance(raw_worker_run, Mapping):
        worker_run: dict[str, Any] = {}
        for key in ("backend", "model", "reasoning", "model_tier"):
            text = _bounded_run_state_text(raw_worker_run.get(key))
            if text:
                worker_run[key] = text
        for key in ("failed", "background"):
            if isinstance(raw_worker_run.get(key), bool):
                worker_run[key] = raw_worker_run[key]
        if worker_run:
            evidence["worker_run"] = worker_run
    raw_advisory_results = raw.get("advisory_results")
    if isinstance(raw_advisory_results, list):
        advisory_results: list[dict[str, str]] = []
        for raw_result in raw_advisory_results[:_MAX_ASYNC_ADVISORY_RESULTS]:
            if not isinstance(raw_result, Mapping):
                continue
            result: dict[str, str] = {}
            for key in ("goal", "status", "summary", "error"):
                text = _bounded_required_async_text(raw_result.get(key))
                if text:
                    result[key] = text
            if result:
                advisory_results.append(result)
        if advisory_results:
            evidence["advisory_results"] = advisory_results
    return evidence


def _bounded_required_async_checkpoint(
    value: Any,
) -> tuple[dict[str, Any] | None, bool]:
    """Normalize one trusted checkpoint intent without accepting partial identity."""

    if value is None:
        return None, False
    if not isinstance(value, Mapping):
        return None, True
    parent_sha = str(value.get("parent_sha") or "").strip().lower()
    tree_sha = str(value.get("tree_sha") or "").strip().lower()
    message = _bounded_required_async_text(value.get("message"))
    repository_root = str(value.get("repository_root") or "").strip()[
        :_REQUIRED_ASYNC_CHECKPOINT_PATH_LIMIT
    ]
    workspace_path = str(value.get("workspace_path") or "").strip()[
        :_REQUIRED_ASYNC_CHECKPOINT_PATH_LIMIT
    ]
    committed_head_sha = str(value.get("committed_head_sha") or "").strip().lower()
    malformed = bool(
        not _CLOSEOUT_SHA_RE.fullmatch(parent_sha)
        or not _CLOSEOUT_SHA_RE.fullmatch(tree_sha)
        or not message
        or not repository_root
        or not workspace_path
        or (
            committed_head_sha
            and not _CLOSEOUT_SHA_RE.fullmatch(committed_head_sha)
        )
    )
    if malformed:
        return None, True
    checkpoint = {
        "parent_sha": parent_sha,
        "tree_sha": tree_sha,
        "message": message,
        "repository_root": repository_root,
        "workspace_path": workspace_path,
    }
    if committed_head_sha:
        checkpoint["committed_head_sha"] = committed_head_sha
    return checkpoint, False


def _bounded_required_async_recovery(value: Any) -> tuple[dict[str, Any], bool]:
    """Normalize the durable Worker Run data needed after a gateway restart."""

    if value is None:
        return {}, False
    if not isinstance(value, Mapping):
        return {}, True
    raw = value
    malformed = False
    recovery: dict[str, Any] = {}

    def text_field(key: str, *, limit: int = _REQUIRED_ASYNC_RECOVERY_TEXT_LIMIT) -> None:
        text = _bounded_run_state_text(raw.get(key), limit=limit)
        if text:
            recovery[key] = text

    for key, limit in (
        ("task", _REQUIRED_ASYNC_RECOVERY_CONTEXT_LIMIT),
        ("context", _REQUIRED_ASYNC_RECOVERY_CONTEXT_LIMIT),
        ("approach", _REQUIRED_ASYNC_RECOVERY_TEXT_LIMIT),
        ("constraints", _REQUIRED_ASYNC_RECOVERY_TEXT_LIMIT),
        ("verification", _REQUIRED_ASYNC_RECOVERY_TEXT_LIMIT),
        ("plan_text", _REQUIRED_ASYNC_RECOVERY_CONTEXT_LIMIT),
        ("worktree", _REQUIRED_ASYNC_CHECKPOINT_PATH_LIMIT),
        ("repository_root", _REQUIRED_ASYNC_CHECKPOINT_PATH_LIMIT),
        ("requested_cwd", _REQUIRED_ASYNC_CHECKPOINT_PATH_LIMIT),
        ("backend", _RUN_STATE_TEXT_LIMIT),
        ("model_tier", _RUN_STATE_TEXT_LIMIT),
        ("reasoning_effort", _RUN_STATE_TEXT_LIMIT),
        ("phase", _RUN_STATE_TEXT_LIMIT),
        ("thread_id", _REQUIRED_ASYNC_EVIDENCE_TEXT_LIMIT),
        ("turn_id", _REQUIRED_ASYNC_EVIDENCE_TEXT_LIMIT),
        ("worker_scope_unit", _REQUIRED_ASYNC_EVIDENCE_TEXT_LIMIT),
        ("worker_run_id", _REQUIRED_ASYNC_EVIDENCE_TEXT_LIMIT),
        ("launch_id", _REQUIRED_ASYNC_EVIDENCE_TEXT_LIMIT),
        ("last_event", _REQUIRED_ASYNC_EVIDENCE_TEXT_LIMIT),
        ("last_error", _REQUIRED_ASYNC_EVIDENCE_TEXT_LIMIT),
        ("base_sha", _REQUIRED_ASYNC_EVIDENCE_TEXT_LIMIT),
        ("git_top_level", _REQUIRED_ASYNC_CHECKPOINT_PATH_LIMIT),
        ("git_common_dir", _REQUIRED_ASYNC_CHECKPOINT_PATH_LIMIT),
    ):
        text_field(key, limit=limit)

    status = str(raw.get("status") or "").strip().lower()
    if status:
        if status not in _REQUIRED_ASYNC_RECOVERY_STATUSES:
            status = "failed"
            malformed = True
        recovery["status"] = status
    policy = str(raw.get("policy") or "").strip().lower()
    if policy:
        if policy not in {"resume_or_relaunch", "manual"}:
            policy = "manual"
            malformed = True
        recovery["policy"] = policy
    side_effect_mode = str(raw.get("side_effect_mode") or "").strip().lower()
    if side_effect_mode:
        if side_effect_mode not in {"workspace_only", "external"}:
            side_effect_mode = "external"
            malformed = True
        recovery["side_effect_mode"] = side_effect_mode

    for key in (
        "allow_git_pr_lifecycle",
        "trusted_allow_git_pr_lifecycle",
        "thread_resume_supported",
    ):
        if key in raw and not isinstance(raw.get(key), bool):
            malformed = True
        elif isinstance(raw.get(key), bool):
            recovery[key] = raw[key]
    external_authority = bool(
        recovery.get("side_effect_mode") == "external"
        or recovery.get("allow_git_pr_lifecycle") is True
        or recovery.get("trusted_allow_git_pr_lifecycle") is True
    )
    if external_authority:
        if recovery.get("policy") not in {None, "manual"}:
            malformed = True
        recovery["policy"] = "manual"
        recovery["side_effect_mode"] = "external"
    for key in (
        "launch_generation",
        "owner_started_at",
        "worker_pid",
        "worker_started_at",
    ):
        number = _bounded_run_state_int(raw.get(key))
        if number:
            recovery[key] = number
    for key in ("heartbeat_at", "claimed_at"):
        stamp = _bounded_run_state_lease(raw.get(key))
        if stamp is not None:
            recovery[key] = stamp
    timeout = _bounded_run_state_lease(raw.get("turn_timeout_seconds"))
    if timeout is not None and timeout >= 0:
        recovery["turn_timeout_seconds"] = timeout

    for key in ("scope_paths", "analysis_handoff_ids", "initial_dirty_paths"):
        values = _bounded_required_async_strings(
            raw.get(key),
            limit=_MAX_REQUIRED_ASYNC_SCOPE_PATHS,
        )
        if values:
            recovery[key] = values

    relevant_files: list[dict[str, str]] = []
    raw_files = raw.get("relevant_files")
    if raw_files is not None and not isinstance(raw_files, list):
        malformed = True
    if isinstance(raw_files, list):
        for raw_file in raw_files[:_MAX_REQUIRED_ASYNC_RECOVERY_FILES]:
            if not isinstance(raw_file, Mapping):
                malformed = True
                continue
            entry: dict[str, str] = {}
            for key, limit in (
                ("path", _REQUIRED_ASYNC_CHECKPOINT_PATH_LIMIT),
                ("note", _REQUIRED_ASYNC_EVIDENCE_TEXT_LIMIT),
            ):
                text = _bounded_run_state_text(raw_file.get(key), limit=limit)
                if text:
                    entry[key] = text
            if entry:
                relevant_files.append(entry)
    if relevant_files:
        recovery["relevant_files"] = relevant_files

    raw_parallel = raw.get("parallel_group")
    if raw_parallel is not None and not isinstance(raw_parallel, Mapping):
        malformed = True
    if isinstance(raw_parallel, Mapping):
        parallel: dict[str, Any] = {}
        for key in ("group_id", "base_cwd", "base_sha"):
            limit = (
                _REQUIRED_ASYNC_CHECKPOINT_PATH_LIMIT
                if key == "base_cwd"
                else _REQUIRED_ASYNC_EVIDENCE_TEXT_LIMIT
            )
            text = _bounded_run_state_text(raw_parallel.get(key), limit=limit)
            if text:
                parallel[key] = text
        dirty = _bounded_required_async_strings(
            raw_parallel.get("initial_dirty_paths"),
            limit=_MAX_REQUIRED_ASYNC_SCOPE_PATHS,
        )
        if dirty:
            parallel["initial_dirty_paths"] = dirty
        if parallel:
            recovery["parallel_group"] = parallel
    return recovery, malformed


def _normalized_required_async_dispatch(
    raw_dispatch: Any,
    *,
    legacy: bool = False,
) -> tuple[dict[str, Any], bool]:
    malformed = not isinstance(raw_dispatch, dict)
    raw = raw_dispatch if isinstance(raw_dispatch, dict) else {}
    state = "terminal" if legacy else str(raw.get("state") or "").strip().lower()
    if state not in _REQUIRED_ASYNC_DISPATCH_STATES:
        state = "outcome_unknown"
        malformed = True
    success: bool | None = None
    if state == "terminal":
        success = raw.get("success") is True
    elif state in {"cancelled", "outcome_unknown"}:
        success = False
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in {"coding_worker", "advisory"}:
        kind = "coding_worker"
        if raw.get("kind") not in {None, ""}:
            malformed = True
    required = raw.get("required") is not False
    if kind == "advisory":
        required = False
    recovery, recovery_malformed = _bounded_required_async_recovery(
        raw.get("recovery")
    )
    malformed = malformed or recovery_malformed
    dispatch = {
        "kind": kind,
        "required": required,
        "state": state,
        "success": success,
        "status": _bounded_run_state_text(raw.get("status")),
        "registered_at": _bounded_run_state_lease(raw.get("registered_at")),
        "started_at": _bounded_run_state_lease(raw.get("started_at")),
        "completed_at": _bounded_run_state_lease(raw.get("completed_at")),
        "owner_pid": _bounded_run_state_int(raw.get("owner_pid")),
        "process_epoch": _bounded_run_state_text(
            raw.get("process_epoch"),
            limit=_RUN_STATE_EPOCH_LIMIT,
        ),
        "closeout_id": _bounded_run_state_text(raw.get("closeout_id")),
        "summary": _bounded_required_async_text(raw.get("summary")),
        "error": _bounded_required_async_text(raw.get("error")),
        "evidence": _bounded_required_async_evidence(raw.get("evidence")),
        "recovery": recovery,
    }
    if raw.get("registration_missing") is True:
        dispatch["registration_missing"] = True
    if raw.get("conflicting_replay") is True:
        dispatch["conflicting_replay"] = True
    return dispatch, malformed


def _required_async_completion_state(item: Any) -> dict[str, Any]:
    source = item if isinstance(item, dict) else {}
    present = "required_async_completions" in source
    raw_value = source.get("required_async_completions")
    raw = raw_value if isinstance(raw_value, dict) else {}
    raw_version = raw.get("schema_version")
    legacy = present and raw_version is None
    malformed = present and not isinstance(raw_value, dict)
    if (
        raw_version is not None
        and _positive_int(raw_version) not in _SUPPORTED_REQUIRED_ASYNC_SCHEMA_VERSIONS
    ):
        malformed = True
    generation = _positive_int(raw.get("generation"))
    attempt_id = _bounded_run_state_text(raw.get("attempt_id"))
    attempt_order = _positive_int(raw.get("attempt_order"))
    dispatches: dict[str, dict[str, Any]] = {}

    raw_dispatches = raw.get("dispatches")
    if raw_version is not None and not isinstance(raw_dispatches, dict):
        malformed = True
    if isinstance(raw_dispatches, dict):
        if len(raw_dispatches) > _MAX_REQUIRED_ASYNC_COMPLETIONS:
            malformed = True
        for delegation_id, raw_dispatch in list(raw_dispatches.items())[
            -_MAX_REQUIRED_ASYNC_COMPLETIONS:
        ]:
            normalized_id = _bounded_run_state_text(delegation_id)
            if not normalized_id:
                malformed = True
                continue
            dispatch, dispatch_malformed = _normalized_required_async_dispatch(raw_dispatch)
            malformed = malformed or dispatch_malformed
            dispatches[normalized_id] = dispatch

    checkpoint, checkpoint_malformed = _bounded_required_async_checkpoint(
        raw.get("checkpoint")
    )
    malformed = malformed or checkpoint_malformed

    raw_outcomes = raw.get("outcomes") if isinstance(raw.get("outcomes"), dict) else {}
    if legacy or (raw_outcomes and not dispatches):
        if len(raw_outcomes) > _MAX_REQUIRED_ASYNC_COMPLETIONS:
            malformed = True
        for delegation_id, raw_outcome in list(raw_outcomes.items())[
            -_MAX_REQUIRED_ASYNC_COMPLETIONS:
        ]:
            normalized_id = _bounded_run_state_text(delegation_id)
            if not normalized_id:
                malformed = True
                continue
            dispatch, dispatch_malformed = _normalized_required_async_dispatch(
                raw_outcome,
                legacy=True,
            )
            malformed = malformed or dispatch_malformed
            dispatches.setdefault(normalized_id, dispatch)

    # A legacy attempt with no recorded outcome is exactly the crash window
    # this state exists to close. Retain ownership and fail closed instead of
    # treating the absent volatile queue as proof that no child existed.
    if legacy and not dispatches:
        malformed = True
    if malformed:
        for delegation_id, dispatch in list(dispatches.items()):
            if dispatch["state"] not in _REQUIRED_ASYNC_PENDING_STATES:
                continue
            dispatches[delegation_id] = {
                **dispatch,
                "state": "outcome_unknown",
                "success": False,
                "status": "malformed_dispatch",
                "error": "durable async dispatch state could not be normalized safely",
            }
    sealed = raw.get("sealed") is True if not legacy else bool(dispatches) or malformed
    if malformed:
        sealed = True
    reconciled_at = _bounded_run_state_lease(raw.get("reconciled_at"))
    outcomes: dict[str, dict[str, Any]] = {}
    for delegation_id, dispatch in dispatches.items():
        if dispatch["state"] not in {"terminal", "cancelled", "outcome_unknown"}:
            continue
        outcomes[delegation_id] = {
            "success": dispatch.get("success") is True,
            "status": str(dispatch.get("status") or ""),
            "completed_at": dispatch.get("completed_at"),
            "closeout_id": str(dispatch.get("closeout_id") or ""),
        }
    required_dispatches = {
        delegation_id: dispatch
        for delegation_id, dispatch in dispatches.items()
        if dispatch.get("required") is True
    }
    advisory_dispatches = {
        delegation_id: dispatch
        for delegation_id, dispatch in dispatches.items()
        if dispatch.get("required") is not True
    }
    required_pending_count = sum(
        dispatch["state"] in _REQUIRED_ASYNC_PENDING_STATES
        for dispatch in required_dispatches.values()
    )
    advisory_pending_count = sum(
        dispatch["state"] in _REQUIRED_ASYNC_PENDING_STATES
        for dispatch in advisory_dispatches.values()
    )
    pending_count = required_pending_count + advisory_pending_count
    cancelled = sum(dispatch["state"] == "cancelled" for dispatch in dispatches.values())
    outcome_unknown = sum(
        dispatch["state"] == "outcome_unknown" for dispatch in dispatches.values()
    )
    required_cancelled = sum(
        dispatch["state"] == "cancelled" for dispatch in required_dispatches.values()
    )
    required_outcome_unknown = sum(
        dispatch["state"] == "outcome_unknown"
        for dispatch in required_dispatches.values()
    )
    advisory_failed = sum(
        dispatch["state"] in {"cancelled", "outcome_unknown"}
        or (
            dispatch["state"] == "terminal"
            and dispatch.get("success") is not True
        )
        for dispatch in advisory_dispatches.values()
    )
    sticky_failure = bool(
        malformed
        or raw.get("sticky_failure") is True
        or raw.get("attempt_cancelled") is True
        or required_cancelled
        or required_outcome_unknown
        or any(
            dispatch["state"] == "terminal" and dispatch.get("success") is not True
            for dispatch in required_dispatches.values()
        )
    )
    all_terminal = bool(dispatches) and pending_count == 0
    required_terminal = bool(required_dispatches) and required_pending_count == 0
    advisory_terminal = bool(advisory_dispatches) and advisory_pending_count == 0
    completion_terminal = required_terminal if required_dispatches else advisory_terminal
    if (malformed or (sealed and sticky_failure)) and not dispatches:
        all_terminal = True
        completion_terminal = True
    owns_recovery = bool(present and reconciled_at is None)
    return {
        "schema_version": _REQUIRED_ASYNC_SCHEMA_VERSION,
        "generation": generation,
        "attempt_id": attempt_id,
        "attempt_order": attempt_order,
        "sealed": sealed,
        "sealed_at": _bounded_run_state_lease(raw.get("sealed_at")),
        "reconciled_at": reconciled_at,
        "reconciliation_id": _bounded_run_state_text(raw.get("reconciliation_id")),
        "cancelled_at": _bounded_run_state_lease(raw.get("cancelled_at")),
        "cancellation_reason": _bounded_required_async_text(
            raw.get("cancellation_reason")
        ),
        "attempt_cancelled": raw.get("attempt_cancelled") is True,
        "attempt_cancelled_at": _bounded_run_state_lease(
            raw.get("attempt_cancelled_at")
        ),
        "attempt_cancellation_reason": _bounded_required_async_text(
            raw.get("attempt_cancellation_reason")
        ),
        "dispatches": dispatches,
        "checkpoint": checkpoint,
        "outcomes": outcomes,
        "pending_count": pending_count,
        "required_pending_count": required_pending_count,
        "advisory_pending_count": advisory_pending_count,
        "all_terminal": all_terminal,
        "completion_terminal": completion_terminal,
        "has_required": bool(required_dispatches),
        "has_advisory": bool(advisory_dispatches),
        "ready_to_reconcile": bool(
            owns_recovery and sealed and completion_terminal and reconciled_at is None
        ),
        "present": present,
        "owns_recovery": owns_recovery,
        "failed": sticky_failure,
        "sticky_failure": sticky_failure,
        "failure_reason": _bounded_run_state_text(raw.get("failure_reason")),
        "succeeded": sum(outcome["success"] is True for outcome in outcomes.values()),
        "required_succeeded": sum(
            dispatch["state"] == "terminal" and dispatch.get("success") is True
            for dispatch in required_dispatches.values()
        ),
        "advisory_succeeded": sum(
            dispatch["state"] == "terminal" and dispatch.get("success") is True
            for dispatch in advisory_dispatches.values()
        ),
        "advisory_failed": advisory_failed,
        "cancelled": cancelled,
        "required_cancelled": required_cancelled,
        "outcome_unknown": outcome_unknown,
        "required_outcome_unknown": required_outcome_unknown,
        "malformed": malformed,
    }


def _required_async_payload(state: Mapping[str, Any]) -> dict[str, Any]:
    dispatches = dict(state.get("dispatches") or {})
    outcomes: dict[str, dict[str, Any]] = {}
    for delegation_id, dispatch in dispatches.items():
        if not isinstance(dispatch, dict) or dispatch.get("state") not in {
            "terminal",
            "cancelled",
            "outcome_unknown",
        }:
            continue
        outcomes[delegation_id] = {
            "success": dispatch.get("success") is True,
            "status": _bounded_run_state_text(dispatch.get("status")),
            "completed_at": _bounded_run_state_lease(dispatch.get("completed_at")),
            "closeout_id": _bounded_run_state_text(dispatch.get("closeout_id")),
        }
    payload: dict[str, Any] = {
        "schema_version": _REQUIRED_ASYNC_SCHEMA_VERSION,
        "generation": _positive_int(state.get("generation")),
        "attempt_id": _bounded_run_state_text(state.get("attempt_id")),
        "attempt_order": _positive_int(state.get("attempt_order")),
        "sealed": state.get("sealed") is True,
        "dispatches": dispatches,
        # Retained for older callers and rollback compatibility. Dispatches are
        # canonical and every mutation rewrites this derived projection.
        "outcomes": outcomes,
    }
    for key in (
        "sealed_at",
        "reconciled_at",
        "cancelled_at",
        "attempt_cancelled_at",
    ):
        value = _bounded_run_state_lease(state.get(key))
        if value is not None:
            payload[key] = value
    for key in ("reconciliation_id", "failure_reason"):
        value = _bounded_run_state_text(state.get(key))
        if value:
            payload[key] = value
    cancellation_reason = _bounded_required_async_text(state.get("cancellation_reason"))
    if cancellation_reason:
        payload["cancellation_reason"] = cancellation_reason
    if state.get("sticky_failure") is True:
        payload["sticky_failure"] = True
    if state.get("attempt_cancelled") is True:
        payload["attempt_cancelled"] = True
    attempt_cancellation_reason = _bounded_required_async_text(
        state.get("attempt_cancellation_reason")
    )
    if attempt_cancellation_reason:
        payload["attempt_cancellation_reason"] = attempt_cancellation_reason
    checkpoint, checkpoint_malformed = _bounded_required_async_checkpoint(
        state.get("checkpoint")
    )
    if checkpoint is not None and not checkpoint_malformed:
        payload["checkpoint"] = checkpoint
    return payload


def _required_async_attempt_matches(
    state: Mapping[str, Any],
    *,
    generation: Any,
    attempt_id: Any,
    attempt_order: Any,
) -> bool:
    return bool(
        _positive_int(generation)
        and _positive_int(generation) == _positive_int(state.get("generation"))
        and _bounded_run_state_text(attempt_id)
        == _bounded_run_state_text(state.get("attempt_id"))
        and _positive_int(attempt_order)
        and _positive_int(attempt_order) == _positive_int(state.get("attempt_order"))
    )


def _required_async_attempt_relation(
    state: Mapping[str, Any],
    *,
    generation: Any,
    attempt_id: Any,
    attempt_order: Any,
) -> str | None:
    incoming_generation = _positive_int(generation)
    incoming_id = _bounded_run_state_text(attempt_id)
    incoming_order = _positive_int(attempt_order)
    if not incoming_generation or not incoming_id or not incoming_order:
        return None
    if not state.get("present"):
        return "new"
    if _required_async_attempt_matches(
        state,
        generation=incoming_generation,
        attempt_id=incoming_id,
        attempt_order=incoming_order,
    ):
        return "same"
    if (
        incoming_order > _positive_int(state.get("attempt_order"))
        and incoming_generation >= _positive_int(state.get("generation"))
        and incoming_id != _bounded_run_state_text(state.get("attempt_id"))
    ):
        return "new"
    return None


def _refresh_required_async_failure_gate(item: dict[str, Any]) -> None:
    required_state = _required_async_completion_state(item)
    if required_state["failed"]:
        gate = classify_delivery_completion(item)
        item["completion_gate"] = gate
        item["summary_status"] = str(gate.get("summary_status") or "Failed")
        return
    existing_gate = (
        item.get("completion_gate")
        if isinstance(item.get("completion_gate"), dict)
        else {}
    )
    if existing_gate.get("reason") == "required_async_completion_failed":
        item.pop("completion_gate", None)
        if str(item.get("summary_status") or "").lower() == "failed":
            item.pop("summary_status", None)


def _normalize_run_state_snapshot(item: Any) -> dict[str, Any]:
    """Return the bounded ownership fields used by finalization CAS checks."""

    source = item if isinstance(item, dict) else {}
    raw_active = source.get("active_run")
    active_run = None
    if isinstance(raw_active, dict):
        active_run = {
            "session_key": _bounded_run_state_text(raw_active.get("session_key")),
            "generation": _bounded_run_state_int(raw_active.get("generation")),
            "owner_pid": _bounded_run_state_int(raw_active.get("owner_pid")),
            "process_epoch": _bounded_run_state_text(
                raw_active.get("process_epoch"),
                limit=_RUN_STATE_EPOCH_LIMIT,
            ),
            "lease_until": _bounded_run_state_lease(raw_active.get("lease_until")),
        }
    return {
        "status": _bounded_run_state_text(source.get("status")),
        "active_run": active_run,
    }


def _run_state_matches(item: dict[str, Any], expected_run_state: Any) -> bool:
    current = _normalize_run_state_snapshot(item)
    if expected_run_state is _RUN_STATE_UNSET:
        # Legacy callers remain safe only when no run can own the item. A
        # claimed/running legacy item may lack active_run metadata, so status is
        # part of the fail-closed omission check.
        return (
            current["active_run"] is None
            and current["status"] not in _RUN_OWNED_STATUSES
        )
    if not isinstance(expected_run_state, dict):
        return False
    return current == _normalize_run_state_snapshot(expected_run_state)


def _apply_visual_qa_completion(item: dict[str, Any], gate: dict[str, Any]) -> dict[str, Any]:
    """Attach the bounded visual-QA result and enforce only proven post-edit work.

    A requirement alone never blocks a Discord completion.  Enforcement needs
    all three signals: an explicit visual request, ``enforce_explicit`` mode,
    and a landed code mutation with a known subsequent tool-order boundary.
    This keeps generic screenshots/navigation and incomplete runtime metadata
    from turning into accidental delivery gates.
    """

    config, requirement = _visual_qa_state_for_item(item)
    if requirement["level"] == "none":
        return gate

    visual: dict[str, Any] = {
        "mode": config["mode"],
        "requirement": requirement,
        "code_mutation_observed": item.get("visual_qa_code_mutation_observed") is True,
        "enforced": False,
    }
    if config["mode"] == "off":
        visual.update({"status": "not_applicable", "reason": "mode_off"})
        gate["visual_qa"] = visual
        return gate

    if not visual["code_mutation_observed"]:
        visual.update({"status": "not_applicable", "reason": "no_code_mutation_observed"})
        gate["visual_qa"] = visual
        return gate

    min_order = _positive_int(item.get("visual_qa_min_receipt_order"))
    if min_order <= 0:
        # A mutation is known, but no tool-order boundary exists to prove a
        # receipt was captured afterward. Shadow mode records that gap; an
        # explicit enforcement request must fail closed rather than accepting
        # an earlier or unrelated receipt.
        visual.update({"status": "missing", "reason": "mutation_order_unavailable"})
        if config["mode"] == "shadow":
            visual["shadow_report"] = "receipt_unverifiable"
        else:
            visual["enforced"] = True
            if gate.get("allowed_to_complete"):
                gate.update(
                    {
                        "allowed_to_complete": False,
                        "summary_status": "Blocked",
                        "terminal_status": "blocked",
                        "reason": "visual_qa_receipt_unverifiable",
                    }
                )
        gate["visual_qa"] = visual
        return gate

    receipts = _visual_qa_receipts(
        item.get("visual_qa_receipts"),
        requirement,
        limit=config["max_receipts_per_turn"],
    )
    completion = visual_receipt_completion(requirement, receipts, min_order=min_order)
    status = str(completion.get("status") or "missing")
    visual.update(
        {
            "status": status,
            "receipt": completion.get("receipt"),
            "min_receipt_order": min_order,
        }
    )
    if config["mode"] == "shadow":
        if status != "passed":
            # This is durable reporting only. Shadow mode never changes the
            # primary delivery decision or user-visible final response.
            visual["shadow_report"] = f"receipt_{status}"
        gate["visual_qa"] = visual
        return gate

    visual["enforced"] = True
    gate["visual_qa"] = visual
    if status != "passed" and gate.get("allowed_to_complete"):
        gate.update(
            {
                "allowed_to_complete": False,
                "summary_status": "Blocked",
                "terminal_status": "blocked",
                "reason": f"visual_qa_receipt_{status}",
            }
        )
    return gate


def _has_any_key(mapping: Any, keys: frozenset[str]) -> bool:
    if not isinstance(mapping, dict):
        return False
    for key, value in mapping.items():
        key_text = str(key).strip()
        if key_text in keys and value not in (None, "", False):
            return True
        if isinstance(value, dict) and _has_any_key(value, keys):
            return True
    return False


def _matches_any(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _without_verification_downgrade_lines(text: str) -> str:
    return "\n".join(
        line for line in str(text or "").splitlines() if "verification downgrade" not in line.lower()
    )


def _is_preserved_protected_checkout_gap(text: str, match: re.Match[str]) -> bool:
    """Ignore an intentionally untouched local checkout when production is current."""

    context = text[max(0, match.start() - 80) : min(len(text), match.end() + 220)]
    return bool(
        re.search(r"\bprotected\s+canonical\s+checkout\b", context, flags=re.IGNORECASE)
        and re.search(r"\bpre-existing\b", context, flags=re.IGNORECASE)
        and re.search(
            r"\bproduction\s+runtime\b[^\n.]{0,80}\b(?:current|synced|clean|up[- ]to[- ]date)\b",
            context,
            flags=re.IGNORECASE,
        )
    )


def _is_explicitly_healthy_runtime_match(match: re.Match[str]) -> bool:
    """Return whether a broad runtime-gap match contains explicit zero-lag evidence."""

    snippet = match.group(0)
    healthy_state = re.search(
        r"\b(?:current|synced|updated|clean|up[- ]to[- ]date)\b",
        snippet,
        flags=re.IGNORECASE,
    )
    zero_gap = re.search(
        r"\b(?:zero|0)\s+(?:commits?\s+)?behind\b|\bno\s+(?:sensitive\s+)?(?:lag|drift)\b",
        snippet,
        flags=re.IGNORECASE,
    )
    return bool(healthy_state and zero_gap)


def _canonical_sync_explicitly_not_required(item: dict[str, Any]) -> bool:
    """Return whether the persisted closeout contract opts out of local sync."""

    closeout = item.get("closeout") if isinstance(item.get("closeout"), dict) else {}
    policy = closeout.get("policy") if isinstance(closeout.get("policy"), dict) else {}
    requirements = (
        policy.get("post_merge_requirements")
        if isinstance(policy.get("post_merge_requirements"), dict)
        else {}
    )
    return requirements.get("canonical_sync") is False


def _merge_explicitly_not_allowed(item: dict[str, Any]) -> bool:
    closeout = item.get("closeout") if isinstance(item.get("closeout"), dict) else {}
    policy = closeout.get("policy") if isinstance(closeout.get("policy"), dict) else {}
    return policy.get("merge") == "never"


def _is_canonical_checkout_only_gap(text: str, match: re.Match[str]) -> bool:
    """Return whether a runtime-gap match concerns only the local checkout."""

    context = text[max(0, match.start() - 120) : min(len(text), match.end() + 260)]
    if not re.search(r"\bcanonical\s+checkout\b", context, flags=re.IGNORECASE):
        return False
    return not re.search(
        r"\b(?:deployed|private|production|live)\s+runtime\b|"
        r"\b(?:live|runtime|process)\s+pickup\b",
        context,
        flags=re.IGNORECASE,
    )


def _incomplete_final_markers(text: str, item: dict[str, Any] | None = None) -> list[str]:
    """Return incomplete-delivery markers while filtering known summary noise."""

    text = _without_verification_downgrade_lines(text)
    markers: list[str] = []
    for reason, pattern in _INCOMPLETE_FINAL_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            snippet = match.group(0)
            prefix = text[max(0, match.start() - 80) : match.start()].lower()
            if reason == "checks_not_green" and ("→" in snippet or "phase timing" in prefix):
                continue
            if reason == "runtime_not_synced" and _is_explicitly_healthy_runtime_match(match):
                continue
            if reason == "runtime_not_synced" and _is_preserved_protected_checkout_gap(text, match):
                continue
            if (
                reason == "not_merged"
                and isinstance(item, dict)
                and _merge_explicitly_not_allowed(item)
            ):
                continue
            if (
                reason == "runtime_not_synced"
                and isinstance(item, dict)
                and _canonical_sync_explicitly_not_required(item)
                and _is_canonical_checkout_only_gap(text, match)
            ):
                continue
            if reason == "runtime_not_synced" and re.search(
                r"\b(?:live\s+)?runtime\s+pickup\b", snippet, flags=re.IGNORECASE
            ):
                continue
            markers.append(reason)
            break
    if "live_pickup_unverified" in markers and _matches_any(text, _SAFE_GATEWAY_RELOAD_HANDOFF_PATTERNS):
        markers = [reason for reason in markers if reason != "live_pickup_unverified"]
    return markers


def _looks_like_review_only_request(text: str) -> bool:
    if not _matches_any(text, _REVIEW_ONLY_REQUEST_PATTERNS):
        return False
    return not _matches_any(text, _REVIEW_REQUEST_ACTION_PATTERNS)


def _looks_like_read_only_request(text: str) -> bool:
    if not _matches_any(text, _READ_ONLY_REQUEST_PATTERNS):
        return False
    return not _matches_any(text, _REVIEW_REQUEST_ACTION_PATTERNS)


def _repo_backed_discord_item(item: dict[str, Any]) -> bool:
    if item.get("platform") != "discord":
        return False
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    if _has_any_key(source, _PROJECT_SOURCE_KEYS):
        return True
    feature_summary = item.get("feature_summary") if isinstance(item.get("feature_summary"), dict) else {}
    project_context = feature_summary.get("project_context") if isinstance(feature_summary, dict) else None
    if project_context:
        return True
    if _has_any_key(feature_summary, frozenset({"project_path", "project_github_url"})):
        return True
    project_summary = item.get("project_summary") if isinstance(item.get("project_summary"), dict) else {}
    return _has_any_key(project_summary, _PROJECT_SUMMARY_KEYS)


def _delivery_intent_for_item(item: dict[str, Any]) -> str:
    feature_summary = item.get("feature_summary") if isinstance(item.get("feature_summary"), dict) else {}
    request_parts = [str(item.get("text") or "")]
    for key in ("initial_request", "source_text", "text"):
        value = feature_summary.get(key) if isinstance(feature_summary, dict) else None
        if value:
            request_parts.append(str(value))
    request_text = "\n".join(request_parts)
    if _matches_any(request_text, _EXPLICIT_NARROW_SCOPE_PATTERNS):
        if re.search(r"\bdraft\s+pr\b", request_text, flags=re.IGNORECASE):
            return "draft_pr"
        if re.search(r"\bpr\s+only\b|\bopen\s+(?:a\s+)?pr\b|\bpull\s+request\b", request_text, flags=re.IGNORECASE):
            return "pr_only"
        if re.search(r"\breview\s+only\b", request_text, flags=re.IGNORECASE):
            return "review_only"
        return "no_merge"
    if _looks_like_review_only_request(request_text):
        return "review_only"
    if _looks_like_read_only_request(request_text):
        return "read_only"
    return "full_lifecycle"


def _review_only_without_code_mutation(item: dict[str, Any], intent: str) -> bool:
    """Return whether negative incidental checks cannot block a review delivery."""

    if intent != "review_only":
        return False
    runtime = item.get("runtime_breakdown")
    if isinstance(runtime, dict) and (
        "mutation_generation" in runtime or "mutation_boundary" in runtime
    ):
        try:
            return (
                int(runtime.get("mutation_generation") or 0) == 0
                and int(runtime.get("mutation_boundary") or 0) == 0
            )
        except (TypeError, ValueError):
            return False
    return item.get("visual_qa_code_mutation_observed") is False


def _deferred_runtime_watch_missing_markers(text: str) -> list[str]:
    """Return missing handoff evidence for a deferred long runtime watch.

    A final response may hand off a newly scheduled Airflow/DAG/data-writing run
    to a background watcher, but only after the repo/runtime handoff itself is
    complete. This pure text gate prevents "watcher started" from masking an
    omitted runtime sync or live-pickup verification claim.
    """
    if not _matches_any(text, _DEFERRED_RUNTIME_WATCH_PATTERNS):
        return []
    missing: list[str] = []
    if not _matches_any(text, _RUNTIME_SYNC_VERIFIED_PATTERNS):
        missing.append("runtime_sync_unverified")
    if not _matches_any(text, _LIVE_PICKUP_VERIFIED_PATTERNS):
        missing.append("live_pickup_unverified")
    return missing


def classify_delivery_completion(item: dict[str, Any], final_response: str | None = None) -> dict[str, Any]:
    """Classify whether a delivered Discord response may close the work item.

    The gate is intentionally pure and uses only persisted request metadata plus
    the assistant's final wording. It never probes live git or network state.
    """
    final_text = str(final_response if final_response is not None else item.get("final_response") or "")
    repo_backed = _repo_backed_discord_item(item)
    intent = _delivery_intent_for_item(item) if repo_backed else "generic"
    required_async = _required_async_completion_state(item)
    if required_async["failed"]:
        return {
            "allowed_to_complete": False,
            "summary_status": "Failed",
            "terminal_status": "blocked",
            "reason": "required_async_completion_failed",
            "delivery_intent": intent,
            "repo_backed": repo_backed,
            "required_async_completions": {
                "generation": required_async["generation"],
                "failed": True,
                "succeeded": required_async["required_succeeded"],
            },
        }
    reported_status = str(item.get("summary_status") or "").strip().lower()
    if repo_backed and reported_status == "failed":
        return {
            "allowed_to_complete": False,
            "summary_status": "Failed",
            "terminal_status": "blocked",
            "reason": "agent_turn_failed",
            "delivery_intent": intent,
            "repo_backed": repo_backed,
        }
    gate_summary_status = str(item.get("summary_status") or "Complete")
    if gate_summary_status.lower() == "blocked":
        gate_summary_status = "Complete"
    gate = {
        "allowed_to_complete": True,
        "summary_status": gate_summary_status,
        "terminal_status": "completed",
        "reason": "not_repo_backed" if not repo_backed else "no_self_declared_delivery_gap",
        "delivery_intent": intent,
        "repo_backed": repo_backed,
    }
    if not repo_backed:
        return _apply_visual_qa_completion(item, gate)

    evidence = evidence_from_runtime_breakdown(item.get("runtime_breakdown"))
    constraints = claim_constraints_for_text(final_text, evidence)
    if not constraints.get("allowed") and not _review_only_without_code_mutation(item, intent):
        gate.update(
            {
                "allowed_to_complete": False,
                "summary_status": "Blocked",
                "terminal_status": "blocked",
                "reason": "latest_verification_evidence_negative",
                "verification_constraints": constraints,
            }
        )
        return _apply_visual_qa_completion(item, gate)

    matched = _incomplete_final_markers(final_text, item)
    runtime_handoff_missing = _deferred_runtime_watch_missing_markers(final_text) if intent == "full_lifecycle" else []
    if not matched:
        if runtime_handoff_missing:
            gate.update(
                {
                    "allowed_to_complete": False,
                    "summary_status": "Blocked",
                    "terminal_status": "blocked",
                    "reason": "runtime_handoff_unverified",
                    "matched_markers": runtime_handoff_missing,
                }
            )
        return _apply_visual_qa_completion(item, gate)

    narrow_intent = intent in {"pr_only", "review_only", "draft_pr", "no_merge"}
    only_narrow_lifecycle_markers = set(matched) <= {"not_merged", "no_deploy"}
    if intent == "review_only" and "not_done_yet" not in matched and _matches_any(final_text, _REVIEW_ONLY_FINAL_PATTERNS):
        gate["reason"] = "intentional_review_only_terminal"
        gate["matched_markers"] = matched
        return _apply_visual_qa_completion(item, gate)
    if intent == "read_only" and "not_done_yet" not in matched:
        gate["reason"] = "answered_read_only_request"
        gate["matched_markers"] = matched
        return _apply_visual_qa_completion(item, gate)
    if narrow_intent and only_narrow_lifecycle_markers and (
        _matches_any(final_text, _PR_ONLY_FINAL_PATTERNS) or _matches_any(final_text, _INTENTIONAL_UNMERGED_PATTERNS)
    ):
        gate["reason"] = "intentional_narrow_scope_terminal"
        gate["matched_markers"] = matched
        return _apply_visual_qa_completion(item, gate)

    gate.update(
        {
            "allowed_to_complete": False,
            "summary_status": "Blocked",
            "terminal_status": "blocked",
            "reason": "self_declared_delivery_incomplete",
            "matched_markers": matched,
        }
    )
    return _apply_visual_qa_completion(item, gate)


def _discord_board_slug_for_item(item: dict[str, Any]) -> str:
    feature_summary = item.get("feature_summary")
    if isinstance(feature_summary, dict):
        board = feature_summary.get("kanban_board")
        if isinstance(board, dict):
            slug = str(board.get("slug") or "").strip()
            if slug:
                return slug
    try:
        from hermes_cli.discord_worker_roles import board_slug_for_discord_thread
        from hermes_cli import kanban_db

        source = item.get("source") if isinstance(item.get("source"), dict) else {}
        thread_id = str(source.get("thread_id") or source.get("chat_id") or "").strip()
        if not thread_id:
            return ""
        slug = board_slug_for_discord_thread(thread_id)
        return slug if kanban_db.board_exists(slug) else ""
    except Exception:
        return ""


def _discord_source_message_id_for_item(item: dict[str, Any]) -> str:
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    return str(item.get("message_id") or source.get("message_id") or "").strip()


def _record_discord_board_final_response(
    item: dict[str, Any],
    *,
    result_message_id: str | None = None,
) -> None:
    if item.get("platform") != "discord":
        return
    final_response = str(item.get("final_response") or "")
    if not final_response and not result_message_id:
        return
    slug = _discord_board_slug_for_item(item)
    if not slug:
        return
    try:
        from hermes_cli.discord_worker_boards import record_final_discord_response

        record_final_discord_response(
            slug,
            final_response=final_response,
            session_id=str(item.get("session_id") or "") or None,
            work_item_id=str(item.get("id") or "") or None,
            result_message_id=result_message_id,
        )
    except Exception:
        pass


def _refresh_successful_closeout_completion(
    item: dict[str, Any],
    *,
    now: float,
) -> bool:
    """Reclassify the delivery tail after authoritative closeout succeeds."""

    status = str(item.get("status") or "")
    if status not in {
        "agent_done",
        "response_delivered",
        "summary_updated",
        "blocked",
    }:
        return False
    prior_gate = (
        dict(item["completion_gate"])
        if isinstance(item.get("completion_gate"), dict)
        else {}
    )
    prior_gate_blocked = bool(
        prior_gate and prior_gate.get("allowed_to_complete") is False
    )
    gate = classify_delivery_completion(item)
    item["completion_gate"] = gate
    item["summary_status"] = str(gate.get("summary_status") or "Complete")
    item["updated_at"] = now
    gate_allowed = gate.get("allowed_to_complete") is True
    response_confirmed = bool(
        item.get("confirmed_message_ids")
        or str(item.get("result_message_id") or "").strip()
        or str(item.get("delivery_outcome") or "") == "delivered"
    )
    stale_block_cleared = prior_gate_blocked and gate_allowed
    next_status = status
    if status == "summary_updated":
        if stale_block_cleared:
            # The summary was rendered from a stale blocked gate. Reopen only
            # the deterministic delivery tail so the existing response is not
            # sent twice and the summary/reaction can be corrected.
            next_status = (
                "response_delivered" if response_confirmed else "agent_done"
            )
        else:
            next_status = (
                "completed"
                if gate_allowed
                else str(gate.get("terminal_status") or "blocked")
            )
    elif (
        status == "blocked"
        and stale_block_cleared
        and not isinstance(item.get("terminal_delivery"), dict)
    ):
        next_status = "response_delivered" if response_confirmed else "agent_done"

    if next_status != status:
        item["status"] = next_status
        if next_status in {"agent_done", "response_delivered"}:
            item.pop("summary_updated_at", None)
            item.pop("blocked_at", None)
            item.pop("blocked_reason", None)
        elif next_status == "blocked":
            item["blocked_at"] = now
        _record_provider_progress(
            item,
            f"ledger_status_{next_status}",
            status=next_status,
        )
    _record_discord_board_final_response(item)
    return True


def _item_id(
    *,
    platform: Any,
    chat_id: str | None,
    thread_id: str | None,
    message_id: str | None,
    command: str | None,
    target: str | None,
) -> str:
    platform_value = str(_platform_value(platform)).lower()
    chat = str(chat_id or "")
    thread = str(thread_id or chat)
    message = str(message_id or "")
    cmd = str(command or "message").strip().lower()
    tgt = str(target or thread or chat).strip()
    return "|".join((platform_value, chat, thread, message, cmd, tgt))


def _source_from_dict(data: dict[str, Any]) -> SessionSource:
    return SessionSource.from_dict(data)


@contextlib.contextmanager
def _ledger_file_lock(path: Path, *, timeout_seconds: float = _CLOSEOUT_LOCK_TIMEOUT_SECONDS):
    """Serialize closeout read-modify-write operations across processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with _PROCESS_LEDGER_LOCK:
        handle = lock_path.open("a+b")
        acquired = False
        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        try:
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError as exc:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"work ledger lock unavailable: {lock_path}") from exc
                    time.sleep(_CLOSEOUT_LOCK_POLL_SECONDS)
            yield
        finally:
            try:
                if acquired:
                    if os.name == "nt":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()


def _locked_ledger_mutation(method):
    """Apply the same cross-process lock to every ledger read-modify-write."""

    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        with _ledger_file_lock(self.path):
            return method(self, *args, **kwargs)

    return wrapped


class GatewayWorkLedger:
    """JSON-backed ledger of accepted gateway work.

    The gateway is single-process, so a small atomic JSON file keeps this
    simple while still surviving shutdowns and crashes.  Entries are keyed by
    the source platform's durable message id so duplicate delivery cannot spawn
    duplicate work.
    """

    def __init__(self, path: Path | None = None, *, now_fn=_now):
        self.path = Path(path) if path is not None else default_path()
        self._now = now_fn

    def _empty(self) -> dict[str, Any]:
        return {"version": 2, "items": {}, "discord_pr_lifecycles": {}}

    def _read(self) -> dict[str, Any]:
        try:
            payload = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._empty()
        try:
            data = json.loads(payload)
        except Exception:
            return self._empty()
        if not isinstance(data, dict):
            return self._empty()
        items = data.get("items")
        if not isinstance(items, dict):
            data["items"] = {}
        if not isinstance(data.get("discord_pr_lifecycles"), dict):
            data["discord_pr_lifecycles"] = {}
        # v2 adds explicit Discord runtime/lifecycle metadata. Rows are
        # migrated lazily: missing fields retain the legacy ACTION contract in
        # event_from_item(), while the next mutation writes version 2.
        try:
            prior_version = int(data.get("version") or 1)
        except (TypeError, ValueError):
            prior_version = 1
        data["version"] = max(2, prior_version)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        try:
            force_budget = self.path.stat().st_size > DEFAULT_LEDGER_HARD_BYTES
        except OSError:
            force_budget = False
        # Legacy rows predate the explicit per-thread lifecycle map. Preserve
        # their generation and merged state before terminal compaction replaces
        # the full records with tombstones.
        self._materialize_discord_pr_lifecycles(data)
        self._compact_if_due(data, force_budget=force_budget)
        self._compact_discord_pr_lifecycles(data)
        # The ledger is a hot recovery path, not an operator-edited config.
        # Keep the existing atomic/fsync write guarantees while avoiding the
        # whitespace and key-sorting cost on every full-file rewrite.
        atomic_json_write(self.path, data, indent=None, separators=(",", ":"))

    @staticmethod
    def _discord_pr_generation(value: Any) -> int:
        try:
            generation = int(value)
        except (TypeError, ValueError, OverflowError):
            return 1
        return max(1, min(2_147_483_647, generation))

    @staticmethod
    def _discord_pr_updated_at(value: Any) -> float:
        try:
            return max(0.0, float(value or 0.0))
        except (TypeError, ValueError, OverflowError):
            return 0.0

    @classmethod
    def _discord_pr_item_merged(cls, item: Mapping[str, Any]) -> bool:
        if item.get("discord_pr_merged") is True:
            return True
        dev_merge = item.get("dev_merge")
        if isinstance(dev_merge, Mapping) and str(dev_merge.get("status") or "") == "merged":
            return True
        closeout = item.get("closeout")
        if not isinstance(closeout, Mapping):
            return False
        pr = closeout.get("pr") if isinstance(closeout.get("pr"), Mapping) else {}
        return (
            str(pr.get("state") or "").strip().upper() == "MERGED"
            or bool(str(pr.get("merge_sha") or "").strip())
        )

    @classmethod
    def _derive_discord_pr_lifecycle(
        cls,
        items: Mapping[str, Any],
        session_key: str,
    ) -> dict[str, Any]:
        candidates = [
            item
            for item in items.values()
            if isinstance(item, Mapping)
            and str(item.get("platform") or "") == "discord"
            and str(item.get("session_key") or "") == session_key
            and str(item.get("discord_runtime_mode") or "action") == "action"
            and item.get("participates_in_work_lifecycle", True) is not False
        ]
        if not candidates:
            return {"generation": 1, "status": "active"}
        generation = max(
            cls._discord_pr_generation(item.get("discord_pr_generation"))
            for item in candidates
        )
        generation_items = [
            item
            for item in candidates
            if cls._discord_pr_generation(item.get("discord_pr_generation")) == generation
        ]
        status = (
            "merged"
            if any(cls._discord_pr_item_merged(item) for item in generation_items)
            else "active"
        )
        updated_at = max(
            cls._discord_pr_updated_at(item.get("updated_at"))
            for item in generation_items
        )
        return {
            "generation": generation,
            "status": status,
            "updated_at": updated_at,
        }

    @classmethod
    def _materialize_discord_pr_lifecycles(cls, data: dict[str, Any]) -> None:
        """Persist reconstructible thread state before item compaction."""

        items = data.get("items")
        if not isinstance(items, Mapping):
            return
        lifecycles = data.setdefault("discord_pr_lifecycles", {})
        if not isinstance(lifecycles, dict):
            lifecycles = {}
            data["discord_pr_lifecycles"] = lifecycles
        session_keys = {
            str(item.get("session_key") or "").strip()[:800]
            for item in items.values()
            if isinstance(item, Mapping)
            and str(item.get("platform") or "") == "discord"
            and str(item.get("discord_runtime_mode") or "action") == "action"
            and item.get("participates_in_work_lifecycle", True) is not False
            and str(item.get("session_key") or "").strip()
        }
        for session_key in session_keys:
            derived = cls._derive_discord_pr_lifecycle(items, session_key)
            current = cls._discord_pr_lifecycle(data, session_key)
            derived_generation = cls._discord_pr_generation(derived.get("generation"))
            current_generation = cls._discord_pr_generation(current.get("generation"))
            if session_key not in lifecycles or derived_generation > current_generation:
                lifecycles[session_key] = derived
            elif (
                derived_generation == current_generation
                and derived.get("status") == "merged"
                and current.get("status") != "merged"
            ):
                lifecycles[session_key] = {
                    **current,
                    "status": "merged",
                    "updated_at": max(
                        cls._discord_pr_updated_at(current.get("updated_at")),
                        cls._discord_pr_updated_at(derived.get("updated_at")),
                    ),
                }

    @classmethod
    def _discord_pr_lifecycle(
        cls,
        data: Mapping[str, Any],
        session_key: str,
    ) -> dict[str, Any]:
        lifecycles = data.get("discord_pr_lifecycles")
        raw = lifecycles.get(session_key) if isinstance(lifecycles, Mapping) else None
        if isinstance(raw, Mapping):
            status = str(raw.get("status") or "active").strip().lower()
            if status not in {"active", "merged"}:
                status = "active"
            return {
                "generation": cls._discord_pr_generation(raw.get("generation")),
                "status": status,
                "updated_at": cls._discord_pr_updated_at(raw.get("updated_at")),
                "pr_url": str(raw.get("pr_url") or "")[:1200],
            }
        items = data.get("items") if isinstance(data.get("items"), Mapping) else {}
        return cls._derive_discord_pr_lifecycle(items, session_key)

    @classmethod
    def _compact_discord_pr_lifecycles(cls, data: dict[str, Any]) -> None:
        raw = data.get("discord_pr_lifecycles")
        if not isinstance(raw, dict):
            data["discord_pr_lifecycles"] = {}
            return
        normalized: dict[str, dict[str, Any]] = {}
        for key, value in raw.items():
            session_key = str(key or "").strip()[:800]
            if not session_key or not isinstance(value, Mapping):
                continue
            status = str(value.get("status") or "active").strip().lower()
            if status not in {"active", "merged"}:
                status = "active"
            normalized[session_key] = {
                "generation": cls._discord_pr_generation(value.get("generation")),
                "status": status,
                "updated_at": cls._discord_pr_updated_at(value.get("updated_at")),
                "pr_url": str(value.get("pr_url") or "")[:1200],
            }
        if len(normalized) > _MAX_DISCORD_PR_LIFECYCLES:
            items = data.get("items") if isinstance(data.get("items"), Mapping) else {}
            reconstructible_sessions = {
                str(item.get("session_key") or "").strip()[:800]
                for item in items.values()
                if isinstance(item, Mapping)
                and str(item.get("platform") or "") == "discord"
                and str(item.get("discord_runtime_mode") or "action") == "action"
                and item.get("participates_in_work_lifecycle", True) is not False
                and str(item.get("session_key") or "").strip()
            }
            ordered = sorted(
                normalized.items(),
                key=lambda item: item[1]["updated_at"],
                reverse=True,
            )
            retained = dict(ordered[:_MAX_DISCORD_PR_LIFECYCLES])
            # An entry without retained item history is the only durable proof
            # that a thread already merged. Never evict that proof merely to
            # satisfy the soft lifecycle-count bound.
            for session_key, lifecycle in ordered[_MAX_DISCORD_PR_LIFECYCLES:]:
                if session_key not in reconstructible_sessions and (
                    lifecycle["status"] == "merged"
                    or cls._discord_pr_generation(lifecycle["generation"]) > 1
                ):
                    retained[session_key] = lifecycle
            normalized = retained
        data["discord_pr_lifecycles"] = normalized

    def discord_pr_generation(self, session_key: str) -> int:
        """Return the active PR generation for one Discord thread session."""

        key = str(session_key or "").strip()[:800]
        if not key:
            return 1
        return self._discord_pr_generation(
            self._discord_pr_lifecycle(self._read(), key).get("generation")
        )

    @classmethod
    def normalize_discord_pr_generation(cls, value: Any) -> int:
        return cls._discord_pr_generation(value)

    @staticmethod
    def _terminal_delivery_is_pending(item: Mapping[str, Any]) -> bool:
        delivery = item.get("terminal_delivery")
        if not isinstance(delivery, dict):
            return False
        return (
            str(delivery.get("status") or "") not in {"completed", "uncertain"}
            or delivery.get("summary_updated_at") is None
        )

    @staticmethod
    def _closeout_reconciliation_is_pending(item: Mapping[str, Any]) -> bool:
        closeout = item.get("closeout")
        if not isinstance(closeout, dict):
            return False
        active = item.get("closeout_authoritative") is True or item.get(
            "closeout_activated_at"
        ) is not None
        if not active:
            return False
        lease = closeout.get("lease") if isinstance(closeout.get("lease"), dict) else {}
        return (
            str(closeout.get("status") or "") not in _QUIESCENT_CLOSEOUT_STATUSES
            or bool(lease.get("owner"))
            or lease.get("until") is not None
            or closeout.get("next_due_at") is not None
        )

    @classmethod
    def _is_quiescent_terminal_item(cls, item: Mapping[str, Any]) -> bool:
        status = str(item.get("status") or "")
        if status not in TERMINAL_STATUSES or status == "blocked":
            return False
        if isinstance(item.get("active_run"), dict) or item.get("lease_until") not in {
            None,
            0,
            0.0,
        }:
            return False
        if item.get("terminal_reaction_sync_pending") is True:
            return False
        if cls._terminal_delivery_is_pending(item):
            return False
        if cls._closeout_reconciliation_is_pending(item):
            return False
        if _required_async_completion_state(item).get("owns_recovery"):
            return False
        dev_merge = item.get("dev_merge")
        if isinstance(dev_merge, dict) and str(dev_merge.get("status") or "") == "attempting":
            return False
        delivery_attempt = item.get("delivery_attempt")
        return not (
            isinstance(delivery_attempt, dict)
            and str(delivery_attempt.get("status") or "") in {"sending", "uncertain"}
        )

    @classmethod
    def _tombstone_for(
        cls,
        item: Mapping[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        tombstone = {
            "id": str(item.get("id") or ""),
            "status": str(item.get("status") or "expired"),
            "tombstone": True,
            "tombstoned_at": now,
            "tombstone_expires_at": now + _TOMBSTONE_RETENTION_SECONDS,
        }
        if (
            str(item.get("platform") or "") == "discord"
            and str(item.get("session_key") or "").strip()
            and str(item.get("discord_runtime_mode") or "action") == "action"
            and item.get("participates_in_work_lifecycle", True) is not False
        ):
            tombstone.update(
                {
                    "platform": "discord",
                    "session_key": str(item.get("session_key") or "")[:800],
                    "discord_runtime_mode": "action",
                    "participates_in_work_lifecycle": True,
                    "discord_pr_generation": cls._discord_pr_generation(
                        item.get("discord_pr_generation")
                    ),
                    "discord_pr_merged": cls._discord_pr_item_merged(item),
                    "updated_at": cls._discord_pr_updated_at(
                        item.get("updated_at")
                    ),
                }
            )
        return tombstone

    def _compact_if_due(
        self,
        data: dict[str, Any],
        *,
        force_budget: bool = False,
    ) -> None:
        """Bound old terminal history without adding read-path rewrites."""

        now = self._now()
        previous = data.get("last_compacted_at")
        if isinstance(previous, (int, float)):
            try:
                if (
                    not force_budget
                    and now - float(previous) < _COMPACTION_INTERVAL_SECONDS
                ):
                    return
            except (TypeError, ValueError, OverflowError):
                pass

        items = data.get("items")
        if not isinstance(items, dict):
            data["items"] = {}
            data["last_compacted_at"] = now
            return
        for work_id, item in list(items.items()):
            if not isinstance(item, dict):
                items.pop(work_id, None)
                continue
            if item.get("tombstone") is True:
                try:
                    tombstone_expires_at = float(item.get("tombstone_expires_at") or 0)
                except (TypeError, ValueError, OverflowError):
                    tombstone_expires_at = 0
                if tombstone_expires_at <= now:
                    items.pop(work_id, None)
                continue
            try:
                updated_at = float(item.get("updated_at") or item.get("created_at") or now)
            except (TypeError, ValueError, OverflowError):
                continue
            if (
                updated_at <= now - _FULL_RECORD_RETENTION_SECONDS
                and self._is_quiescent_terminal_item(item)
            ):
                items[work_id] = self._tombstone_for(item, now=now)
        if force_budget:
            enforce_ledger_budget(
                data,
                now=now,
                is_quiescent=self._is_quiescent_terminal_item,
                make_tombstone=lambda item: self._tombstone_for(item, now=now),
                target_bytes=DEFAULT_LEDGER_TARGET_BYTES,
                hard_bytes=DEFAULT_LEDGER_HARD_BYTES,
            )
        data["last_compacted_at"] = now

    @staticmethod
    def id_for_event(event: Any, session_key: str | None = None) -> str | None:
        source = getattr(event, "source", None)
        if source is None or getattr(source, "platform", None) != Platform.DISCORD:
            return None
        message_id = getattr(event, "message_id", None) or getattr(source, "message_id", None)
        if not message_id:
            return None
        command = None
        try:
            command = event.get_command()
        except Exception:
            command = None
        return _item_id(
            platform=source.platform,
            chat_id=getattr(source, "chat_id", None),
            thread_id=getattr(source, "thread_id", None),
            message_id=message_id,
            command=command,
            target=session_key,
        )

    @_locked_ledger_mutation
    def accept_event(
        self,
        event: Any,
        *,
        session_key: str,
        freshness_seconds: float,
        status: str = "accepted",
        visual_qa_config: Any = None,
        drain_recovery: bool = False,
    ) -> dict[str, Any] | None:
        source = getattr(event, "source", None)
        work_id = self.id_for_event(event, session_key)
        if source is None or work_id is None:
            return None
        now = self._now()
        data = self._read()
        items = data["items"]
        existing = items.get(work_id)
        if isinstance(existing, dict):
            copy = dict(existing)
            copy["_existing"] = True
            return copy

        message_type = getattr(getattr(event, "message_type", None), "value", None)
        runtime_mode = str(
            getattr(event, "discord_runtime_mode", None) or "action"
        ).strip().lower()
        participates_in_lifecycle = bool(
            getattr(event, "participates_in_work_lifecycle", True)
        )
        pr_generation = 1
        pr_rollover = False
        lifecycle_key = str(session_key or "").strip()[:800]
        if runtime_mode == "action" and participates_in_lifecycle and lifecycle_key:
            lifecycle = self._discord_pr_lifecycle(data, lifecycle_key)
            pr_generation = self._discord_pr_generation(lifecycle.get("generation"))
            if lifecycle.get("status") == "merged":
                pr_generation = min(2_147_483_647, pr_generation + 1)
                pr_rollover = True
            data.setdefault("discord_pr_lifecycles", {})[lifecycle_key] = {
                "generation": pr_generation,
                "status": "active",
                "updated_at": now,
                "pr_url": "",
            }
        normalized_visual_config = normalize_visual_qa_config(visual_qa_config)
        visual_requirement = _visual_qa_requirement_for_event(event)
        channel_prompt = getattr(event, "channel_prompt", None)
        base_channel_prompt = getattr(
            event,
            "discord_action_request_base_channel_prompt",
            None,
        )
        item = {
            "id": work_id,
            "status": status,
            "created_at": now,
            "updated_at": now,
            "expires_at": now + max(1.0, float(freshness_seconds)),
            "session_key": session_key,
            "platform": "discord",
            "source": source.to_dict(),
            "text": getattr(event, "text", "") or "",
            "message_type": message_type or "text",
            "message_id": getattr(event, "message_id", None) or getattr(source, "message_id", None),
            "reply_to_message_id": getattr(event, "reply_to_message_id", None),
            "reply_to_text": getattr(event, "reply_to_text", None),
            "channel_prompt": channel_prompt,
            # Keep the effective read-only prompt for restart recovery and the
            # unmodified operator/channel prompt for a later action promotion.
            # Older rows stored only the base prompt in ``channel_prompt``;
            # event_from_item() treats that value as the compatibility fallback.
            "discord_action_request_base_channel_prompt": base_channel_prompt,
            "channel_context": getattr(event, "channel_context", None),
            "goal_thread_context": getattr(event, "goal_thread_context", None),
            "discord_runtime_mode": runtime_mode,
            "discord_runtime_reason": getattr(event, "discord_runtime_reason", None),
            "discord_action_escalation_allowed": bool(
                getattr(event, "discord_action_escalation_allowed", False)
            ),
            "discord_explicit_no_action_denial": bool(
                getattr(event, "discord_explicit_no_action_denial", False)
            ),
            "participates_in_work_lifecycle": participates_in_lifecycle,
            "discord_pr_generation": pr_generation,
            "discord_pr_rollover": pr_rollover,
            "drain_recovery": bool(drain_recovery),
            "feature_summary": _durable_metadata(getattr(event, "feature_summary", None)),
            "project_summary": _durable_metadata(getattr(event, "project_summary", None)),
            # These are deliberately normalized before persistence. The
            # request requirement contains only opaque target/assertion IDs,
            # config retains no unknown keys, and receipts are sanitized
            # separately when a turn ends.
            "visual_qa_config": normalized_visual_config,
            "visual_qa_requirement": visual_requirement,
            "visual_qa_receipts": [],
            "active_run": None,
            "lease_until": None,
            "result_message_id": None,
        }
        items[work_id] = item
        self._write(data)
        copy = dict(item)
        copy["_existing"] = False
        return copy

    def get(self, work_id: str) -> dict[str, Any] | None:
        item = self._read().get("items", {}).get(work_id)
        return dict(item) if isinstance(item, dict) else None

    @_locked_ledger_mutation
    def claim_dev_merge_for_message(
        self,
        *,
        chat_id: str,
        message_id: str,
        actor_id: str,
        lease_seconds: float = 300.0,
    ) -> dict[str, Any] | None:
        """Claim a merge only for a delivered terminal Discord response."""

        target_chat = str(chat_id or "").strip()
        target_message = str(message_id or "").strip()
        if not target_chat or not target_message:
            return None
        data = self._read()
        candidates: list[dict[str, Any]] = []
        for item in data.get("items", {}).values():
            if not isinstance(item, dict) or str(item.get("platform") or "") != "discord":
                continue
            source = item.get("source") if isinstance(item.get("source"), dict) else {}
            if str(source.get("chat_id") or "") != target_chat:
                continue
            confirmed_ids = _bounded_delivery_message_ids(
                item.get("confirmed_message_ids"),
                primary=item.get("result_message_id"),
            )
            if target_message not in confirmed_ids:
                continue
            closeout = item.get("closeout") if isinstance(item.get("closeout"), dict) else {}
            if str(closeout.get("status") or "") != "pr_published":
                continue
            candidates.append(item)
        if not candidates:
            return None

        item = max(
            candidates,
            key=lambda value: float(value.get("updated_at") or value.get("created_at") or 0),
        )
        now = self._now()
        current = item.get("dev_merge") if isinstance(item.get("dev_merge"), dict) else {}
        current_status = str(current.get("status") or "")
        result = dict(item)
        if current_status == "merged":
            result["_dev_merge_claim"] = "already_merged"
            return result
        if current_status == "attempting" and float(current.get("lease_until") or 0) > now:
            result["_dev_merge_claim"] = "in_progress"
            return result

        revision = _positive_int(current.get("revision")) + 1
        attempt_id = f"dev-merge-{revision}"
        item["dev_merge"] = {
            "status": "attempting",
            "revision": revision,
            "attempt_id": attempt_id,
            "actor_id": str(actor_id or "")[:160],
            "message_id": target_message[:_MAX_DELIVERY_MESSAGE_ID_LENGTH],
            "started_at": now,
            "lease_until": now + max(1.0, float(lease_seconds)),
        }
        item["updated_at"] = now
        self._write(data)
        result = dict(item)
        result["_dev_merge_claim"] = "claimed"
        result["_dev_merge_attempt_id"] = attempt_id
        return result

    @_locked_ledger_mutation
    def finish_dev_merge(
        self,
        work_id: str,
        *,
        attempt_id: str,
        outcome: str,
        message: str,
        pr_url: str,
    ) -> bool:
        data = self._read()
        item = data.get("items", {}).get(work_id)
        current = item.get("dev_merge") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or not isinstance(current, dict)
            or str(current.get("status") or "") != "attempting"
            or str(current.get("attempt_id") or "") != str(attempt_id or "")
        ):
            return False
        safe_outcome = str(outcome or "blocked").strip().lower()
        if safe_outcome not in {"merged", "already_merged", "blocked", "uncertain"}:
            safe_outcome = "blocked"
        now = self._now()
        item["dev_merge"] = {
            **current,
            "status": "merged" if safe_outcome in {"merged", "already_merged"} else safe_outcome,
            "outcome": safe_outcome,
            "message": str(message or "")[:1000],
            "pr_url": str(pr_url or "")[:1200],
            "finished_at": now,
            "lease_until": None,
        }
        if safe_outcome in {"merged", "already_merged"}:
            session_key = str(item.get("session_key") or "").strip()[:800]
            if session_key:
                generation = self._discord_pr_generation(
                    item.get("discord_pr_generation")
                )
                lifecycle = self._discord_pr_lifecycle(data, session_key)
                if self._discord_pr_generation(lifecycle.get("generation")) <= generation:
                    data.setdefault("discord_pr_lifecycles", {})[session_key] = {
                        "generation": generation,
                        "status": "merged",
                        "updated_at": now,
                        "pr_url": str(pr_url or "")[:1200],
                    }
        item["updated_at"] = now
        self._write(data)
        return True

    def required_async_completion_state(self, work_id: str) -> dict[str, Any] | None:
        item = self._read().get("items", {}).get(work_id)
        if not isinstance(item, dict):
            return None
        return _required_async_completion_state(item)

    @staticmethod
    def _required_async_write_context(
        data: dict[str, Any],
        work_id: str,
        *,
        generation: Any,
        attempt_id: Any,
        attempt_order: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        item = data.get("items", {}).get(work_id)
        if not isinstance(item, dict):
            return None
        state = _required_async_completion_state(item)
        if not _required_async_attempt_matches(
            state,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        ):
            return None
        return item, state

    def _persist_required_async_state(
        self,
        data: dict[str, Any],
        item: dict[str, Any],
        state: Mapping[str, Any],
        *,
        refresh_gate: bool = False,
    ) -> dict[str, Any]:
        item["required_async_completions"] = _required_async_payload(state)
        if refresh_gate:
            _refresh_required_async_failure_gate(item)
        item["updated_at"] = self._now()
        self._write(data)
        return _required_async_completion_state(item)

    @_locked_ledger_mutation
    def begin_required_async_attempt(
        self,
        work_id: str,
        *,
        attempt_id: str | None,
        attempt_order: int | None,
        generation: int | None = None,
    ) -> dict[str, Any] | None:
        """Compatibility API; producers should atomically register a dispatch."""
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return None
        incoming_id = _bounded_run_state_text(attempt_id)
        incoming_order = _positive_int(attempt_order)
        incoming_generation = _positive_int(generation)
        state = _required_async_completion_state(item)
        relation = _required_async_attempt_relation(
            state,
            generation=incoming_generation,
            attempt_id=incoming_id,
            attempt_order=incoming_order,
        )
        if relation == "same":
            return state
        if relation != "new":
            return None
        item["required_async_completions"] = _required_async_payload({
            "attempt_id": incoming_id,
            "attempt_order": incoming_order,
            "generation": incoming_generation,
            "sealed": False,
            "dispatches": {},
        })
        _refresh_required_async_failure_gate(item)
        item["updated_at"] = self._now()
        self._write(data)
        return _required_async_completion_state(item)

    @_locked_ledger_mutation
    def register_required_async_dispatch(
        self,
        work_id: str,
        *,
        delegation_id: str,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        owner_pid: int | None = None,
        process_epoch: str | None = None,
        registered_at: float | None = None,
        closeout_id: str | None = None,
        scope_paths: Any = None,
        kind: str = "coding_worker",
        required: bool = True,
        evidence: Mapping[str, Any] | None = None,
        recovery: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Register one actual executor dispatch before submission begins.

        The historical method name is retained for compatibility. ``required``
        distinguishes coding work that gates reconciliation from advisory
        read-only work whose result is included only when it is ready.
        """

        data = self._read()
        item = data.get("items", {}).get(work_id)
        if not isinstance(item, dict):
            return None
        state = _required_async_completion_state(item)
        relation = _required_async_attempt_relation(
            state,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        )
        if relation is None:
            return None
        if relation == "new":
            state = {
                "generation": _positive_int(generation),
                "attempt_id": _bounded_run_state_text(attempt_id),
                "attempt_order": _positive_int(attempt_order),
                "sealed": False,
                "dispatches": {},
            }
        elif state["sealed"]:
            return None
        completion_id = _bounded_run_state_text(delegation_id)
        if not completion_id:
            return None
        dispatches = dict(state["dispatches"])
        existing = dispatches.get(completion_id)
        epoch = _bounded_run_state_text(process_epoch, limit=_RUN_STATE_EPOCH_LIMIT)
        pid = _bounded_run_state_int(owner_pid or os.getpid())
        closeout = _bounded_run_state_text(closeout_id)
        dispatch_kind = str(kind or "coding_worker").strip().lower()
        if dispatch_kind not in {"coding_worker", "advisory"}:
            return None
        is_required = bool(required) and dispatch_kind == "coding_worker"
        bounded_evidence = _bounded_required_async_evidence(
            {**dict(evidence or {}), "scope_paths": scope_paths}
        )
        bounded_recovery, recovery_malformed = _bounded_required_async_recovery(
            recovery
        )
        if recovery_malformed:
            return None
        if isinstance(existing, dict):
            if (
                existing.get("state") == "registered"
                and existing.get("kind") == dispatch_kind
                and existing.get("required") is is_required
                and int(existing.get("owner_pid") or 0) == pid
                and str(existing.get("process_epoch") or "") == epoch
                and str(existing.get("closeout_id") or "") == closeout
                and existing.get("evidence", {}).get("scope_paths", [])
                == bounded_evidence.get("scope_paths", [])
                and existing.get("recovery", {}) == bounded_recovery
            ):
                return state
            return None
        if len(dispatches) >= _MAX_REQUIRED_ASYNC_COMPLETIONS:
            return None
        dispatches[completion_id] = {
            "kind": dispatch_kind,
            "required": is_required,
            "state": "registered",
            "success": None,
            "status": "registered",
            "registered_at": _bounded_run_state_lease(
                registered_at if registered_at is not None else self._now()
            ),
            "started_at": None,
            "completed_at": None,
            "owner_pid": pid,
            "process_epoch": epoch,
            "closeout_id": closeout,
            "summary": "",
            "error": "",
            "evidence": bounded_evidence,
            "recovery": bounded_recovery,
        }
        state["dispatches"] = dispatches
        return self._persist_required_async_state(
            data,
            item,
            state,
            refresh_gate=relation == "new",
        )

    @_locked_ledger_mutation
    def mark_required_async_dispatch_running(
        self,
        work_id: str,
        *,
        delegation_id: str,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        owner_pid: int | None = None,
        process_epoch: str | None = None,
        started_at: float | None = None,
    ) -> dict[str, Any] | None:
        data = self._read()
        context = self._required_async_write_context(
            data,
            work_id,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        )
        if context is None:
            return None
        item, state = context
        completion_id = _bounded_run_state_text(delegation_id)
        dispatches = dict(state["dispatches"])
        existing = dispatches.get(completion_id)
        if not isinstance(existing, dict):
            return None
        pid = _bounded_run_state_int(owner_pid or os.getpid())
        epoch = _bounded_run_state_text(process_epoch, limit=_RUN_STATE_EPOCH_LIMIT)
        if (
            int(existing.get("owner_pid") or 0) != pid
            or str(existing.get("process_epoch") or "") != epoch
        ):
            return None
        if existing.get("state") == "running":
            return state
        if existing.get("state") != "registered":
            return None
        dispatch = dict(existing)
        recovery = dict(dispatch.get("recovery") or {})
        if recovery:
            recovery.update(
                {
                    "status": "running",
                    "heartbeat_at": _bounded_run_state_lease(
                        started_at if started_at is not None else self._now()
                    ),
                }
            )
            recovery, _ = _bounded_required_async_recovery(recovery)
        dispatch.update(
            {
                "state": "running",
                "status": "running",
                "started_at": _bounded_run_state_lease(
                    started_at if started_at is not None else self._now()
                ),
                "recovery": recovery,
            }
        )
        dispatches[completion_id] = dispatch
        state["dispatches"] = dispatches
        return self._persist_required_async_state(data, item, state)

    @_locked_ledger_mutation
    def update_required_async_dispatch_recovery(
        self,
        work_id: str,
        *,
        delegation_id: str,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        owner_pid: int | None = None,
        process_epoch: str | None = None,
        updates: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """CAS-update one pending child checkpoint without changing its identity."""

        data = self._read()
        context = self._required_async_write_context(
            data,
            work_id,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        )
        if context is None:
            return None
        item, state = context
        completion_id = _bounded_run_state_text(delegation_id)
        dispatches = dict(state["dispatches"])
        existing = dispatches.get(completion_id)
        if (
            not isinstance(existing, dict)
            or existing.get("state") not in _REQUIRED_ASYNC_PENDING_STATES
        ):
            return None
        expected_pid = _bounded_run_state_int(owner_pid)
        expected_epoch = _bounded_run_state_text(
            process_epoch,
            limit=_RUN_STATE_EPOCH_LIMIT,
        )
        if owner_pid is not None and int(existing.get("owner_pid") or 0) != expected_pid:
            return None
        if process_epoch is not None and str(existing.get("process_epoch") or "") != expected_epoch:
            return None
        merged = dict(existing.get("recovery") or {})
        merged.update(dict(updates or {}))
        recovery, malformed = _bounded_required_async_recovery(merged)
        if malformed:
            return None
        dispatch = dict(existing)
        dispatch["recovery"] = recovery
        dispatches[completion_id] = dispatch
        state["dispatches"] = dispatches
        return self._persist_required_async_state(data, item, state)

    @_locked_ledger_mutation
    def claim_required_async_dispatch_recovery(
        self,
        work_id: str,
        *,
        delegation_id: str,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        expected_owner_pid: int | None = None,
        expected_process_epoch: str | None = None,
        owner_pid: int,
        process_epoch: str,
        launch_id: str,
        claimed_at: float | None = None,
    ) -> dict[str, Any] | None:
        """Atomically fence a dead producer and assign one recovery launch."""

        data = self._read()
        context = self._required_async_write_context(
            data,
            work_id,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        )
        if context is None:
            return None
        item, state = context
        completion_id = _bounded_run_state_text(delegation_id)
        dispatches = dict(state["dispatches"])
        existing = dispatches.get(completion_id)
        if (
            not isinstance(existing, dict)
            or existing.get("state") not in _REQUIRED_ASYNC_PENDING_STATES
        ):
            return None
        expected_pid = _bounded_run_state_int(expected_owner_pid)
        expected_epoch = _bounded_run_state_text(
            expected_process_epoch,
            limit=_RUN_STATE_EPOCH_LIMIT,
        )
        if expected_owner_pid is not None and int(existing.get("owner_pid") or 0) != expected_pid:
            return None
        if expected_process_epoch is not None and str(existing.get("process_epoch") or "") != expected_epoch:
            return None
        new_pid = _bounded_run_state_int(owner_pid)
        new_epoch = _bounded_run_state_text(process_epoch, limit=_RUN_STATE_EPOCH_LIMIT)
        new_launch_id = _bounded_run_state_text(
            launch_id,
            limit=_REQUIRED_ASYNC_EVIDENCE_TEXT_LIMIT,
        )
        if not new_pid or not new_epoch or not new_launch_id:
            return None
        recovery = dict(existing.get("recovery") or {})
        if (
            int(existing.get("owner_pid") or 0) == new_pid
            and str(existing.get("process_epoch") or "") == new_epoch
            and str(recovery.get("launch_id") or "") == new_launch_id
        ):
            return state
        now = _bounded_run_state_lease(
            claimed_at if claimed_at is not None else self._now()
        )
        try:
            from gateway.status import get_process_start_time

            owner_started_at = int(get_process_start_time(new_pid) or 0)
        except Exception:
            owner_started_at = 0
        recovery.update(
            {
                "status": "claimed",
                "claimed_at": now,
                "heartbeat_at": now,
                "launch_id": new_launch_id,
                "launch_generation": _positive_int(
                    recovery.get("launch_generation")
                )
                + 1,
                "owner_started_at": owner_started_at,
                "last_error": "",
                "worker_pid": 0,
                "worker_started_at": 0,
                "worker_scope_unit": "",
                "turn_id": "",
            }
        )
        normalized_recovery, malformed = _bounded_required_async_recovery(recovery)
        if malformed:
            return None
        dispatch = dict(existing)
        dispatch.update(
            {
                "state": "registered",
                "status": "recovery_claimed",
                "started_at": None,
                "owner_pid": new_pid,
                "process_epoch": new_epoch,
                "recovery": normalized_recovery,
            }
        )
        dispatches[completion_id] = dispatch
        state["dispatches"] = dispatches
        return self._persist_required_async_state(data, item, state)

    @_locked_ledger_mutation
    def mark_required_async_dispatch_outcome_unknown(
        self,
        work_id: str,
        *,
        delegation_id: str,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        expected_owner_pid: int | None = None,
        expected_process_epoch: str | None = None,
        reason: str,
        recovery_status: str = "manual_fallback",
        completed_at: float | None = None,
    ) -> dict[str, Any] | None:
        """Fail one unsafe/unsupported child closed with visible recovery detail."""

        data = self._read()
        context = self._required_async_write_context(
            data,
            work_id,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        )
        if context is None:
            return None
        item, state = context
        completion_id = _bounded_run_state_text(delegation_id)
        dispatches = dict(state["dispatches"])
        existing = dispatches.get(completion_id)
        if (
            not isinstance(existing, dict)
            or existing.get("state") not in _REQUIRED_ASYNC_PENDING_STATES
        ):
            return None
        expected_pid = _bounded_run_state_int(expected_owner_pid)
        expected_epoch = _bounded_run_state_text(
            expected_process_epoch,
            limit=_RUN_STATE_EPOCH_LIMIT,
        )
        if expected_owner_pid is not None and int(existing.get("owner_pid") or 0) != expected_pid:
            return None
        if expected_process_epoch is not None and str(existing.get("process_epoch") or "") != expected_epoch:
            return None
        now = _bounded_run_state_lease(
            completed_at if completed_at is not None else self._now()
        )
        recovery = dict(existing.get("recovery") or {})
        recovery.update(
            {
                "status": recovery_status,
                "heartbeat_at": now,
                "last_error": reason,
            }
        )
        normalized_recovery, _ = _bounded_required_async_recovery(recovery)
        dispatch = dict(existing)
        dispatch.update(
            {
                "state": "outcome_unknown",
                "success": False,
                "status": "recovery_manual_fallback",
                "completed_at": now,
                "error": _bounded_required_async_text(reason),
                "recovery": normalized_recovery,
            }
        )
        dispatches[completion_id] = dispatch
        state.update(
            {
                "dispatches": dispatches,
                "sticky_failure": True,
                "failure_reason": "required_async_outcome_unknown",
            }
        )
        if not any(
            row.get("state") in _REQUIRED_ASYNC_PENDING_STATES
            for row in dispatches.values()
        ):
            state["sealed"] = True
            state["sealed_at"] = state.get("sealed_at") or now
        return self._persist_required_async_state(
            data,
            item,
            state,
            refresh_gate=True,
        )

    def _record_required_async_terminal_locked(
        self,
        item: dict[str, Any],
        *,
        delegation_id: str,
        success: bool,
        generation: int | None,
        attempt_id: str | None,
        attempt_order: int | None,
        owner_pid: int | None,
        process_epoch: str | None,
        status: str | None,
        completed_at: float | None,
        closeout_id: str | None,
        summary: str | None,
        error: str | None,
        evidence: Mapping[str, Any] | None,
        allow_missing_registration: bool,
    ) -> dict[str, Any] | None:
        state = _required_async_completion_state(item)
        if not _required_async_attempt_matches(
            state,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        ):
            return None
        completion_id = _bounded_run_state_text(delegation_id)
        if not completion_id:
            return None
        dispatches = dict(state["dispatches"])
        existing = dispatches.get(completion_id)
        if not isinstance(existing, dict):
            if not allow_missing_registration or state["sealed"]:
                return None
            # Compatibility for callers from before durable dispatch
            # registration. New producer code always registers first.
            existing = {
                "kind": "coding_worker",
                "required": True,
                "state": "registered",
                "success": None,
                "status": "registered",
                "registered_at": _bounded_run_state_lease(completed_at),
                "started_at": None,
                "completed_at": None,
                "owner_pid": 0,
                "process_epoch": "",
                "closeout_id": "",
                "summary": "",
                "error": "",
                "evidence": {},
                "recovery": {},
                "registration_missing": True,
            }
        elif (
            owner_pid is not None
            and int(existing.get("owner_pid") or 0)
            != _bounded_run_state_int(owner_pid)
        ) or (
            process_epoch is not None
            and str(existing.get("process_epoch") or "")
            != _bounded_run_state_text(process_epoch, limit=_RUN_STATE_EPOCH_LIMIT)
        ):
            return None
        incoming_status = _bounded_run_state_text(status)
        incoming_success = bool(success)
        if existing.get("state") in {"cancelled", "outcome_unknown"}:
            return state
        if existing.get("state") == "terminal":
            prior_success = existing.get("success") is True
            prior_status = str(existing.get("status") or "")
            conflict = prior_success != incoming_success or bool(
                prior_status and incoming_status and prior_status != incoming_status
            )
            if not conflict:
                return state
            dispatch = dict(existing)
            dispatch.update(
                {
                    "success": False,
                    "status": "conflicting_replay",
                    "error": "conflicting terminal replay for the same delegation id",
                    "conflicting_replay": True,
                }
            )
            dispatches[completion_id] = dispatch
            if existing.get("required") is True:
                state["sticky_failure"] = True
                state["failure_reason"] = "required_async_conflicting_replay"
        else:
            dispatch = dict(existing)
            combined_evidence = dict(dispatch.get("evidence") or {})
            combined_evidence.update(_bounded_required_async_evidence(evidence))
            recovery = dict(dispatch.get("recovery") or {})
            if recovery:
                recovery.update(
                    {
                        "status": "recovered" if incoming_success else "failed",
                        "heartbeat_at": _bounded_run_state_lease(
                            completed_at if completed_at is not None else self._now()
                        ),
                        "last_error": error or "",
                    }
                )
                recovery, _ = _bounded_required_async_recovery(recovery)
            closeout = _bounded_run_state_text(closeout_id) or str(
                dispatch.get("closeout_id") or ""
            )
            dispatch.update(
                {
                    "state": "terminal",
                    "success": incoming_success,
                    "status": incoming_status,
                    "completed_at": _bounded_run_state_lease(
                        completed_at if completed_at is not None else self._now()
                    ),
                    "closeout_id": closeout,
                    "summary": _bounded_required_async_text(summary),
                    "error": _bounded_required_async_text(error),
                    "evidence": combined_evidence,
                    "recovery": recovery,
                }
            )
            dispatches[completion_id] = dispatch
            if not incoming_success and dispatch.get("required") is True:
                state["sticky_failure"] = True
                state["failure_reason"] = "required_async_completion_failed"
        state["dispatches"] = dispatches
        item["required_async_completions"] = _required_async_payload(state)
        _refresh_required_async_failure_gate(item)
        return _required_async_completion_state(item)

    @_locked_ledger_mutation
    def record_required_async_submit_failure(
        self,
        work_id: str,
        *,
        delegation_id: str,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        owner_pid: int | None = None,
        process_epoch: str | None = None,
        error: str | None = None,
        status: str | None = "submit_failed",
        completed_at: float | None = None,
        closeout_id: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return None
        result = self._record_required_async_terminal_locked(
            item,
            delegation_id=delegation_id,
            success=False,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
            owner_pid=owner_pid,
            process_epoch=process_epoch,
            status=status,
            completed_at=completed_at,
            closeout_id=closeout_id,
            summary=None,
            error=error,
            evidence=evidence,
            allow_missing_registration=False,
        )
        if result is not None:
            item["updated_at"] = self._now()
            self._write(data)
        return result

    @_locked_ledger_mutation
    def record_required_async_completion(
        self,
        work_id: str,
        *,
        delegation_id: str,
        success: bool,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        owner_pid: int | None = None,
        process_epoch: str | None = None,
        status: str | None = None,
        completed_at: float | None = None,
        closeout_id: str | None = None,
        summary: str | None = None,
        error: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Latch one registered coding outcome on its exact attempt."""
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return None
        result = self._record_required_async_terminal_locked(
            item,
            delegation_id=delegation_id,
            success=success,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
            owner_pid=owner_pid,
            process_epoch=process_epoch,
            status=status,
            completed_at=completed_at,
            closeout_id=closeout_id,
            summary=summary,
            error=error,
            evidence=evidence,
            allow_missing_registration=True,
        )
        if result is not None:
            item["updated_at"] = self._now()
            self._write(data)
        return result

    @_locked_ledger_mutation
    def cancel_required_async_attempt(
        self,
        work_id: str,
        *,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        reason: str | None = None,
        cancelled_at: float | None = None,
    ) -> dict[str, Any] | None:
        """CAS-fence one exact attempt without rewriting terminal dispatch evidence."""

        data = self._read()
        context = self._required_async_write_context(
            data,
            work_id,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        )
        if context is None:
            return None
        item, state = context
        if state.get("attempt_cancelled") is True:
            return state
        now = _bounded_run_state_lease(
            cancelled_at if cancelled_at is not None else self._now()
        )
        state.update(
            {
                "attempt_cancelled": True,
                "attempt_cancelled_at": now,
                "attempt_cancellation_reason": _bounded_required_async_text(reason),
                "sticky_failure": True,
                "failure_reason": "required_async_attempt_cancelled",
            }
        )
        return self._persist_required_async_state(
            data,
            item,
            state,
            refresh_gate=True,
        )

    @_locked_ledger_mutation
    def cancel_required_async_dispatch(
        self,
        work_id: str,
        *,
        delegation_id: str,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        reason: str | None = None,
        status: str | None = "cancelled",
        cancelled_at: float | None = None,
    ) -> dict[str, Any] | None:
        data = self._read()
        context = self._required_async_write_context(
            data,
            work_id,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        )
        if context is None:
            return None
        item, state = context
        completion_id = _bounded_run_state_text(delegation_id)
        dispatches = dict(state["dispatches"])
        existing = dispatches.get(completion_id)
        if not isinstance(existing, dict):
            return None
        if existing.get("state") == "cancelled":
            return state
        if existing.get("state") not in _REQUIRED_ASYNC_PENDING_STATES:
            return None
        now = _bounded_run_state_lease(
            cancelled_at if cancelled_at is not None else self._now()
        )
        dispatch = dict(existing)
        dispatch.update(
            {
                "state": "cancelled",
                "success": False,
                "status": _bounded_run_state_text(status) or "cancelled",
                "completed_at": now,
                "error": _bounded_required_async_text(reason),
            }
        )
        dispatches[completion_id] = dispatch
        state.update(
            {
                "dispatches": dispatches,
                "cancelled_at": now,
                "cancellation_reason": _bounded_required_async_text(reason),
            }
        )
        if dispatch.get("required") is True:
            state["sticky_failure"] = True
            state["failure_reason"] = "required_async_dispatch_cancelled"
        return self._persist_required_async_state(
            data,
            item,
            state,
            refresh_gate=True,
        )

    @_locked_ledger_mutation
    def seal_required_async_attempt(
        self,
        work_id: str,
        *,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        sealed_at: float | None = None,
    ) -> dict[str, Any] | None:
        data = self._read()
        context = self._required_async_write_context(
            data,
            work_id,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        )
        if context is None:
            return None
        item, state = context
        if state["sealed"]:
            return state
        state["sealed"] = True
        state["sealed_at"] = _bounded_run_state_lease(
            sealed_at if sealed_at is not None else self._now()
        )
        if not state["dispatches"]:
            state["sticky_failure"] = True
            state["failure_reason"] = "required_async_attempt_empty"
        return self._persist_required_async_state(
            data,
            item,
            state,
            refresh_gate=True,
        )

    @_locked_ledger_mutation
    def record_required_async_checkpoint(
        self,
        work_id: str,
        *,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        parent_sha: str,
        tree_sha: str,
        message: str,
        repository_root: str,
        workspace_path: str,
        committed_head_sha: str | None = None,
    ) -> dict[str, Any] | None:
        """Persist or complete the exact trusted checkpoint identity idempotently."""

        data = self._read()
        context = self._required_async_write_context(
            data,
            work_id,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        )
        if context is None:
            return None
        item, state = context
        if (
            not state["sealed"]
            or not state["completion_terminal"]
            or state.get("attempt_cancelled") is True
        ):
            return None
        incoming, malformed = _bounded_required_async_checkpoint(
            {
                "parent_sha": parent_sha,
                "tree_sha": tree_sha,
                "message": message,
                "repository_root": repository_root,
                "workspace_path": workspace_path,
                "committed_head_sha": committed_head_sha,
            }
        )
        if malformed or incoming is None:
            return None
        existing = state.get("checkpoint")
        if isinstance(existing, dict):
            identity_keys = (
                "parent_sha",
                "tree_sha",
                "message",
                "repository_root",
                "workspace_path",
            )
            if any(existing.get(key) != incoming.get(key) for key in identity_keys):
                return None
            existing_head = str(existing.get("committed_head_sha") or "")
            incoming_head = str(incoming.get("committed_head_sha") or "")
            if existing_head and incoming_head and existing_head != incoming_head:
                return None
            if existing_head and not incoming_head:
                incoming["committed_head_sha"] = existing_head
            if existing == incoming:
                return state
        state["checkpoint"] = incoming
        return self._persist_required_async_state(data, item, state)

    @_locked_ledger_mutation
    def mark_required_async_reconciled(
        self,
        work_id: str,
        *,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        reconciliation_id: str | None = None,
        reconciled_at: float | None = None,
    ) -> dict[str, Any] | None:
        data = self._read()
        context = self._required_async_write_context(
            data,
            work_id,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        )
        if context is None:
            return None
        item, state = context
        if (
            not state["sealed"]
            or not state["completion_terminal"]
            or state.get("attempt_cancelled") is True
        ):
            return None
        incoming_id = _bounded_run_state_text(reconciliation_id)
        if state["reconciled_at"] is not None:
            if incoming_id and incoming_id != str(state.get("reconciliation_id") or ""):
                return None
            return state
        state["reconciliation_id"] = incoming_id
        state["reconciled_at"] = _bounded_run_state_lease(
            reconciled_at if reconciled_at is not None else self._now()
        )
        return self._persist_required_async_state(data, item, state)

    @_locked_ledger_mutation
    def mark_orphaned_required_async_dispatches_unknown(
        self,
        *,
        current_process_epoch: str,
        current_owner_pid: int | None = None,
        completed_at: float | None = None,
    ) -> list[str]:
        """Fail closed dispatches owned by a process other than this gateway."""

        epoch = _bounded_run_state_text(
            current_process_epoch,
            limit=_RUN_STATE_EPOCH_LIMIT,
        )
        if not epoch:
            return []
        owner_pid = _bounded_run_state_int(current_owner_pid or os.getpid())
        now = _bounded_run_state_lease(
            completed_at if completed_at is not None else self._now()
        )
        data = self._read()
        changed_work_ids: list[str] = []
        for work_id, item in data.get("items", {}).items():
            if not isinstance(item, dict):
                continue
            state = _required_async_completion_state(item)
            if state["malformed"]:
                continue
            dispatches = dict(state["dispatches"])
            changed = False
            for delegation_id, existing in list(dispatches.items()):
                if existing.get("state") not in _REQUIRED_ASYNC_PENDING_STATES:
                    continue
                dispatch_epoch = str(existing.get("process_epoch") or "")
                dispatch_pid = int(existing.get("owner_pid") or 0)
                if dispatch_epoch == epoch and dispatch_pid == owner_pid:
                    continue
                dispatch = dict(existing)
                dispatch.update(
                    {
                        "state": "outcome_unknown",
                        "success": False,
                        "status": "producer_process_lost",
                        "completed_at": now,
                        "error": "producer process exited before a durable terminal outcome",
                    }
                )
                dispatches[delegation_id] = dispatch
                changed = True
            if not changed:
                continue
            state["dispatches"] = dispatches
            if any(
                dispatch.get("required") is True
                and dispatch.get("state") == "outcome_unknown"
                for dispatch in dispatches.values()
            ):
                state["sticky_failure"] = True
                state["failure_reason"] = "required_async_outcome_unknown"
            if not any(
                dispatch.get("state") in _REQUIRED_ASYNC_PENDING_STATES
                for dispatch in dispatches.values()
            ):
                state["sealed"] = True
                state["sealed_at"] = state.get("sealed_at") or now
            item["required_async_completions"] = _required_async_payload(state)
            _refresh_required_async_failure_gate(item)
            item["updated_at"] = self._now()
            changed_work_ids.append(str(work_id))
        if changed_work_ids:
            self._write(data)
        return changed_work_ids

    @_locked_ledger_mutation
    def mark_orphaned_advisory_async_dispatch_terminal(
        self,
        work_id: str,
        *,
        delegation_id: str,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        expected_owner_pid: int | None = None,
        expected_process_epoch: str | None = None,
        reason: str = (
            "advisory producer exited before recording a durable terminal outcome"
        ),
        completed_at: float | None = None,
    ) -> dict[str, Any] | None:
        """Terminalize one orphaned advisory without failing required work.

        The caller must first prove that the owning process is gone. Exact
        attempt and producer identity fencing prevents a stale startup scan
        from overwriting a live or replacement dispatch.
        """

        data = self._read()
        context = self._required_async_write_context(
            data,
            work_id,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        )
        if context is None:
            return None
        item, state = context
        completion_id = _bounded_run_state_text(delegation_id)
        dispatches = dict(state["dispatches"])
        existing = dispatches.get(completion_id)
        if not isinstance(existing, dict):
            return None
        if (
            existing.get("kind") != "advisory"
            or existing.get("required") is True
        ):
            return None
        if (
            existing.get("state") == "terminal"
            and existing.get("status") == "producer_process_lost"
        ):
            return state
        if existing.get("state") not in _REQUIRED_ASYNC_PENDING_STATES:
            return None
        if (
            expected_owner_pid is not None
            and int(existing.get("owner_pid") or 0)
            != _bounded_run_state_int(expected_owner_pid)
        ):
            return None
        if (
            expected_process_epoch is not None
            and str(existing.get("process_epoch") or "")
            != _bounded_run_state_text(
                expected_process_epoch,
                limit=_RUN_STATE_EPOCH_LIMIT,
            )
        ):
            return None

        now = _bounded_run_state_lease(
            completed_at if completed_at is not None else self._now()
        )
        error = _bounded_required_async_text(reason)
        evidence = dict(existing.get("evidence") or {})
        prior_results = evidence.get("advisory_results")
        advisory_results: list[dict[str, str]] = []
        if isinstance(prior_results, list):
            for raw_result in prior_results[:_MAX_ASYNC_ADVISORY_RESULTS]:
                if not isinstance(raw_result, Mapping):
                    continue
                result: dict[str, str] = {}
                goal = _bounded_required_async_text(raw_result.get("goal"))
                if goal:
                    result["goal"] = goal
                result["status"] = "error"
                result["error"] = error
                advisory_results.append(result)
        if not advisory_results:
            advisory_results.append({"status": "error", "error": error})
        evidence["advisory_results"] = advisory_results

        dispatch = dict(existing)
        dispatch.update(
            {
                "state": "terminal",
                "success": False,
                "status": "producer_process_lost",
                "completed_at": now,
                "summary": "",
                "error": error,
                "evidence": _bounded_required_async_evidence(evidence),
            }
        )
        dispatches[completion_id] = dispatch
        state["dispatches"] = dispatches
        # The parent producer is gone, so registration for this attempt is
        # complete even when required coding children still need recovery.
        # Sealing now lets the normal reconciler finish once those children
        # become terminal, without replaying the parent model turn.
        state["sealed"] = True
        state["sealed_at"] = state.get("sealed_at") or now
        return self._persist_required_async_state(
            data,
            item,
            state,
            refresh_gate=True,
        )

    @_locked_ledger_mutation
    def finalize_required_async_failure(
        self,
        work_id: str,
        *,
        generation: int | None = None,
        attempt_id: str | None = None,
        attempt_order: int | None = None,
        final_response: str,
        reason: str = "required_async_completion_failed",
        reconciliation_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Atomically fence a deterministic async block and terminal delivery."""

        data = self._read()
        context = self._required_async_write_context(
            data,
            work_id,
            generation=generation,
            attempt_id=attempt_id,
            attempt_order=attempt_order,
        )
        if context is None:
            return None
        item, state = context
        if not state["sealed"] or not state["completion_terminal"]:
            return None
        terminal_delivery = (
            item.get("terminal_delivery")
            if isinstance(item.get("terminal_delivery"), dict)
            else None
        )
        if terminal_delivery is not None:
            if (
                str(terminal_delivery.get("source") or "")
                == "required_async_completion"
                and str(terminal_delivery.get("async_attempt_id") or "")
                == str(state["attempt_id"] or "")
                and _positive_int(terminal_delivery.get("async_generation"))
                == int(state["generation"] or 0)
                and _positive_int(terminal_delivery.get("async_attempt_order"))
                == int(state["attempt_order"] or 0)
            ):
                return dict(item)
            return None
        now = self._now()
        block_reason = _bounded_run_state_text(reason) or "required_async_completion_failed"
        state["reconciliation_id"] = _bounded_run_state_text(reconciliation_id)
        state["reconciled_at"] = now
        item["required_async_completions"] = _required_async_payload(state)
        item.update(
            {
                "status": "blocked",
                "updated_at": now,
                "agent_done_at": now,
                "blocked_at": now,
                "blocked_reason": block_reason,
                "final_response": str(final_response or ""),
                "summary_status": "Blocked",
                "active_run": None,
                "lease_until": None,
                "completion_gate": {
                    "allowed_to_complete": False,
                    "summary_status": "Blocked",
                    "terminal_status": "blocked",
                    "reason": block_reason,
                    "required_async_completions": {
                        "generation": state["generation"],
                        "attempt_id": state["attempt_id"],
                        "attempt_order": state["attempt_order"],
                        "failed": state["failed"],
                        "succeeded": state["required_succeeded"],
                    },
                },
                "terminal_delivery": {
                    "source": "required_async_completion",
                    "async_generation": state["generation"],
                    "async_attempt_id": state["attempt_id"],
                    "async_attempt_order": state["attempt_order"],
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
                },
            }
        )
        item.pop("claim_pid", None)
        _record_discord_board_final_response(item)
        _record_provider_progress(item, "ledger_status_blocked", status="blocked")
        self._write(data)
        return dict(item)

    @staticmethod
    def run_state_snapshot(item: Any) -> dict[str, Any]:
        """Normalize one observed item into the bounded finalization CAS state."""

        return _normalize_run_state_snapshot(item)

    def capture_run_state(
        self,
        work_id: str,
        *,
        session_key: str,
        run_generation: int,
        owner_pid: int,
        process_epoch: str,
    ) -> dict[str, Any] | None:
        """Capture the exact active run only when the requested owner still holds it."""

        with _ledger_file_lock(self.path):
            item = self._read().get("items", {}).get(work_id)
            if not isinstance(item, dict):
                return None
            snapshot = _normalize_run_state_snapshot(item)
            active_run = snapshot.get("active_run")
            if not isinstance(active_run, dict):
                return None
            if (
                active_run["session_key"] != _bounded_run_state_text(session_key)
                or active_run["generation"] != _bounded_run_state_int(run_generation)
                or active_run["owner_pid"] != _bounded_run_state_int(owner_pid)
                or active_run["process_epoch"]
                != _bounded_run_state_text(process_epoch, limit=_RUN_STATE_EPOCH_LIMIT)
            ):
                return None
            return snapshot

    def attach_closeout_workspace(
        self,
        work_id: str,
        *,
        workspace_path: str,
        canonical_path: str = "",
        repository: str = "",
        branch: str = "",
        base_branch: str = "main",
        closeout_id: str = "",
        source: str = "direct",
        mode: str = "shadow",
        policy: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Atomically attach mutable/canonical workspace identity to one item."""

        from hermes_cli.trusted_closeout import normalize_closeout_state

        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if not isinstance(item, dict):
                return None
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                return None
            existing = item.get("closeout") if isinstance(item.get("closeout"), dict) else {}
            existing_policy = (
                existing.get("policy") if isinstance(existing.get("policy"), dict) else None
            )
            if isinstance(policy, dict):
                resolved_policy = dict(policy)
            elif policy is None:
                resolved_policy = dict(existing_policy or {})
            else:
                resolved_policy = policy
            normalized_source = str(
                source or existing.get("source") or "direct"
            ).strip().lower()
            state = normalize_closeout_state(
                {
                    **existing,
                    "id": str(existing.get("id") or closeout_id or f"{work_id}:closeout"),
                    "source": normalized_source,
                    "mode": str(mode or existing.get("mode") or "off"),
                    "workspace": {
                        "path": str(workspace_path or ""),
                        "canonical_path": str(canonical_path or ""),
                        "repository": str(repository or ""),
                        "branch": str(branch or ""),
                        "base_branch": str(base_branch or "main"),
                    },
                    "policy": resolved_policy,
                }
            )
            state["revision"] = int(existing.get("revision") or 0) + 1
            item["closeout"] = state
            if state["mode"] != "enforce":
                item["closeout_authoritative"] = False
            else:
                item.setdefault("closeout_authoritative", False)
            item["updated_at"] = self._now()
            self._write(data)
            return dict(state)

    def activate_closeout(
        self,
        work_id: str,
        closeout_state: dict[str, Any],
        *,
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        """Atomically activate closeout; only enforce mode becomes authoritative."""

        from agent.runtime_spans import RuntimeSpanRecorder, sanitize_runtime_spans
        from hermes_cli.trusted_closeout import normalize_closeout_state

        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if not isinstance(item, dict):
                return None
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                return None
            existing = item.get("closeout") if isinstance(item.get("closeout"), dict) else {}
            revision = int(existing.get("revision") or 0)
            if expected_revision is not None and revision != int(expected_revision):
                return None
            state = normalize_closeout_state(closeout_state)
            if state["mode"] == "off":
                return None
            recorder = RuntimeSpanRecorder(work_id=work_id, max_spans=4)
            handoff_span = recorder.start(
                "closeout_handoff",
                phase="gateway_handoff",
                attempt_id=f"revision-{revision + 1}",
                metadata={"operation": "closeout_handoff", "surface": "gateway"},
            )
            recorder.finish(
                handoff_span,
                status="ok",
                metadata={
                    "source": state.get("source") or "direct",
                    "mode": state.get("mode") or "off",
                },
            )
            telemetry = state.get("telemetry") if isinstance(state.get("telemetry"), dict) else {}
            telemetry["phase_spans"] = sanitize_runtime_spans(
                [*(telemetry.get("phase_spans") or []), *recorder.export()],
                max_spans=120,
            )
            state["telemetry"] = telemetry
            state["revision"] = revision + 1
            item["closeout"] = state
            item.pop("closeout_visual_completion", None)
            item["closeout_authoritative"] = state["mode"] == "enforce"
            item["closeout_activated_at"] = self._now()
            item["updated_at"] = item["closeout_activated_at"]
            self._write(data)
            return dict(state)

    def publish_closeout_verified_head(
        self,
        work_id: str,
        *,
        expected_head_sha: str,
        verified_head_sha: str,
    ) -> dict[str, Any] | None:
        """Advance an active direct/Fable closeout from exact head H to verified H2.

        The revision and lease generation fences invalidate any watcher result
        still reconciling H. Head-bound review, visual, CI, readiness, merge,
        and post-merge evidence is reset before H2 can be published.
        """

        from hermes_cli.trusted_closeout import normalize_closeout_state

        expected_head = str(expected_head_sha or "").strip().lower()
        verified_head = str(verified_head_sha or "").strip().lower()
        if not (
            _CLOSEOUT_SHA_RE.fullmatch(expected_head)
            and _CLOSEOUT_SHA_RE.fullmatch(verified_head)
        ):
            return None
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("closeout"), dict)
                or (
                    item.get("closeout_authoritative") is not True
                    and item.get("closeout_activated_at") is None
                )
            ):
                return None
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                return None
            state = normalize_closeout_state(item["closeout"])
            if state["mode"] == "off" or state["source"] not in {"direct", "fable", "opus"}:
                return None
            if str(state["pr"].get("head_sha") or "").strip().lower() != expected_head:
                return None
            if expected_head == verified_head:
                return dict(state)

            now = self._now()
            state["status"] = "pending"
            state["local_verification"] = {
                "status": "passed",
                "head_sha": verified_head,
            }
            state["review"] = (
                {"status": "stale"}
                if state["policy"]["require_review"]
                else {"status": "not_required"}
            )
            state["visual_qa"] = (
                {"status": "pending", "head_sha": verified_head}
                if state["policy"]["require_visual_qa"]
                else {"status": "not_required"}
            )
            state["ci"] = {
                "head_sha": verified_head,
                "status": "not_checked",
                "total": 0,
                "failed": [],
                "wait_state": "queued",
                "required": list(state["ci"].get("required") or []),
            }
            state["pr"].update(
                {
                    "head_sha": verified_head,
                    "merge_sha": "",
                    "merge_state": "UNKNOWN",
                    "mergeable": "unknown",
                    "review_decision": "UNKNOWN",
                    "ready_at": None,
                    "merge_attempted_head_sha": "",
                    "pending_push_head_sha": verified_head,
                }
            )
            state["canonical_sync"] = {"status": "not_started"}
            state["post_merge"] = {
                "target_sha": "",
                "canonical_sync": {"status": "not_started"},
                "ci": {"status": "not_started"},
                "deployment": {"status": "not_configured"},
                "production_qa": {"status": "not_configured"},
                "restart": {"status": "not_configured"},
            }
            state["mutation_uncertainty"] = {}
            state["telemetry"]["green_unmerged_since"] = None
            state["telemetry"]["green_unmerged_overdue"] = False
            state["lease_generation"] = min(
                2_147_483_647,
                int(state.get("lease_generation") or 0) + 1,
            )
            state["lease"] = {"owner": "", "until": None}
            state["next_due_at"] = now
            state["revision"] = min(
                2_147_483_647,
                int(state.get("revision") or 0) + 1,
            )
            item["closeout"] = state
            _sync_closeout_preview_delivery(item, state, now=now)
            item.pop("closeout_visual_completion", None)
            item["updated_at"] = now
            self._write(data)
            return dict(state)

    def apply_closeout_visual_completion(
        self,
        work_id: str,
        *,
        expected_head_sha: str,
        receipts: Any,
        min_receipt_order: int = 0,
    ) -> dict[str, Any] | None:
        """Apply one sanitized visual result while the latest closeout expects H.

        An active watcher lease keeps its revision. The sanitized result waits
        beside the leased state and is merged atomically when that revision is
        released; an H2 reconciliation discards a late H receipt instead.
        """

        from hermes_cli.trusted_closeout import normalize_closeout_state

        expected_head = str(expected_head_sha or "").strip().lower()
        if not _CLOSEOUT_SHA_RE.fullmatch(expected_head):
            return None
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if not isinstance(item, dict) or not isinstance(item.get("closeout"), dict):
                return None
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                return None
            state = normalize_closeout_state(item["closeout"])
            if not _closeout_expects_head(state, expected_head):
                return None
            visual_config, visual_requirement = _visual_qa_state_for_item(item)
            if visual_requirement["level"] == "none":
                return None
            safe_receipts = _visual_qa_receipts(
                receipts,
                visual_requirement,
                limit=visual_config["max_receipts_per_turn"],
            )
            completion = visual_receipt_completion(
                visual_requirement,
                safe_receipts,
                min_order=_positive_int(min_receipt_order),
            )
            status = str(completion.get("status") or "missing").strip().lower()
            if status not in _CLOSEOUT_VISUAL_STATUSES:
                status = "missing"
            visual = {"status": status, "head_sha": expected_head}
            now = self._now()
            lease = state.get("lease") if isinstance(state.get("lease"), dict) else {}
            if str(lease.get("owner") or "") and float(lease.get("until") or 0) > now:
                item["closeout_visual_completion"] = visual
                item["updated_at"] = now
                self._write(data)
                return dict(state)
            if state.get("visual_qa") == visual:
                if item.pop("closeout_visual_completion", None) is not None:
                    item["updated_at"] = now
                    self._write(data)
                return dict(state)
            state["visual_qa"] = visual
            state["next_due_at"] = now
            state["revision"] = min(
                2_147_483_647,
                int(state.get("revision") or 0) + 1,
            )
            item["closeout"] = state
            item.pop("closeout_visual_completion", None)
            item["updated_at"] = now
            self._write(data)
            return dict(state)

    def update_closeout(
        self,
        work_id: str,
        closeout_state: dict[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any] | None:
        """Compare-and-swap one normalized closeout state."""

        from hermes_cli.trusted_closeout import normalize_closeout_state

        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if not isinstance(item, dict):
                return None
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                return None
            existing = item.get("closeout") if isinstance(item.get("closeout"), dict) else None
            if existing is None or int(existing.get("revision") or 0) != int(expected_revision):
                return None
            now = self._now()
            state = normalize_closeout_state(closeout_state)
            state["revision"] = int(expected_revision) + 1
            item["closeout"] = state
            _sync_closeout_preview_delivery(item, state, now=now)
            item.pop("closeout_mutation_uncertainty", None)
            item.pop("closeout_mutation_fence", None)
            if state["mode"] != "enforce":
                item["closeout_authoritative"] = False
            elif item.get("closeout_activated_at") is not None:
                item["closeout_authoritative"] = True
            item["updated_at"] = now
            self._write(data)
            return dict(state)

    def lease_closeout(
        self,
        work_id: str,
        *,
        owner: str,
        lease_seconds: float,
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        """Claim a due closeout lease without replaying model work."""

        from hermes_cli.trusted_closeout import normalize_closeout_state

        owner_text = str(owner or "").strip()
        if not owner_text:
            return None
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if not isinstance(item, dict) or not isinstance(item.get("closeout"), dict):
                return None
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                return None
            now = self._now()
            state = normalize_closeout_state(item["closeout"])
            pending_uncertainty = item.get("closeout_mutation_uncertainty")
            mutation_fence = item.get("closeout_mutation_fence")
            for candidate in (pending_uncertainty, mutation_fence):
                if not isinstance(candidate, dict):
                    continue
                normalized_uncertainty = normalize_closeout_state(
                    {"mutation_uncertainty": candidate}
                )["mutation_uncertainty"]
                if normalized_uncertainty.get("status") == "uncertain":
                    state["mutation_uncertainty"] = normalized_uncertainty
            revision = int(state.get("revision") or 0)
            if expected_revision is not None and revision != int(expected_revision):
                return None
            lease = state.get("lease") if isinstance(state.get("lease"), dict) else {}
            lease_until = float(lease.get("until") or 0)
            lease_owner = str(lease.get("owner") or "")
            if lease_until > now and lease_owner and lease_owner != owner_text:
                return None
            next_due = float(state.get("next_due_at") or 0)
            if next_due > now:
                return None
            _merge_pending_closeout_visual_completion(item, state)
            state["lease_generation"] = min(
                2_147_483_647,
                int(state.get("lease_generation") or 0) + 1,
            )
            state["lease"] = {
                "owner": owner_text[:160],
                "until": now + max(1.0, min(3600.0, float(lease_seconds))),
            }
            state["revision"] = revision + 1
            item["closeout"] = state
            item.pop("closeout_mutation_uncertainty", None)
            item.pop("closeout_mutation_fence", None)
            item["closeout_last_claimed_at"] = now
            item["updated_at"] = now
            self._write(data)
            result = dict(item)
            result["closeout"] = state
            return result

    def renew_closeout_lease(
        self,
        work_id: str,
        *,
        owner: str,
        lease_seconds: float,
        expected_revision: int,
        expected_generation: int,
    ) -> bool:
        """Extend an unexpired owned lease without changing logical revision."""

        from hermes_cli.trusted_closeout import normalize_closeout_state

        owner_text = str(owner or "").strip()
        if not owner_text:
            return False
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if not isinstance(item, dict) or not isinstance(item.get("closeout"), dict):
                return False
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                return False
            now = self._now()
            state = normalize_closeout_state(item["closeout"])
            if (
                int(state.get("revision") or 0) != int(expected_revision)
                or int(state.get("lease_generation") or 0)
                != int(expected_generation)
            ):
                return False
            lease = state.get("lease") if isinstance(state.get("lease"), dict) else {}
            if (
                str(lease.get("owner") or "") != owner_text
                or float(lease.get("until") or 0) <= now
            ):
                return False
            state["lease"] = {
                "owner": owner_text[:160],
                "until": now + max(1.0, min(3600.0, float(lease_seconds))),
            }
            item["closeout"] = state
            item["updated_at"] = now
            self._write(data)
            return True

    def record_closeout_mutation_start(
        self,
        work_id: str,
        *,
        owner: str,
        expected_revision: int,
        expected_generation: int,
        operation: str,
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        """Durably fence a remote mutation before its subprocess can start."""

        from hermes_cli.trusted_closeout import normalize_closeout_state

        raw_context = dict(context) if isinstance(context, Mapping) else {}
        marker = normalize_closeout_state(
            {
                "mutation_uncertainty": {
                    "status": "uncertain",
                    "operation": operation,
                    **raw_context,
                }
            }
        )["mutation_uncertainty"]
        if marker.get("status") != "uncertain":
            return False
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if not isinstance(item, dict) or not isinstance(item.get("closeout"), dict):
                return False
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                return False
            now = self._now()
            current = normalize_closeout_state(item["closeout"])
            lease = current.get("lease") if isinstance(current.get("lease"), dict) else {}
            if (
                int(current.get("revision") or 0) != int(expected_revision)
                or int(current.get("lease_generation") or 0)
                != int(expected_generation)
                or str(lease.get("owner") or "") != str(owner or "")
                or float(lease.get("until") or 0) <= now
            ):
                return False
            marker["at"] = float(marker.get("at") or now)
            item["closeout_mutation_fence"] = marker
            item["updated_at"] = now
            self._write(data)
            return True

    def record_closeout_mutation_uncertainty(
        self,
        work_id: str,
        *,
        owner: str,
        expected_revision: int,
        expected_generation: int,
        uncertainty: dict[str, Any],
    ) -> bool:
        """Persist a conservative non-authoritative marker after lease loss."""

        from hermes_cli.trusted_closeout import normalize_closeout_state

        marker = normalize_closeout_state(
            {"mutation_uncertainty": uncertainty}
        )["mutation_uncertainty"]
        if marker.get("status") != "uncertain":
            return False
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if not isinstance(item, dict) or not isinstance(item.get("closeout"), dict):
                return False
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                return False
            current = normalize_closeout_state(item["closeout"])
            lease = current.get("lease") if isinstance(current.get("lease"), dict) else {}
            if (
                int(current.get("revision") or 0) != int(expected_revision)
                or int(current.get("lease_generation") or 0)
                != int(expected_generation)
                or str(lease.get("owner") or "") != str(owner or "")
            ):
                return False
            item["closeout_mutation_uncertainty"] = marker
            item["updated_at"] = self._now()
            self._write(data)
            return True

    def release_closeout(
        self,
        work_id: str,
        *,
        owner: str,
        expected_revision: int,
        expected_generation: int,
        closeout_state: dict[str, Any] | None = None,
        expected_run_state: Any = _RUN_STATE_UNSET,
    ) -> dict[str, Any] | None:
        """Release an owned lease and optionally persist the reconciliation result."""

        from hermes_cli.trusted_closeout import normalize_closeout_state

        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if not isinstance(item, dict) or not isinstance(item.get("closeout"), dict):
                return None
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                return None
            if not _run_state_matches(item, expected_run_state):
                return None
            now = self._now()
            current = normalize_closeout_state(item["closeout"])
            if (
                int(current.get("revision") or 0) != int(expected_revision)
                or int(current.get("lease_generation") or 0)
                != int(expected_generation)
            ):
                return None
            lease = current.get("lease") if isinstance(current.get("lease"), dict) else {}
            if (
                str(lease.get("owner") or "") != str(owner or "")
                or float(lease.get("until") or 0) <= now
            ):
                return None
            state = normalize_closeout_state(closeout_state if closeout_state is not None else current)
            _merge_pending_closeout_visual_completion(item, state)
            state["lease_generation"] = int(expected_generation)
            state["lease"] = {"owner": "", "until": None}
            state["revision"] = int(expected_revision) + 1
            item["closeout"] = state
            _sync_closeout_preview_delivery(item, state, now=now)
            item.pop("closeout_mutation_uncertainty", None)
            item.pop("closeout_mutation_fence", None)
            item["closeout_authoritative"] = state["mode"] == "enforce"
            item["updated_at"] = now
            self._write(data)
            return dict(state)

    def finalize_blocked_closeout(
        self,
        work_id: str,
        *,
        owner: str,
        expected_revision: int,
        expected_generation: int,
        closeout_state: dict[str, Any],
        final_response: str,
        reason: str,
        expected_run_state: Any = _RUN_STATE_UNSET,
    ) -> dict[str, Any] | None:
        """CAS the leased closeout and blocked delivery record in one write."""

        from hermes_cli.trusted_closeout import normalize_closeout_state

        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if not isinstance(item, dict) or not isinstance(item.get("closeout"), dict):
                return None
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                return None
            if not _run_state_matches(item, expected_run_state):
                return None
            now = self._now()
            current = normalize_closeout_state(item["closeout"])
            if (
                int(current.get("revision") or 0) != int(expected_revision)
                or int(current.get("lease_generation") or 0)
                != int(expected_generation)
            ):
                return None
            lease = current.get("lease") if isinstance(current.get("lease"), dict) else {}
            if (
                str(lease.get("owner") or "") != str(owner or "")
                or float(lease.get("until") or 0) <= now
            ):
                return None
            state = normalize_closeout_state(closeout_state)
            _merge_pending_closeout_visual_completion(item, state)
            state["lease_generation"] = int(expected_generation)
            state["lease"] = {"owner": "", "until": None}
            state["revision"] = int(expected_revision) + 1
            response = str(final_response or "")
            _sync_closeout_preview_delivery(item, state, now=now)
            item.pop("closeout_mutation_uncertainty", None)
            item.pop("closeout_mutation_fence", None)
            item.update(
                {
                    "closeout": state,
                    "closeout_authoritative": state["mode"] == "enforce",
                    "status": "blocked",
                    "updated_at": now,
                    "agent_done_at": now,
                    "blocked_at": now,
                    "blocked_reason": str(reason or "trusted_closeout_repair_required"),
                    "final_response": response,
                    "summary_status": "Blocked",
                    "active_run": None,
                    "lease_until": None,
                    "completion_gate": {
                        "allowed_to_complete": False,
                        "summary_status": "Blocked",
                        "terminal_status": "blocked",
                        "reason": str(reason or "trusted_closeout_repair_required"),
                    },
                    "terminal_delivery": {
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
                    },
                }
            )
            item.pop("claim_pid", None)
            _record_discord_board_final_response(item)
            _record_provider_progress(item, "ledger_status_blocked", status="blocked")
            self._write(data)
            return dict(item)

    def finalize_successful_closeout(
        self,
        work_id: str,
        *,
        owner: str,
        expected_revision: int,
        expected_generation: int,
        closeout_state: dict[str, Any],
        final_response: str,
        expected_run_state: Any = _RUN_STATE_UNSET,
    ) -> dict[str, Any] | None:
        """Atomically release terminal closeout authority and advance work state."""

        from hermes_cli.trusted_closeout import (
            closeout_terminal_eligible,
            normalize_closeout_state,
        )

        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if not isinstance(item, dict) or not isinstance(item.get("closeout"), dict):
                return None
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                return None
            if not _run_state_matches(item, expected_run_state):
                return None
            now = self._now()
            current = normalize_closeout_state(item["closeout"])
            if (
                int(current.get("revision") or 0) != int(expected_revision)
                or int(current.get("lease_generation") or 0)
                != int(expected_generation)
            ):
                return None
            lease = current.get("lease") if isinstance(current.get("lease"), dict) else {}
            if (
                str(lease.get("owner") or "") != str(owner or "")
                or float(lease.get("until") or 0) <= now
            ):
                return None
            state = normalize_closeout_state(closeout_state)
            _merge_pending_closeout_visual_completion(item, state)
            state["lease_generation"] = int(expected_generation)
            state["lease"] = {"owner": "", "until": None}
            state["revision"] = int(expected_revision) + 1
            if not closeout_terminal_eligible(state):
                return None

            item["closeout"] = state
            _sync_closeout_preview_delivery(item, state, now=now)
            item["closeout_authoritative"] = state["mode"] == "enforce"
            item.pop("closeout_mutation_uncertainty", None)
            item.pop("closeout_mutation_fence", None)
            status = str(item.get("status") or "")
            if status in {"accepted", "claimed", "agent_running"}:
                item.update(
                    {
                        "status": "agent_done",
                        "updated_at": now,
                        "agent_done_at": now,
                        "final_response": str(final_response or ""),
                        "summary_status": "Complete",
                        "active_run": None,
                        "lease_until": None,
                    }
                )
                item.pop("claim_pid", None)
                gate = classify_delivery_completion(item)
                item["completion_gate"] = gate
                item["summary_status"] = str(
                    gate.get("summary_status") or "Complete"
                )
                _record_discord_board_final_response(item)
                _record_provider_progress(
                    item,
                    "ledger_status_agent_done",
                    status="agent_done",
                )
            elif not _refresh_successful_closeout_completion(item, now=now):
                item["updated_at"] = now
            self._write(data)
            return dict(item)

    def claim_terminal_delivery(
        self,
        work_id: str,
        *,
        owner: str,
        lease_seconds: float = 120.0,
    ) -> dict[str, Any] | None:
        """Claim terminal response or summary work without replaying an uncertain send."""

        owner_text = str(owner or "").strip()
        if not owner_text:
            return None
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            delivery = item.get("terminal_delivery") if isinstance(item, dict) else None
            if not isinstance(item, dict) or not isinstance(delivery, dict):
                return None
            required_state = _required_async_completion_state(item)
            if (
                required_state.get("attempt_cancelled") is True
                and str(delivery.get("source") or "") != "required_async_completion"
            ):
                return None
            now = self._now()
            status = str(delivery.get("status") or "")
            if status == "completed" or (
                status == "uncertain" and delivery.get("summary_updated_at") is not None
            ):
                return None
            if (
                status in {"delivering", "sending", "uncertain"}
                and float(delivery.get("lease_until") or 0) > now
            ):
                return None
            next_delivery = dict(delivery)
            send_was_confirmed = bool(
                delivery.get("send_confirmed_at")
                or str(delivery.get("result_message_id") or "").strip()
                or _bounded_delivery_message_ids(delivery.get("confirmed_message_ids"))
            )
            if status == "sending" and not send_was_confirmed:
                next_delivery.update(
                    {
                        "status": "uncertain",
                        "uncertain_at": now,
                        "uncertain_reason": "send_attempt_outcome_unknown",
                        "operator_repair_required": True,
                    }
                )
                item["delivery_outcome"] = "uncertain"
            elif status != "uncertain":
                next_delivery["status"] = "delivering"
            next_delivery.update(
                {
                    "revision": _positive_int(delivery.get("revision")) + 1,
                    "owner": owner_text[:160],
                    "lease_until": now + max(1.0, min(3600.0, float(lease_seconds))),
                }
            )
            item["terminal_delivery"] = next_delivery
            item["updated_at"] = now
            self._write(data)
            return dict(item)

    def claim_preview_delivery(
        self,
        work_id: str,
        *,
        owner: str,
        lease_seconds: float = 120.0,
    ) -> dict[str, Any] | None:
        """Claim the one preview-ready Discord notification."""

        owner_text = str(owner or "").strip()
        if not owner_text:
            return None
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            delivery = item.get("preview_delivery") if isinstance(item, dict) else None
            if not isinstance(item, dict) or not isinstance(delivery, dict):
                return None
            now = self._now()
            status = str(delivery.get("status") or "")
            if status in {"completed", "uncertain", "cancelled"}:
                return None
            if status == "pending" and float(delivery.get("next_attempt_at") or 0) > now:
                return None
            if not _preview_delivery_matches_closeout(item, delivery):
                item["preview_delivery"] = {
                    **delivery,
                    "status": "cancelled",
                    "owner": "",
                    "lease_until": None,
                    "cancelled_at": now,
                    "cancelled_reason": "preview_identity_stale",
                }
                item["updated_at"] = now
                self._write(data)
                return None
            if status in {"delivering", "sending"} and float(delivery.get("lease_until") or 0) > now:
                return None
            if status == "sending":
                next_delivery = dict(delivery)
                next_delivery.update(
                    {
                        "status": "uncertain",
                        "owner": "",
                        "lease_until": None,
                        "uncertain_at": now,
                        "uncertain_reason": "send_attempt_outcome_unknown",
                    }
                )
                item["preview_delivery"] = next_delivery
                item["updated_at"] = now
                self._write(data)
                return None
            next_delivery = dict(delivery)
            next_delivery.update(
                {
                    "status": "delivering",
                    "revision": _positive_int(delivery.get("revision")) + 1,
                    "owner": owner_text[:160],
                    "lease_until": now + max(1.0, min(3600.0, float(lease_seconds))),
                }
            )
            item["preview_delivery"] = next_delivery
            item["updated_at"] = now
            self._write(data)
            return dict(item)

    def begin_preview_send_attempt(self, work_id: str, *, owner: str) -> bool:
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            delivery = item.get("preview_delivery") if isinstance(item, dict) else None
            now = self._now()
            if (
                not isinstance(item, dict)
                or not isinstance(delivery, dict)
                or str(delivery.get("status") or "") != "delivering"
                or str(delivery.get("owner") or "") != str(owner or "")
                or float(delivery.get("lease_until") or 0) <= now
                or not _preview_delivery_matches_closeout(item, delivery)
            ):
                return False
            next_delivery = dict(delivery)
            next_delivery.update(
                {
                    "status": "sending",
                    "revision": _positive_int(delivery.get("revision")) + 1,
                    "attempt_count": _positive_int(delivery.get("attempt_count")) + 1,
                    "send_started_at": now,
                }
            )
            item["preview_delivery"] = next_delivery
            item["updated_at"] = now
            self._write(data)
            return True

    def complete_preview_delivery(
        self,
        work_id: str,
        *,
        owner: str,
        result_message_id: str | None = None,
        confirmed_message_ids: Any = None,
    ) -> bool:
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            delivery = item.get("preview_delivery") if isinstance(item, dict) else None
            if (
                not isinstance(item, dict)
                or not isinstance(delivery, dict)
                or str(delivery.get("status") or "") != "sending"
                or str(delivery.get("owner") or "") != str(owner or "")
            ):
                return False
            now = self._now()
            confirmed_ids = _bounded_delivery_message_ids(
                confirmed_message_ids,
                primary=result_message_id,
            )
            next_delivery = dict(delivery)
            next_delivery.update(
                {
                    "status": "completed",
                    "revision": _positive_int(delivery.get("revision")) + 1,
                    "owner": "",
                    "lease_until": None,
                    "send_confirmed_at": now,
                    "next_attempt_at": None,
                    "confirmed_message_ids": list(confirmed_ids),
                    "result_message_id": confirmed_ids[0] if confirmed_ids else None,
                }
            )
            item["preview_delivery"] = next_delivery
            item["updated_at"] = now
            self._write(data)
            return True

    def fail_preview_delivery(
        self,
        work_id: str,
        *,
        owner: str,
        uncertain: bool,
    ) -> bool:
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            delivery = item.get("preview_delivery") if isinstance(item, dict) else None
            if (
                not isinstance(item, dict)
                or not isinstance(delivery, dict)
                or str(delivery.get("status") or "") not in {"delivering", "sending"}
                or str(delivery.get("owner") or "") != str(owner or "")
            ):
                return False
            now = self._now()
            next_delivery = dict(delivery)
            next_delivery.update(
                {
                    "status": "uncertain" if uncertain else "pending",
                    "revision": _positive_int(delivery.get("revision")) + 1,
                    "owner": "",
                    "lease_until": None,
                }
            )
            if uncertain:
                next_delivery["uncertain_at"] = now
                next_delivery["uncertain_reason"] = "send_attempt_outcome_unknown"
                next_delivery["next_attempt_at"] = None
            else:
                retry_count = _positive_int(delivery.get("retry_count")) + 1
                next_delivery["retry_count"] = retry_count
                next_delivery["next_attempt_at"] = now + min(
                    300.0,
                    float(2 ** min(retry_count, 8)),
                )
            item["preview_delivery"] = next_delivery
            item["updated_at"] = now
            self._write(data)
            return True

    def begin_terminal_send_attempt(self, work_id: str, *, owner: str) -> bool:
        """Persist the ambiguous send window before invoking the platform side effect."""

        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            delivery = item.get("terminal_delivery") if isinstance(item, dict) else None
            now = self._now()
            if (
                not isinstance(item, dict)
                or not isinstance(delivery, dict)
                or str(delivery.get("status") or "") != "delivering"
                or str(delivery.get("owner") or "") != str(owner or "")
                or float(delivery.get("lease_until") or 0) <= now
                or delivery.get("send_confirmed_at") is not None
                or bool(str(delivery.get("result_message_id") or "").strip())
                or bool(_bounded_delivery_message_ids(delivery.get("confirmed_message_ids")))
            ):
                return False
            attempt_count = _positive_int(delivery.get("attempt_count")) + 1
            next_delivery = dict(delivery)
            next_delivery.update(
                {
                    "status": "sending",
                    "revision": _positive_int(delivery.get("revision")) + 1,
                    "attempt_count": attempt_count,
                    "attempt_id": f"send-{attempt_count}",
                    "send_started_at": now,
                    "send_confirmed_at": None,
                }
            )
            item["terminal_delivery"] = next_delivery
            item["updated_at"] = now
            self._write(data)
            return True

    def mark_terminal_response_delivered(
        self,
        work_id: str,
        *,
        owner: str,
        result_message_id: str | None = None,
        confirmed_message_ids: Any = None,
    ) -> bool:
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            delivery = item.get("terminal_delivery") if isinstance(item, dict) else None
            if (
                not isinstance(item, dict)
                or not isinstance(delivery, dict)
                or str(delivery.get("status") or "") != "sending"
                or str(delivery.get("owner") or "") != str(owner or "")
            ):
                return False
            now = self._now()
            next_delivery = dict(delivery)
            confirmed_ids = _bounded_delivery_message_ids(
                confirmed_message_ids,
                primary=result_message_id,
            )
            primary_ids = _bounded_delivery_message_ids((result_message_id,))
            message_id = primary_ids[0] if primary_ids else confirmed_ids[0] if confirmed_ids else ""
            next_delivery["confirmed_message_ids"] = list(confirmed_ids)
            item["confirmed_message_ids"] = list(confirmed_ids)
            if message_id:
                next_delivery["result_message_id"] = message_id
                item["result_message_id"] = message_id
            next_delivery.update(
                {
                    "status": "delivering",
                    "send_confirmed_at": now,
                    "revision": _positive_int(delivery.get("revision")) + 1,
                }
            )
            item["delivery_outcome"] = "delivered"
            item["terminal_delivery"] = next_delivery
            item["updated_at"] = now
            _record_discord_board_final_response(item, result_message_id=message_id or None)
            self._write(data)
            return True

    def mark_terminal_delivery_uncertain(
        self,
        work_id: str,
        *,
        owner: str,
        reason: str = "send_attempt_outcome_unknown",
        result_message_id: str | None = None,
        confirmed_message_ids: Any = None,
    ) -> bool:
        """Fail closed after an attempted send whose durable receipt is unavailable."""

        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            delivery = item.get("terminal_delivery") if isinstance(item, dict) else None
            if (
                not isinstance(item, dict)
                or not isinstance(delivery, dict)
                or str(delivery.get("status") or "") != "sending"
                or str(delivery.get("owner") or "") != str(owner or "")
            ):
                return False
            now = self._now()
            next_delivery = dict(delivery)
            confirmed_ids = _bounded_delivery_message_ids(
                confirmed_message_ids,
                primary=result_message_id,
            )
            if confirmed_ids:
                next_delivery["confirmed_message_ids"] = list(confirmed_ids)
                item["confirmed_message_ids"] = list(confirmed_ids)
                primary_ids = _bounded_delivery_message_ids((result_message_id,))
                message_id = primary_ids[0] if primary_ids else confirmed_ids[0]
                next_delivery["result_message_id"] = message_id
                item["result_message_id"] = message_id
            next_delivery.update(
                {
                    "status": "uncertain",
                    "revision": _positive_int(delivery.get("revision")) + 1,
                    "uncertain_at": now,
                    "uncertain_reason": str(reason or "send_attempt_outcome_unknown")[:120],
                    "operator_repair_required": True,
                }
            )
            item["terminal_delivery"] = next_delivery
            item["delivery_outcome"] = "uncertain"
            item["updated_at"] = now
            self._write(data)
            return True

    def mark_terminal_delivery_unreachable(
        self,
        work_id: str,
        *,
        owner: str,
        reason: str = "target_not_found",
    ) -> bool:
        """CAS-close terminal delivery when its exact destination is gone.

        Unlike an ambiguous timeout, Discord ``Unknown Channel`` proves that
        this send produced no side effect and that retrying the same thread is
        futile.  The blocked work item remains durable for operator diagnosis;
        only its response/summary delivery obligation is retired.
        """

        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            delivery = item.get("terminal_delivery") if isinstance(item, dict) else None
            if (
                not isinstance(item, dict)
                or not isinstance(delivery, dict)
                or str(delivery.get("status") or "") != "sending"
                or str(delivery.get("owner") or "") != str(owner or "")
            ):
                return False
            now = self._now()
            next_delivery = dict(delivery)
            next_delivery.update(
                {
                    "status": "completed",
                    "revision": _positive_int(delivery.get("revision")) + 1,
                    "owner": "",
                    "lease_until": None,
                    "summary_updated_at": now,
                    "unreachable_at": now,
                    "unreachable_reason": str(reason or "target_not_found")[:120],
                }
            )
            item["terminal_delivery"] = next_delivery
            item["delivery_outcome"] = "unreachable"
            item["summary_updated_at"] = now
            item["updated_at"] = now
            self._write(data)
            return True

    def release_terminal_delivery(self, work_id: str, *, owner: str) -> bool:
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            delivery = item.get("terminal_delivery") if isinstance(item, dict) else None
            status = str(delivery.get("status") or "") if isinstance(delivery, dict) else ""
            if (
                not isinstance(item, dict)
                or not isinstance(delivery, dict)
                or status not in {"delivering", "sending", "uncertain"}
                or str(delivery.get("owner") or "") != str(owner or "")
            ):
                return False
            next_delivery = dict(delivery)
            next_delivery.update(
                {
                    "status": "uncertain" if status == "uncertain" else "pending",
                    "revision": _positive_int(delivery.get("revision")) + 1,
                    "owner": "",
                    "lease_until": None,
                    "retry_count": _positive_int(delivery.get("retry_count")) + 1,
                }
            )
            if status == "sending":
                next_delivery["send_confirmed_at"] = None
            item["terminal_delivery"] = next_delivery
            item["updated_at"] = self._now()
            self._write(data)
            return True

    def complete_terminal_delivery(self, work_id: str, *, owner: str) -> bool:
        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            delivery = item.get("terminal_delivery") if isinstance(item, dict) else None
            status = str(delivery.get("status") or "") if isinstance(delivery, dict) else ""
            if (
                not isinstance(item, dict)
                or not isinstance(delivery, dict)
                or status not in {"delivering", "uncertain"}
                or str(delivery.get("owner") or "") != str(owner or "")
            ):
                return False
            now = self._now()
            final_status = "uncertain" if status == "uncertain" else "completed"
            next_delivery = dict(delivery)
            next_delivery.update(
                {
                    "status": final_status,
                    "revision": _positive_int(delivery.get("revision")) + 1,
                    "owner": "",
                    "lease_until": None,
                    "summary_updated_at": now,
                }
            )
            item["terminal_delivery"] = next_delivery
            item["delivery_outcome"] = final_status
            item["summary_updated_at"] = now
            item["updated_at"] = now
            self._write(data)
            return True

    def preview_delivery_satisfied(self, work_id: str) -> bool:
        """Return whether the current exact-head preview obligation is complete."""

        item = self.get(work_id)
        if not isinstance(item, dict):
            return False
        closeout = item.get("closeout") if isinstance(item.get("closeout"), dict) else {}
        policy = closeout.get("policy") if isinstance(closeout.get("policy"), dict) else {}
        if policy.get("require_preview") is not True:
            return True
        delivery = item.get("preview_delivery") if isinstance(item.get("preview_delivery"), dict) else {}
        if not delivery or not _preview_delivery_matches_closeout(item, delivery):
            activated = (
                item.get("closeout_authoritative") is True
                or item.get("closeout_activated_at") is not None
            )
            if not activated:
                # Failures before closeout activation have no preview
                # obligation to order before their error response.
                return True
            preview = closeout.get("preview") if isinstance(closeout.get("preview"), dict) else {}
            closeout_status = str(closeout.get("status") or "")
            preview_status = str(preview.get("status") or "")
            if (
                closeout_status in {"blocked", "repair_required"}
                and preview_status not in {"pending", "ready"}
            ):
                # Deterministic activation failures that never produced a
                # preview must still report their blocker.
                return True
            # A successful or reopened activated closeout with a stale/missing
            # current-head delivery must wait for the replacement preview.
            return False
        return str(delivery.get("status") or "") == "completed"

    def pending_preview_deliveries(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Return preview sends that need a first attempt or lease recovery."""

        now = self._now()
        pending: list[dict[str, Any]] = []
        for item in self._read().get("items", {}).values():
            delivery = item.get("preview_delivery") if isinstance(item, dict) else None
            if not isinstance(item, dict) or not isinstance(delivery, dict):
                continue
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                continue
            status = str(delivery.get("status") or "")
            if (
                status == "pending"
                and float(delivery.get("next_attempt_at") or 0) <= now
            ) or (
                status in {"delivering", "sending"}
                and float(delivery.get("lease_until") or 0) <= now
            ):
                pending.append(dict(item))
        pending.sort(
            key=lambda item: (
                float(item.get("preview_delivery", {}).get("next_attempt_at") or 0),
                float(item.get("preview_delivery", {}).get("created_at") or 0),
                str(item.get("id") or ""),
            )
        )
        return pending[: max(1, min(200, int(limit)))]

    def pending_closeouts(
        self,
        *,
        due_at: float | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Return a bounded due scan, including already-delivered model work."""

        from hermes_cli.trusted_closeout import (
            SUCCESS_CLOSEOUT_STATUSES,
            TERMINAL_CLOSEOUT_STATUSES,
            normalize_closeout_state,
        )

        now = self._now() if due_at is None else float(due_at)
        pending: list[dict[str, Any]] = []
        for item in self._read().get("items", {}).values():
            if not isinstance(item, dict) or not isinstance(item.get("closeout"), dict):
                continue
            if _required_async_completion_state(item).get("attempt_cancelled") is True:
                continue
            state = normalize_closeout_state(item["closeout"])
            active = item.get("closeout_authoritative") is True or (
                state["mode"] == "shadow" and item.get("closeout_activated_at") is not None
            )
            if not active:
                continue
            if state["mode"] == "off" or state["status"] in SUCCESS_CLOSEOUT_STATUSES:
                continue
            if (
                state["status"] in TERMINAL_CLOSEOUT_STATUSES
                and str(item.get("status") or "") in TERMINAL_STATUSES
            ):
                continue
            next_due = float(state.get("next_due_at") or 0)
            lease = state.get("lease") if isinstance(state.get("lease"), dict) else {}
            if next_due > now or float(lease.get("until") or 0) > now:
                continue
            copy_item = dict(item)
            copy_item["closeout"] = state
            pending.append(copy_item)
        pending.sort(
            key=lambda item: (
                float(item.get("closeout_last_claimed_at") or 0),
                float(item.get("closeout", {}).get("next_due_at") or 0),
                float(item.get("created_at") or 0),
                str(item.get("id") or ""),
            )
        )
        return pending[: max(1, min(200, int(limit)))]

    @_locked_ledger_mutation
    def claim(
        self,
        work_id: str,
        *,
        session_key: str | None = None,
        run_generation: int | None = None,
        owner_pid: int | None = None,
        process_epoch: str | None = None,
    ) -> dict[str, Any] | None:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return None
        now = self._now()
        item["status"] = "claimed"
        item["updated_at"] = now
        generation = _positive_int(run_generation)
        active_session = str(session_key or item.get("session_key") or "").strip()
        epoch = str(process_epoch or "").strip()[:160]
        if generation and active_session:
            item["active_run"] = {
                "session_key": active_session,
                "generation": generation,
                "owner_pid": _positive_int(owner_pid or os.getpid()),
                "process_epoch": epoch,
                "lease_until": now + LEASE_SECONDS,
            }
        else:
            item["active_run"] = None
        item.pop("claim_pid", None)
        item["lease_until"] = now + LEASE_SECONDS
        item["expires_at"] = max(float(item.get("expires_at") or 0), now + LEASE_SECONDS)
        _record_provider_progress(item, "ledger_status_claimed", status="claimed")
        self._write(data)
        return dict(item)

    @_locked_ledger_mutation
    def mark_agent_running(
        self,
        work_id: str,
        *,
        session_id: str | None = None,
        session_key: str | None = None,
        run_generation: int | None = None,
        owner_pid: int | None = None,
        process_epoch: str | None = None,
    ) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return False
        now = self._now()
        item["status"] = "agent_running"
        item["updated_at"] = now
        generation = _positive_int(run_generation)
        active_session = str(session_key or item.get("session_key") or "").strip()
        epoch = str(process_epoch or "").strip()[:160]
        if generation and active_session:
            item["active_run"] = {
                "session_key": active_session,
                "generation": generation,
                "owner_pid": _positive_int(owner_pid or os.getpid()),
                "process_epoch": epoch,
                "lease_until": now + LEASE_SECONDS,
            }
        else:
            item["active_run"] = None
        item.pop("claim_pid", None)
        item["lease_until"] = now + LEASE_SECONDS
        item["expires_at"] = max(float(item.get("expires_at") or 0), now + LEASE_SECONDS)
        for key in (
            "agent_done_at",
            "blocked_at",
            "blocked_reason",
            "completion_gate",
            "confirmed_message_ids",
            "delivery_outcome",
            "delivery_uncertain_at",
            "final_response",
            "provider_no_progress",
            "result_message_id",
            "summary_status",
            "summary_updated_at",
            "terminal_delivery",
            "closeout_visual_completion",
            "visual_qa_code_mutation_observed",
            "visual_qa_min_receipt_order",
        ):
            item.pop(key, None)
        # Tool order restarts on a retried agent turn. Keep the durable
        # requirement/config, but never let a receipt from an earlier turn
        # satisfy a fresh post-edit boundary.
        item["visual_qa_receipts"] = []
        runtime_breakdown = item.get("runtime_breakdown")
        if isinstance(runtime_breakdown, dict) and "visual_qa_receipts" in runtime_breakdown:
            item["runtime_breakdown"] = {
                **runtime_breakdown,
                "visual_qa_receipts": [],
            }
        if session_id:
            item["session_id"] = str(session_id)
        if _required_async_completion_state(item)["failed"]:
            gate = classify_delivery_completion(item)
            item["completion_gate"] = gate
            item["summary_status"] = str(gate.get("summary_status") or "Failed")
        _record_provider_progress(item, "ledger_status_agent_running", status="agent_running")
        self._write(data)
        return True

    @_locked_ledger_mutation
    def renew_active_run(
        self,
        work_id: str,
        *,
        session_key: str,
        run_generation: int,
        owner_pid: int,
        process_epoch: str,
        lease_seconds: float = LEASE_SECONDS,
    ) -> dict[str, Any] | None:
        """Renew one exact active run without reviving stale ownership."""

        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict) or item.get("status") not in _RUN_OWNED_STATUSES:
            return None
        active_run = item.get("active_run") if isinstance(item.get("active_run"), dict) else {}
        if (
            str(active_run.get("session_key") or "") != str(session_key or "")
            or _positive_int(active_run.get("generation")) != _positive_int(run_generation)
            or _positive_int(active_run.get("owner_pid")) != _positive_int(owner_pid)
            or str(active_run.get("process_epoch") or "") != str(process_epoch or "")
        ):
            return None
        try:
            duration = max(1.0, float(lease_seconds))
        except (TypeError, ValueError, OverflowError):
            duration = LEASE_SECONDS
        now = self._now()
        lease_until = now + duration
        active_run = dict(active_run)
        active_run["lease_until"] = lease_until
        item["active_run"] = active_run
        item["lease_until"] = lease_until
        item["expires_at"] = max(float(item.get("expires_at") or 0), lease_until)
        item["updated_at"] = now
        self._write(data)
        return dict(item)

    @_locked_ledger_mutation
    def promote_visual_qa_requirement(
        self,
        work_id: str,
        requirement: Any,
        *,
        expected_run_state: Any = _RUN_STATE_UNSET,
    ) -> dict[str, Any] | None:
        """Persist a host-owned post-edit promotion before closeout activates."""

        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict) or not _run_state_matches(item, expected_run_state):
            return None
        if not _promote_visual_qa_requirement_in_item(item, requirement):
            return dict(item)
        item["updated_at"] = self._now()
        self._write(data)
        return dict(item)

    @_locked_ledger_mutation
    def promote_visual_qa_config(
        self,
        work_id: str,
        config: Any,
        *,
        expected_run_state: Any = _RUN_STATE_UNSET,
    ) -> dict[str, Any] | None:
        """Promote a stale durable visual policy without weakening it."""

        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict) or not _run_state_matches(item, expected_run_state):
            return None
        current = normalize_visual_qa_config(item.get("visual_qa_config"))
        candidate = normalize_visual_qa_config(config)
        rank = {"off": 0, "shadow": 1, "enforce_explicit": 2}
        if rank[candidate["mode"]] <= rank[current["mode"]]:
            return dict(item)
        item["visual_qa_config"] = candidate
        item["updated_at"] = self._now()
        self._write(data)
        return dict(item)

    @_locked_ledger_mutation
    def mark_agent_done(
        self,
        work_id: str,
        *,
        final_response: str,
        session_id: str | None = None,
        summary_status: str | None = None,
        title: str | None = None,
        feature_summary: dict[str, Any] | None = None,
        project_summary: dict[str, Any] | None = None,
        runtime_breakdown: dict[str, Any] | None = None,
        provider_no_progress: dict[str, Any] | None = None,
        visual_qa_requirement: dict[str, Any] | None = None,
        visual_qa_receipts: list[dict[str, Any]] | None = None,
        visual_qa_code_mutation_observed: bool | None = None,
        visual_qa_min_receipt_order: int | None = None,
        already_delivered: bool = False,
        expected_run_state: Any = _RUN_STATE_UNSET,
    ) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict) or not _run_state_matches(item, expected_run_state):
            return False
        if _required_async_completion_state(item).get("attempt_cancelled") is True:
            return False
        now = self._now()
        item["status"] = "response_delivered" if already_delivered else "agent_done"
        item["updated_at"] = now
        item["active_run"] = None
        item.pop("claim_pid", None)
        item["lease_until"] = None
        item["agent_done_at"] = now
        item["final_response"] = str(final_response or "")
        if session_id:
            item["session_id"] = str(session_id)
        if summary_status:
            item["summary_status"] = str(summary_status)
        if title:
            item["title"] = str(title)
        if feature_summary is not None:
            item["feature_summary"] = _durable_metadata(feature_summary)
        if project_summary is not None:
            item["project_summary"] = _durable_metadata(project_summary)
        if visual_qa_requirement is not None:
            _promote_visual_qa_requirement_in_item(item, visual_qa_requirement)
        visual_config, visual_requirement = _visual_qa_state_for_item(item)
        if runtime_breakdown is not None:
            item["runtime_breakdown"] = _durable_runtime_breakdown(
                runtime_breakdown,
                visual_requirement,
                receipt_limit=visual_config["max_receipts_per_turn"],
            )
            durable_runtime = item.get("runtime_breakdown")
            if isinstance(durable_runtime, dict):
                _adopt_repo_native_closeout_receipt(
                    item,
                    durable_runtime.get("closeout_receipt"),
                    now=now,
                )
        if provider_no_progress is not None:
            item["provider_no_progress"] = _durable_metadata(provider_no_progress)
        if visual_qa_receipts is not None:
            item["visual_qa_receipts"] = _visual_qa_receipts(
                visual_qa_receipts,
                visual_requirement,
                limit=visual_config["max_receipts_per_turn"],
            )
        if visual_qa_code_mutation_observed is not None:
            item["visual_qa_code_mutation_observed"] = bool(
                item.get("visual_qa_code_mutation_observed") is True
                or visual_qa_code_mutation_observed
            )
        if visual_qa_min_receipt_order is not None:
            min_order = _positive_int(visual_qa_min_receipt_order)
            if min_order:
                item["visual_qa_min_receipt_order"] = max(
                    min_order,
                    _positive_int(item.get("visual_qa_min_receipt_order")),
                )
            elif not _positive_int(item.get("visual_qa_min_receipt_order")):
                item.pop("visual_qa_min_receipt_order", None)
        _record_provider_progress(item, f"ledger_status_{item['status']}", status=str(item["status"]))
        gate = classify_delivery_completion(item)
        item["completion_gate"] = gate
        if not gate.get("allowed_to_complete"):
            item["summary_status"] = str(gate.get("summary_status") or "Blocked")
            blocked_surfaces = (
                gate.get("verification_constraints", {}).get("blocked_surfaces")
                if isinstance(gate.get("verification_constraints"), dict)
                else None
            )
            if isinstance(blocked_surfaces, list) and blocked_surfaces:
                if isinstance(item.get("feature_summary"), dict):
                    item["feature_summary"] = downgrade_verified_metadata(item["feature_summary"], blocked_surfaces)
                if isinstance(item.get("project_summary"), dict):
                    item["project_summary"] = downgrade_verified_metadata(item["project_summary"], blocked_surfaces)
        else:
            item["summary_status"] = str(gate.get("summary_status") or "Complete")
        item["final_response"] = annotate_enforced_visual_qa_blocked_response(
            item,
            item["final_response"],
            already_delivered=already_delivered,
        )
        _record_discord_board_final_response(item)
        self._write(data)
        return True

    @_locked_ledger_mutation
    def mark_response_delivery_started(
        self,
        work_id: str,
        *,
        expected_run_state: Any = _RUN_STATE_UNSET,
    ) -> bool:
        """Fence one non-idempotent logical send before platform I/O begins."""

        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict) or not _run_state_matches(item, expected_run_state):
            return False
        prior = (
            item.get("delivery_attempt")
            if isinstance(item.get("delivery_attempt"), dict)
            else {}
        )
        if (
            str(prior.get("status") or "") in {"sending", "uncertain"}
            or str(item.get("delivery_outcome") or "")
            in {"sending", "delivered", "uncertain"}
            or str(item.get("status") or "") == "response_delivered"
        ):
            return False
        now = self._now()
        attempt_count = min(1000, int(prior.get("attempt_count") or 0) + 1)
        item["delivery_attempt"] = {
            "status": "sending",
            "started_at": now,
            "attempt_count": attempt_count,
        }
        item["delivery_outcome"] = "sending"
        item["updated_at"] = now
        self._write(data)
        return True

    @_locked_ledger_mutation
    def release_response_delivery_attempt(
        self,
        work_id: str,
        *,
        expected_run_state: Any = _RUN_STATE_UNSET,
    ) -> bool:
        """Clear a definitively side-effect-free send attempt for safe retry."""

        data = self._read()
        item = data["items"].get(work_id)
        attempt = item.get("delivery_attempt") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or not _run_state_matches(item, expected_run_state)
            or not isinstance(attempt, dict)
            or str(attempt.get("status") or "") != "sending"
        ):
            return False
        item.pop("delivery_attempt", None)
        item.pop("delivery_outcome", None)
        item["updated_at"] = self._now()
        self._write(data)
        return True

    @_locked_ledger_mutation
    def mark_response_delivered(
        self,
        work_id: str,
        *,
        result_message_id: str | None = None,
        confirmed_message_ids: Any = None,
        expected_run_state: Any = _RUN_STATE_UNSET,
    ) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict) or not _run_state_matches(item, expected_run_state):
            return False
        item["status"] = "response_delivered"
        item["updated_at"] = self._now()
        item["delivery_outcome"] = "delivered"
        item.pop("delivery_attempt", None)
        item.pop("delivery_uncertain_at", None)
        existing_confirmed = _bounded_delivery_message_ids(
            item.get("confirmed_message_ids"),
            primary=item.get("result_message_id"),
        )
        new_confirmed = _bounded_delivery_message_ids(
            confirmed_message_ids,
            primary=result_message_id,
        )
        confirmed_ids = _bounded_delivery_message_ids(
            (*existing_confirmed, *new_confirmed),
            primary=item.get("result_message_id") or result_message_id,
        )
        if confirmed_ids:
            item["confirmed_message_ids"] = list(confirmed_ids)
        primary_ids = _bounded_delivery_message_ids(
            (item.get("result_message_id") or result_message_id,)
        )
        message_id = primary_ids[0] if primary_ids else confirmed_ids[0] if confirmed_ids else ""
        if message_id:
            item["result_message_id"] = message_id
        _record_discord_board_final_response(
            item,
            result_message_id=message_id or None,
        )
        _record_provider_progress(item, "ledger_status_response_delivered", status="response_delivered")
        self._write(data)
        return True

    @_locked_ledger_mutation
    def mark_response_delivery_uncertain(
        self,
        work_id: str,
        *,
        result_message_id: str | None = None,
        confirmed_message_ids: Any = None,
        reason: str = "send_attempt_outcome_unknown",
        expected_run_state: Any = _RUN_STATE_UNSET,
    ) -> bool:
        """Persist an unsafe logical send without allowing automatic replay."""

        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict) or not _run_state_matches(item, expected_run_state):
            return False
        now = self._now()
        confirmed_ids = _bounded_delivery_message_ids(
            confirmed_message_ids,
            primary=result_message_id,
        )
        primary_ids = _bounded_delivery_message_ids((result_message_id,))
        message_id = primary_ids[0] if primary_ids else confirmed_ids[0] if confirmed_ids else ""
        item.update(
            {
                "status": "response_delivered",
                "updated_at": now,
                "delivery_outcome": "uncertain",
                "delivery_uncertain_at": now,
                "delivery_attempt": {
                    "status": "uncertain",
                    "started_at": (
                        item.get("delivery_attempt", {}).get("started_at")
                        if isinstance(item.get("delivery_attempt"), dict)
                        else now
                    ),
                    "attempt_count": min(
                        1000,
                        int(item.get("delivery_attempt", {}).get("attempt_count") or 1)
                        if isinstance(item.get("delivery_attempt"), dict)
                        else 1,
                    ),
                },
                "confirmed_message_ids": list(confirmed_ids),
                "summary_status": "Blocked",
                "blocked_reason": str(reason or "send_attempt_outcome_unknown")[:120],
                "completion_gate": {
                    "allowed_to_complete": False,
                    "summary_status": "Blocked",
                    "terminal_status": "blocked",
                    "reason": str(reason or "send_attempt_outcome_unknown")[:120],
                },
            }
        )
        if message_id:
            item["result_message_id"] = message_id
        _record_discord_board_final_response(item, result_message_id=message_id or None)
        _record_provider_progress(item, "ledger_delivery_uncertain", status="response_delivered")
        self._write(data)
        return True

    @_locked_ledger_mutation
    def mark_summary_updated(
        self,
        work_id: str,
        *,
        expected_run_state: Any = _RUN_STATE_UNSET,
    ) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict) or not _run_state_matches(item, expected_run_state):
            return False
        item["status"] = "summary_updated"
        item["updated_at"] = self._now()
        item["summary_updated_at"] = item["updated_at"]
        _record_provider_progress(item, "ledger_status_summary_updated", status="summary_updated")
        self._write(data)
        return True

    @_locked_ledger_mutation
    def mark_completed(
        self,
        work_id: str,
        *,
        result_message_id: str | None = None,
        confirmed_message_ids: Any = None,
        expected_run_state: Any = _RUN_STATE_UNSET,
    ) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict) or not _run_state_matches(item, expected_run_state):
            return False
        confirmed_ids = _bounded_delivery_message_ids(
            confirmed_message_ids,
            primary=result_message_id,
        )
        if confirmed_ids:
            item["confirmed_message_ids"] = list(confirmed_ids)
            primary_ids = _bounded_delivery_message_ids((result_message_id,))
            item["result_message_id"] = primary_ids[0] if primary_ids else confirmed_ids[0]
        if item.get("closeout_authoritative") is True and isinstance(item.get("closeout"), dict):
            from hermes_cli.trusted_closeout import closeout_terminal_eligible, normalize_closeout_state

            normalized_closeout = normalize_closeout_state(item["closeout"])
            if normalized_closeout["mode"] == "enforce" and not closeout_terminal_eligible(normalized_closeout):
                # Enforced structured closeout is authoritative. Preserve the
                # delivery phase so the watcher can resume without model replay.
                item["updated_at"] = self._now()
                self._write(data)
                return False
        gate = item.get("completion_gate") if isinstance(item.get("completion_gate"), dict) else None
        if gate and not gate.get("allowed_to_complete"):
            item["status"] = str(gate.get("terminal_status") or "blocked")
            item["summary_status"] = str(gate.get("summary_status") or "Blocked")
            item["updated_at"] = self._now()
            item["blocked_at"] = item["updated_at"]
            item["terminal_reaction_state"] = (
                "blocked" if item["status"] == "blocked" else "errored"
            )
            item["terminal_reaction_sync_pending"] = True
            _record_provider_progress(item, "ledger_status_blocked", status=str(item["status"]))
            self._write(data)
            return True
        item["status"] = "completed"
        item["updated_at"] = self._now()
        item["terminal_reaction_state"] = "done"
        item["terminal_reaction_sync_pending"] = True
        _record_provider_progress(item, "ledger_status_completed", status="completed")
        self._write(data)
        return True

    @_locked_ledger_mutation
    def mark_consumed(self, work_id: str, *, reason: str) -> bool:
        """Terminalize input absorbed by an already-running agent turn."""
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return False
        if str(item.get("status") or "") in TERMINAL_STATUSES:
            return True
        now = self._now()
        item["status"] = "completed"
        item["updated_at"] = now
        item["consumed_at"] = now
        item["consumed_reason"] = str(reason or "active_turn")[:120]
        item["active_run"] = None
        item.pop("claim_pid", None)
        item["lease_until"] = None
        item["terminal_reaction_state"] = "done"
        item["terminal_reaction_sync_pending"] = False
        _record_provider_progress(item, "ledger_status_consumed", status="completed")
        self._write(data)
        return True

    @_locked_ledger_mutation
    def mark_blocked(self, work_id: str, *, reason: str | None = None) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return False
        item["status"] = "blocked"
        item["updated_at"] = self._now()
        item["blocked_at"] = item["updated_at"]
        gate = item.get("completion_gate") if isinstance(item.get("completion_gate"), dict) else None
        if gate and not gate.get("allowed_to_complete"):
            item["summary_status"] = str(gate.get("summary_status") or "Blocked")
        else:
            item["summary_status"] = "Blocked"
        if reason:
            item["blocked_reason"] = str(reason)
        _record_provider_progress(item, "ledger_status_blocked", status="blocked")
        self._write(data)
        return True

    @_locked_ledger_mutation
    def mark_expired(self, work_id: str) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return False
        if (
            str(item.get("status") or "") in {"response_delivered", "summary_updated"}
            and str(item.get("summary_status") or "").strip().lower()
            in {"complete", "completed", "done", "success", "succeeded"}
        ):
            item["status"] = "completed"
            item["updated_at"] = self._now()
            item["terminal_reaction_state"] = "done"
            item["terminal_reaction_sync_pending"] = True
            self._write(data)
            return True
        terminal_delivery = (
            item.get("terminal_delivery")
            if isinstance(item.get("terminal_delivery"), dict)
            else {}
        )
        if item.get("status") == "blocked" and terminal_delivery:
            delivery_status = str(terminal_delivery.get("status") or "")
            summary_pending = terminal_delivery.get("summary_updated_at") is None
            if delivery_status not in {"completed", "uncertain"} or summary_pending:
                return False
        item["status"] = "expired"
        now = self._now()
        item["updated_at"] = now
        item["expired_at"] = now
        item["active_run"] = None
        item.pop("claim_pid", None)
        item["lease_until"] = None
        item["summary_status"] = "Interrupted"
        item["final_response"] = (
            "Work was interrupted before completion and could not be resumed "
            "within the recovery window."
        )
        item["terminal_reaction_state"] = "errored"
        item["terminal_reaction_sync_pending"] = True
        self._write(data)
        return True

    def discord_thread_reaction_state(self, item_or_work_id: Any) -> str | None:
        """Aggregate durable work into one reaction state for a Discord thread."""

        if isinstance(item_or_work_id, dict):
            target = item_or_work_id
        else:
            target = self.get(str(item_or_work_id or ""))
        key = _discord_thread_key(target)
        if key is None:
            return None
        candidates = [
            item
            for item in self._read().get("items", {}).values()
            if isinstance(item, dict) and _discord_thread_key(item) == key
        ]
        if any(str(item.get("status") or "") in INCOMPLETE_STATUSES for item in candidates):
            return "running"
        terminal = [
            item
            for item in candidates
            if str(item.get("status") or "") in TERMINAL_STATUSES
        ]
        if not terminal:
            return None
        latest_status = str(max(terminal, key=_discord_item_order).get("status") or "")
        if latest_status == "completed":
            return "done"
        if latest_status == "blocked":
            return "blocked"
        return "errored"

    def pending_terminal_reaction_items(self) -> list[dict[str, Any]]:
        """Return one representative item for every pending Discord thread sync."""

        representatives: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for item in self._read().get("items", {}).values():
            if not isinstance(item, dict) or item.get("terminal_reaction_sync_pending") is not True:
                continue
            key = _discord_thread_key(item)
            if key is None:
                continue
            current = representatives.get(key)
            if current is None or _discord_item_order(item) > _discord_item_order(current):
                representatives[key] = dict(item)
        return list(representatives.values())

    @_locked_ledger_mutation
    def mark_discord_thread_reaction_synced(self, item_or_work_id: Any) -> bool:
        """Clear pending terminal-reaction markers for one Discord thread."""

        data = self._read()
        target = (
            item_or_work_id
            if isinstance(item_or_work_id, dict)
            else data.get("items", {}).get(str(item_or_work_id or ""))
        )
        key = _discord_thread_key(target)
        if key is None:
            return False
        changed = False
        for item in data.get("items", {}).values():
            if not isinstance(item, dict) or _discord_thread_key(item) != key:
                continue
            if item.pop("terminal_reaction_sync_pending", None) is not None:
                changed = True
            item.pop("terminal_reaction_state", None)
        if changed:
            self._write(data)
        return changed

    def incomplete_items(self) -> list[dict[str, Any]]:
        now = self._now()
        items = []
        for item in self._read().get("items", {}).values():
            if not isinstance(item, dict):
                continue
            terminal_delivery = (
                item.get("terminal_delivery")
                if isinstance(item.get("terminal_delivery"), dict)
                else {}
            )
            delivery_status = str(terminal_delivery.get("status") or "")
            blocked_delivery_pending = (
                item.get("status") == "blocked"
                and bool(terminal_delivery)
                and (
                    delivery_status not in {"completed", "uncertain"}
                    or terminal_delivery.get("summary_updated_at") is None
                )
            )
            if (
                item.get("status") not in INCOMPLETE_STATUSES
                and not blocked_delivery_pending
            ):
                continue
            if _required_async_completion_state(item)["owns_recovery"]:
                # Required-worker ownership replaces the volatile intake TTL.
                # Keep the item discoverable until reconciliation hands off to
                # terminal delivery; silently expiring here would strand the
                # durable dispatch or replay the original Discord request.
                items.append(dict(item))
                continue
            if blocked_delivery_pending:
                # A deterministic blocked response and its summary outlive the
                # original intake/source-message freshness window. They leave
                # recovery only after a durable completed or uncertain outcome.
                items.append(dict(item))
                continue
            if item.get("platform") == "discord" and discord_message_exceeds_age_limit(
                _discord_source_message_id_for_item(item),
                now=now,
            ):
                self.mark_expired(str(item.get("id") or ""))
                continue
            if float(item.get("expires_at") or 0) <= now:
                self.mark_expired(str(item.get("id") or ""))
                continue
            items.append(dict(item))
        return items

    def terminal_action_worktree_paths(self, *, older_than: float) -> list[str]:
        """Return action worktrees whose persisted users are all old and terminal."""

        by_path: dict[str, dict[str, Any]] = {}
        terminal_statuses = {"completed", "blocked", "expired", "failed"}
        for item in self._read().get("items", {}).values():
            if not isinstance(item, dict):
                continue
            closeout = item.get("closeout") if isinstance(item.get("closeout"), dict) else {}
            workspace = (
                closeout.get("workspace")
                if isinstance(closeout.get("workspace"), dict)
                else {}
            )
            path = str(workspace.get("path") or "").strip()
            if not path:
                continue
            state = by_path.setdefault(path, {"safe": True, "latest": 0.0})
            updated_at = float(item.get("updated_at") or 0.0)
            state["latest"] = max(float(state["latest"]), updated_at)
            if str(item.get("status") or "") not in terminal_statuses:
                state["safe"] = False
        return sorted(
            path
            for path, state in by_path.items()
            if state["safe"] and float(state["latest"]) <= float(older_than)
        )

    @staticmethod
    def agent_run_active(
        item: dict[str, Any],
        *,
        session_key: str,
        run_generation: int,
        process_epoch: str,
        registry_active: bool,
    ) -> bool:
        """Validate one exact live turn against the gateway run registry."""

        if not registry_active:
            return False
        active_run = item.get("active_run") if isinstance(item.get("active_run"), dict) else {}
        persisted_epoch = str(active_run.get("process_epoch") or "").strip()
        current_epoch = str(process_epoch or "").strip()
        if (
            str(active_run.get("session_key") or "") != str(session_key or "")
            or _positive_int(active_run.get("generation")) != _positive_int(run_generation)
            or not persisted_epoch
            or not current_epoch
            or persisted_epoch != current_epoch
        ):
            return False
        owner_pid = _positive_int(active_run.get("owner_pid"))
        if owner_pid <= 0:
            return False
        try:
            from gateway.status import _pid_exists

            return _pid_exists(owner_pid)
        except Exception:
            return False

    @staticmethod
    def event_from_item(item: dict[str, Any]):
        from gateway.platforms.base import MessageEvent, MessageType
        from agent.runtime_capabilities import RuntimeMode, normalize_runtime_mode

        try:
            msg_type = MessageType(str(item.get("message_type") or "text"))
        except ValueError:
            msg_type = MessageType.TEXT
        runtime_mode = normalize_runtime_mode(
            item.get("discord_runtime_mode"),
            default=RuntimeMode.ACTION,
        )
        lifecycle = (
            bool(item.get("participates_in_work_lifecycle"))
            if "participates_in_work_lifecycle" in item
            else True
        )
        base_channel_prompt = item.get(
            "discord_action_request_base_channel_prompt",
            item.get("channel_prompt"),
        )
        event = MessageEvent(
            text=str(item.get("text") or ""),
            message_type=msg_type,
            source=_source_from_dict(item.get("source") or {}),
            message_id=item.get("message_id"),
            reply_to_message_id=item.get("reply_to_message_id"),
            reply_to_text=item.get("reply_to_text"),
            channel_prompt=item.get("channel_prompt"),
            discord_action_request_base_channel_prompt=base_channel_prompt,
            feature_summary=item.get("feature_summary"),
            project_summary=item.get("project_summary"),
            channel_context=item.get("channel_context"),
            goal_thread_context=item.get("goal_thread_context"),
            discord_runtime_mode=runtime_mode.value,
            discord_action_escalation_allowed=bool(
                item.get("discord_action_escalation_allowed", False)
            ),
            discord_runtime_reason=item.get("discord_runtime_reason"),
            discord_explicit_no_action_denial=bool(
                item.get("discord_explicit_no_action_denial", False)
            ),
            participates_in_work_lifecycle=lifecycle,
        )
        event.work_item_id = item.get("id")
        event.work_replay = True
        event.discord_pr_generation = GatewayWorkLedger.normalize_discord_pr_generation(
            item.get("discord_pr_generation")
        )
        event.discord_pr_rollover = bool(item.get("discord_pr_rollover", False))
        event.discord_drain_recovery = bool(item.get("drain_recovery", False))
        event.visual_qa_requirement = normalize_visual_requirement(item.get("visual_qa_requirement"))
        event.visual_qa_config = normalize_visual_qa_config(item.get("visual_qa_config"))
        return event
