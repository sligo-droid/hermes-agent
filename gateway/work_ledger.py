"""Durable gateway work ledger for idempotent Discord recovery."""

from __future__ import annotations

import contextlib
import functools
import os
import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Any

from hermes_cli.discord_time import discord_message_exceeds_age_limit
from gateway.session import Platform, SessionSource
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

    route = getattr(event, "worker_route", None)
    if route:
        return route
    fable_metadata = getattr(event, "fable_plan_metadata", None)
    if isinstance(fable_metadata, dict):
        return fable_metadata.get("route")
    return None


def _visual_qa_requirement_for_event(event: Any) -> dict[str, Any]:
    """Classify only the accepted request text into a bounded public shape."""

    feature_summary = getattr(event, "feature_summary", None)
    request_text = merge_request_fragments(
        getattr(event, "text", ""),
        feature_summary.get("initial_request") if isinstance(feature_summary, dict) else "",
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
    return durable


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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


def _incomplete_final_markers(text: str) -> list[str]:
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
    return "full_lifecycle"


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
    if not constraints.get("allowed"):
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

    matched = _incomplete_final_markers(final_text)
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
        return {"version": 1, "items": {}}

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
        data.setdefault("version", 1)
        return data

    def _write(self, data: dict[str, Any]) -> None:
        atomic_json_write(self.path, data, indent=2, sort_keys=True)

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
        normalized_visual_config = normalize_visual_qa_config(visual_qa_config)
        visual_requirement = _visual_qa_requirement_for_event(event)
        action_intent = getattr(event, "discord_action_request_intent", None)
        channel_prompt = getattr(event, "channel_prompt", None)
        if isinstance(action_intent, bool):
            channel_prompt = getattr(
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
            "channel_context": getattr(event, "channel_context", None),
            "goal_thread_context": getattr(event, "goal_thread_context", None),
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
            existing = item.get("closeout") if isinstance(item.get("closeout"), dict) else {}
            state = normalize_closeout_state(
                {
                    **existing,
                    "id": str(existing.get("id") or closeout_id or f"{work_id}:closeout"),
                    "source": str(source or existing.get("source") or "direct"),
                    "mode": str(mode or existing.get("mode") or "off"),
                    "workspace": {
                        "path": str(workspace_path or ""),
                        "canonical_path": str(canonical_path or ""),
                        "repository": str(repository or ""),
                        "branch": str(branch or ""),
                        "base_branch": str(base_branch or "main"),
                    },
                    "policy": policy if policy is not None else existing.get("policy"),
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
            item["closeout_authoritative"] = state["mode"] == "enforce"
            item["closeout_activated_at"] = self._now()
            item["updated_at"] = item["closeout_activated_at"]
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
            existing = item.get("closeout") if isinstance(item.get("closeout"), dict) else None
            if existing is None or int(existing.get("revision") or 0) != int(expected_revision):
                return None
            state = normalize_closeout_state(closeout_state)
            state["revision"] = int(expected_revision) + 1
            item["closeout"] = state
            if state["mode"] != "enforce":
                item["closeout_authoritative"] = False
            elif item.get("closeout_activated_at") is not None:
                item["closeout_authoritative"] = True
            item["updated_at"] = self._now()
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
            now = self._now()
            state = normalize_closeout_state(item["closeout"])
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
            state["lease"] = {
                "owner": owner_text[:160],
                "until": now + max(1.0, min(3600.0, float(lease_seconds))),
            }
            state["revision"] = revision + 1
            item["closeout"] = state
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
            now = self._now()
            state = normalize_closeout_state(item["closeout"])
            if int(state.get("revision") or 0) != int(expected_revision):
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

    def release_closeout(
        self,
        work_id: str,
        *,
        owner: str,
        expected_revision: int,
        closeout_state: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Release an owned lease and optionally persist the reconciliation result."""

        from hermes_cli.trusted_closeout import normalize_closeout_state

        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if not isinstance(item, dict) or not isinstance(item.get("closeout"), dict):
                return None
            current = normalize_closeout_state(item["closeout"])
            if int(current.get("revision") or 0) != int(expected_revision):
                return None
            lease = current.get("lease") if isinstance(current.get("lease"), dict) else {}
            if str(lease.get("owner") or "") != str(owner or ""):
                return None
            state = normalize_closeout_state(closeout_state if closeout_state is not None else current)
            state["lease"] = {"owner": "", "until": None}
            state["revision"] = int(expected_revision) + 1
            item["closeout"] = state
            item["closeout_authoritative"] = state["mode"] == "enforce"
            item["updated_at"] = self._now()
            self._write(data)
            return dict(state)

    def finalize_blocked_closeout(
        self,
        work_id: str,
        *,
        owner: str,
        expected_revision: int,
        closeout_state: dict[str, Any],
        final_response: str,
        reason: str,
    ) -> dict[str, Any] | None:
        """CAS the leased closeout and blocked delivery record in one write."""

        from hermes_cli.trusted_closeout import normalize_closeout_state

        with _ledger_file_lock(self.path):
            data = self._read()
            item = data["items"].get(work_id)
            if not isinstance(item, dict) or not isinstance(item.get("closeout"), dict):
                return None
            current = normalize_closeout_state(item["closeout"])
            if int(current.get("revision") or 0) != int(expected_revision):
                return None
            lease = current.get("lease") if isinstance(current.get("lease"), dict) else {}
            if str(lease.get("owner") or "") != str(owner or ""):
                return None
            state = normalize_closeout_state(closeout_state)
            state["lease"] = {"owner": "", "until": None}
            state["revision"] = int(expected_revision) + 1
            now = self._now()
            response = str(final_response or "")
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
                        "summary_updated_at": None,
                    },
                }
            )
            item.pop("claim_pid", None)
            _record_discord_board_final_response(item)
            _record_provider_progress(item, "ledger_status_blocked", status="blocked")
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
            message_id = str(result_message_id or "").strip()
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
        for key in (
            "agent_done_at",
            "blocked_at",
            "blocked_reason",
            "completion_gate",
            "final_response",
            "provider_no_progress",
            "result_message_id",
            "summary_status",
            "summary_updated_at",
            "terminal_delivery",
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
        _record_provider_progress(item, "ledger_status_agent_running", status="agent_running")
        self._write(data)
        return True

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
        visual_qa_receipts: list[dict[str, Any]] | None = None,
        visual_qa_code_mutation_observed: bool | None = None,
        visual_qa_min_receipt_order: int | None = None,
        already_delivered: bool = False,
    ) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
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
        visual_config, visual_requirement = _visual_qa_state_for_item(item)
        if runtime_breakdown is not None:
            item["runtime_breakdown"] = _durable_runtime_breakdown(
                runtime_breakdown,
                visual_requirement,
                receipt_limit=visual_config["max_receipts_per_turn"],
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
            item["visual_qa_code_mutation_observed"] = bool(visual_qa_code_mutation_observed)
        if visual_qa_min_receipt_order is not None:
            min_order = _positive_int(visual_qa_min_receipt_order)
            if min_order:
                item["visual_qa_min_receipt_order"] = min_order
            else:
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
        _record_discord_board_final_response(item)
        self._write(data)
        return True

    @_locked_ledger_mutation
    def mark_response_delivered(self, work_id: str, *, result_message_id: str | None = None) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return False
        item["status"] = "response_delivered"
        item["updated_at"] = self._now()
        if result_message_id:
            item["result_message_id"] = str(result_message_id)
        _record_discord_board_final_response(
            item,
            result_message_id=str(result_message_id) if result_message_id else None,
        )
        _record_provider_progress(item, "ledger_status_response_delivered", status="response_delivered")
        self._write(data)
        return True

    @_locked_ledger_mutation
    def mark_summary_updated(self, work_id: str) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return False
        item["status"] = "summary_updated"
        item["updated_at"] = self._now()
        item["summary_updated_at"] = item["updated_at"]
        _record_provider_progress(item, "ledger_status_summary_updated", status="summary_updated")
        self._write(data)
        return True

    @_locked_ledger_mutation
    def mark_completed(self, work_id: str, *, result_message_id: str | None = None) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return False
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
            if result_message_id:
                item["result_message_id"] = str(result_message_id)
            _record_provider_progress(item, "ledger_status_blocked", status=str(item["status"]))
            self._write(data)
            return True
        item["status"] = "completed"
        item["updated_at"] = self._now()
        if result_message_id:
            item["result_message_id"] = str(result_message_id)
        _record_provider_progress(item, "ledger_status_completed", status="completed")
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
        item["updated_at"] = self._now()
        self._write(data)
        return True

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
            if item.get("status") not in INCOMPLETE_STATUSES and not blocked_delivery_pending:
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

        try:
            msg_type = MessageType(str(item.get("message_type") or "text"))
        except ValueError:
            msg_type = MessageType.TEXT
        event = MessageEvent(
            text=str(item.get("text") or ""),
            message_type=msg_type,
            source=_source_from_dict(item.get("source") or {}),
            message_id=item.get("message_id"),
            reply_to_message_id=item.get("reply_to_message_id"),
            reply_to_text=item.get("reply_to_text"),
            channel_prompt=item.get("channel_prompt"),
            feature_summary=item.get("feature_summary"),
            project_summary=item.get("project_summary"),
            channel_context=item.get("channel_context"),
            goal_thread_context=item.get("goal_thread_context"),
        )
        event.work_item_id = item.get("id")
        event.work_replay = True
        event.visual_qa_requirement = normalize_visual_requirement(item.get("visual_qa_requirement"))
        event.visual_qa_config = normalize_visual_qa_config(item.get("visual_qa_config"))
        return event
