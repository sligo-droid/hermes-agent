"""Durable gateway work ledger for idempotent Discord recovery."""

from __future__ import annotations

import os
import json
import math
import re
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


def _incomplete_final_markers(text: str) -> list[str]:
    """Return incomplete-delivery markers while filtering known summary noise."""

    markers: list[str] = []
    for reason, pattern in _INCOMPLETE_FINAL_PATTERNS:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            snippet = match.group(0)
            prefix = text[max(0, match.start() - 80) : match.start()].lower()
            if reason == "checks_not_green" and ("→" in snippet or "phase timing" in prefix):
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
    gate = {
        "allowed_to_complete": True,
        "summary_status": str(item.get("summary_status") or "Complete"),
        "terminal_status": "completed",
        "reason": "not_repo_backed" if not repo_backed else "no_self_declared_delivery_gap",
        "delivery_intent": intent,
        "repo_backed": repo_backed,
    }
    if not repo_backed:
        return gate

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
        return gate

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
        return gate

    narrow_intent = intent in {"pr_only", "review_only", "draft_pr", "no_merge"}
    only_narrow_lifecycle_markers = set(matched) <= {"not_merged", "no_deploy"}
    if intent == "review_only" and "not_done_yet" not in matched and _matches_any(final_text, _REVIEW_ONLY_FINAL_PATTERNS):
        gate["reason"] = "intentional_review_only_terminal"
        gate["matched_markers"] = matched
        return gate
    if narrow_intent and only_narrow_lifecycle_markers and (
        _matches_any(final_text, _PR_ONLY_FINAL_PATTERNS) or _matches_any(final_text, _INTENTIONAL_UNMERGED_PATTERNS)
    ):
        gate["reason"] = "intentional_narrow_scope_terminal"
        gate["matched_markers"] = matched
        return gate

    gate.update(
        {
            "allowed_to_complete": False,
            "summary_status": "Blocked",
            "terminal_status": "blocked",
            "reason": "self_declared_delivery_incomplete",
            "matched_markers": matched,
        }
    )
    return gate


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

    def accept_event(
        self,
        event: Any,
        *,
        session_key: str,
        freshness_seconds: float,
        status: str = "accepted",
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
            "channel_prompt": getattr(event, "channel_prompt", None),
            "channel_context": getattr(event, "channel_context", None),
            "goal_thread_context": getattr(event, "goal_thread_context", None),
            "feature_summary": _durable_metadata(getattr(event, "feature_summary", None)),
            "project_summary": _durable_metadata(getattr(event, "project_summary", None)),
            "claim_pid": None,
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

    def claim(self, work_id: str) -> dict[str, Any] | None:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return None
        now = self._now()
        item["status"] = "claimed"
        item["updated_at"] = now
        item["claim_pid"] = os.getpid()
        item["lease_until"] = now + LEASE_SECONDS
        self._write(data)
        return dict(item)

    def mark_agent_running(self, work_id: str, *, session_id: str | None = None) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return False
        item["status"] = "agent_running"
        item["updated_at"] = self._now()
        for key in (
            "agent_done_at",
            "completion_gate",
            "final_response",
            "result_message_id",
            "summary_status",
            "summary_updated_at",
        ):
            item.pop(key, None)
        if session_id:
            item["session_id"] = str(session_id)
        self._write(data)
        return True

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
        already_delivered: bool = False,
    ) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return False
        now = self._now()
        item["status"] = "response_delivered" if already_delivered else "agent_done"
        item["updated_at"] = now
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
        if runtime_breakdown is not None:
            item["runtime_breakdown"] = _durable_metadata(runtime_breakdown)
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
        _record_discord_board_final_response(item)
        self._write(data)
        return True

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
        self._write(data)
        return True

    def mark_summary_updated(self, work_id: str) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return False
        item["status"] = "summary_updated"
        item["updated_at"] = self._now()
        item["summary_updated_at"] = item["updated_at"]
        self._write(data)
        return True

    def mark_completed(self, work_id: str, *, result_message_id: str | None = None) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
            return False
        gate = item.get("completion_gate") if isinstance(item.get("completion_gate"), dict) else None
        if gate and not gate.get("allowed_to_complete"):
            item["status"] = str(gate.get("terminal_status") or "blocked")
            item["summary_status"] = str(gate.get("summary_status") or "Blocked")
            item["updated_at"] = self._now()
            item["blocked_at"] = item["updated_at"]
            if result_message_id:
                item["result_message_id"] = str(result_message_id)
            self._write(data)
            return True
        item["status"] = "completed"
        item["updated_at"] = self._now()
        if result_message_id:
            item["result_message_id"] = str(result_message_id)
        self._write(data)
        return True

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
        self._write(data)
        return True

    def mark_expired(self, work_id: str) -> bool:
        data = self._read()
        item = data["items"].get(work_id)
        if not isinstance(item, dict):
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
            if item.get("status") not in INCOMPLETE_STATUSES:
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
    def claim_pid_alive(item: dict[str, Any]) -> bool:
        pid = item.get("claim_pid")
        try:
            pid_int = int(pid)
        except (TypeError, ValueError):
            return False
        if pid_int <= 0:
            return False
        try:
            os.kill(pid_int, 0)
            return True
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
        return event
